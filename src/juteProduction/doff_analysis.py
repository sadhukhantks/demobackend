"""Doff Analysis report — port of the legacy MIS doff10 pages (doff10new =
single-day doff details + weight bands, spgframe = frame-vs-date pivot + drill).

One endpoint, two modes:

* ``mode=details`` — one report_date: per spell+frame+quality summary (with the
  spell's last doff weight), spell totals, the fixed 2 kg average-weight bands
  and the raw doff grid.
* ``mode=frames`` — a date range (<= 31 days): quality x shift averages, the
  frame x date pivot (each cell normalised by the number of shifts that frame
  ran that day) and, when a ``frame`` is named, its shift-wise and worker-wise
  drill-down.

Every actual comes from daily_doff_tbl spinning rows only; scope is co (machine
spine) + branch. The pivots are pure functions (``build_details`` /
``build_frames``) so the arithmetic is testable without a database.

Target / difference from target (TKT-000168): the production target of a
(spell, frame, yarn) unit is the Spinning Production planning grid's *Std Prod*
— ``p100prod * std_eff / 100`` — resolved from the Spinning Standards master
and the day's SQC count (see ``get_doff_target_inputs_query``). Diff is
TARGET - ACTUAL, so a positive figure is a shortfall (owner decision). A unit
whose standards have not been entered has NO target: its target / diff are
``None`` (blank on screen) and it is left out of every target total, never
counted as a zero target.

Standard per quality x shift (TKT-000212): the Frame Analysis "Quality x Shift
Average" cell is the mean of per (date, frame) shift weights; each such
frame-shift also gets its standard (the sum of the Std Prod of the spells /
yarns behind it — None if any one is missing), and the row reports the mean
standard and the mean deviation over the frame-shifts that HAVE a standard.
"""

import re
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import SPELL_MINUTES
from src.juteProduction.doff_analysis_query import (
    get_branch_scope_check_query,
    get_doff_detail_rows_query,
    get_doff_frame_range_query,
    get_doff_target_inputs_query,
)
from src.juteProduction.services.spinning_rules import p100_prod_spell

doff_analysis_router = APIRouter()

MODE_DETAILS = "details"
MODE_FRAMES = "frames"

# Raw grid over a range is the heavy cut of this report, so it gets the tighter
# of the two spec caps.
MAX_FRAME_DAYS = 31

UNMAPPED_QUALITY = "Unmapped"
CONTRACT_LABEL = "Contract"
OTHER_SHIFT = "G"

# Legacy MIS weight categories, verbatim: 2 kg steps from 26 to 44, then the
# 44-45 kg tail band and an open top. Kept as data so the FE can render an
# always-complete axis (bands with no frames come back with frame_count 0).
BAND_ORDER = ["<26"] + [f"{lo}-{lo + 2}" for lo in range(26, 44, 2)] + ["44-45", ">45"]


def _band(avg_wt: float) -> str:
    if avg_wt < 26:
        return BAND_ORDER[0]
    for lo in range(26, 44, 2):
        if avg_wt < lo + 2:
            return f"{lo}-{lo + 2}"
    return "44-45" if avg_wt <= 45 else ">45"


def _shift(spell_code) -> str:
    """Shift letter for grouping: first char of spell_code, non-A/B/C -> 'G'."""
    ch = (str(spell_code or "").strip()[:1] or "").upper()
    return ch if ch in ("A", "B", "C") else OTHER_SHIFT


def _frame_key(frame) -> tuple:
    """Natural sort for frame codes — '10' after '2', like the legacy
    CAST(frameno AS UNSIGNED) ordering, but safe for codes like 'SPG-07'."""
    s = str(frame or "")
    m = re.search(r"\d+", s)
    return (int(m.group()) if m else 10**9, s)


def _f(v) -> float:
    """DECIMAL/None -> float. Every weight in this report is 2dp."""
    return round(float(v or 0), 2)


def _iso(d) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)


def _eb_label(eb_id, emp_code) -> str:
    if eb_id is None:
        return CONTRACT_LABEL
    return str(emp_code).strip() if emp_code else f"EB{eb_id}"


def _quality(name) -> str:
    return (str(name).strip() or UNMAPPED_QUALITY) if name else UNMAPPED_QUALITY


def _avg_no_zero(vals) -> float:
    live = [v for v in vals if v]
    return round(sum(live) / len(live), 2) if live else 0.0


def _min_no_zero(vals) -> float:
    live = [v for v in vals if v]
    return round(min(live), 2) if live else 0.0


# ---------------------------------------------------------------------------
# Target (pure — target-input rows in, per-unit targets out)
# ---------------------------------------------------------------------------

# Inputs a unit needs before a target exists, with the label the note uses so
# the mill knows WHICH master to fill. Order = the order they are reported in.
TARGET_INPUTS = (
    ("std_speed", "Std Speed (frame)"),
    ("spindles", "Spindles (frame)"),
    ("std_tpi", "Std TPI (quality)"),
    ("std_eff", "Std Eff % (quality)"),
    ("act_count", "SQC spinning count for the day"),
)
SPELL_HOURS_LABEL = "spell working hours"


def _pos(v) -> float:
    """DECIMAL/None -> float, anything not strictly positive -> 0.0."""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return f if f > 0 else 0.0


def _spell_minutes(spell_code, working_hours) -> float:
    """Spell minutes exactly as the planning slice resolves them."""
    if working_hours is not None:
        return _pos(working_hours) * 60
    return float(SPELL_MINUTES.get(str(spell_code or "").strip(), 0))


def unit_target(r: dict) -> tuple:
    """(target_kg | None, [labels of the inputs that are missing]).

    A Processed unit's frozen non-zero std_prod wins, so the report agrees with
    the locked planning grid. Otherwise the target is recomputed live with the
    planning formula; minutes are split by weight share when the frame changed
    yarn mid-spell (same attribution as the slice).
    """
    frozen = _pos(r.get("frozen_target"))
    if frozen:
        return round(frozen, 2), []

    missing = [label for key, label in TARGET_INPUTS if not _pos(r.get(key))]
    minutes = _spell_minutes(r.get("spell_code"), r.get("working_hours"))
    if not minutes:
        missing.append(SPELL_HOURS_LABEL)
    if missing:
        return None, missing

    frame_net = _pos(r.get("frame_net"))
    share = _pos(r.get("item_net")) / frame_net if frame_net else 1.0
    p100 = p100_prod_spell(
        _pos(r.get("std_speed")),
        round(minutes * share),
        _pos(r.get("act_count")),
        _pos(r.get("spindles")),
        _pos(r.get("std_tpi")),
    )
    target = round(p100 * _pos(r.get("std_eff")) / 100, 2)
    return (target or None), []


def build_units(target_rows: list) -> list:
    """Target-input rows -> one dict per (date, spell, frame, yarn) unit."""
    units = []
    for r in target_rows or []:
        target, missing = unit_target(r)
        units.append(
            {
                "date": _iso(r.get("doff_date")),
                "spell_code": r.get("spell_code") or "",
                "shift": _shift(r.get("spell_code")),
                "machine_id": r.get("machine_id"),
                "frame": r.get("frame"),
                "item_id": r.get("item_id"),
                "target": target,
                "missing": missing,
            }
        )
    return units


def _vs_target(net: float, target: Optional[float]) -> dict:
    """The target columns of a row; both None when there is no target.
    diff_wt = target - actual: positive = short of target, negative = over."""
    if not target:
        return {"target_wt": None, "diff_wt": None}
    return {
        "target_wt": round(target, 2),
        "diff_wt": round(target - net, 2),
    }


def _sum_or_none(targets: list) -> Optional[float]:
    """Sum of unit targets — None as soon as ONE unit has none, because a
    partial target set against the full actual would overstate achievement."""
    if not targets or any(t is None for t in targets):
        return None
    return round(sum(targets), 2)


def target_notes(units: list) -> list:
    if not units:
        return []
    blank = [u for u in units if u["target"] is None]
    if not blank:
        return []
    counts: dict = {}
    for u in blank:
        for m in u["missing"]:
            counts[m] = counts.get(m, 0) + 1
    order = [label for _k, label in TARGET_INPUTS] + [SPELL_HOURS_LABEL]
    labels = [f"{label} ({counts[label]} rows)" for label in order if label in counts]
    note = (
        f"Target is blank for {len(blank)} of {len(units)} frame rows"
        + (f" — not entered: {', '.join(labels)}" if labels else "")
        + ". Targets come from the Spinning Standards master and the day's SQC "
        "count; rows without a target are left out of the target totals."
    )
    return [note]


# ---------------------------------------------------------------------------
# Builders (pure — rows in, response dict out)
# ---------------------------------------------------------------------------


def build_details(rows: list, units: Optional[list] = None) -> dict:
    """mode=details cuts from the day's raw doff rows.

    ``last_doff_wt`` is the net weight of the highest doff PK for the spell+frame
    (not per quality) — a frame that changed yarn mid-spell shows the same last
    doff against both of its summary rows, matching the legacy join.

    ``units`` (from ``build_units``) carries the production target per
    (spell, frame, yarn). A summary row with no target keeps target_wt / diff_wt
    = None; a spell total sums ONLY the rows that have one and says
    how many that was (``target_rows`` of ``rows``).
    """
    # Summary rows group by spell_CODE, so two spell ids sharing a code fold
    # into one row — their targets fold the same way (None if either has none).
    unit_targets: dict = {}
    for u in units or []:
        unit_targets.setdefault(
            (u["spell_code"], u["machine_id"], u["item_id"]), []
        ).append(u["target"])

    summary: dict = {}
    last_doff: dict = {}
    raw_rows = []

    for r in rows:
        spell_code = r.get("spell_code") or ""
        frame = r.get("frame")
        quality = _quality(r.get("quality_name"))
        net = _f(r.get("net_wt"))
        key = (spell_code, r.get("machine_id"), r.get("item_id"))
        agg = summary.get(key)
        if agg is None:
            agg = summary[key] = {
                "spell_code": spell_code,
                "machine_id": r.get("machine_id"),
                "frame": frame,
                "item_id": r.get("item_id"),
                "quality_name": quality,
                "net_wt": 0.0,
                "num_doffs": 0,
                "max_wt": net,
                "min_wt": net,
            }
        agg["net_wt"] += net
        agg["num_doffs"] += 1
        agg["max_wt"] = max(agg["max_wt"], net)
        agg["min_wt"] = min(agg["min_wt"], net)

        doff_id = r.get("doff_id") or 0
        lkey = (spell_code, r.get("machine_id"))
        if lkey not in last_doff or doff_id >= last_doff[lkey][0]:
            last_doff[lkey] = (doff_id, net)

        raw_rows.append(
            {
                "doff_id": r.get("doff_id"),
                "frame": frame,
                "machine_id": r.get("machine_id"),
                "item_id": r.get("item_id"),
                "quality_name": quality,
                "spell_code": spell_code,
                "eb_label": _eb_label(r.get("eb_id"), r.get("emp_code")),
                "net_wt": net,
            }
        )

    doff_summary = []
    for (spell_code, machine_id, _item), agg in summary.items():
        agg["net_wt"] = round(agg["net_wt"], 2)
        agg["avg_wt"] = round(agg["net_wt"] / agg["num_doffs"], 2) if agg["num_doffs"] else 0.0
        agg["last_doff_wt"] = last_doff.get((spell_code, machine_id), (0, 0.0))[1]
        agg.update(
            _vs_target(
                agg["net_wt"],
                _sum_or_none(unit_targets.get((spell_code, machine_id, _item), [])),
            )
        )
        doff_summary.append(agg)
    doff_summary.sort(key=lambda x: (x["spell_code"], _frame_key(x["frame"])))

    totals: dict = {}
    for agg in doff_summary:
        t = totals.setdefault(
            agg["spell_code"],
            {"net_wt": 0.0, "target_wt": 0.0, "diff_wt": 0.0, "target_rows": 0, "rows": 0},
        )
        t["net_wt"] += agg["net_wt"]
        t["rows"] += 1
        if agg["target_wt"] is not None:
            t["target_wt"] += agg["target_wt"]
            t["diff_wt"] += agg["diff_wt"]
            t["target_rows"] += 1
    spell_totals = [
        {
            "spell_code": k,
            "net_wt": round(t["net_wt"], 2),
            "target_wt": round(t["target_wt"], 2) if t["target_rows"] else None,
            "diff_wt": round(t["diff_wt"], 2) if t["target_rows"] else None,
            "target_rows": t["target_rows"],
            "rows": t["rows"],
        }
        for k, t in sorted(totals.items())
    ]

    bands = {b: {"band": b, "frame_count": 0, "spell_breakup": {}} for b in BAND_ORDER}
    for agg in doff_summary:
        b = bands[_band(agg["avg_wt"])]
        b["frame_count"] += 1
        brk = b["spell_breakup"]
        brk[agg["spell_code"]] = brk.get(agg["spell_code"], 0) + 1

    raw_rows.sort(key=lambda x: (x["spell_code"], _frame_key(x["frame"]), x["doff_id"] or 0))
    return {
        "doff_summary": doff_summary,
        "spell_totals": spell_totals,
        "weight_bands": [bands[b] for b in BAND_ORDER],
        "raw_rows": raw_rows,
    }


def _quality_shift_target(cells: list) -> dict:
    """Standard columns of one Quality x Shift row (TKT-000212).

    ``cells`` = [(actual_net, std_or_None)] per (date, frame) frame-shift — the
    same population ``avg_net_wt`` is the mean of. Standard and deviation are
    taken over the frame-shifts that HAVE a standard only (a missing standard
    is never a zero), so ``diff_wt`` compares like with like even when part of
    the range has no standards; ``target_cells`` of ``cells`` says how much of
    the row that was.

    target_wt = mean standard per frame-shift (kg); diff_wt = target - actual
    (positive = short, the report's TKT-000168 convention); diff_pct = diff_wt
    as a % of target_wt.
    """
    matched = [(net, t) for net, t in cells if t is not None and t > 0]
    out = {
        "target_wt": None,
        "diff_wt": None,
        "diff_pct": None,
        "target_cells": len(matched),
        "cells": len(cells),
    }
    if not matched:
        return out
    n = len(matched)
    target = sum(t for _net, t in matched) / n
    diff = target - sum(net for net, _t in matched) / n
    out["target_wt"] = round(target, 2)
    out["diff_wt"] = round(diff, 2)
    out["diff_pct"] = round(diff / target * 100, 2)
    return out


def build_frames(
    rows: list, frame: Optional[str] = None, units: Optional[list] = None
) -> dict:
    """mode=frames cuts from the (date, shift, frame, quality, worker) aggregate.

    Pivot cell = the day's weight for that frame+quality divided by the number of
    DISTINCT shifts it ran that day, so a frame running 3 shifts is comparable
    with one running 1 (the legacy per-shift normalisation).

    ``units`` (from ``build_units``, ALL frames of the range) adds the standard
    to each Quality x Shift row — see ``_quality_shift_target``.
    """
    day_cell: dict = {}  # (frame, quality) -> {date: net}
    day_shifts: dict = {}  # (frame, quality, date) -> {shift}
    qs: dict = {}  # (quality, shift) -> [per date+frame sums]
    qs_pairs: dict = {}  # same key -> {(machine_id, item_id)} behind the sum
    dates: set = set()

    # (date, shift, machine_id, item_id) -> [unit targets] (one per spell)
    unit_targets: dict = {}
    for u in units or []:
        unit_targets.setdefault(
            (u["date"], u["shift"], u["machine_id"], u["item_id"]), []
        ).append(u["target"])

    def _cell_std(d, sh, pairs) -> Optional[float]:
        """Standard of one frame-shift: every (machine, yarn) behind it must
        have units, and every unit a target — else None (unstamped doffs too)."""
        targets: list = []
        for machine_id, item_id in pairs:
            found = unit_targets.get((d, sh, machine_id, item_id))
            if item_id is None or not found:
                return None
            targets.extend(found)
        return _sum_or_none(targets)

    for r in rows:
        d = _iso(r.get("doff_date"))
        sh = _shift(r.get("shift"))
        quality = _quality(r.get("quality_name"))
        fr = r.get("frame")
        net = _f(r.get("net_wt"))
        dates.add(d)

        cells = day_cell.setdefault((fr, quality), {})
        cells[d] = round(cells.get(d, 0.0) + net, 2)
        day_shifts.setdefault((fr, quality, d), set()).add(sh)
        qs.setdefault((quality, sh, d, fr), 0.0)
        qs[(quality, sh, d, fr)] += net
        qs_pairs.setdefault((quality, sh, d, fr), set()).add(
            (r.get("machine_id"), r.get("item_id"))
        )

    # Quality x shift average over the per (date, frame) shift weights.
    qs_group: dict = {}
    for (quality, sh, d, fr), net in qs.items():
        std = _cell_std(d, sh, qs_pairs[(quality, sh, d, fr)]) if units else None
        qs_group.setdefault((quality, sh), []).append((net, std))
    quality_shift_avg = [
        {
            "quality_name": quality,
            "shift": sh,
            "avg_net_wt": (
                round(sum(net for net, _t in v) / len(v), 2) if v else 0.0
            ),
            **_quality_shift_target(v),
        }
        for (quality, sh), v in sorted(qs_group.items())
    ]

    frame_pivot = []
    for (fr, quality), cells in day_cell.items():
        per_date = {}
        for d, net in cells.items():
            n_shifts = len(day_shifts.get((fr, quality, d)) or ()) or 1
            per_date[d] = round(net / n_shifts, 2)
        vals = list(per_date.values())
        frame_pivot.append(
            {
                "frame": fr,
                "quality_name": quality,
                "per_date": dict(sorted(per_date.items())),
                "avg": _avg_no_zero(vals),
                "max": round(max(vals), 2) if vals else 0.0,
                "min": _min_no_zero(vals),
            }
        )
    frame_pivot.sort(key=lambda x: (_frame_key(x["frame"]), x["quality_name"]))

    out = {
        "quality_shift_avg": quality_shift_avg,
        "frame_pivot": frame_pivot,
        "dates": sorted(dates),
    }
    if frame:
        out["frame_detail"] = _frame_detail(rows, frame, units)
    return out


def _frame_detail(rows: list, frame: str, units: Optional[list] = None) -> dict:
    """Shift-wise production + per-worker doff events for one frame.

    A date+shift row is several (spell, yarn) units. It gets a target only when
    EVERY unit behind it has one and none of its doffs is unstamped — a partial
    target against the full shift weight would flatter the frame.
    """
    by_shift: dict = {}
    events: dict = {}
    unstamped: set = set()
    shift_targets: dict = {}
    for u in units or []:
        if str(u["frame"] or "") == str(frame):
            shift_targets.setdefault((u["date"], u["shift"]), []).append(u["target"])
    for r in rows:
        if str(r.get("frame") or "") != str(frame):
            continue
        d = _iso(r.get("doff_date"))
        sh = _shift(r.get("shift"))
        net = _f(r.get("net_wt"))
        by_shift[(d, sh)] = round(by_shift.get((d, sh), 0.0) + net, 2)
        if r.get("item_id") is None:
            unstamped.add((d, sh))

        ekey = (d, sh, r.get("eb_id"))
        ev = events.get(ekey)
        if ev is None:
            ev = events[ekey] = {
                "date": d,
                "shift": sh,
                "eb_label": _eb_label(r.get("eb_id"), r.get("emp_code")),
                "num_doffs": 0,
                "production": 0.0,
                "max_doff": _f(r.get("max_wt")),
                "min_doff": _f(r.get("min_wt")),
            }
        ev["num_doffs"] += int(r.get("num_doffs") or 0)
        ev["production"] = round(ev["production"] + net, 2)
        ev["max_doff"] = max(ev["max_doff"], _f(r.get("max_wt")))
        ev["min_doff"] = min(ev["min_doff"], _f(r.get("min_wt")))

    doff_events = []
    for ev in events.values():
        ev["avg_doff_wt"] = (
            round(ev["production"] / ev["num_doffs"], 2) if ev["num_doffs"] else 0.0
        )
        doff_events.append(ev)
    doff_events.sort(key=lambda x: (x["date"], x["shift"], x["eb_label"]))

    return {
        "date_shift": [
            {
                "date": d,
                "shift": sh,
                "net_wt": net,
                **_vs_target(
                    net,
                    None
                    if (d, sh) in unstamped
                    else _sum_or_none(shift_targets.get((d, sh), [])),
                ),
            }
            for (d, sh), net in sorted(by_shift.items())
        ],
        "doff_events": doff_events,
    }


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


def _require_int(request: Request, key: str) -> int:
    raw = request.query_params.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


def _optional_int(request: Request, key: str) -> Optional[int]:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


def _require_date(request: Request, key: str) -> date:
    raw = request.query_params.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key} format")


def _verify_pair(db: Session, co_id: int, branch_id: int) -> None:
    """daily_doff_tbl has no co_id, so a branch of ANOTHER company of the same
    tenant would report that company's frames under this co_id's header."""
    if db.execute(
        get_branch_scope_check_query(), {"co_id": co_id, "branch_id": branch_id}
    ).first() is None:
        raise HTTPException(status_code=400, detail="branch_id does not belong to co_id")


def _meta(co_id, branch_id, d_from, d_to, notes) -> dict:
    return {
        "co_id": co_id,
        "branch_id": branch_id,
        "date_from": _iso(d_from),
        "date_to": _iso(d_to),
        "notes": notes,
    }


def _load_units(db: Session, binds: dict) -> tuple:
    """(units, notes). The target is an overlay on a report whose actuals must
    keep working on a tenant that has not got the spinning-standards tables yet
    (schema drift) — so a failing target query blanks the targets and says so
    instead of failing the whole report."""
    try:
        target_rows = [
            dict(r._mapping)
            for r in db.execute(get_doff_target_inputs_query(), binds).fetchall()
        ]
    except Exception:
        return [], ["Targets could not be loaded — target columns are blank."]
    units = build_units(target_rows)
    return units, target_notes(units)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@doff_analysis_router.get("/doff_analysis")
def doff_analysis(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    # Every parameter is validated BEFORE the session is touched, so a bad date
    # is a 400 that never costs a round trip.
    mode = (request.query_params.get("mode") or MODE_DETAILS).strip().lower()
    if mode not in (MODE_DETAILS, MODE_FRAMES):
        raise HTTPException(status_code=400, detail="mode must be 'details' or 'frames'")
    co_id = _require_int(request, "co_id")
    branch_id = _require_int(request, "branch_id")
    item_id = _optional_int(request, "item_id")
    details = mode == MODE_DETAILS
    frame = None

    if details:
        report_date = _require_date(request, "report_date")
        d_from = d_to = report_date
        binds = {
            "co_id": co_id,
            "branch_id": branch_id,
            "report_date": report_date,
            "spell_id": _optional_int(request, "spell_id"),
            "item_id": item_id,
        }
    else:
        d_from = _require_date(request, "date_from")
        d_to = _require_date(request, "date_to")
        if d_to < d_from:
            raise HTTPException(status_code=400, detail="date_to is before date_from")
        if (d_to - d_from).days >= MAX_FRAME_DAYS:
            raise HTTPException(
                status_code=400,
                detail=f"Date range too large — at most {MAX_FRAME_DAYS} days",
            )
        frame = (request.query_params.get("frame") or "").strip() or None
        binds = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": d_from,
            "date_to": d_to,
            "item_id": item_id,
        }

    try:
        _verify_pair(db, co_id, branch_id)
        query = get_doff_detail_rows_query() if details else get_doff_frame_range_query()
        rows = [dict(r._mapping) for r in db.execute(query, binds).fetchall()]

        # Targets: the single day, the per-frame drill, and (TKT-000212) the
        # Quality x Shift standard — which needs EVERY frame of the range, so
        # frames mode loads units unfiltered by frame and the drill picks its
        # own out in Python. No doffs -> nothing to compare, no extra query.
        units = []
        target_notes_ = []
        if rows:
            units, target_notes_ = _load_units(
                db,
                {
                    "co_id": co_id,
                    "branch_id": branch_id,
                    "date_from": d_from,
                    "date_to": d_to,
                    "spell_id": binds.get("spell_id"),
                    "item_id": item_id,
                    "frame": None,
                },
            )

        if details:
            data = build_details(rows, units)
            notes = [f"{len(rows)} doff rows (spinning weight types) on {d_from}."]
            if any(r["quality_name"] == UNMAPPED_QUALITY for r in data["raw_rows"]):
                notes.append("Doffs with no yarn stamped are grouped as 'Unmapped'.")
        else:
            data = build_frames(rows, frame, units)
            notes = [
                "Pivot cells are the day's weight divided by the number of shifts "
                "that frame ran that day; averages and minimums ignore blank days."
            ]
            if frame and not data.get("frame_detail", {}).get("date_shift"):
                notes.append(f"No doffs for frame '{frame}' in this range.")

        if rows:
            notes.extend(target_notes_)
        data["meta"] = _meta(co_id, branch_id, d_from, d_to, notes)
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

"""Weaving Low Producer report — GET /api/juteProductionReports/weaving_low_producer

One endpoint replacing three legacy MIS pages (S4 Low Producer, Hessian Low
Producer, S4 FromDayToDay): the legacy "shed" split was a hardcoded loom-number
prefix, which here becomes the quality / loom filters.

Everything is computed from the weaving day-slice — ``fetch_weaving_day_rows``
per date over the range (fresh frozen-log rows per spell where they exist, live
recompute otherwise), then aggregated in Python. The slice already carries
efficiency, production_kg, std_prod_yds and ozs_yds per loom+spell, so this
module never re-derives a production formula; it only groups, averages and
filters.

"100% Prod (kg)" is the loom's 100%-efficiency CAPACITY for the spell —
``std_prod_yds`` (eff_speed x hours x 60 / (36 x std_picks)) weighed at the
quality's oz/yd, exactly as the DPR's std basis. It is NOT the slice's
``std_prod_kg``, which is the ACTUAL yards re-weighed at the quality's STANDARD
oz/yd and therefore equals the actual kg whenever std_ozs_yds == ozs_yds
(TKT-000177).

Averages everywhere use the legacy "shed average" rule: rows with zero/NULL
efficiency are EXCLUDED from every average (a loom entered but not run would
otherwise drag the shed average down), while production sums include them.
``meta`` carries both the filtered average and the whole-range (unfiltered)
average so the page can show "am I below the shed?".
"""

import re
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.dpr_reports import _f, _ratio, _round
from src.juteProduction.lowprod_weaving_query import (
    get_branch_in_company_query,
    get_weaving_worker_names_query,
)
from src.juteProduction.reports import _checklist_scope
from src.juteProduction.weaving_lock import fetch_weaving_day_rows

weaving_lowprod_router = APIRouter()

SHIFTS = ("A", "B", "C")
LOOM_GROUP_SIZE = 6
# The endpoint re-runs the day-slice once per date; 31 days is the "heavy raw
# grid" cap (~120 looms x 5 spells x 31 days of base rows).
MAX_DAYS = 31


# ---------------------------------------------------------------------------
# Param helpers
# ---------------------------------------------------------------------------


def _opt_int(request: Request, key: str) -> Optional[int]:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


def _opt_float(request: Request, key: str) -> Optional[float]:
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


def _opt_shift(request: Request) -> Optional[str]:
    raw = (request.query_params.get("shift") or "").strip().upper()
    if not raw:
        return None
    if raw not in SHIFTS:
        raise HTTPException(status_code=400, detail="shift must be one of A, B, C")
    return raw


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------


def shift_of(spell_code) -> str:
    """Shift letter for grouping = first char of the spell code ('A1' -> 'A').

    Anything outside A/B/C (general shift, an unmapped spell) buckets as 'G' so
    its production is never silently dropped from the day totals."""
    s = str(spell_code or "").strip()[:1].upper()
    return s if s in SHIFTS else "G"


def loom_group(loom) -> str:
    """Numeric tail of the loom name, bucketed into bands of 6.

    The band key is the WHOLE trailing digit run of the name, same as the legacy
    report: start = ((n-1)//6)*6+1, label "{start}to{start+5}". A name with no
    trailing digits (or loom 0) has no band -> 'Unknown', never a label like
    '-5to0'."""
    m = re.search(r"(\d+)$", str(loom or ""))
    if not m:
        return "Unknown"
    n = int(m.group(1))
    if n <= 0:
        return "Unknown"
    start = ((n - 1) // LOOM_GROUP_SIZE) * LOOM_GROUP_SIZE + 1
    return f"{start}to{start + LOOM_GROUP_SIZE - 1}"


def _loom_group_key(label: str) -> int:
    m = re.match(r"(\d+)to", label or "")
    return int(m.group(1)) if m else 10**9


def _iso(v) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def build_base_rows(slice_rows: list, names: dict) -> list:
    """Day-slice rows -> report rows (one per loom+spell entry)."""
    out = []
    for r in slice_rows:
        loom = r.get("machine_name") or r.get("mech_code") or ""
        info = names.get(r.get("eb_id")) or {}
        out.append(
            {
                "tran_date": _iso(r.get("tran_date")),
                "shift": shift_of(r.get("spell_code")),
                "spell_code": r.get("spell_code"),
                "machine_id": r.get("machine_id"),
                "loom": loom,
                "mech_code": r.get("mech_code"),
                "eb_id": r.get("eb_id"),
                "eb_no": info.get("eb_no"),
                "worker_name": info.get("worker_name"),
                "quality_id": r.get("weaving_quality_id"),
                "quality_name": r.get("weaving_quality_name"),
                "loom_group": loom_group(loom),
                "act_prod_kg": _round(_f(r.get("production_kg")), 2),
                # 100%-efficiency capacity in kg (DPR basis) — see module doc.
                "prod_100_kg": _round(
                    _f(r.get("std_prod_yds")) * _f(r.get("ozs_yds")) * 28.35 / 1000.0, 2
                ),
                "eff_pct": _round(_f(r.get("efficiency")), 2),
                "hours": _round(_f(r.get("working_hours")), 2),
            }
        )
    out.sort(key=lambda x: (x["tran_date"] or "", x["shift"], x["loom"]))
    return out


def apply_row_filters(
    rows: list,
    quality_id: Optional[int] = None,
    shift: Optional[str] = None,
    eb_no: Optional[str] = None,
    loom: Optional[str] = None,
) -> list:
    """Base-row filters. ``eb_no`` matches the emp code or the raw eb_id, ``loom``
    is a case-insensitive substring of the loom name OR its mech_code — the
    legacy report's shed-prefix selector generalised."""
    eb_q = (eb_no or "").strip().lower()
    loom_q = (loom or "").strip().lower()
    out = []
    for r in rows:
        if quality_id is not None and r["quality_id"] != quality_id:
            continue
        if shift and r["shift"] != shift:
            continue
        if eb_q and eb_q not in (str(r.get("eb_no") or "").lower(), str(r.get("eb_id") or "")):
            continue
        if loom_q and loom_q not in (
            f"{r.get('loom') or ''} {r.get('mech_code') or ''}".lower()
        ):
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Section builders (pure aggregation over already-filtered base rows)
# ---------------------------------------------------------------------------


def _avg(vals: list) -> Optional[float]:
    """Mean, or None for an empty set — a worker/shift with no runnable
    efficiency must read blank, never 0 (0 would sort them to the top of a
    LOW-producer list and get someone shouted at for not working that shift)."""
    return _round(sum(vals) / len(vals), 2) if vals else None


def _eff_of(row) -> Optional[float]:
    """The row's efficiency when it counts towards an average — zero/NULL eff is
    excluded (shed-average rule)."""
    eff = row.get("eff_pct")
    return eff if eff else None


def build_worker_summary(rows: list) -> list:
    """Per-worker roll-up, worst average efficiency first.

    ``avg_looms_run`` counts loom ROWS (not distinct looms) per attended day —
    a worker minding the same loom in A1 and A2 was on two loom-shifts."""
    by_eb: dict = {}
    for r in rows:
        eb = r.get("eb_id")
        if eb is None:
            continue  # unassigned loom — counted in meta.notes instead
        w = by_eb.setdefault(
            eb,
            {
                "eb_id": eb,
                "eb_no": r.get("eb_no"),
                "worker_name": r.get("worker_name"),
                "_dates": set(),
                "_rows": 0,
                "_eff": {s: [] for s in (*SHIFTS, "G")},
                "_all": [],
            },
        )
        w["_dates"].add(r["tran_date"])
        w["_rows"] += 1
        eff = _eff_of(r)
        if eff is not None:
            w["_eff"].setdefault(r["shift"], []).append(eff)
            w["_all"].append(eff)

    out = []
    for w in by_eb.values():
        days = len(w["_dates"])
        out.append(
            {
                "eb_id": w["eb_id"],
                "eb_no": w["eb_no"],
                "worker_name": w["worker_name"],
                "days_attended": days,
                "avg_looms_run": _ratio(w["_rows"], days, 2),
                "eff_a": _avg(w["_eff"]["A"]),
                "eff_b": _avg(w["_eff"]["B"]),
                "eff_c": _avg(w["_eff"]["C"]),
                "avg_eff": _avg(w["_all"]),
            }
        )
    # Lowest average first; workers with no measurable efficiency last.
    out.sort(key=lambda x: (x["avg_eff"] is None, x["avg_eff"] or 0.0))
    return out


def filter_worker_summary(
    workers: list,
    min_days: Optional[int] = None,
    max_days: Optional[int] = None,
    min_eff: Optional[float] = None,
    max_eff: Optional[float] = None,
) -> list:
    """Post-aggregation thresholds (base rows and the other sections keep every
    worker — these narrow the low-producer list only)."""
    out = []
    for w in workers:
        if min_days is not None and w["days_attended"] < min_days:
            continue
        if max_days is not None and w["days_attended"] > max_days:
            continue
        if min_eff is not None or max_eff is not None:
            eff = w["avg_eff"]
            if eff is None:
                continue
            if min_eff is not None and eff < min_eff:
                continue
            if max_eff is not None and eff > max_eff:
                continue
        out.append(w)
    return out


def build_day_summary(rows: list) -> list:
    """Per-date shift pivot: production, average efficiency, looms run, kg/loom.

    ``looms_run`` counts DISTINCT looms that actually produced (act prod > 0) —
    a loom entered with no output was not run. Production sums include those
    zero rows (they add nothing) so kg/loom stays kg-of-running-looms."""
    by_date: dict = {}
    for r in rows:
        d = by_date.setdefault(
            r["tran_date"],
            {
                "date": r["tran_date"],
                "prod": {},
                "eff": {},
                "looms": {},
                "all_eff": [],
            },
        )
        s = r["shift"]
        d["prod"][s] = d["prod"].get(s, 0.0) + _f(r["act_prod_kg"])
        eff = _eff_of(r)
        if eff is not None and _f(r["act_prod_kg"]) > 0:
            d["eff"].setdefault(s, []).append(eff)
            d["all_eff"].append(eff)
        if _f(r["act_prod_kg"]) > 0:
            d["looms"].setdefault(s, set()).add(r["machine_id"])

    out = []
    for d in by_date.values():
        row = {"date": d["date"]}
        total_prod = sum(d["prod"].values())
        # Loom-SHIFTS, not distinct looms: a loom run in both A and B is two
        # runs, matching the per-shift columns it is the total of.
        total_looms = sum(len(v) for v in d["looms"].values())
        for s in SHIFTS:
            prod = d["prod"].get(s, 0.0)
            looms = len(d["looms"].get(s, ()))
            key = s.lower()
            row[f"act_prod_{key}"] = _round(prod, 2)
            row[f"avg_eff_{key}"] = _avg(d["eff"].get(s, []))
            row[f"looms_run_{key}"] = looms
            row[f"kg_per_loom_{key}"] = _ratio(prod, looms, 2)
        row["act_prod_total"] = _round(total_prod, 2)
        # Count-weighted: the mean over every qualifying row, which is exactly
        # sum(shift_avg * shift_count) / sum(shift_count).
        row["avg_eff_total"] = _avg(d["all_eff"])
        row["looms_run_total"] = total_looms
        row["kg_per_loom_total"] = _ratio(total_prod, total_looms, 2)
        out.append(row)
    out.sort(key=lambda x: x["date"] or "")
    return out


def build_loom_group_summary(rows: list) -> list:
    """Per loom band (6 looms): shift averages plus the band's best/worst run."""
    by_group: dict = {}
    for r in rows:
        eff = _eff_of(r)
        if eff is None:
            continue
        g = by_group.setdefault(
            r["loom_group"], {"loom_group": r["loom_group"], "eff": {}, "all": []}
        )
        g["eff"].setdefault(r["shift"], []).append(eff)
        g["all"].append(eff)

    out = []
    for g in by_group.values():
        out.append(
            {
                "loom_group": g["loom_group"],
                "avg_eff_a": _avg(g["eff"].get("A", [])),
                "avg_eff_b": _avg(g["eff"].get("B", [])),
                "avg_eff_c": _avg(g["eff"].get("C", [])),
                "avg_eff": _avg(g["all"]),
                "max_eff": _round(max(g["all"]), 2),
                "min_eff": _round(min(g["all"]), 2),
            }
        )
    out.sort(key=lambda x: _loom_group_key(x["loom_group"]))
    return out


def build_meta(co_id, branch_id, d_from, d_to, all_rows: list, rows: list) -> dict:
    """Filtered vs whole-range (shed) average + the caveats the page must show."""
    notes = []
    no_eb = sum(1 for r in rows if r.get("eb_id") is None)
    if no_eb:
        notes.append(
            f"{no_eb} loom entries carry no worker (eb) and are excluded from the "
            "worker summary."
        )
    idle = len({r["machine_id"] for r in rows if not _f(r["act_prod_kg"])})
    if idle:
        notes.append(f"{idle} looms were entered but produced nothing (act prod = 0).")
    notes.append(
        "Averages exclude rows with zero/blank efficiency (shed-average rule); "
        "production totals include them."
    )
    return {
        "co_id": co_id,
        "branch_id": branch_id,
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "filtered_avg_eff": _avg([e for e in map(_eff_of, rows) if e is not None]),
        "overall_avg_eff": _avg([e for e in map(_eff_of, all_rows) if e is not None]),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Shared fetch (also used by the Low Performer Follow-up)
# ---------------------------------------------------------------------------


def fetch_weaving_lowprod_rows(db: Session, co_id: int, branch_id: int, d_from, d_to) -> list:
    """Unfiltered base rows (one per loom + spell + date) for a branch and range.

    The caller has already verified the co/branch pair and capped the range.
    """
    # ponytail: one day-slice call per date (the slice is day-scoped by
    # design and picks frozen-log vs live per spell itself). Fine at the
    # 31-day cap; if the range ever widens, add a range-scoped log query
    # for the fully-frozen days instead of unrolling the loop here.
    slice_rows = []
    d = d_from
    while d <= d_to:
        slice_rows.extend(fetch_weaving_day_rows(db, co_id, d, branch_id))
        d += timedelta(days=1)

    eb_ids = sorted({r.get("eb_id") for r in slice_rows if r.get("eb_id") is not None})
    names = {}
    if eb_ids:
        names = {
            r._mapping["eb_id"]: dict(r._mapping)
            for r in db.execute(
                get_weaving_worker_names_query(), {"eb_ids": eb_ids}
            ).fetchall()
        }
    return build_base_rows(slice_rows, names)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@weaving_lowprod_router.get("/weaving_low_producer")
def weaving_low_producer(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id, branch_id, d_from, d_to = _checklist_scope(request, MAX_DAYS)
    if branch_id is None:
        raise HTTPException(status_code=400, detail="branch_id is required")
    quality_id = _opt_int(request, "quality_id")
    shift = _opt_shift(request)
    eb_no = request.query_params.get("eb_no")
    loom = request.query_params.get("loom")
    min_days = _opt_int(request, "min_days")
    max_days = _opt_int(request, "max_days")
    min_eff = _opt_float(request, "min_eff")
    max_eff = _opt_float(request, "max_eff")

    try:
        if not db.execute(
            get_branch_in_company_query(), {"branch_id": branch_id, "co_id": co_id}
        ).first():
            raise HTTPException(status_code=400, detail="branch_id does not belong to co_id")

        all_rows = fetch_weaving_lowprod_rows(db, co_id, branch_id, d_from, d_to)
        rows = apply_row_filters(all_rows, quality_id, shift, eb_no, loom)
        workers = filter_worker_summary(
            build_worker_summary(rows), min_days, max_days, min_eff, max_eff
        )
        return {
            "data": {
                "base_rows": rows,
                "worker_summary": workers,
                "day_summary": build_day_summary(rows),
                "loom_group_summary": build_loom_group_summary(rows),
                "meta": build_meta(co_id, branch_id, d_from, d_to, all_rows, rows),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

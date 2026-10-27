"""Winding Low Producer report — port of the legacy MIS low-producer view.

One endpoint, ``GET /winding_low_producer``, under the existing
``/api/juteProductionReports`` prefix. It answers "which winders are running
below target, and on which shifts", over a date range.

    eff% = prod_kg / (target_kg_8h / 8 * att_hrs) * 100

``prod_kg`` is the reconciled figure the winding module already computes at read
time (doff sum - opening jugar + closing jugar, once per winder per spell);
``target_kg_8h`` comes from the winding target master (KG per winder per 8-hour
shift); ``att_hrs`` is ``working_hours - idle_hours`` from ``daily_attendance``,
matched on eb_id + date + spell_id.

That attendance match is new here. The older winding reports state, correctly
for themselves, that no attendance link exists and that their targets are
therefore NOT pro-rated by hours worked. This report closes exactly that gap —
a winder who worked four hours is measured against half a shift's target, not a
whole one. Where no attendance row is found the row falls back to 8.0 hours and
is FLAGGED (``att_source = 'default8'``) and counted in ``meta.notes``, so a
fallback can never be mistaken for a measured shift.

Conventions follow winding_reports.py / reports.py: plain ``def`` handlers,
``get_tenant_db`` + ``get_current_user_with_refresh``, ``{"data": ...}``
responses, params validated before the try/except so a bad date is a 400 and
never a 500. The pivots and the arithmetic are pure builders (no I/O), which is
what makes them testable without a database.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.lowprod_winding_query import (
    get_spell_hours_query,
    get_winding_attendance_hours_query,
    get_winding_reconciliation_range_query,
)
from src.juteProduction.winding_query import resolve_winding_targets_query


router = APIRouter()
# main.py's convention is `import router as <name>_router`; the alias exists so the
# integrator can import either spelling.
winding_lowprod_router = router

SHIFT_BUCKETS = ("A", "B", "C")
OFF_SHIFT = "G"  # anything that does not resolve to A/B/C

# Full-shift hours come from spell_mst.working_hours (get_spell_hours_query);
# these are only the FALLBACKS for a spell with no configured hours. The target
# master's unit stays "KG per winder per 8-HOUR shift", so std production
# always pro-rates on TARGET_UNIT_HOURS regardless of shift length.
TARGET_UNIT_HOURS = 8.0
FALLBACK_SHIFT_HOURS = 8.0
FALLBACK_C_SHIFT_HOURS = 7.5

ATT_SOURCE_REAL = "attendance"
ATT_SOURCE_DEFAULT = "default8"

# Raw per-winder-per-spell grid, so the tighter of the two range caps.
MAX_RANGE_DAYS = 31


# =============================================================================
# Parameter validation
# =============================================================================


def _require_int(request: Request, key: str) -> int:
    raw = request.query_params.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


def _scope(request: Request) -> tuple:
    """co_id + branch_id + a validated date range. Mirrors reports._checklist_scope,
    except branch_id is REQUIRED here (every winding read is branch-keyed)."""
    co_id = _require_int(request, "co_id")
    branch_id = _require_int(request, "branch_id")
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")
    if not date_from or not date_to:
        raise HTTPException(status_code=400, detail="date_from and date_to are required")
    try:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date_from or date_to format")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="date_to is before date_from")
    if (d_to - d_from).days >= MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range too large — at most {MAX_RANGE_DAYS} days",
        )
    return co_id, branch_id, d_from, d_to


def _optional_filters(request: Request) -> tuple:
    shift = (request.query_params.get("shift") or "").strip().upper() or None
    if shift and shift not in SHIFT_BUCKETS + (OFF_SHIFT,):
        raise HTTPException(status_code=400, detail="Invalid shift — expected A, B, C or G")
    eb_no = (request.query_params.get("eb_no") or "").strip() or None
    raw_item = request.query_params.get("item_id")
    try:
        item_id = int(raw_item) if raw_item else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid item_id")
    return shift, eb_no, item_id


def _verify_pair(db: Session, co_id: int, branch_id: int) -> None:
    ok = db.execute(
        text("SELECT 1 FROM branch_mst WHERE branch_id = :branch_id AND co_id = :co_id"),
        {"branch_id": branch_id, "co_id": co_id},
    ).first()
    if not ok:
        raise HTTPException(status_code=400, detail="branch_id does not belong to co_id")


# =============================================================================
# Pure builders
# =============================================================================


def _f(v, default: float = 0.0) -> float:
    return default if v is None else float(v)


def _day_key(v) -> str:
    """Date part of a date/datetime/string, as the index key both sides share."""
    return str(v)[:10] if v is not None else ""


def _bucket(shift) -> str:
    b = (shift or "")[:1].upper()
    return b if b in SHIFT_BUCKETS else OFF_SHIFT


def _avg(values):
    """The "shed average": zero and missing efficiencies are NOT averaged in.

    A winder with no target configured, or no hours, has no efficiency — folding
    a 0 into the mean would drag every average down and invent low producers.
    """
    vals = [float(v) for v in values if v]
    return round(sum(vals) / len(vals), 2) if vals else None




def _full_hours(spell_id, shift, spell_hours_map) -> float:
    """A full shift's hours for this row: spell_mst.working_hours first,
    hardcoded legacy values only when the spell has none configured."""
    configured = _f(spell_hours_map.get(spell_id))
    if configured > 0:
        return configured
    return FALLBACK_C_SHIFT_HOURS if _bucket(shift) == "C" else FALLBACK_SHIFT_HOURS


def winders_of(att_hrs, full_hours) -> float:
    """Fractional winder-count for one row: hours worked over a full shift, so
    a full shift of any configured length is one winder."""
    return round(_f(att_hrs) / (full_hours or FALLBACK_SHIFT_HOURS), 3)


def std_and_eff(prod_kg, target_kg_8h, att_hrs) -> tuple:
    """(std_prod, eff_pct) for one row.

    ``std_prod = target_kg_8h / 8 * att_hrs`` — the target the target master
    states for a full 8-hour shift, scaled to the hours actually worked. The
    8 here is the target master's UNIT, not a shift length.

    eff is None (not 0.0) when there is no denominator — no target configured or
    no hours worked. 0.0 would read as "produced nothing against a real target",
    which is a different and much more damning statement about a worker.
    """
    std = round(_f(target_kg_8h) / TARGET_UNIT_HOURS * _f(att_hrs), 3)
    if std <= 0:
        return std, None
    return std, round(_f(prod_kg) / std * 100.0, 2)


def build_base_rows(recon_rows, att_index, targets, spell_hours_map) -> list:
    """One row per winder per spell per date, with hours, target and efficiency.

    ``att_index`` maps (date, spell_id, eb_id) -> att_hrs; a key that is absent
    falls back to the spell's own configured hours and is flagged
    ``att_source='default8'``. ``targets`` maps item_id -> KG per winder per
    8-hour shift. ``spell_hours_map`` maps spell_id -> spell_mst.working_hours.
    """
    out = []
    for m in recon_rows:
        shift = _bucket(m.get("shift"))
        full_hours = _full_hours(m.get("spell_id"), m.get("shift"), spell_hours_map)
        key = (_day_key(m.get("tran_date")), m.get("spell_id"), m.get("eb_id"))
        measured = att_index.get(key)
        att_hrs = full_hours if measured is None else round(_f(measured), 2)
        target = _f(targets.get(m.get("item_id")))
        prod_kg = round(_f(m.get("production_kg")), 3)
        std, eff = std_and_eff(prod_kg, target, att_hrs)
        out.append(
            {
                "tran_date": _day_key(m.get("tran_date")),
                "shift": shift,
                "spell_id": m.get("spell_id"),
                "spell_code": m.get("spell_code"),
                "eb_id": m.get("eb_id"),
                "eb_no": m.get("emp_code"),
                "worker_name": m.get("worker_name"),
                "item_id": m.get("item_id"),
                "quality_name": m.get("item_name") or m.get("item_code"),
                "prod_kg": prod_kg,
                "att_hrs": att_hrs,
                "att_source": ATT_SOURCE_DEFAULT if measured is None else ATT_SOURCE_REAL,
                "target_kg_8h": round(target, 3),
                "std_prod": std,
                "eff_pct": eff,
                "no_of_winders": winders_of(att_hrs, full_hours),
            }
        )
    out.sort(key=lambda r: (r["tran_date"], r["shift"], r["eb_no"] or "", r["eb_id"] or 0))
    return out


def keep_row(row, shift, eb_no) -> bool:
    """Post-fetch worker filters. ``eb_no`` is a case-insensitive substring of the
    employee code, the worker name, or the raw eb_id — the FE search box sends
    whichever of those the user typed."""
    if shift and row["shift"] != shift:
        return False
    if eb_no:
        needle = eb_no.lower()
        haystack = " ".join(
            str(row.get(k) or "") for k in ("eb_no", "worker_name", "eb_id")
        ).lower()
        if needle not in haystack:
            return False
    return True


def build_worker_summary(base_rows) -> list:
    """Per winder over the whole range, worst average efficiency FIRST.

    ``days_attended`` counts DISTINCT dates, not rows: a winder on two spells of
    one day attended one day. ``avg_eff`` is the shed average over their rows
    (zeros/nulls excluded), so a worker whose qualities have no target at all
    lands with avg_eff None and sorts last rather than at the top of the
    low-producer list.
    """
    acc: dict = {}
    for r in base_rows:
        e = acc.get(r["eb_id"])
        if e is None:
            e = acc[r["eb_id"]] = {
                "eb_id": r["eb_id"],
                "eb_no": r["eb_no"],
                "worker_name": r["worker_name"],
                "days": set(),
                "prod_kg": 0.0,
                "winders": [],
                "eff": {b: [] for b in SHIFT_BUCKETS + (OFF_SHIFT,)},
                "all_eff": [],
            }
        e["days"].add(r["tran_date"])
        e["prod_kg"] += r["prod_kg"]
        e["winders"].append(r["no_of_winders"])
        e["eff"][r["shift"]].append(r["eff_pct"])
        e["all_eff"].append(r["eff_pct"])

    rows = [
        {
            "eb_id": e["eb_id"],
            "eb_no": e["eb_no"],
            "worker_name": e["worker_name"],
            "days_attended": len(e["days"]),
            "avg_winders": round(sum(e["winders"]) / len(e["winders"]), 3),
            "total_prod_kg": round(e["prod_kg"], 3),
            "eff_a": _avg(e["eff"]["A"]),
            "eff_b": _avg(e["eff"]["B"]),
            "eff_c": _avg(e["eff"]["C"]),
            "avg_eff": _avg(e["all_eff"]),
        }
        for e in acc.values()
    ]
    rows.sort(key=lambda r: (r["avg_eff"] is None, r["avg_eff"] or 0.0, r["eb_no"] or ""))
    return rows


def build_day_summary(base_rows) -> list:
    """Per date: production, average efficiency and winders run, per shift.

    ``avg_eff_total`` is count-weighted — the shed average over the day's rows,
    NOT the mean of the three shift averages, which would over-weight a shift
    that ran one winder. The ``*_total`` columns include rows whose spell does
    not resolve to A/B/C, so no production disappears from the day's total.
    """
    acc: dict = {}
    for r in base_rows:
        e = acc.get(r["tran_date"])
        if e is None:
            e = acc[r["tran_date"]] = {
                "prod": {b: 0.0 for b in SHIFT_BUCKETS},
                "winders": {b: 0.0 for b in SHIFT_BUCKETS},
                "eff": {b: [] for b in SHIFT_BUCKETS},
                "prod_total": 0.0,
                "winders_total": 0.0,
                "eff_total": [],
            }
        if r["shift"] in SHIFT_BUCKETS:
            e["prod"][r["shift"]] += r["prod_kg"]
            e["winders"][r["shift"]] += r["no_of_winders"]
            e["eff"][r["shift"]].append(r["eff_pct"])
        e["prod_total"] += r["prod_kg"]
        e["winders_total"] += r["no_of_winders"]
        e["eff_total"].append(r["eff_pct"])

    rows = []
    for d in sorted(acc):
        e = acc[d]
        row = {"date": d}
        for b in SHIFT_BUCKETS:
            key = b.lower()
            row[f"act_prod_{key}"] = round(e["prod"][b], 3)
            row[f"winders_run_{key}"] = round(e["winders"][b], 3)
            row[f"avg_eff_{key}"] = _avg(e["eff"][b])
            row[f"kg_per_winder_{key}"] = (
                round(e["prod"][b] / e["winders"][b], 2) if e["winders"][b] else 0.0
            )
        row["act_prod_total"] = round(e["prod_total"], 3)
        row["winders_run_total"] = round(e["winders_total"], 3)
        row["avg_eff_total"] = _avg(e["eff_total"])
        row["kg_per_winder_total"] = (
            round(e["prod_total"] / e["winders_total"], 2) if e["winders_total"] else 0.0
        )
        rows.append(row)
    return rows


def build_notes(base_rows, no_target_items, dup_att_keys) -> list:
    """What the numbers do NOT say, stated on every response."""
    total = len(base_rows)
    default8 = sum(1 for r in base_rows if r["att_source"] == ATT_SOURCE_DEFAULT)
    zero_hours = sum(
        1 for r in base_rows if r["att_source"] == ATT_SOURCE_REAL and r["att_hrs"] <= 0
    )
    off_shift_kg = round(
        sum(r["prod_kg"] for r in base_rows if r["shift"] == OFF_SHIFT), 3
    )
    notes = [
        f"{default8} of {total} row(s) found NO matching attendance row "
        "(eb_id + date + spell_id) and default to their spell's configured "
        f"full-shift hours: att_source='{ATT_SOURCE_DEFAULT}'. Their efficiency is "
        "measured against a full shift's target whether or not a full shift was "
        "worked.",
        "Efficiency = prod_kg / (target_kg_8h / 8 * att_hrs) * 100. Attendance hours "
        "are working_hours - idle_hours from active, non-rejected daily_attendance "
        "rows; no designation gate is applied (mill winding designations share no "
        "prefix, so any gate would drop real winders).",
        "Targets resolve from jute_prod_winding_target_map as of date_to (latest "
        "effective row on or before it, 'target' preferred over 'standard'). A "
        "target changed inside the range is applied to the WHOLE range.",
    ]
    if no_target_items:
        notes.append(
            f"{no_target_items} quality(ies) in this range have no target row "
            "configured; their rows carry std_prod 0 and a blank efficiency, and are "
            "excluded from every average."
        )
    if zero_hours:
        notes.append(
            f"{zero_hours} row(s) have an attendance row whose net hours are zero or "
            "negative — efficiency is blank for them rather than fabricated."
        )
    if dup_att_keys:
        notes.append(
            f"{dup_att_keys} attendance key(s) (date + spell + winder) carry more than "
            "one row; their hours are summed, which understates efficiency if the rows "
            "are duplicates."
        )
    if off_shift_kg:
        notes.append(
            f"{off_shift_kg} KG sits on spells that do not resolve to shift A, B or C "
            f"(shift '{OFF_SHIFT}'): counted in the *_total columns, absent from the "
            "per-shift ones."
        )
    return notes


# =============================================================================
# Endpoint
# =============================================================================


def _fetch_attendance(db: Session, branch_id: int, d_from, d_to, recon_rows) -> tuple:
    """(index of (date, spell_id, eb_id) -> att_hrs, count of duplicated keys).

    Restricted to the winders that actually produced, so a whole branch's
    attendance is never dragged into memory for a report about a few dozen
    people. No producers means no query at all — an empty expanding IN list is
    not valid SQL.
    """
    eb_ids = sorted({m["eb_id"] for m in recon_rows if m.get("eb_id") is not None})
    if not eb_ids:
        return {}, 0
    rows = db.execute(
        get_winding_attendance_hours_query(),
        {
            "branch_id": branch_id,
            "date_from": d_from,
            "date_to": d_to,
            "eb_ids": eb_ids,
        },
    ).fetchall()
    index, dupes = {}, 0
    for r in rows:
        m = dict(r._mapping)
        index[(_day_key(m.get("tran_date")), m.get("spell_id"), m.get("eb_id"))] = _f(
            m.get("att_hrs")
        )
        if _f(m.get("att_rows")) > 1:
            dupes += 1
    return index, dupes


def fetch_winding_lowprod_rows(
    db: Session, co_id: int, branch_id: int, d_from, d_to, item_id=None
) -> tuple:
    """(unfiltered base rows, duplicated attendance keys) for a branch and range.

    Shared by the endpoint below and the Low Performer Follow-up. The caller has
    already verified the co/branch pair and capped the range.
    """
    # One round trip for the whole range: the same reconciliation SQL as the
    # single-date query, ranged (a per-day loop cost ~65 ms per day on the
    # remote DB, about 2 s for a 31-day range).
    recon_rows = [
        dict(r._mapping)
        for r in db.execute(
            get_winding_reconciliation_range_query(),
            {
                "date_from": d_from,
                "date_to": d_to,
                "spell_id": None,
                "branch_id": branch_id,
                "item_id": item_id,
            },
        ).fetchall()
    ]

    att_index, dup_att_keys = _fetch_attendance(db, branch_id, d_from, d_to, recon_rows)
    targets = {
        r.item_id: _f(r.value)
        for r in db.execute(
            resolve_winding_targets_query(), {"co_id": co_id, "on_date": d_to}
        ).fetchall()
    }
    spell_hours_map = {
        r.spell_id: r.working_hours
        for r in db.execute(get_spell_hours_query()).fetchall()
    }
    return build_base_rows(recon_rows, att_index, targets, spell_hours_map), dup_att_keys


@router.get("/winding_low_producer")
def winding_low_producer(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Winder-by-winder efficiency over a date range, worst first.

    Optional filters: ``shift`` (A/B/C/G), ``eb_no`` (substring of employee code,
    name or eb_id), ``item_id`` (one yarn quality — pushed down into the
    reconciliation query).
    """
    co_id, branch_id, d_from, d_to = _scope(request)
    shift, eb_no, item_id = _optional_filters(request)
    try:
        _verify_pair(db, co_id, branch_id)

        all_rows, dup_att_keys = fetch_winding_lowprod_rows(
            db, co_id, branch_id, d_from, d_to, item_id
        )
        base_rows = [r for r in all_rows if keep_row(r, shift, eb_no)]
        no_target_items = len(
            {r["item_id"] for r in base_rows if not r["target_kg_8h"]}
        )

        return {
            "data": {
                "base_rows": base_rows,
                "worker_summary": build_worker_summary(base_rows),
                "day_summary": build_day_summary(base_rows),
                "meta": {
                    "co_id": co_id,
                    "branch_id": branch_id,
                    "date_from": str(d_from),
                    "date_to": str(d_to),
                    "filtered_avg_eff": _avg([r["eff_pct"] for r in base_rows]),
                    "overall_avg_eff": _avg([r["eff_pct"] for r in all_rows]),
                    "notes": build_notes(base_rows, no_target_items, dup_att_keys),
                },
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

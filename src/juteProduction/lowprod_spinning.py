"""Spinning Low Producer report (MIS port, spec section 2).

GET /api/juteProductionReports/spinning_low_producer

One SQL round trip (lowprod_spinning_query.get_spinning_lowprod_base_query)
returns the worker-grain rows already carrying whrs / std_prod / eff_pct; the
three summaries are pure Python builders over those rows so they can be tested
without a database.

Averages follow the "shed average" rule: zero / missing efficiency is EXCLUDED
from every average (a frame with no standard, or an unstamped yarn, would
otherwise drag a worker to the bottom of a low-producer list for a data gap).
``meta`` carries both that filtered average and the unfiltered one.
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.lowprod_spinning_query import get_spinning_lowprod_base_query
from src.juteProduction.spinning_query import SPINNER_DESIG_PREFIX

logger = logging.getLogger(__name__)

spinning_lowprod_router = APIRouter()

MAX_DAYS = 62
VALID_SHIFTS = ("A", "B", "C", "G")
_TRUE = ("1", "true", "yes", "y", "on")
_NUMERIC_COLS = ("net_wt", "whrs", "spell_hours", "p100prod", "std_prod",
                 "no_of_frame", "eff_pct")


# ---------------------------------------------------------------------------
# Parameter validation (reports.py / winding_reports.py helpers)
# ---------------------------------------------------------------------------


def _require_int(request: Request, key: str) -> int:
    raw = request.query_params.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


def _scope(request: Request) -> tuple:
    """co_id / branch_id / date range, all four required. Both ids are needed:
    daily_doff_tbl has no co_id (scope rides the machine spine) while the
    standards are company-keyed, so the pair is verified against branch_mst."""
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
        raise HTTPException(status_code=400, detail="Invalid date — use YYYY-MM-DD")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="date_to is before date_from")
    if (d_to - d_from).days >= MAX_DAYS:
        raise HTTPException(
            status_code=400, detail=f"Date range too large — at most {MAX_DAYS} days"
        )
    return co_id, branch_id, d_from, d_to


def _optional_int(request: Request, key: str):
    raw = request.query_params.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {key}")


def _like(request: Request, key: str):
    raw = (request.query_params.get(key) or "").strip()
    return f"%{raw}%" if raw else None


def _shift(request: Request):
    raw = (request.query_params.get("shift") or "").strip().upper()
    if not raw:
        return None
    if raw not in VALID_SHIFTS:
        raise HTTPException(status_code=400, detail="shift must be one of A, B, C, G")
    return raw


def _flag(request: Request, key: str) -> bool:
    return (request.query_params.get(key) or "").strip().lower() in _TRUE


def _verify_branch(db: Session, co_id: int, branch_id: int) -> None:
    ok = db.execute(
        text("SELECT 1 FROM branch_mst WHERE branch_id = :branch_id AND co_id = :co_id"),
        {"branch_id": branch_id, "co_id": co_id},
    ).first()
    if not ok:
        raise HTTPException(status_code=400, detail="branch_id does not belong to co_id")


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------


def _f(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _avg(values):
    """Mean of the values given, 2dp; None when there is nothing to average
    (same semantics as the weaving/winding siblings — a missing efficiency is
    not a zero one). Callers pass only the non-zero effs."""
    return round(sum(values) / len(values), 2) if values else None


def _nonzero_eff(rows) -> list:
    return [_f(r.get("eff_pct")) for r in rows if _f(r.get("eff_pct")) > 0]


def build_worker_summary(rows) -> list:
    """One row per worker (eb_id; every contract row folds into a single
    'Contract' bucket), sorted worst average efficiency first.

    ``avg_frames_run`` is the fractional frame count (whrs / spell hours)
    summed over the range and divided by the days the worker appears."""
    grouped: dict = {}
    for r in rows:
        eb_id = r.get("eb_id")
        key = eb_id if eb_id is not None else "__contract__"
        entry = grouped.setdefault(
            key,
            {
                "eb_id": eb_id,
                "eb_label": r.get("eb_label") or "Contract",
                "worker_name": r.get("worker_name") or "Contract",
                "days": set(),
                "frames": 0.0,
                "eff": {"A": [], "B": [], "C": [], "G": []},
                "all_eff": [],
            },
        )
        entry["days"].add(r.get("doff_date"))
        entry["frames"] += _f(r.get("no_of_frame"))
        eff = _f(r.get("eff_pct"))
        if eff > 0:
            entry["eff"].setdefault(r.get("shift") or "G", []).append(eff)
            entry["all_eff"].append(eff)

    out = []
    for entry in grouped.values():
        days = len(entry["days"]) or 1
        out.append(
            {
                "eb_id": entry["eb_id"],
                "eb_label": entry["eb_label"],
                "worker_name": entry["worker_name"],
                "days_attended": len(entry["days"]),
                "avg_frames_run": round(entry["frames"] / days, 2),
                "eff_a": _avg(entry["eff"]["A"]),
                "eff_b": _avg(entry["eff"]["B"]),
                "eff_c": _avg(entry["eff"]["C"]),
                "avg_eff": _avg(entry["all_eff"]),
            }
        )
    # None (no standard at all) sorts LAST — a blank is not a zero producer.
    out.sort(
        key=lambda x: (x["avg_eff"] is None, x["avg_eff"] or 0.0, x["worker_name"] or "")
    )
    return out


def build_day_summary(rows) -> list:
    """Per date: shift-wise net weight, average efficiency and frames run.

    ``frames_run_*`` counts DISTINCT machines (a frame worked by two spells of
    the same shift is one frame), the total counts distinct machines that day.
    'G' rows (spell codes outside A/B/C) land in the totals only."""
    grouped: dict = {}
    for r in rows:
        d = r.get("doff_date")
        entry = grouped.setdefault(
            d,
            {
                "date": d,
                "net": {"A": 0.0, "B": 0.0, "C": 0.0, "G": 0.0},
                "eff": {"A": [], "B": [], "C": [], "G": []},
                "frames": {"A": set(), "B": set(), "C": set(), "G": set()},
                "all_eff": [],
                "all_frames": set(),
            },
        )
        shift = r.get("shift") or "G"
        entry["net"][shift] = entry["net"].get(shift, 0.0) + _f(r.get("net_wt"))
        entry["frames"].setdefault(shift, set()).add(r.get("machine_id"))
        entry["all_frames"].add(r.get("machine_id"))
        eff = _f(r.get("eff_pct"))
        if eff > 0:
            entry["eff"].setdefault(shift, []).append(eff)
            entry["all_eff"].append(eff)

    out = []
    for entry in sorted(grouped.values(), key=lambda e: str(e["date"])):
        out.append(
            {
                "date": entry["date"],
                "net_wt_a": round(entry["net"]["A"], 2),
                "net_wt_b": round(entry["net"]["B"], 2),
                "net_wt_c": round(entry["net"]["C"], 2),
                "net_wt_total": round(sum(entry["net"].values()), 2),
                "avg_eff_a": _avg(entry["eff"]["A"]),
                "avg_eff_b": _avg(entry["eff"]["B"]),
                "avg_eff_c": _avg(entry["eff"]["C"]),
                "avg_eff_total": _avg(entry["all_eff"]),
                "frames_run_a": len(entry["frames"]["A"]),
                "frames_run_b": len(entry["frames"]["B"]),
                "frames_run_c": len(entry["frames"]["C"]),
                "frames_run_total": len(entry["all_frames"]),
            }
        )
    return out


def build_meta(co_id, branch_id, d_from, d_to, rows, notes) -> dict:
    effs = [_f(r.get("eff_pct")) for r in rows]
    nonzero = [e for e in effs if e > 0]
    return {
        "co_id": co_id,
        "branch_id": branch_id,
        "date_from": d_from.isoformat() if hasattr(d_from, "isoformat") else d_from,
        "date_to": d_to.isoformat() if hasattr(d_to, "isoformat") else d_to,
        "filtered_avg_eff": _avg(nonzero),
        "overall_avg_eff": _avg(effs),
        "rows": len(rows),
        "notes": notes,
    }


def shape_rows(raw_rows) -> list:
    """DB rows -> JSON-safe base_rows carrying the contract's column names."""
    out = []
    for r in raw_rows:
        m = dict(r._mapping) if hasattr(r, "_mapping") else dict(r)
        for k in _NUMERIC_COLS:
            m[k] = round(_f(m.get(k)), 3 if k in ("std_prod", "no_of_frame") else 2)
        d = m.get("doff_date")
        m["doff_date"] = d.isoformat() if hasattr(d, "isoformat") else d
        eb_no = (m.get("eb_no") or "").strip() if m.get("eb_no") else ""
        if m.get("eb_id") is None:
            m["eb_label"] = "Contract"
        else:
            m["eb_label"] = eb_no or str(m["eb_id"])
        m["worker_name"] = m.get("worker_name") or (
            "Contract" if m.get("eb_id") is None else m["eb_label"]
        )
        m["att_approved"] = int(m.get("att_approved") or 0)
        out.append(m)
    return out


def apply_full_shift_filter(rows) -> list:
    """The legacy "whatsapp" view: only full-shift rows backed by a real
    attendance row, worst efficiency first. "Full shift" is the row's own
    spell's configured hours (spell_mst.working_hours), not a fixed 8."""
    kept = [
        r
        for r in rows
        if r.get("attendance_type") == "R"
        and _f(r.get("spell_hours")) > 0
        and abs(_f(r.get("whrs")) - _f(r.get("spell_hours"))) < 0.01
    ]
    kept.sort(key=lambda r: _f(r.get("eff_pct")))
    return kept


# ---------------------------------------------------------------------------
# Shared fetch (also used by the Low Performer Follow-up)
# ---------------------------------------------------------------------------


def fetch_spinning_lowprod_rows(
    db: Session,
    co_id: int,
    branch_id: int,
    d_from,
    d_to,
    shift=None,
    item_id=None,
    frame_like=None,
    eb_no_like=None,
) -> list:
    """Shaped worker-grain base rows for a branch and range (one round trip).

    The caller has already verified the co/branch pair and capped the range."""
    raw = db.execute(
        get_spinning_lowprod_base_query(),
        {
            "co_id": co_id,
            "branch_id": branch_id,
            "d_from": d_from,
            "d_to": d_to,
            "desig_prefix": SPINNER_DESIG_PREFIX,
            "shift": shift,
            "item_id": item_id,
            "frame_like": frame_like,
            "eb_no_like": eb_no_like,
        },
    ).fetchall()
    return shape_rows(raw)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@spinning_lowprod_router.get("/spinning_low_producer")
def spinning_low_producer(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id, branch_id, d_from, d_to = _scope(request)
    shift = _shift(request)
    item_id = _optional_int(request, "item_id")
    frame_like = _like(request, "frame")
    eb_no_like = _like(request, "eb_no")
    full_shift = _flag(request, "full_shift_regular_only")
    try:
        _verify_branch(db, co_id, branch_id)
        rows = fetch_spinning_lowprod_rows(
            db, co_id, branch_id, d_from, d_to, shift, item_id, frame_like, eb_no_like
        )

        notes = []
        contract = sum(1 for r in rows if r.get("attendance_type") != "R")
        if contract:
            notes.append(
                f"{contract} of {len(rows)} rows have no spinner attendance row "
                "(contract): hours defaulted to the spell's full hours."
            )
        over = sum(1 for r in rows if _f(r.get("whrs")) > _f(r.get("spell_hours")) + 0.01)
        if over:
            notes.append(
                f"{over} rows carry more attended hours than the spell is long "
                "(attendance recorded for the whole day on a part-day spell), so "
                "their standard is scaled above the frame's full-spell production."
            )
        no_std = sum(1 for r in rows if _f(r.get("std_prod")) <= 0)
        if no_std:
            notes.append(
                f"{no_std} rows have no standard (unmapped yarn, or no speed / "
                "spindles / TPI in the target map) — excluded from every average."
            )
        if full_shift:
            rows = apply_full_shift_filter(rows)
            notes.append(
                "Full-shift regular only: kept attendance-backed rows whose worked "
                "hours equal their spell's configured hours, sorted worst "
                f"efficiency first ({len(rows)} rows)."
            )

        return {
            "data": {
                "base_rows": rows,
                "worker_summary": build_worker_summary(rows),
                "day_summary": build_day_summary(rows),
                "meta": build_meta(co_id, branch_id, d_from, d_to, rows, notes),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching spinning low producer report: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to build the spinning low producer report"
        )

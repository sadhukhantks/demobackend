"""Selection vs Jute Issue report (/api/juteProductionReports/selection_vs_issue) —
../vowerp3ui/docs/claude/modules/jute-production/selection-trolley-weight-spec.md §3.3.

Compares, quality-wise, the jute ISSUED to a branch (jute_issue under the settled
attribution rule, D3: MR-sourced rows only, status_id <> 4, quality =
jute_mr_li.actual_item_id) with the jute SELECTED on the floor
(jute_prod_selection_trolly_wt, Σ net_wt and trolley count). Both sides share the
raw-jute item_mst identity, so the merge key is (date, item_id).

Sections:
* by_quality    — one row per quality over the range, diff_kg / diff_pct
* by_day        — one row per (date, quality)
* by_gate_entry — selection rows that name a gate entry (jute_mr_id), against that
                  MR's issued kg of the same quality in the range
* totals

Shape follows the sibling report routers (bale_reports.py): sync ``def`` handlers,
``get_tenant_db`` + ``get_current_user_with_refresh``, ``{"data": ...}`` responses,
parameters validated before the try/except so a bad input is a 400 and never a 500,
and DB-free pure builders (``build_selection_vs_issue``, ``build_by_gate_entry``) that
the tests hit directly with stub rows.
"""

from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.bale_reports_query import get_branch_name_query
from src.juteProduction.selection_query import (
    get_issue_by_gate_entry_query,
    get_issue_daily_by_quality_query,
    get_selection_by_gate_entry_query,
    get_selection_daily_by_quality_query,
)

router = APIRouter()
selection_reports_router = router

BAD_BRANCH_MSG = "branch_id must be an active branch of this company"

# Per-(quality, day) aggregates are cheap, but a range wider than a year is never a
# selection-vs-issue question — the FE quick buttons are Today / MTD.
MAX_RANGE_DAYS = 366


# =============================================================================
# Small helpers
# =============================================================================


def _f(v, default: float = 0.0) -> float:
    return default if v is None else float(v)


def _i(v, default: int = 0) -> int:
    return default if v is None else int(v)


def _day_key(v) -> str:
    """Date part of a date/datetime/string, as the index key both sides share."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def _iso_date(v) -> Optional[str]:
    return _day_key(v) or None


def _diff_pct(diff_kg: float, issue_kg: float) -> Optional[float]:
    """diff_kg / issue_kg × 100, 2 dp; None when nothing was issued."""
    if not issue_kg:
        return None
    return round(diff_kg / issue_kg * 100.0, 2)


def _name_key(name) -> Tuple[int, str]:
    return (name is None, str(name or "").lower())


# =============================================================================
# Pure builders (no DB) — tested directly
# =============================================================================


def build_selection_vs_issue(issue_rows: List[dict], selection_rows: List[dict]) -> dict:
    """Merge the two daily-by-quality aggregates on (tran_date, item_id).

    issue_rows:     {tran_date, item_id, item_code, item_name, issue_kg}
    selection_rows: {tran_date, item_id, item_code, item_name, selection_kg, trolleys}

    A quality present on only one side still appears with the other side 0.
    diff_kg = selection_kg − issue_kg; diff_pct = diff_kg / issue_kg × 100 (2 dp,
    None when issue_kg = 0). Weights rounded to 3. by_quality is ordered by item_name,
    by_day by (tran_date, item_name).
    """
    days: Dict[Tuple[str, int], dict] = {}
    labels: Dict[int, dict] = {}

    def _cell(day: str, item_id: int, code, name) -> dict:
        key = (day, item_id)
        c = days.get(key)
        if c is None:
            c = days[key] = {
                "tran_date": day,
                "item_id": item_id,
                "issue_kg": 0.0,
                "selection_kg": 0.0,
                "trolleys": 0,
            }
        lab = labels.setdefault(item_id, {"item_code": None, "item_name": None})
        if lab["item_code"] is None and code is not None:
            lab["item_code"] = code
        if lab["item_name"] is None and name is not None:
            lab["item_name"] = name
        return c

    for r in issue_rows:
        if r.get("item_id") is None:
            continue
        c = _cell(_day_key(r.get("tran_date")), int(r["item_id"]), r.get("item_code"), r.get("item_name"))
        c["issue_kg"] += _f(r.get("issue_kg"))
    for r in selection_rows:
        if r.get("item_id") is None:
            continue
        c = _cell(_day_key(r.get("tran_date")), int(r["item_id"]), r.get("item_code"), r.get("item_name"))
        c["selection_kg"] += _f(r.get("selection_kg"))
        c["trolleys"] += _i(r.get("trolleys"))

    by_quality_acc: Dict[int, dict] = {}
    by_day: List[dict] = []
    for (day, item_id), c in days.items():
        lab = labels[item_id]
        diff = c["selection_kg"] - c["issue_kg"]
        by_day.append(
            {
                "tran_date": day,
                "item_id": item_id,
                "item_name": lab["item_name"],
                "issue_kg": round(c["issue_kg"], 3),
                "selection_kg": round(c["selection_kg"], 3),
                "trolleys": c["trolleys"],
                "diff_kg": round(diff, 3),
            }
        )
        q = by_quality_acc.get(item_id)
        if q is None:
            q = by_quality_acc[item_id] = {
                "item_id": item_id,
                "item_code": lab["item_code"],
                "item_name": lab["item_name"],
                "issue_kg": 0.0,
                "selection_kg": 0.0,
                "trolleys": 0,
            }
        q["issue_kg"] += c["issue_kg"]
        q["selection_kg"] += c["selection_kg"]
        q["trolleys"] += c["trolleys"]

    by_day.sort(key=lambda r: (r["tran_date"], _name_key(r["item_name"]), r["item_id"]))

    by_quality: List[dict] = []
    totals = {"issue_kg": 0.0, "selection_kg": 0.0, "trolleys": 0}
    for q in sorted(by_quality_acc.values(), key=lambda r: (_name_key(r["item_name"]), r["item_id"])):
        diff = q["selection_kg"] - q["issue_kg"]
        by_quality.append(
            {
                "item_id": q["item_id"],
                "item_code": q["item_code"],
                "item_name": q["item_name"],
                "issue_kg": round(q["issue_kg"], 3),
                "selection_kg": round(q["selection_kg"], 3),
                "trolleys": q["trolleys"],
                "diff_kg": round(diff, 3),
                "diff_pct": _diff_pct(diff, q["issue_kg"]),
            }
        )
        totals["issue_kg"] += q["issue_kg"]
        totals["selection_kg"] += q["selection_kg"]
        totals["trolleys"] += q["trolleys"]

    total_diff = totals["selection_kg"] - totals["issue_kg"]
    totals_out = {
        "issue_kg": round(totals["issue_kg"], 3),
        "selection_kg": round(totals["selection_kg"], 3),
        "trolleys": totals["trolleys"],
        "diff_kg": round(total_diff, 3),
        "diff_pct": _diff_pct(total_diff, totals["issue_kg"]),
    }
    return {"by_quality": by_quality, "by_day": by_day, "totals": totals_out}


def build_by_gate_entry(selection_rows: List[dict], issue_rows: List[dict]) -> List[dict]:
    """by_gate_entry: every selection (jute_mr_id, item_id) group, with that MR's
    issued kg of the same quality in the range and diff_kg. Selection-driven — an MR
    with issues but no selection rows is NOT listed (the day/quality sections cover
    it). Ordered by gate_entry_no desc, then item_name.

    selection_rows: {jute_mr_id, gate_entry_no, gate_entry_date, item_id, item_name,
                     selection_kg, trolleys}
    issue_rows:     {jute_mr_id, item_id, issue_kg}
    """
    issue_index: Dict[Tuple[int, int], float] = {}
    for r in issue_rows:
        if r.get("jute_mr_id") is None or r.get("item_id") is None:
            continue
        key = (int(r["jute_mr_id"]), int(r["item_id"]))
        issue_index[key] = issue_index.get(key, 0.0) + _f(r.get("issue_kg"))

    out: List[dict] = []
    for r in selection_rows:
        if r.get("jute_mr_id") is None or r.get("item_id") is None:
            continue
        mr_id, item_id = int(r["jute_mr_id"]), int(r["item_id"])
        sel = _f(r.get("selection_kg"))
        iss = issue_index.get((mr_id, item_id), 0.0)
        out.append(
            {
                "jute_mr_id": mr_id,
                "gate_entry_no": None if r.get("gate_entry_no") is None else int(r["gate_entry_no"]),
                "gate_entry_date": _iso_date(r.get("gate_entry_date")),
                "item_id": item_id,
                "item_name": r.get("item_name"),
                "issue_kg": round(iss, 3),
                "selection_kg": round(sel, 3),
                "trolleys": _i(r.get("trolleys")),
                "diff_kg": round(sel - iss, 3),
            }
        )
    out.sort(
        key=lambda r: (
            r["gate_entry_no"] is None,
            -(r["gate_entry_no"] or 0),
            _name_key(r["item_name"]),
            r["item_id"],
        )
    )
    return out


# =============================================================================
# Parameter validation
# =============================================================================


def _require_int_param(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _require_date_param(request: Request, name: str) -> date:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name} format — expected YYYY-MM-DD")


def _date_range(request: Request) -> Tuple[date, date]:
    d_from = _require_date_param(request, "date_from")
    d_to = _require_date_param(request, "date_to")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="date_to is before date_from")
    if (d_to - d_from).days >= MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range too large — at most {MAX_RANGE_DAYS} days",
        )
    return d_from, d_to


def _rows(db: Session, query, binds: dict) -> List[dict]:
    return [dict(r._mapping) for r in db.execute(query, binds).fetchall()]


# =============================================================================
# Endpoint
# =============================================================================


@router.get("/selection_vs_issue")
def selection_vs_issue(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Selection vs Jute Issue — ?co_id&branch_id&date_from&date_to (all required)."""
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    d_from, d_to = _date_range(request)
    try:
        branch = db.execute(
            get_branch_name_query(), {"co_id": co_id, "branch_id": branch_id}
        ).fetchone()
        if not branch:
            raise HTTPException(status_code=400, detail=BAD_BRANCH_MSG)

        issue_binds = {"branch_id": branch_id, "date_from": d_from, "date_to": d_to}
        sel_binds = {"co_id": co_id, **issue_binds}
        issue_rows = _rows(db, get_issue_daily_by_quality_query(), issue_binds)
        selection_rows = _rows(db, get_selection_daily_by_quality_query(), sel_binds)
        sel_ge_rows = _rows(db, get_selection_by_gate_entry_query(), sel_binds)
        issue_ge_rows = _rows(db, get_issue_by_gate_entry_query(), issue_binds)

        report = build_selection_vs_issue(issue_rows, selection_rows)
        return {
            "data": {
                "date_from": str(d_from),
                "date_to": str(d_to),
                "branch_id": branch_id,
                "branch_name": dict(branch._mapping).get("branch_name"),
                **report,
                "by_gate_entry": build_by_gate_entry(sel_ge_rows, issue_ge_rows),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

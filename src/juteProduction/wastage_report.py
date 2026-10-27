"""Daily Wastage Report (/api/juteProductionReports/wastage_report).

Replaces the mill's "NEW WASTAGE FORMAT" Excel: one row per day of the month,
context columns (looms worked hessian/sacking, spinning frames run, weaving /
spinning / breaker-feed / jute-selection figures) and one column per waste
component, grouped with a kg total and a percentage per group, plus a monthly
totals row.

Everything is DERIVED — waste kg from the existing Waste Entry page
(jute_prod_waste_entry), denominators from production entries. Which
(waste item, department) lands in which group, and which denominator each
group's % divides by, is data in jute_waste_report_map: day_denom and
total_denom are separate because the mill's format divides daily rows and the
monthly total row by different bases for some groups (kept as-is by user
decision 2026-08-25). Waste with no mapping row shows in an OTHER bucket —
the mapping can never silently hide waste.

TKT-000183 (owner 2026-10-05):
  * WINDING denominator = reconciled winding production kg (day + month),
    shown as a winding_kg context column.
  * A mapping row naming a denominator this code does not know gives
    pct = None, never a 500 — mapping rows are user-editable and older
    servers read the same table.
  * Grand "Total Wastage": total_waste_kg sums every group INCLUDING OTHER,
    with total_pct_by_base = its % on each base (the page picks which one
    to show); denom_options carries the base labels.
  * unmapped lists the (item, dept) pairs that fell into OTHER for the month,
    heaviest first, so the page can say what still needs a mapping row.

Shape follows the sibling report routers: sync handlers, {"data": ...}
returns, pure builders free of DB access so tests exercise the arithmetic
directly.
"""

from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    FINISHING_QUALITY_TYPE_HESSIAN,
    FINISHING_QUALITY_TYPE_SACKING,
    SPINNING_MACHINE_TYPE_NAME,
    WASTE_REPORT_DENOM_BREAKER_FEED,
    WASTE_REPORT_DENOM_LABELS,
    WASTE_REPORT_DENOM_NONE,
    WASTE_REPORT_DENOM_SELECTION,
    WASTE_REPORT_DENOM_SPINNING,
    WASTE_REPORT_DENOM_WEAVING,
    WASTE_REPORT_DENOM_WINDING,
    WASTE_REPORT_OTHER_GROUP,
)
from src.juteProduction.gain_loss import parse_month
from src.juteProduction.gain_loss_query import verify_branch_co_query
from src.juteProduction.wastage_report_query import (
    get_breaker_feed_daily_kg_query,
    get_selection_daily_weight_query,
    get_spinning_daily_kg_query,
    get_waste_daily_sums_query,
    get_waste_report_map_query,
    get_weaving_daily_kg_query,
    get_winding_daily_kg_query,
)

router = APIRouter()

BAD_BRANCH_MSG = "branch_id must be an active branch of this company"

# Bases the grand Total Wastage % is computed on (every real denominator).
TOTAL_PCT_BASES = (
    WASTE_REPORT_DENOM_WEAVING,
    WASTE_REPORT_DENOM_SPINNING,
    WASTE_REPORT_DENOM_BREAKER_FEED,
    WASTE_REPORT_DENOM_SELECTION,
    WASTE_REPORT_DENOM_WINDING,
)


def denom_options() -> List[dict]:
    """[{code, label}] for the page's "Total % on" picker — every labelled
    denominator except NONE, in the constants' order."""
    return [
        {"code": code, "label": label}
        for code, label in WASTE_REPORT_DENOM_LABELS.items()
        if code != WASTE_REPORT_DENOM_NONE
    ]


def _f(v, default: float = 0.0) -> float:
    return default if v is None else float(v)


def month_days(month: date, today: Optional[date] = None) -> List[date]:
    """Every calendar day of the month, capped at today for the running month."""
    today = today or date.today()
    if month.month == 12:
        month_end = date(month.year, 12, 31)
    else:
        month_end = date(month.year, month.month + 1, 1) - timedelta(days=1)
    end = min(month_end, today)
    return [month + timedelta(days=i) for i in range((end - month).days + 1)] if end >= month else []


# =============================================================================
# Pure builders (no DB) — tested directly
# =============================================================================


def resolve_map(
    map_rows: List[dict],
) -> Tuple[Dict[Tuple[int, int], dict], Dict[int, dict], Dict[str, dict]]:
    """Split mapping rows into exact (item, dept) and item-level lookups, and
    collect group definitions. First row wins on duplicates (rows arrive
    ordered by display_order)."""
    exact: Dict[Tuple[int, int], dict] = {}
    by_item: Dict[int, dict] = {}
    groups: Dict[str, dict] = {}
    for m in map_rows:
        groups.setdefault(
            str(m["group_code"]),
            {
                "group_code": str(m["group_code"]),
                "group_name": m["group_name"],
                "day_denom": m["day_denom"],
                "total_denom": m["total_denom"],
                "display_order": int(m.get("display_order") or 0),
            },
        )
        if m.get("dept_id") is not None:
            exact.setdefault((int(m["item_id"]), int(m["dept_id"])), m)
        else:
            by_item.setdefault(int(m["item_id"]), m)
    return exact, by_item, groups


def build_wastage_report(
    days: List[date],
    waste_rows: List[dict],
    map_rows: List[dict],
    weaving_rows: List[dict],
    spinning_rows: List[dict],
    breaker_rows: List[dict],
    selection_rows: List[dict],
    winding_rows: Optional[List[dict]] = None,
) -> dict:
    exact, by_item, groups = resolve_map(map_rows)
    day_set = set(days)

    other_group = {
        "group_code": WASTE_REPORT_OTHER_GROUP,
        "group_name": "Other / Unmapped",
        "day_denom": WASTE_REPORT_DENOM_NONE,
        "total_denom": WASTE_REPORT_DENOM_NONE,
        "display_order": 10_000,
    }

    # ---- context/denominator figures per day ----
    weaving_kg: Dict[date, float] = {}
    hess_looms: Dict[date, set] = {}
    sack_looms: Dict[date, set] = {}
    for r in weaving_rows:
        d = r["tran_date"]
        kg = _f(r.get("production_kg"))
        weaving_kg[d] = weaving_kg.get(d, 0.0) + kg
        if kg > 0 and r.get("machine_id") is not None:
            qt = r.get("quality_type")
            if qt == FINISHING_QUALITY_TYPE_HESSIAN:
                hess_looms.setdefault(d, set()).add(int(r["machine_id"]))
            elif qt == FINISHING_QUALITY_TYPE_SACKING:
                sack_looms.setdefault(d, set()).add(int(r["machine_id"]))
    spinning_kg = {r["tran_date"]: _f(r.get("production_kg")) for r in spinning_rows}
    frame_runs = {r["tran_date"]: int(r.get("frame_runs") or 0) for r in spinning_rows}
    breaker_kg = {r["tran_date"]: _f(r.get("feed_kg")) for r in breaker_rows}
    selection_wt = {r["tran_date"]: _f(r.get("selection_wt")) for r in selection_rows}
    winding_kg = {r["tran_date"]: _f(r.get("production_kg")) for r in (winding_rows or [])}

    base_series = {
        WASTE_REPORT_DENOM_WEAVING: weaving_kg,
        WASTE_REPORT_DENOM_SPINNING: spinning_kg,
        WASTE_REPORT_DENOM_BREAKER_FEED: breaker_kg,
        WASTE_REPORT_DENOM_SELECTION: selection_wt,
        WASTE_REPORT_DENOM_WINDING: winding_kg,
    }

    def denoms_of(d: Optional[date]) -> Dict[str, Optional[float]]:
        """Denominator per code for one day, or month sums when d is None.
        Callers read it with .get(): an unknown code (a newer/hand-edited
        mapping row) must yield pct None, not a KeyError."""
        out: Dict[str, Optional[float]] = {
            code: (
                sum(series.get(x, 0.0) for x in days)
                if d is None
                else series.get(d, 0.0)
            )
            for code, series in base_series.items()
        }
        out[WASTE_REPORT_DENOM_NONE] = None
        return out

    # ---- classify waste rows into (group, component) cells ----
    # component key: "item_id:dept_id" — one column per department that really
    # has entries, even for item-level (dept NULL) mapping rows
    comp_meta: Dict[str, dict] = {}
    cells: Dict[date, Dict[str, float]] = {}
    group_day_totals: Dict[date, Dict[str, float]] = {}
    used_groups: Dict[str, dict] = {}
    # (item, dept) pairs no mapping row claims, summed over the month
    unmapped: Dict[Tuple[int, int], dict] = {}

    for w in waste_rows:
        d = w["tran_date"]
        item_id, dept_id = int(w["item_id"]), int(w["dept_id"])
        m = exact.get((item_id, dept_id)) or by_item.get(item_id)
        grp = groups[str(m["group_code"])] if m else other_group
        used_groups.setdefault(grp["group_code"], grp)
        if m is None and d in day_set:
            u = unmapped.setdefault(
                (item_id, dept_id),
                {
                    "item_id": item_id,
                    "item_name": w.get("item_name"),
                    "dept_id": dept_id,
                    "dept_name": w.get("dept_name"),
                    "waste_kg": 0.0,
                },
            )
            u["waste_kg"] += _f(w.get("waste_kg"))

        key = f"{item_id}:{dept_id}"
        if key not in comp_meta:
            label = (m or {}).get("col_label") or f"{w['item_name']} — {w['dept_name']}"
            comp_meta[key] = {
                "key": key,
                "label": label,
                "group_code": grp["group_code"],
                "order": (int(m.get("display_order") or 0) if m else 10_000),
            }
        kg = _f(w.get("waste_kg"))
        cells.setdefault(d, {})[key] = cells.get(d, {}).get(key, 0.0) + kg
        group_day_totals.setdefault(d, {})
        group_day_totals[d][grp["group_code"]] = (
            group_day_totals[d].get(grp["group_code"], 0.0) + kg
        )

    ordered_groups = sorted(
        used_groups.values(), key=lambda g: (g["display_order"], g["group_code"])
    )
    columns = [
        {
            **g,
            "components": sorted(
                (c for c in comp_meta.values() if c["group_code"] == g["group_code"]),
                key=lambda c: (c["order"], c["label"]),
            ),
        }
        for g in ordered_groups
    ]

    def pct(total: float, denom: Optional[float]) -> Optional[float]:
        if denom is None or denom == 0:
            return None
        return round(total / denom * 100.0, 2)

    def pct_by_base(total: float, dn: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
        return {code: pct(total, dn.get(code)) for code in TOTAL_PCT_BASES}

    day_rows = []
    for d in days:
        dn = denoms_of(d)
        gvals = {}
        for g in ordered_groups:
            total = group_day_totals.get(d, {}).get(g["group_code"], 0.0)
            gvals[g["group_code"]] = {
                "total": round(total, 3),
                "pct": pct(total, dn.get(g["day_denom"])),
            }
        # grand total: every group, OTHER included
        day_waste = sum(group_day_totals.get(d, {}).values())
        day_rows.append(
            {
                "date": str(d),
                "hessian_looms": len(hess_looms.get(d, ())),
                "sacking_looms": len(sack_looms.get(d, ())),
                "frames_run": frame_runs.get(d, 0),
                "weaving_kg": round(weaving_kg.get(d, 0.0), 3),
                "spinning_kg": round(spinning_kg.get(d, 0.0), 3),
                "breaker_feed_kg": round(breaker_kg.get(d, 0.0), 3),
                "selection_wt": round(selection_wt.get(d, 0.0), 3),
                "winding_kg": round(winding_kg.get(d, 0.0), 3),
                "cells": {k: round(v, 3) for k, v in cells.get(d, {}).items()},
                "groups": gvals,
                "total_waste_kg": round(day_waste, 3),
                "total_pct_by_base": pct_by_base(day_waste, dn),
            }
        )

    total_dn = denoms_of(None)
    total_cells: Dict[str, float] = {}
    for d in days:
        for k, v in cells.get(d, {}).items():
            total_cells[k] = total_cells.get(k, 0.0) + v
    total_groups = {}
    for g in ordered_groups:
        total = sum(
            group_day_totals.get(d, {}).get(g["group_code"], 0.0) for d in days
        )
        total_groups[g["group_code"]] = {
            "total": round(total, 3),
            "pct": pct(total, total_dn.get(g["total_denom"])),
        }
    month_waste = sum(sum(group_day_totals.get(d, {}).values()) for d in days)
    totals_row = {
        "hessian_looms": sum(len(hess_looms.get(d, ())) for d in days),
        "sacking_looms": sum(len(sack_looms.get(d, ())) for d in days),
        "frames_run": sum(frame_runs.get(d, 0) for d in days),
        "weaving_kg": round(total_dn[WASTE_REPORT_DENOM_WEAVING] or 0.0, 3),
        "spinning_kg": round(total_dn[WASTE_REPORT_DENOM_SPINNING] or 0.0, 3),
        "breaker_feed_kg": round(total_dn[WASTE_REPORT_DENOM_BREAKER_FEED] or 0.0, 3),
        "selection_wt": round(total_dn[WASTE_REPORT_DENOM_SELECTION] or 0.0, 3),
        "winding_kg": round(total_dn[WASTE_REPORT_DENOM_WINDING] or 0.0, 3),
        "cells": {k: round(v, 3) for k, v in total_cells.items()},
        "groups": total_groups,
        "total_waste_kg": round(month_waste, 3),
        "total_pct_by_base": pct_by_base(month_waste, total_dn),
    }

    unmapped_list = sorted(
        ({**u, "waste_kg": round(u["waste_kg"], 3)} for u in unmapped.values()),
        key=lambda u: (-u["waste_kg"], str(u["item_name"] or ""), str(u["dept_name"] or "")),
    )

    return {
        "columns": columns,
        "days": day_rows,
        "totals": totals_row,
        "denom_options": denom_options(),
        "unmapped": unmapped_list,
    }


# =============================================================================
# Endpoint
# =============================================================================


def _require_int_param(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


@router.get("/wastage_report")
def wastage_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    raw_month = request.query_params.get("month")
    if not raw_month:
        raise HTTPException(status_code=400, detail="month is required")
    month = parse_month(raw_month)
    try:
        row = db.execute(
            verify_branch_co_query(), {"co_id": co_id, "branch_id": branch_id}
        ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail=BAD_BRANCH_MSG)

        days = month_days(month)
        if not days:
            return {
                "data": {
                    "month": str(month),
                    "columns": [],
                    "days": [],
                    "totals": None,
                    "denom_options": denom_options(),
                    "unmapped": [],
                }
            }
        binds = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": days[0],
            "date_to": days[-1],
        }

        def rows(query, extra=None):
            return [
                dict(r._mapping)
                for r in db.execute(query, {**binds, **(extra or {})}).fetchall()
            ]

        report = build_wastage_report(
            days,
            rows(get_waste_daily_sums_query()),
            rows(get_waste_report_map_query(), {"co_id": co_id}),
            rows(get_weaving_daily_kg_query()),
            rows(
                get_spinning_daily_kg_query(),
                {"spinning_type": SPINNING_MACHINE_TYPE_NAME},
            ),
            rows(get_breaker_feed_daily_kg_query()),
            rows(get_selection_daily_weight_query()),
            rows(get_winding_daily_kg_query()),
        )
        return {"data": {"month": str(month), **report}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

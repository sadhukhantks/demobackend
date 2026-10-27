"""Target vs actual spreader roll stock at an "as at" datetime (TKT-000170).

Roll Stock Reports > Stock As At shows the actual roll stock at any date and
hour. This module adds the stock the plan *proposed* for that moment, so the
two can be compared per jute quality.

Target = the Jute Issue Planner's "target roll stock" for the mill day that
contains the as-at time (src/juteProcurement/issuePlanner.py, step 4):
requirement on the day today's issue matures x maturity days. The planner is
called directly, so both screens always show the same number for a day.

Mill day: the planner snapshots at 06:00 and aims the day's issue at the stock
to hold by the end of the day, so a mill day D runs (D 06:00, D+1 06:00]. The
end of spell C (06:00 next day) therefore still belongs to day D -- exactly
the point where actual should have reached target(D).

Actual = the same bin/group stock the page already shows
(get_bins_with_stock_query with the as-at cutoff), with blend rolls split into
their component qualities by ratio, because the planner targets component
qualities, not blend labels.

Jute waste qualities (item_grp_mst.item_type_id = 3) are planned and issued
but never spread into rolls, so a waste target can never be met by roll stock.
They are left out of the rows and totals (their target is reported separately
as excluded_waste_*) unless waste rolls actually exist at the as-at time.

The target is recomputed on every request from the batch plan, previous-day
production and maturity master; it is not a stored snapshot.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.juteProcurement.constants import (
    ISSUE_PLANNER_SNAPSHOT_HOUR,
    JUTE_WASTE_ITEM_TYPE_ID,
)
from src.juteProcurement.issuePlanner import build_issue_planner_report
from src.juteProduction.constants import ROLL_STOCK_TARGET_TOLERANCE_PCT
from src.juteProduction.query import get_bins_with_stock_query

STATUS_ON_TARGET = "on_target"
STATUS_SHORT = "short"
STATUS_EXCESS = "excess"
STATUS_NO_TARGET = "no_target"


class BranchCompanyMismatchError(ValueError):
    """Raised when branch_id does not belong to the requested co_id."""


def get_item_type_query():
    """item_grp_mst.item_type_id per item. Binds: :item_ids (expanding list)."""
    return text(
        """
        SELECT i.item_id, g.item_type_id
        FROM item_mst i
        JOIN item_grp_mst g ON g.item_grp_id = i.item_grp_id
        WHERE i.item_id IN :item_ids
        """
    ).bindparams(bindparam("item_ids", expanding=True))


def _round(value, digits=3):
    return round(float(value or 0), digits)


def target_mill_day(as_of_dt: datetime) -> date:
    """Mill day whose planner target applies at as_of_dt.

    A mill day D runs (D 06:00, D+1 06:00]: 06:00 itself closes the previous
    day (end of spell C), 06:01 .. next 06:00 belongs to D.
    """
    shifted = as_of_dt - timedelta(hours=ISSUE_PLANNER_SNAPSHOT_HOUR)
    if shifted.time() == datetime.min.time():
        return shifted.date() - timedelta(days=1)
    return shifted.date()


def split_stock_by_quality(stock_rows: Iterable) -> tuple[dict, dict]:
    """kg per component quality from bins-with-stock rows (blends split by ratio).

    Same ratio rule as /roll_stock_quality_summary's comp1/comp2 columns, so
    the actual total here equals that table's total weight.
    """
    kg_by_item: dict = defaultdict(float)
    names: dict = {}
    for r in stock_rows:
        m = r._mapping
        item_id = int(m["item_id"])
        kg = float(m["current_kg"] or 0)
        if m.get("item_name"):
            names.setdefault(item_id, m["item_name"])
        if m.get("item_id_2") is None:
            kg_by_item[item_id] += kg
            continue
        item_id_2 = int(m["item_id_2"])
        if m.get("item_name_2"):
            names.setdefault(item_id_2, m["item_name_2"])
        r1 = int(m["ratio_1"] or 1)
        r2 = int(m["ratio_2"] or 1)
        part1 = kg * r1 / (r1 + r2)
        kg_by_item[item_id] += part1
        kg_by_item[item_id_2] += kg - part1
    return dict(kg_by_item), names


def classify(target_mt: float, actual_mt: float, tolerance_pct: float) -> tuple[Optional[float], str]:
    """(difference %, status) of actual against target."""
    if target_mt <= 0:
        return None, STATUS_NO_TARGET
    pct = (actual_mt - target_mt) / target_mt * 100.0
    if pct < -tolerance_pct:
        return pct, STATUS_SHORT
    if pct > tolerance_pct:
        return pct, STATUS_EXCESS
    return pct, STATUS_ON_TARGET


def compare_target_vs_actual(
    planner_rows: list,
    actual_kg: dict,
    actual_names: dict,
    tolerance_pct: float = ROLL_STOCK_TARGET_TOLERANCE_PCT,
    waste_item_ids: Optional[set] = None,
) -> tuple[list, dict]:
    """Per-quality rows + totals.

    Qualities with neither target nor stock are dropped. Waste qualities with
    no roll stock are dropped too and their target summed into
    excluded_waste_target_mt.
    """
    waste_item_ids = waste_item_ids or set()
    by_item = {r["item_id"]: r for r in planner_rows}
    rows = []
    excluded_waste_mt = 0.0
    excluded_waste_count = 0
    for item_id in set(by_item) | set(actual_kg):
        p = by_item.get(item_id, {})
        target_mt = float(p.get("target_stock_mt") or 0)
        actual_mt = actual_kg.get(item_id, 0.0) / 1000.0
        if round(target_mt, 3) == 0 and round(actual_mt, 3) == 0:
            continue
        if item_id in waste_item_ids and round(actual_mt, 3) == 0:
            excluded_waste_mt += target_mt
            excluded_waste_count += 1
            continue
        pct, status = classify(target_mt, actual_mt, tolerance_pct)
        rows.append({
            "item_id": item_id,
            "quality_name": p.get("quality_name") or actual_names.get(item_id),
            "maturity_hours": p.get("maturity_hours"),
            "maturity_source": p.get("maturity_source"),
            "target_stock_date": p.get("target_stock_date"),
            "target_date_clamped": bool(p.get("target_date_clamped")),
            "target_mt": _round(target_mt),
            "actual_mt": _round(actual_mt),
            "diff_mt": _round(actual_mt - target_mt),
            "diff_pct": _round(pct, 1) if pct is not None else None,
            "status": status,
        })
    rows.sort(key=lambda r: (r["quality_name"] or "", r["item_id"]))

    total_target = sum(r["target_mt"] for r in rows)
    total_actual = sum(r["actual_mt"] for r in rows)
    total_pct, total_status = classify(total_target, total_actual, tolerance_pct)
    totals = {
        "target_mt": _round(total_target),
        "actual_mt": _round(total_actual),
        "diff_mt": _round(total_actual - total_target),
        "diff_pct": _round(total_pct, 1) if total_pct is not None else None,
        "status": total_status,
        "short_count": sum(1 for r in rows if r["status"] == STATUS_SHORT),
        "excess_count": sum(1 for r in rows if r["status"] == STATUS_EXCESS),
        "excluded_waste_target_mt": _round(excluded_waste_mt),
        "excluded_waste_count": excluded_waste_count,
    }
    return rows, totals


def build_roll_stock_target_vs_actual(
    db: Session,
    co_id: int,
    branch_id: int,
    as_of_dt: datetime,
    target_day: Optional[date] = None,
) -> dict:
    target_day = target_day or target_mill_day(as_of_dt)
    planner = build_issue_planner_report(
        db, branch_id, target_day, include_godown_cover=False
    )
    if int(planner["summary"]["co_id"]) != int(co_id):
        raise BranchCompanyMismatchError(
            f"branch_id {branch_id} does not belong to co_id {co_id}"
        )

    stock_rows = db.execute(
        get_bins_with_stock_query(),
        {"co_id": int(co_id), "branch_id": int(branch_id), "as_of_dt": as_of_dt},
    ).fetchall()
    actual_kg, actual_names = split_stock_by_quality(stock_rows)

    planner_rows = planner["required_issue"]
    item_ids = sorted({r["item_id"] for r in planner_rows} | set(actual_kg))
    waste_item_ids = set()
    if item_ids:
        waste_item_ids = {
            int(r.item_id)
            for r in db.execute(get_item_type_query(), {"item_ids": item_ids}).fetchall()
            if r.item_type_id is not None
            and int(r.item_type_id) == JUTE_WASTE_ITEM_TYPE_ID
        }

    rows, totals = compare_target_vs_actual(
        planner_rows, actual_kg, actual_names, waste_item_ids=waste_item_ids
    )
    day_start = datetime.combine(target_day, datetime.min.time()) + timedelta(
        hours=ISSUE_PLANNER_SNAPSHOT_HOUR
    )
    return {
        "as_of_dt": as_of_dt.isoformat(sep=" "),
        "target_day": target_day.isoformat(),
        "target_day_from": day_start.isoformat(sep=" "),
        "target_day_to": (day_start + timedelta(days=1)).isoformat(sep=" "),
        "plan_window_from": planner["summary"]["window_from"],
        "plan_window_to": planner["summary"]["window_to"],
        "tolerance_pct": ROLL_STOCK_TARGET_TOLERANCE_PCT,
        "rows": rows,
        "totals": totals,
    }

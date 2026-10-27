"""
Jute Issue Planner report builder (/juteReports/issue-planner).

Estimates the jute that should be issued from godown to batching on the
report date ("issue day" T):

1. Reference production per yarn = previous day's (T-1) spinning doff output.
2. Batch plan window = daily assigns for T .. T+4 decomposed into jute
   qualities via each plan's percentage lines -> daily requirement/quality.
3. Target roll stock per quality = requirement on the day the jute issued
   today matures (T + maturity days) x maturity days of pipeline stock.
4. Required issue = target - (roll stock at the 06:00 snapshot - today's
   planned consumption); also expressed in standard 148 kg bales.
5. Stock coverage = godown closing stock per quality / average daily
   requirement over the window -> position in days.

Ported from the MIS Streamlit planner (mis/pages/RequiredIssue.py) onto the
vowerp3 schema: item_mst qualities instead of jute_quality_price_master,
jute_batch_daily_assign instead of batch_plan_daily_implement, co_id/branch_id
scoping, and spreader blend (dual-quality) stock split by ratio.

Deliberate deviations from the MIS math:
- A target date past the plan window is clamped to the window end instead of
  silently producing a zero target (flagged target_date_clamped).
- The headline total sums only positive required-issue rows, so excess-stock
  qualities don't net it down.
- A quality missing from item_maturity_mst gets the module-wide 48 h default
  (MIS treated it as 0 h / zero target); maturity_source flags those rows.
- Half-day maturities round the target date half-up (int(x + 0.5)) — MIS used
  Python round(), whose round-half-to-even sends 60 h to T+2 but 84 h to T+4.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from src.juteProcurement.constants import (
    ISSUE_PLANNER_DEFAULT_REFERENCE_KG,
    ISSUE_PLANNER_SNAPSHOT_HOUR,
    ISSUE_PLANNER_WINDOW_DAYS,
    JUTE_BALE_WEIGHT_KG,
)
from src.juteProcurement.issuePlannerQueries import (
    get_branch_co_query,
    get_maturity_map_query,
    get_plan_window_query,
    get_reference_production_query,
    get_roll_stock_snapshot_query,
)
from src.juteProcurement.reportQueries import get_jute_stock_report_query
from src.juteProduction.constants import DEFAULT_MATURITY_HOURS


class UnknownBranchError(ValueError):
    """Raised when branch_id does not resolve to a company."""


def _round(value, digits=3):
    return round(float(value or 0), digits)


def _split_blend_kg(row) -> dict:
    """kg of one stock row per component quality (blends split by ratio)."""
    total = float(row.current_kg or 0)
    if row.item_id_2 is None:
        return {row.item_id: total}
    r1 = float(row.ratio_1 or 0)
    r2 = float(row.ratio_2 or 0)
    denom = r1 + r2
    if denom <= 0:
        return {row.item_id: total}
    return {
        row.item_id: total * r1 / denom,
        row.item_id_2: total * r2 / denom,
    }


def build_issue_planner_report(
    db: Session,
    branch_id: int,
    report_date: date,
    include_godown_cover: bool = True,
) -> dict:
    """Build the planner payload for one branch and issue day.

    include_godown_cover=False skips the godown stock query and returns an
    empty stock_coverage list -- for callers that only need the per-quality
    target roll stock (Roll Stock Reports target-vs-actual, TKT-000170).
    """
    production_date = report_date - timedelta(days=1)
    window_start = report_date
    window_end = report_date + timedelta(days=ISSUE_PLANNER_WINDOW_DAYS - 1)
    snapshot_dt = datetime.combine(report_date, time(hour=ISSUE_PLANNER_SNAPSHOT_HOUR))

    co_id = db.execute(
        get_branch_co_query(), {"branch_id": int(branch_id)}
    ).scalar()
    if co_id is None:
        raise UnknownBranchError(f"Unknown branch_id {branch_id}")

    prod_rows = db.execute(
        get_reference_production_query(),
        {"branch_id": int(branch_id), "production_date": production_date.isoformat()},
    ).fetchall()

    plan_rows = db.execute(
        get_plan_window_query(),
        {
            "branch_id": int(branch_id),
            "date_from": window_start.isoformat(),
            "date_to": window_end.isoformat(),
        },
    ).fetchall()

    maturity_rows = db.execute(
        get_maturity_map_query(), {"co_id": int(co_id)}
    ).fetchall()

    snapshot_rows = db.execute(
        get_roll_stock_snapshot_query(),
        {"co_id": int(co_id), "branch_id": int(branch_id), "cutoff_dt": snapshot_dt},
    ).fetchall()

    # Position rows only (mtd_rows=False): the godown cover lists what is in
    # stock, not every quality that moved earlier in the month.
    godown_rows = (
        db.execute(
            get_jute_stock_report_query(mtd_rows=False),
            {"branch_id": int(branch_id), "report_date": report_date.isoformat()},
        ).fetchall()
        if include_godown_cover
        else []
    )

    # --- 1. Reference production per yarn item (kg) --------------------------
    production_by_item = {r.item_id: float(r.production_kg or 0) for r in prod_rows}
    yarn_names = {r.item_id: r.yarn_name for r in prod_rows if r.yarn_name}

    # Planned yarns across the window (deduped); yarn_item_id can be NULL when
    # jute_yarn_mst lacks the item link — those can only use the default.
    planned_yarns = {}
    for r in plan_rows:
        planned_yarns[r.jute_yarn_id] = (r.yarn_item_id, r.yarn_name)
        if r.yarn_item_id and r.yarn_name:
            yarn_names.setdefault(r.yarn_item_id, r.yarn_name)

    def reference_for(yarn_item_id):
        actual = production_by_item.get(yarn_item_id, 0.0) if yarn_item_id else 0.0
        if actual > 0:
            return actual, "actual"
        return ISSUE_PLANNER_DEFAULT_REFERENCE_KG, "default"

    planned_item_ids = {v[0] for v in planned_yarns.values() if v[0]}
    yarn_id_by_item = {r.item_id: r.jute_yarn_id for r in prod_rows}
    reference_production = []
    for item_id, kg in sorted(
        production_by_item.items(), key=lambda kv: yarn_names.get(kv[0]) or ""
    ):
        planned = item_id in planned_item_ids
        # A planned yarn whose production nets to 0 kg falls back to the
        # default in the requirement math — show the same reference here.
        ref_kg, source = reference_for(item_id) if planned else (kg, "actual")
        reference_production.append({
            "item_id": item_id,
            "jute_yarn_id": yarn_id_by_item.get(item_id),
            "yarn_name": yarn_names.get(item_id),
            "production_kg": _round(kg),
            "reference_kg": _round(ref_kg),
            "reference_source": source,
            "planned": planned,
        })
    for yarn_id, (yarn_item_id, yarn_name) in sorted(
        planned_yarns.items(), key=lambda kv: kv[1][1] or ""
    ):
        if yarn_item_id in production_by_item:
            continue
        ref_kg, source = reference_for(yarn_item_id)
        reference_production.append({
            "item_id": yarn_item_id,
            "jute_yarn_id": yarn_id,
            "yarn_name": yarn_name,
            "production_kg": 0.0,
            "reference_kg": _round(ref_kg),
            "reference_source": source,
            "planned": True,
        })

    # --- 2. Plan window + per-quality daily requirement (kg) -----------------
    batch_plan = []
    seen_assigns = set()
    requirement = defaultdict(float)  # (assign_date, quality item_id) -> kg
    quality_names = {}
    for r in plan_rows:
        assign_key = (r.assign_date, r.jute_yarn_id, r.batch_plan_id)
        if assign_key not in seen_assigns:
            seen_assigns.add(assign_key)
            batch_plan.append({
                "assign_date": r.assign_date.isoformat(),
                "jute_yarn_id": r.jute_yarn_id,
                "yarn_name": r.yarn_name,
                "batch_plan_id": r.batch_plan_id,
                "plan_name": r.plan_name,
                "status_id": r.status_id,
            })
        if r.item_id is None or r.percentage is None:
            continue
        ref_kg, _src = reference_for(r.yarn_item_id)
        requirement[(r.assign_date, r.item_id)] += ref_kg * float(r.percentage) / 100.0
        if r.quality_name:
            quality_names.setdefault(r.item_id, r.quality_name)

    quality_requirement = [
        {
            "assign_date": d.isoformat(),
            "item_id": item_id,
            "quality_name": quality_names.get(item_id),
            "requirement_kg": _round(kg),
            "requirement_mt": _round(kg / 1000.0),
        }
        for (d, item_id), kg in sorted(
            requirement.items(),
            key=lambda kv: (kv[0][0], quality_names.get(kv[0][1]) or ""),
        )
    ]

    # --- 3. Roll stock snapshot per quality (kg), blends split by ratio ------
    roll_stock_kg = defaultdict(float)
    for r in snapshot_rows:
        for item_id, kg in _split_blend_kg(r).items():
            roll_stock_kg[item_id] += kg
        if r.item_name:
            quality_names.setdefault(r.item_id, r.item_name)
        if r.item_id_2 is not None and r.item_name_2:
            quality_names.setdefault(r.item_id_2, r.item_name_2)

    # --- 4. Required issue per quality ---------------------------------------
    maturity_by_item = {r.item_id: float(r.maturity_hours) for r in maturity_rows}
    planned_qualities = {item_id for (_d, item_id) in requirement}
    required_issue = []
    for item_id in sorted(
        planned_qualities | set(roll_stock_kg),
        key=lambda i: quality_names.get(i) or "",
    ):
        maturity_hours = maturity_by_item.get(item_id)
        maturity_source = "master" if maturity_hours is not None else "default"
        if maturity_hours is None:
            maturity_hours = float(DEFAULT_MATURITY_HOURS)
        maturity_days = maturity_hours / 24.0
        target_date = report_date + timedelta(days=int(maturity_days + 0.5))
        target_date_clamped = target_date > window_end
        if target_date_clamped:
            # The plan window is the horizon: for maturities beyond it, plan
            # against the last known day instead of a zero target.
            target_date = window_end
        target_mt = requirement.get((target_date, item_id), 0.0) / 1000.0 * maturity_days
        current_mt = roll_stock_kg.get(item_id, 0.0) / 1000.0
        plan_issue_mt = requirement.get((report_date, item_id), 0.0) / 1000.0
        required_mt = target_mt - (current_mt - plan_issue_mt)
        required_issue.append({
            "item_id": item_id,
            "quality_name": quality_names.get(item_id),
            "maturity_hours": _round(maturity_hours, 1),
            "maturity_source": maturity_source,
            "target_stock_date": target_date.isoformat(),
            "target_date_clamped": target_date_clamped,
            "target_stock_mt": _round(target_mt),
            "current_roll_stock_mt": _round(current_mt),
            "plan_issue_mt": _round(plan_issue_mt),
            "required_issue_mt": _round(required_mt),
            "required_issue_bales": _round(required_mt * 1000.0 / JUTE_BALE_WEIGHT_KG),
        })

    # --- 5. Godown stock coverage in days ------------------------------------
    godown_by_item = {}
    for r in godown_rows:
        godown_by_item[r.item_id] = float(r.closing_weight or 0)
        if r.item_name:
            quality_names.setdefault(r.item_id, r.item_name)
    window_requirement = defaultdict(float)
    for (_d, item_id), kg in requirement.items():
        window_requirement[item_id] += kg

    stock_coverage = []
    for item_id in sorted(
        set(godown_by_item) | set(window_requirement),
        key=lambda i: quality_names.get(i) or "",
    ):
        godown_kg = godown_by_item.get(item_id, 0.0)
        avg_daily_kg = window_requirement.get(item_id, 0.0) / ISSUE_PLANNER_WINDOW_DAYS
        days = godown_kg / avg_daily_kg if avg_daily_kg > 0 else None
        stock_coverage.append({
            "item_id": item_id,
            "quality_name": quality_names.get(item_id),
            "godown_stock_kg": _round(godown_kg),
            "godown_stock_mt": _round(godown_kg / 1000.0),
            "avg_daily_requirement_mt": _round(avg_daily_kg / 1000.0),
            "days_of_stock": _round(days, 1) if days is not None else None,
        })

    if not include_godown_cover:
        stock_coverage = []

    # Headline total counts only qualities that actually need issuing —
    # negative rows (excess stock) must not net it down.
    total_required_mt = sum(
        row["required_issue_mt"] for row in required_issue if row["required_issue_mt"] > 0
    )
    return {
        "summary": {
            "report_date": report_date.isoformat(),
            "production_date": production_date.isoformat(),
            "window_from": window_start.isoformat(),
            "window_to": window_end.isoformat(),
            "snapshot_at": snapshot_dt.isoformat(sep=" "),
            "co_id": int(co_id),
            "branch_id": int(branch_id),
            "total_reference_production_kg": _round(sum(production_by_item.values())),
            "total_required_issue_mt": _round(total_required_mt),
            "total_required_issue_bales": _round(
                total_required_mt * 1000.0 / JUTE_BALE_WEIGHT_KG
            ),
        },
        "reference_production": reference_production,
        "batch_plan": batch_plan,
        "quality_requirement": quality_requirement,
        "required_issue": required_issue,
        "stock_coverage": stock_coverage,
    }

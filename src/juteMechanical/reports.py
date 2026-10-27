"""Jute Mechanical & Maintenance report endpoints (`/api/mechMaintReports`).

Twelve kebab-case reports that replace the mill's hand-computed "MILL SIDE
MECHANICAL BREAK DOWN SUMMARY" / "spreader machine breakdown report" workbooks.

The metric definitions are the consultant value of this module, so they are
applied identically everywhere (spec: docs/jute-mechanical-maintenance-spec.md,
"Metric formulas"):

    Only downtime_type in FAILURE_DOWNTIME_TYPES AND active = 1 counts as a
    failure -- planned PM, overhaul, no-material and changeover downtime are
    downtime but not failures, and letting them through would inflate breakdown
    counts and wreck MTBF.

    breakdown_count = COUNT(*)
    total_downtime  = SUM(duration_min)
    MTTR (min)      = SUM(duration_min) / COUNT(*)     -- the workbook's
                                                          "duration per breakdown"
    available_min   = days_in_range * 1440             (DEFAULT_AVAILABLE_HOURS_PER_DAY)
    MTBF (min)      = (available_min - SUM(duration_min)) / COUNT(*)
    availability_%  = (available_min - SUM(duration_min)) / available_min * 100
    unattributed_%  = downtime on reason_code 'NR' / total downtime * 100

Every ratio goes through _safe_div, which returns None on a zero/absent
denominator instead of raising -- an empty date range must render an empty
report, never a 500.

Every summary-style report returns `unattributed_pct` (both at the top level and
inside `summary`) so 'NR' -- "No Reason mentioned" -- stays visible rather than
quietly absorbing real causes.

House conventions (src/inventory/reports.py): plain `def` handlers, page/limit
with limit capped at 10000, SQL-side LIMIT/OFFSET, paired *_query() /
*_count_query() builders, derived fields computed in Python, {"data", "total"}
responses, logger.error(..., exc_info=True) in the except block.
"""

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteMechanical.constants import (
    CRITICALITY_A,
    DEFAULT_AVAILABLE_HOURS_PER_DAY,
    REPEAT_FAILURE_WINDOW_DAYS,
)
from src.juteMechanical.reportQueries import (
    FAILURE_TYPE_PARAMS,
    NR_REASON_CODE,
    get_breakdown_metrics_query,
    get_breakdown_register_count_query,
    get_breakdown_register_query,
    get_date_machine_grid_columns_query,
    get_date_machine_grid_count_query,
    get_date_machine_grid_query,
    get_department_summary_count_query,
    get_department_summary_query,
    get_machine_history_breakdown_count_query,
    get_machine_history_breakdowns_query,
    get_machine_history_machine_query,
    get_machine_history_overhaul_query,
    get_machine_history_pm_query,
    get_machine_history_spares_query,
    get_machine_summary_count_query,
    get_machine_summary_query,
    get_maintenance_due_count_query,
    get_maintenance_due_query,
    get_open_breakdown_board_count_query,
    get_open_breakdown_board_query,
    get_reason_pareto_count_query,
    get_reason_pareto_query,
    get_reason_trend_count_query,
    get_reason_trend_query,
    get_reliability_count_query,
    get_reliability_query,
    get_repeat_failures_query,
    get_spares_consumption_count_query,
    get_spares_consumption_query,
    get_spares_machine_cost_query,
    get_top_machines_by_count_query,
    get_top_machines_by_downtime_query,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: Minutes of theoretical availability per machine per day (24h fallback).
AVAILABLE_MIN_PER_DAY = float(DEFAULT_AVAILABLE_HOURS_PER_DAY) * 60.0

#: Hard cap on page size for every report (house reports convention).
MAX_LIMIT = 10000

#: Default lookahead for the maintenance-due report.
DEFAULT_DUE_WITHIN_DAYS = 30

#: Default N for the top-problem-machines report.
DEFAULT_TOP_N = 10


# =============================================================================
# Serialisation + arithmetic helpers
# =============================================================================


def _iso(value):
    """date/datetime -> ISO string; everything else untouched."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _num(value, default=None):
    """Decimal/int/str -> float. Decimals never reach JSON as objects."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=0):
    """Best-effort int for COUNT() columns."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _round(value, digits=2):
    """Round, tolerating None (a None metric means 'not computable', not zero)."""
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _safe_div(numerator, denominator):
    """Division that returns None on a zero/absent denominator rather than raising.

    Every metric ratio in this module goes through here: an empty range, a
    machine with no downtime, or a report run before any data exists must render
    empty, never 500.
    """
    if numerator is None or denominator is None:
        return None
    try:
        denominator = float(denominator)
        if denominator == 0:
            return None
        return float(numerator) / denominator
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _parse_date(value, field):
    """YYYY-MM-DD -> date, 400 on anything else. None passes through."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {field} format, expected YYYY-MM-DD")


def _days_in_range(date_from, date_to, first_seen=None, last_seen=None):
    """Days covered by the report, for available_min.

    Prefers the requested range; falls back to the span of the data actually
    found so an unbounded run still yields an availability figure. Returns None
    when neither is resolvable -- callers then report availability as None
    instead of inventing a denominator.
    """
    start = _parse_date(date_from, "date_from") or (
        first_seen.date() if isinstance(first_seen, datetime) else first_seen
    )
    end = _parse_date(date_to, "date_to") or (
        last_seen.date() if isinstance(last_seen, datetime) else last_seen
    )
    if not isinstance(start, date) or not isinstance(end, date):
        return None
    days = (end - start).days + 1
    return days if days > 0 else None


def _metrics(count, downtime_min, nr_downtime_min, days):
    """The locked metric set, computed once so no report can drift from another."""
    count = _int(count, 0)
    downtime = _num(downtime_min, 0.0) or 0.0
    nr_downtime = _num(nr_downtime_min, 0.0) or 0.0

    available_min = days * AVAILABLE_MIN_PER_DAY if days else None
    uptime_min = None if available_min is None else available_min - downtime
    running_hrs = None if uptime_min is None else uptime_min / 60.0

    return {
        "breakdown_count": count,
        "total_downtime_min": _round(downtime),
        "nr_downtime_min": _round(nr_downtime),
        "days_in_range": days,
        "available_min": _round(available_min),
        "mttr_min": _round(_safe_div(downtime, count)),
        "mtbf_min": _round(_safe_div(uptime_min, count)),
        "availability_pct": _round(_safe_div(
            None if uptime_min is None else uptime_min * 100.0, available_min
        )),
        "unattributed_pct": _round(_safe_div(nr_downtime * 100.0, downtime)),
        "downtime_per_1000_running_hrs": _round(_safe_div(downtime * 1000.0, running_hrs)),
    }


def _common_params(co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline):
    """The filter bind set shared by every breakdown-sourced query.

    co_id is validated by the caller; the rest are optional (NULL = all) and are
    int()-cast so a query-string "3" never reaches SQL as a string.
    """
    return {
        "co_id": int(co_id),
        "branch_id": int(branch_id) if branch_id else None,
        "date_from": date_from if date_from else None,
        "date_to": date_to if date_to else None,
        "machine_type_id": int(machine_type_id) if machine_type_id else None,
        "machine_id": int(machine_id) if machine_id else None,
        "discipline": discipline.strip() if discipline and discipline.strip() else None,
    }


def _failure_params(common):
    """Common filters + the BREAKDOWN-only gate + the 'NR' code."""
    return {**common, "nr_reason_code": NR_REASON_CODE, **FAILURE_TYPE_PARAMS}


def _count_params(common):
    """Common filters + the BREAKDOWN-only gate.

    The paired *_count_query() builders never select the NR split, so their bind
    set stops here -- each execute() is given exactly the params its SQL names.
    """
    return {**common, **FAILURE_TYPE_PARAMS}


def _require_co_id(co_id):
    if not co_id:
        raise HTTPException(status_code=400, detail="co_id is required")


def _paging(page, limit, default_limit):
    """page/limit -> (limit, offset), limit capped at MAX_LIMIT."""
    page = max(_int(page, 1) or 1, 1)
    limit = _int(limit, default_limit) or default_limit
    limit = max(min(limit, MAX_LIMIT), 1)
    return limit, (page - 1) * limit


def _scalar_total(db, query, params):
    """COUNT(*) -> int, tolerating an empty result."""
    result = db.execute(query, params).scalar()
    return _int(result, 0)


def _fetch_summary(db, common, date_from, date_to):
    """Mill-level failure metrics for the current filter set.

    Returned by every summary-style report as `summary`, with `unattributed_pct`
    also lifted to the top level so the 'NR' share is impossible to miss.
    """
    row = db.execute(get_breakdown_metrics_query(), _failure_params(common)).fetchone()
    mapped = dict(row._mapping) if row else {}

    days = _days_in_range(
        date_from,
        date_to,
        mapped.get("first_breakdown_date"),
        mapped.get("last_breakdown_date"),
    )
    summary = _metrics(
        mapped.get("breakdown_count"),
        mapped.get("total_downtime_min"),
        mapped.get("nr_downtime_min"),
        days,
    )
    total_response = _num(mapped.get("total_response_min"), 0.0) or 0.0
    response_count = _int(mapped.get("response_count"), 0)
    summary.update({
        "nr_count": _int(mapped.get("nr_count"), 0),
        "machine_count": _int(mapped.get("machine_count"), 0),
        "open_count": _int(mapped.get("open_count"), 0),
        "avg_response_min": _round(_safe_div(total_response, response_count)),
        "production_loss_qty": _round(_num(mapped.get("production_loss_qty"), 0.0)),
        "first_breakdown_date": _iso(mapped.get("first_breakdown_date")),
        "last_breakdown_date": _iso(mapped.get("last_breakdown_date")),
    })
    return summary


def _month_range(date_from, date_to):
    """Inclusive list of YYYY-MM strings spanning two YYYY-MM-DD dates."""
    start = _parse_date(date_from, "date_from")
    end = _parse_date(date_to, "date_to")
    if not start or not end or start > end:
        return []
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


# =============================================================================
# 1. /breakdown-register
# =============================================================================


@router.get("/breakdown-register")
def get_breakdown_register(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 100,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
    downtime_type: str | None = None,
    status: str | None = None,
    search: str | None = None,
):
    """Raw filterable incident log — every breakdown row with its display names.

    Unlike the analytic reports this is NOT limited to downtime_type =
    'BREAKDOWN': the register is the audit trail and must show planned PM /
    no-material / changeover rows too (each row carries `downtime_type`). The
    `summary` block alongside it is still failure-only, so the metrics on screen
    match every other report.
    """
    try:
        _require_co_id(co_id)
        _parse_date(date_from, "date_from")
        _parse_date(date_to, "date_to")

        limit, offset = _paging(page, limit, 100)
        common = _common_params(
            co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline
        )
        register_params = {
            **common,
            "downtime_type": downtime_type.strip() if downtime_type and downtime_type.strip() else None,
            "status": status.strip() if status and status.strip() else None,
            "search_like": f"%{search.strip()}%" if search and search.strip() else None,
        }

        rows = db.execute(
            get_breakdown_register_query(),
            {**register_params, "limit": limit, "offset": offset},
        ).fetchall()

        data = []
        for row in rows:
            m = dict(row._mapping)
            data.append({
                "breakdown_id": m.get("breakdown_id"),
                "tran_date": _iso(m.get("tran_date")),
                "machine_id": m.get("machine_id"),
                "machine_name": m.get("machine_name"),
                "mech_code": m.get("mech_code"),
                "machine_type_id": m.get("machine_type_id"),
                "machine_type_name": m.get("machine_type_name"),
                "dept_id": m.get("dept_id"),
                "dept_name": m.get("dept_name"),
                "branch_id": m.get("branch_id"),
                "spell_id": m.get("spell_id"),
                "spell_code": m.get("spell_code"),
                "stop_datetime": _iso(m.get("stop_datetime")),
                "attended_datetime": _iso(m.get("attended_datetime")),
                "restart_datetime": _iso(m.get("restart_datetime")),
                "duration_min": _num(m.get("duration_min")),
                "response_min": _num(m.get("response_min")),
                "reason_id": m.get("reason_id"),
                "reason_code": m.get("reason_code"),
                "reason_desc": m.get("reason_desc"),
                "component_id": m.get("component_id"),
                "component_name": m.get("component_name"),
                "component_group": m.get("component_group"),
                "discipline": m.get("discipline"),
                "downtime_type": m.get("downtime_type"),
                "attended_dept_id": m.get("attended_dept_id"),
                "attended_dept_name": m.get("attended_dept_name"),
                "attended_by": m.get("attended_by"),
                "reported_by": m.get("reported_by"),
                "root_cause": m.get("root_cause"),
                "action_taken": m.get("action_taken"),
                "preventive_action": m.get("preventive_action"),
                "production_loss_qty": _num(m.get("production_loss_qty")),
                "status": m.get("status"),
                "remarks": m.get("remarks"),
                "reported_by_user_name": m.get("reported_by_user_name"),
                "reported_date_time": _iso(m.get("reported_date_time")),
                "fixed_by_user_name": m.get("fixed_by_user_name"),
                "fixed_date_time": _iso(m.get("fixed_date_time")),
                "prod_confirm_status": m.get("prod_confirm_status"),
                "prod_confirm_by_name": m.get("prod_confirm_by_name"),
                "prod_confirm_date_time": _iso(m.get("prod_confirm_date_time")),
                "prod_restart_datetime": _iso(m.get("prod_restart_datetime")),
                "prod_confirm_remarks": m.get("prod_confirm_remarks"),
            })

        total = _scalar_total(db, get_breakdown_register_count_query(), register_params)
        summary = _fetch_summary(db, common, date_from, date_to)

        return {
            "data": data,
            "total": total,
            "summary": summary,
            "unattributed_pct": summary.get("unattributed_pct"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech breakdown register: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 2. /machine-summary  — the workbook's main sheet
# =============================================================================


@router.get("/machine-summary")
def get_machine_summary(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 10000,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
):
    """Per machine: breakdown count, total downtime, MTTR, MTBF, availability.

    This is the sheet the mill maintains by hand. MTTR is what the workbook calls
    "duration per breakdown"; MTBF and availability are what it cannot compute at
    all, and both are None (not zero) when the date range gives no denominator.
    """
    try:
        _require_co_id(co_id)
        _parse_date(date_from, "date_from")
        _parse_date(date_to, "date_to")

        limit, offset = _paging(page, limit, 10000)
        common = _common_params(
            co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline
        )
        params = _failure_params(common)

        rows = db.execute(
            get_machine_summary_query(), {**params, "limit": limit, "offset": offset}
        ).fetchall()

        data = []
        for row in rows:
            m = dict(row._mapping)
            days = _days_in_range(
                date_from, date_to,
                m.get("first_breakdown_date"), m.get("last_breakdown_date"),
            )
            metrics = _metrics(
                m.get("breakdown_count"),
                m.get("total_downtime_min"),
                m.get("nr_downtime_min"),
                days,
            )
            grand_downtime = _num(m.get("grand_downtime_min"), 0.0)
            total_response = _num(m.get("total_response_min"), 0.0) or 0.0
            response_count = _int(m.get("response_count"), 0)
            data.append({
                "machine_id": m.get("machine_id"),
                "machine_name": m.get("machine_name"),
                "mech_code": m.get("mech_code"),
                "machine_type_id": m.get("machine_type_id"),
                "machine_type_name": m.get("machine_type_name"),
                "dept_id": m.get("dept_id"),
                "dept_name": m.get("dept_name"),
                "branch_id": m.get("branch_id"),
                "criticality": m.get("criticality"),
                "breakdown_count": metrics["breakdown_count"],
                "total_downtime_min": metrics["total_downtime_min"],
                "mttr_min": metrics["mttr_min"],
                "mtbf_min": metrics["mtbf_min"],
                "availability_pct": metrics["availability_pct"],
                "available_min": metrics["available_min"],
                "days_in_range": metrics["days_in_range"],
                "nr_downtime_min": metrics["nr_downtime_min"],
                "unattributed_pct": metrics["unattributed_pct"],
                "nr_count": _int(m.get("nr_count"), 0),
                "avg_response_min": _round(_safe_div(total_response, response_count)),
                "open_count": _int(m.get("open_count"), 0),
                "production_loss_qty": _round(_num(m.get("production_loss_qty"), 0.0)),
                "pct_of_mill_downtime": _round(
                    _safe_div((metrics["total_downtime_min"] or 0.0) * 100.0, grand_downtime)
                ),
                "first_breakdown_date": _iso(m.get("first_breakdown_date")),
                "last_breakdown_date": _iso(m.get("last_breakdown_date")),
            })

        total = _scalar_total(db, get_machine_summary_count_query(), _count_params(common))
        summary = _fetch_summary(db, common, date_from, date_to)

        return {
            "data": data,
            "total": total,
            "summary": summary,
            "unattributed_pct": summary.get("unattributed_pct"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech machine summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 3. /reason-pareto  — the workbook's reason block, made a real Pareto
# =============================================================================


@router.get("/reason-pareto")
def get_reason_pareto(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 10000,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
):
    """Per reason: count, total duration, duration per breakdown, % share, cumulative %.

    The workbook stops after the first three columns; pct_share and
    cumulative_pct are what turn the block into an actual Pareto ("which four
    codes are 80% of the downtime"). The cumulative comes off a SQL window
    function so it stays correct from page 2 onwards.
    """
    try:
        _require_co_id(co_id)
        _parse_date(date_from, "date_from")
        _parse_date(date_to, "date_to")

        limit, offset = _paging(page, limit, 10000)
        common = _common_params(
            co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline
        )
        params = {**common, **FAILURE_TYPE_PARAMS}

        rows = db.execute(
            get_reason_pareto_query(), {**params, "limit": limit, "offset": offset}
        ).fetchall()

        data = []
        for row in rows:
            m = dict(row._mapping)
            count = _int(m.get("breakdown_count"), 0)
            duration = _num(m.get("total_downtime_min"), 0.0) or 0.0
            cumulative = _num(m.get("cum_downtime_min"), 0.0) or 0.0
            grand = _num(m.get("grand_downtime_min"), 0.0)
            data.append({
                "reason_id": m.get("reason_id"),
                "reason_code": m.get("reason_code"),
                "reason_desc": m.get("reason_desc"),
                "discipline": m.get("discipline"),
                "component_id": m.get("component_id"),
                "component_name": m.get("component_name"),
                "machine_count": _int(m.get("machine_count"), 0),
                "count": count,
                "total_duration": _round(duration),
                "duration_per_breakdown": _round(_safe_div(duration, count)),
                "pct_share": _round(_safe_div(duration * 100.0, grand)),
                "cumulative_duration": _round(cumulative),
                "cumulative_pct": _round(_safe_div(cumulative * 100.0, grand)),
            })

        total = _scalar_total(db, get_reason_pareto_count_query(), params)
        summary = _fetch_summary(db, common, date_from, date_to)

        return {
            "data": data,
            "total": total,
            "summary": summary,
            "unattributed_pct": summary.get("unattributed_pct"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech reason pareto: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 4. /date-machine-grid  — exact replica of the sheet they key today
# =============================================================================


@router.get("/date-machine-grid")
def get_date_machine_grid(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 100,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
):
    """Date x machine matrix of reason codes.

    Multiple incidents on the same machine on the same date are joined with "/"
    — exactly how the mill writes them into one Excel cell today ("RF/DRJ",
    "DRJ/PB/BJ"). Paging is on the DATE axis: one grid row per date.

    Returns `data` rows keyed by machine_id (as a string) plus a `columns` list
    describing those machine columns.
    """
    try:
        _require_co_id(co_id)
        _parse_date(date_from, "date_from")
        _parse_date(date_to, "date_to")

        limit, offset = _paging(page, limit, 100)
        common = _common_params(
            co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline
        )
        params = {**common, **FAILURE_TYPE_PARAMS}

        column_rows = db.execute(get_date_machine_grid_columns_query(), params).fetchall()
        columns = []
        for row in column_rows:
            m = dict(row._mapping)
            columns.append({
                "machine_id": m.get("machine_id"),
                "machine_name": m.get("machine_name"),
                "mech_code": m.get("mech_code"),
                "machine_type_id": m.get("machine_type_id"),
                "machine_type_name": m.get("machine_type_name"),
            })

        cell_rows = db.execute(
            get_date_machine_grid_query(), {**params, "limit": limit, "offset": offset}
        ).fetchall()

        # Pivot: one row per date, machine_id (stringified) as the cell key.
        grid: dict = {}
        order: list = []
        for row in cell_rows:
            m = dict(row._mapping)
            tran_date = _iso(m.get("tran_date"))
            entry = grid.get(tran_date)
            if entry is None:
                entry = {
                    "tran_date": tran_date,
                    "breakdown_count": 0,
                    "total_downtime_min": 0.0,
                }
                grid[tran_date] = entry
                order.append(tran_date)
            entry[str(m.get("machine_id"))] = m.get("reason_codes")
            entry["breakdown_count"] += _int(m.get("breakdown_count"), 0)
            entry["total_downtime_min"] += _num(m.get("total_downtime_min"), 0.0) or 0.0

        data = []
        for tran_date in order:
            entry = grid[tran_date]
            entry["total_downtime_min"] = _round(entry["total_downtime_min"])
            data.append(entry)

        total = _scalar_total(db, get_date_machine_grid_count_query(), params)
        summary = _fetch_summary(db, common, date_from, date_to)

        return {
            "data": data,
            "columns": columns,
            "total": total,
            "summary": summary,
            "unattributed_pct": summary.get("unattributed_pct"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech date-machine grid: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 5. /department-summary
# =============================================================================


@router.get("/department-summary")
def get_department_summary(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 10000,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
):
    """Per department + machine type: count, duration, MTTR, % of mill downtime."""
    try:
        _require_co_id(co_id)
        _parse_date(date_from, "date_from")
        _parse_date(date_to, "date_to")

        limit, offset = _paging(page, limit, 10000)
        common = _common_params(
            co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline
        )
        params = _failure_params(common)
        days = _days_in_range(date_from, date_to)

        rows = db.execute(
            get_department_summary_query(), {**params, "limit": limit, "offset": offset}
        ).fetchall()

        data = []
        for row in rows:
            m = dict(row._mapping)
            metrics = _metrics(
                m.get("breakdown_count"),
                m.get("total_downtime_min"),
                m.get("nr_downtime_min"),
                days,
            )
            grand_downtime = _num(m.get("grand_downtime_min"), 0.0)
            data.append({
                "dept_id": m.get("dept_id"),
                "dept_name": m.get("dept_name"),
                "branch_id": m.get("branch_id"),
                "machine_type_id": m.get("machine_type_id"),
                "machine_type_name": m.get("machine_type_name"),
                "machine_count": _int(m.get("machine_count"), 0),
                "breakdown_count": metrics["breakdown_count"],
                "total_downtime_min": metrics["total_downtime_min"],
                "mttr_min": metrics["mttr_min"],
                "mtbf_min": metrics["mtbf_min"],
                "availability_pct": metrics["availability_pct"],
                "nr_downtime_min": metrics["nr_downtime_min"],
                "unattributed_pct": metrics["unattributed_pct"],
                "nr_count": _int(m.get("nr_count"), 0),
                "production_loss_qty": _round(_num(m.get("production_loss_qty"), 0.0)),
                "pct_of_mill_downtime": _round(
                    _safe_div((metrics["total_downtime_min"] or 0.0) * 100.0, grand_downtime)
                ),
            })

        total = _scalar_total(db, get_department_summary_count_query(), _count_params(common))
        summary = _fetch_summary(db, common, date_from, date_to)

        return {
            "data": data,
            "total": total,
            "summary": summary,
            "unattributed_pct": summary.get("unattributed_pct"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech department summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 6. /reliability
# =============================================================================


@router.get("/reliability")
def get_reliability(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 10000,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
):
    """Per machine: MTBF, MTTR, availability %, downtime per 1000 running hours.

    running_hours = (available_min - downtime) / 60, so the per-1000-hours figure
    is downtime against time the machine was actually able to run. Every ratio is
    None rather than 0 when its denominator is missing.
    """
    try:
        _require_co_id(co_id)
        _parse_date(date_from, "date_from")
        _parse_date(date_to, "date_to")

        limit, offset = _paging(page, limit, 10000)
        common = _common_params(
            co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline
        )
        params = _failure_params(common)

        rows = db.execute(
            get_reliability_query(), {**params, "limit": limit, "offset": offset}
        ).fetchall()

        data = []
        for row in rows:
            m = dict(row._mapping)
            days = _days_in_range(
                date_from, date_to,
                m.get("first_breakdown_date"), m.get("last_breakdown_date"),
            )
            metrics = _metrics(
                m.get("breakdown_count"),
                m.get("total_downtime_min"),
                m.get("nr_downtime_min"),
                days,
            )
            available_min = metrics["available_min"]
            downtime = metrics["total_downtime_min"] or 0.0
            running_hrs = None if available_min is None else (available_min - downtime) / 60.0
            total_response = _num(m.get("total_response_min"), 0.0) or 0.0
            response_count = _int(m.get("response_count"), 0)
            data.append({
                "machine_id": m.get("machine_id"),
                "machine_name": m.get("machine_name"),
                "mech_code": m.get("mech_code"),
                "machine_type_id": m.get("machine_type_id"),
                "machine_type_name": m.get("machine_type_name"),
                "dept_id": m.get("dept_id"),
                "dept_name": m.get("dept_name"),
                "branch_id": m.get("branch_id"),
                "criticality": m.get("criticality"),
                "breakdown_count": metrics["breakdown_count"],
                "total_downtime_min": metrics["total_downtime_min"],
                "days_in_range": metrics["days_in_range"],
                "available_min": available_min,
                "running_hrs": _round(running_hrs),
                "mtbf_min": metrics["mtbf_min"],
                "mtbf_hrs": _round(_safe_div(metrics["mtbf_min"], 60.0)),
                "mttr_min": metrics["mttr_min"],
                "availability_pct": metrics["availability_pct"],
                "downtime_per_1000_running_hrs": metrics["downtime_per_1000_running_hrs"],
                "avg_response_min": _round(_safe_div(total_response, response_count)),
                "nr_downtime_min": metrics["nr_downtime_min"],
                "unattributed_pct": metrics["unattributed_pct"],
                "open_count": _int(m.get("open_count"), 0),
            })

        total = _scalar_total(db, get_reliability_count_query(), _count_params(common))
        summary = _fetch_summary(db, common, date_from, date_to)

        return {
            "data": data,
            "total": total,
            "summary": summary,
            "unattributed_pct": summary.get("unattributed_pct"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech reliability report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 7. /reason-trend  — the workbook's Summary sheets
# =============================================================================


@router.get("/reason-trend")
def get_reason_trend(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 10000,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
):
    """Month x reason: count and duration per month, one row per reason.

    Each data row carries "<YYYY-MM>_count" and "<YYYY-MM>_duration" keys for
    every month in `months`, plus total_count / total_duration. Reasons are
    ranked by total downtime, so page 1 is the reasons that matter.
    """
    try:
        _require_co_id(co_id)
        _parse_date(date_from, "date_from")
        _parse_date(date_to, "date_to")

        limit, offset = _paging(page, limit, 10000)
        common = _common_params(
            co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline
        )
        params = {**common, **FAILURE_TYPE_PARAMS}

        rows = db.execute(
            get_reason_trend_query(), {**params, "limit": limit, "offset": offset}
        ).fetchall()
        mapped_rows = [dict(row._mapping) for row in rows]

        # Prefer the requested range so months with zero breakdowns still appear
        # as columns; fall back to the months actually present.
        months = _month_range(date_from, date_to)
        if not months:
            months = sorted({r.get("ym") for r in mapped_rows if r.get("ym")})

        reasons: dict = {}
        order: list = []
        for m in mapped_rows:
            reason_id = m.get("reason_id")
            entry = reasons.get(reason_id)
            if entry is None:
                entry = {
                    "reason_id": reason_id,
                    "reason_code": m.get("reason_code"),
                    "reason_desc": m.get("reason_desc"),
                    "discipline": m.get("discipline"),
                    "component_name": m.get("component_name"),
                    "total_count": 0,
                    "total_duration": 0.0,
                }
                for ym in months:
                    entry[f"{ym}_count"] = 0
                    entry[f"{ym}_duration"] = 0.0
                reasons[reason_id] = entry
                order.append(reason_id)
            ym = m.get("ym")
            count = _int(m.get("breakdown_count"), 0)
            duration = _num(m.get("total_downtime_min"), 0.0) or 0.0
            if ym:
                entry[f"{ym}_count"] = entry.get(f"{ym}_count", 0) + count
                entry[f"{ym}_duration"] = round(entry.get(f"{ym}_duration", 0.0) + duration, 2)
                if ym not in months:
                    months.append(ym)
            entry["total_count"] += count
            entry["total_duration"] += duration

        data = []
        for reason_id in order:
            entry = reasons[reason_id]
            entry["total_duration"] = _round(entry["total_duration"])
            entry["duration_per_breakdown"] = _round(
                _safe_div(entry["total_duration"], entry["total_count"])
            )
            data.append(entry)

        months = sorted(set(months))
        total = _scalar_total(db, get_reason_trend_count_query(), params)
        summary = _fetch_summary(db, common, date_from, date_to)

        return {
            "data": data,
            "months": months,
            "total": total,
            "summary": summary,
            "unattributed_pct": summary.get("unattributed_pct"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech reason trend: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 8. /maintenance-due
# =============================================================================


@router.get("/maintenance-due")
def get_maintenance_due(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 10000,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
    due_within_days: int | None = None,
):
    """PM and overhaul due/overdue, criticality A first then most overdue first.

    Reads jute_mech_machine_attr: a row is emitted per due item, so a machine
    with both a PM and an overhaul due appears twice (`due_type` says which).
    `days_overdue` is positive when the date has passed and negative when the
    item is merely upcoming inside the `due_within_days` window (default 30).

    date_from / date_to are accepted for a uniform report toolbar but do not
    filter this report — the due date is what matters, not a transaction range.
    """
    try:
        _require_co_id(co_id)

        window = _int(due_within_days, DEFAULT_DUE_WITHIN_DAYS)
        if window is None or window < 0:
            window = DEFAULT_DUE_WITHIN_DAYS
        today = date.today()
        due_cutoff = today + timedelta(days=window)

        limit, offset = _paging(page, limit, 10000)
        params = {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "machine_type_id": int(machine_type_id) if machine_type_id else None,
            "machine_id": int(machine_id) if machine_id else None,
            "due_cutoff": due_cutoff.isoformat(),
        }

        rows = db.execute(
            get_maintenance_due_query(), {**params, "limit": limit, "offset": offset}
        ).fetchall()

        data = []
        pm_due = pm_overdue = overhaul_due = overhaul_overdue = critical_count = 0
        for row in rows:
            m = dict(row._mapping)
            due_date = m.get("due_date")
            days_overdue = (today - due_date).days if isinstance(due_date, date) else None
            is_overdue = bool(days_overdue is not None and days_overdue >= 0)
            due_type = m.get("due_type")
            if due_type == "PM":
                pm_due += 1
                pm_overdue += 1 if is_overdue else 0
            else:
                overhaul_due += 1
                overhaul_overdue += 1 if is_overdue else 0
            if m.get("criticality") == CRITICALITY_A:
                critical_count += 1
            data.append({
                "due_type": due_type,
                "machine_attr_id": m.get("machine_attr_id"),
                "machine_id": m.get("machine_id"),
                "machine_name": m.get("machine_name"),
                "mech_code": m.get("mech_code"),
                "machine_type_id": m.get("machine_type_id"),
                "machine_type_name": m.get("machine_type_name"),
                "dept_id": m.get("dept_id"),
                "dept_name": m.get("dept_name"),
                "branch_id": m.get("branch_id"),
                "criticality": m.get("criticality"),
                "make": m.get("make"),
                "model": m.get("model"),
                "due_date": _iso(due_date),
                "last_done_date": _iso(m.get("last_done_date")),
                "freq_days": m.get("freq_days"),
                "freq_months": m.get("freq_months"),
                "days_overdue": days_overdue,
                "is_overdue": is_overdue,
            })

        total = _scalar_total(db, get_maintenance_due_count_query(), params)

        return {
            "data": data,
            "total": total,
            "summary": {
                "as_on_date": today.isoformat(),
                "due_within_days": window,
                "due_cutoff": due_cutoff.isoformat(),
                "pm_due_count": pm_due,
                "pm_overdue_count": pm_overdue,
                "overhaul_due_count": overhaul_due,
                "overhaul_overdue_count": overhaul_overdue,
                "critical_count": critical_count,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech maintenance due report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 9. /spares-consumption
# =============================================================================


@router.get("/spares-consumption")
def get_spares_consumption(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 100,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
):
    """Per machine + item spares qty and value, plus maintenance cost per machine.

    Covers both spare sources (breakdown and overhaul). `machine_cost` folds in
    overhaul labour as well, so "cost per machine" is the real maintenance spend
    rather than only the parts bill.
    """
    try:
        _require_co_id(co_id)
        _parse_date(date_from, "date_from")
        _parse_date(date_to, "date_to")

        limit, offset = _paging(page, limit, 100)
        params = _common_params(
            co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline
        )

        rows = db.execute(
            get_spares_consumption_query(), {**params, "limit": limit, "offset": offset}
        ).fetchall()

        data = []
        total_qty = 0.0
        total_value = 0.0
        for row in rows:
            m = dict(row._mapping)
            row_qty = _num(m.get("total_qty"), 0.0) or 0.0
            row_value = _num(m.get("total_value"), 0.0) or 0.0
            total_qty += row_qty
            total_value += row_value
            data.append({
                "machine_id": m.get("machine_id"),
                "machine_name": m.get("machine_name"),
                "mech_code": m.get("mech_code"),
                "machine_type_id": m.get("machine_type_id"),
                "machine_type_name": m.get("machine_type_name"),
                "dept_id": m.get("dept_id"),
                "dept_name": m.get("dept_name"),
                "branch_id": m.get("branch_id"),
                "item_id": m.get("item_id"),
                "item_code": m.get("item_code"),
                "item_name": m.get("item_name"),
                "uom_name": m.get("uom_name"),
                "breakdown_qty": _round(_num(m.get("breakdown_qty"), 0.0), 3),
                "breakdown_value": _round(_num(m.get("breakdown_value"), 0.0)),
                "overhaul_qty": _round(_num(m.get("overhaul_qty"), 0.0), 3),
                "overhaul_value": _round(_num(m.get("overhaul_value"), 0.0)),
                "total_qty": _round(row_qty, 3),
                "total_value": _round(row_value),
                "line_count": _int(m.get("line_count"), 0),
            })

        cost_rows = db.execute(
            get_spares_machine_cost_query(), {**params, "limit": MAX_LIMIT}
        ).fetchall()
        machine_cost = []
        total_maintenance_cost = 0.0
        for row in cost_rows:
            m = dict(row._mapping)
            machine_total = _num(m.get("total_maintenance_cost"), 0.0) or 0.0
            total_maintenance_cost += machine_total
            machine_cost.append({
                "machine_id": m.get("machine_id"),
                "machine_name": m.get("machine_name"),
                "mech_code": m.get("mech_code"),
                "machine_type_id": m.get("machine_type_id"),
                "machine_type_name": m.get("machine_type_name"),
                "dept_id": m.get("dept_id"),
                "dept_name": m.get("dept_name"),
                "branch_id": m.get("branch_id"),
                "breakdown_spare_value": _round(_num(m.get("breakdown_spare_value"), 0.0)),
                "overhaul_spare_value": _round(_num(m.get("overhaul_spare_value"), 0.0)),
                "overhaul_labour_cost": _round(_num(m.get("overhaul_labour_cost"), 0.0)),
                "total_maintenance_cost": _round(machine_total),
            })

        total = _scalar_total(db, get_spares_consumption_count_query(), params)

        return {
            "data": data,
            "machine_cost": machine_cost,
            "total": total,
            "summary": {
                "page_total_qty": _round(total_qty, 3),
                "page_total_value": _round(total_value),
                "machine_count": len(machine_cost),
                "total_maintenance_cost": _round(total_maintenance_cost),
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech spares consumption: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 10. /top-problem-machines
# =============================================================================


def _top_machine_row(mapped, date_from, date_to):
    """Shared shaping for the two top-N blocks."""
    days = _days_in_range(
        date_from, date_to,
        mapped.get("first_breakdown_date"), mapped.get("last_breakdown_date"),
    )
    metrics = _metrics(
        mapped.get("breakdown_count"),
        mapped.get("total_downtime_min"),
        mapped.get("nr_downtime_min"),
        days,
    )
    grand_downtime = _num(mapped.get("grand_downtime_min"), 0.0)
    return {
        "machine_id": mapped.get("machine_id"),
        "machine_name": mapped.get("machine_name"),
        "mech_code": mapped.get("mech_code"),
        "machine_type_id": mapped.get("machine_type_id"),
        "machine_type_name": mapped.get("machine_type_name"),
        "dept_id": mapped.get("dept_id"),
        "dept_name": mapped.get("dept_name"),
        "branch_id": mapped.get("branch_id"),
        "criticality": mapped.get("criticality"),
        "breakdown_count": metrics["breakdown_count"],
        "total_downtime_min": metrics["total_downtime_min"],
        "mttr_min": metrics["mttr_min"],
        "mtbf_min": metrics["mtbf_min"],
        "availability_pct": metrics["availability_pct"],
        "nr_downtime_min": metrics["nr_downtime_min"],
        "unattributed_pct": metrics["unattributed_pct"],
        "pct_of_mill_downtime": _round(
            _safe_div((metrics["total_downtime_min"] or 0.0) * 100.0, grand_downtime)
        ),
        "first_breakdown_date": _iso(mapped.get("first_breakdown_date")),
        "last_breakdown_date": _iso(mapped.get("last_breakdown_date")),
    }


@router.get("/top-problem-machines")
def get_top_problem_machines(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 10000,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
    top_n: int | None = None,
):
    """Top-N machines by downtime and by count, plus the repeat-failure list.

    A repeat failure is the same machine_id + component_id failing again within
    REPEAT_FAILURE_WINDOW_DAYS (detected with LAG() over the incident sequence).
    It is the sharpest signal the module produces: it separates "we fixed it"
    from "we patched it". Incidents with no component tagged cannot be paired and
    are excluded rather than guessed at.
    """
    try:
        _require_co_id(co_id)
        _parse_date(date_from, "date_from")
        _parse_date(date_to, "date_to")

        top = _int(top_n, DEFAULT_TOP_N) or DEFAULT_TOP_N
        top = max(min(top, MAX_LIMIT), 1)

        common = _common_params(
            co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline
        )
        params = _failure_params(common)

        by_downtime_rows = db.execute(
            get_top_machines_by_downtime_query(), {**params, "top_n": top}
        ).fetchall()
        by_count_rows = db.execute(
            get_top_machines_by_count_query(), {**params, "top_n": top}
        ).fetchall()

        by_downtime = [
            _top_machine_row(dict(row._mapping), date_from, date_to) for row in by_downtime_rows
        ]
        by_count = [
            _top_machine_row(dict(row._mapping), date_from, date_to) for row in by_count_rows
        ]

        repeat_rows = db.execute(
            get_repeat_failures_query(),
            {
                **{**common, **FAILURE_TYPE_PARAMS},
                "repeat_window_days": REPEAT_FAILURE_WINDOW_DAYS,
                "top_n": max(top * 10, MAX_LIMIT // 10),
            },
        ).fetchall()

        repeat_failures = []
        for row in repeat_rows:
            m = dict(row._mapping)
            repeat_failures.append({
                "breakdown_id": m.get("breakdown_id"),
                "machine_id": m.get("machine_id"),
                "machine_name": m.get("machine_name"),
                "mech_code": m.get("mech_code"),
                "machine_type_id": m.get("machine_type_id"),
                "machine_type_name": m.get("machine_type_name"),
                "dept_id": m.get("dept_id"),
                "dept_name": m.get("dept_name"),
                "branch_id": m.get("branch_id"),
                "component_id": m.get("component_id"),
                "component_name": m.get("component_name"),
                "component_group": m.get("component_group"),
                "reason_id": m.get("reason_id"),
                "reason_code": m.get("reason_code"),
                "reason_desc": m.get("reason_desc"),
                "tran_date": _iso(m.get("tran_date")),
                "stop_datetime": _iso(m.get("stop_datetime")),
                "duration_min": _num(m.get("duration_min")),
                "prev_breakdown_id": m.get("prev_breakdown_id"),
                "prev_tran_date": _iso(m.get("prev_tran_date")),
                "prev_duration_min": _num(m.get("prev_duration_min")),
                "prev_reason_code": m.get("prev_reason_code"),
                "days_since_previous": _int(m.get("days_since_previous"), 0),
            })

        summary = _fetch_summary(db, common, date_from, date_to)
        summary.update({
            "top_n": top,
            "repeat_failure_window_days": REPEAT_FAILURE_WINDOW_DAYS,
            "repeat_failure_count": len(repeat_failures),
        })

        return {
            "data": {
                "by_downtime": by_downtime,
                "by_count": by_count,
                "repeat_failures": repeat_failures,
            },
            "total": len(by_downtime),
            "summary": summary,
            "unattributed_pct": summary.get("unattributed_pct"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech top problem machines: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 11. /machine-history
# =============================================================================


@router.get("/machine-history")
def get_machine_history(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 10000,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
):
    """Everything about one machine: breakdowns, PM executions, overhauls, spares.

    machine_id is required. Breakdowns are shown for ALL downtime types (this is
    the machine's history, not a failure analysis), while the `summary` metrics
    stay failure-only so they line up with every other report.
    """
    try:
        _require_co_id(co_id)
        if not machine_id:
            raise HTTPException(status_code=400, detail="machine_id is required")
        _parse_date(date_from, "date_from")
        _parse_date(date_to, "date_to")

        limit, offset = _paging(page, limit, 10000)
        common = _common_params(
            co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline
        )

        machine_row = db.execute(
            get_machine_history_machine_query(),
            {"co_id": int(co_id), "machine_id": int(machine_id)},
        ).fetchone()
        if machine_row is None:
            raise HTTPException(status_code=404, detail="Machine not found")
        mm = dict(machine_row._mapping)
        machine = {
            "machine_id": mm.get("machine_id"),
            "machine_name": mm.get("machine_name"),
            "mech_code": mm.get("mech_code"),
            "machine_type_id": mm.get("machine_type_id"),
            "machine_type_name": mm.get("machine_type_name"),
            "dept_id": mm.get("dept_id"),
            "dept_name": mm.get("dept_name"),
            "branch_id": mm.get("branch_id"),
            "machine_attr_id": mm.get("machine_attr_id"),
            "make": mm.get("make"),
            "model": mm.get("model"),
            "serial_no": mm.get("serial_no"),
            "commissioning_date": _iso(mm.get("commissioning_date")),
            "criticality": mm.get("criticality"),
            "last_overhaul_date": _iso(mm.get("last_overhaul_date")),
            "overhaul_freq_months": mm.get("overhaul_freq_months"),
            "next_overhaul_due": _iso(mm.get("next_overhaul_due")),
            "last_pm_date": _iso(mm.get("last_pm_date")),
            "pm_freq_days": mm.get("pm_freq_days"),
            "next_pm_due": _iso(mm.get("next_pm_due")),
            "remarks": mm.get("remarks"),
        }

        breakdown_rows = db.execute(
            get_machine_history_breakdowns_query(),
            {**common, "limit": limit, "offset": offset},
        ).fetchall()
        breakdowns = []
        for row in breakdown_rows:
            m = dict(row._mapping)
            breakdowns.append({
                "breakdown_id": m.get("breakdown_id"),
                "tran_date": _iso(m.get("tran_date")),
                "spell_id": m.get("spell_id"),
                "spell_code": m.get("spell_code"),
                "stop_datetime": _iso(m.get("stop_datetime")),
                "attended_datetime": _iso(m.get("attended_datetime")),
                "restart_datetime": _iso(m.get("restart_datetime")),
                "duration_min": _num(m.get("duration_min")),
                "response_min": _num(m.get("response_min")),
                "reason_id": m.get("reason_id"),
                "reason_code": m.get("reason_code"),
                "reason_desc": m.get("reason_desc"),
                "component_id": m.get("component_id"),
                "component_name": m.get("component_name"),
                "discipline": m.get("discipline"),
                "downtime_type": m.get("downtime_type"),
                "attended_dept_name": m.get("attended_dept_name"),
                "attended_by": m.get("attended_by"),
                "reported_by": m.get("reported_by"),
                "root_cause": m.get("root_cause"),
                "action_taken": m.get("action_taken"),
                "preventive_action": m.get("preventive_action"),
                "production_loss_qty": _num(m.get("production_loss_qty")),
                "status": m.get("status"),
                "remarks": m.get("remarks"),
            })

        history_params = {
            "co_id": int(co_id),
            "machine_id": int(machine_id),
            "branch_id": common["branch_id"],
            "date_from": common["date_from"],
            "date_to": common["date_to"],
            "limit": limit,
        }

        pm_rows = db.execute(get_machine_history_pm_query(), history_params).fetchall()
        pm_executions = []
        for row in pm_rows:
            m = dict(row._mapping)
            pm_executions.append({
                "pm_exec_id": m.get("pm_exec_id"),
                "pm_date": _iso(m.get("pm_date")),
                "frequency": m.get("frequency"),
                "done_by": m.get("done_by"),
                "downtime_min": _num(m.get("downtime_min")),
                "next_due_date": _iso(m.get("next_due_date")),
                "task_count": _int(m.get("task_count"), 0),
                "not_ok_count": _int(m.get("not_ok_count"), 0),
                "remarks": m.get("remarks"),
            })

        overhaul_rows = db.execute(
            get_machine_history_overhaul_query(), history_params
        ).fetchall()
        overhauls = []
        for row in overhaul_rows:
            m = dict(row._mapping)
            overhauls.append({
                "overhaul_id": m.get("overhaul_id"),
                "overhaul_type": m.get("overhaul_type"),
                "from_date": _iso(m.get("from_date")),
                "to_date": _iso(m.get("to_date")),
                "downtime_days": _num(m.get("downtime_days")),
                "work_done": m.get("work_done"),
                "done_by": m.get("done_by"),
                "party_id": m.get("party_id"),
                "party_name": m.get("party_name"),
                "labour_cost": _num(m.get("labour_cost")),
                "spare_cost": _num(m.get("spare_cost")),
                "total_cost": _num(m.get("total_cost")),
                "next_due_date": _iso(m.get("next_due_date")),
                "remarks": m.get("remarks"),
            })

        spare_rows = db.execute(
            get_machine_history_spares_query(),
            {k: v for k, v in history_params.items() if k != "branch_id"},
        ).fetchall()
        spares = []
        spare_value = 0.0
        for row in spare_rows:
            m = dict(row._mapping)
            amount = _num(m.get("amount"), 0.0) or 0.0
            spare_value += amount
            spares.append({
                "source": m.get("source"),
                "document_id": m.get("document_id"),
                "document_date": _iso(m.get("document_date")),
                "item_id": m.get("item_id"),
                "item_code": m.get("item_code"),
                "item_name": m.get("item_name"),
                "uom_name": m.get("uom_name"),
                "qty": _num(m.get("qty")),
                "rate": _num(m.get("rate")),
                "amount": _round(amount),
                "remarks": m.get("remarks"),
            })

        total = _scalar_total(db, get_machine_history_breakdown_count_query(), common)
        summary = _fetch_summary(db, common, date_from, date_to)
        summary.update({
            "breakdown_row_count": total,
            "pm_execution_count": len(pm_executions),
            "overhaul_count": len(overhauls),
            "spare_line_count": len(spares),
            "spare_value": _round(spare_value),
            "overhaul_cost": _round(
                sum((o.get("total_cost") or 0.0) for o in overhauls)
            ),
        })

        return {
            "data": {
                "machine": machine,
                "breakdowns": breakdowns,
                "pm_executions": pm_executions,
                "overhauls": overhauls,
                "spares": spares,
            },
            "total": total,
            "summary": summary,
            "unattributed_pct": summary.get("unattributed_pct"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech machine history: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# 12. /open-breakdown-board
# =============================================================================


@router.get("/open-breakdown-board")
def get_open_breakdown_board(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    page: int = 1,
    limit: int = 10000,
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    machine_type_id: int | None = None,
    machine_id: int | None = None,
    discipline: str | None = None,
):
    """Live board: incidents with no restart_datetime, elapsed minutes, longest first.

    Not failure-filtered — a machine stopped for a planned PM that has not
    restarted is still a stopped machine the board must show; `downtime_type`
    travels with each row so the UI can label it.
    """
    try:
        _require_co_id(co_id)
        _parse_date(date_from, "date_from")
        _parse_date(date_to, "date_to")

        limit, offset = _paging(page, limit, 10000)
        params = _common_params(
            co_id, branch_id, date_from, date_to, machine_type_id, machine_id, discipline
        )

        rows = db.execute(
            get_open_breakdown_board_query(), {**params, "limit": limit, "offset": offset}
        ).fetchall()

        data = []
        for row in rows:
            m = dict(row._mapping)
            elapsed = _num(m.get("elapsed_min"), 0.0) or 0.0
            data.append({
                "breakdown_id": m.get("breakdown_id"),
                "tran_date": _iso(m.get("tran_date")),
                "machine_id": m.get("machine_id"),
                "machine_name": m.get("machine_name"),
                "mech_code": m.get("mech_code"),
                "machine_type_id": m.get("machine_type_id"),
                "machine_type_name": m.get("machine_type_name"),
                "dept_id": m.get("dept_id"),
                "dept_name": m.get("dept_name"),
                "branch_id": m.get("branch_id"),
                "criticality": m.get("criticality"),
                "spell_id": m.get("spell_id"),
                "spell_code": m.get("spell_code"),
                "stop_datetime": _iso(m.get("stop_datetime")),
                "attended_datetime": _iso(m.get("attended_datetime")),
                "response_min": _num(m.get("response_min")),
                "reason_id": m.get("reason_id"),
                "reason_code": m.get("reason_code"),
                "reason_desc": m.get("reason_desc"),
                "component_id": m.get("component_id"),
                "component_name": m.get("component_name"),
                "discipline": m.get("discipline"),
                "downtime_type": m.get("downtime_type"),
                "attended_dept_name": m.get("attended_dept_name"),
                "attended_by": m.get("attended_by"),
                "reported_by": m.get("reported_by"),
                "reported_by_user_name": m.get("reported_by_user_name"),
                "reported_date_time": _iso(m.get("reported_date_time")),
                "status": m.get("status"),
                "remarks": m.get("remarks"),
                "elapsed_min": _round(elapsed),
                "elapsed_hrs": _round(elapsed / 60.0),
            })

        count_row = db.execute(get_open_breakdown_board_count_query(), params).fetchone()
        counts = dict(count_row._mapping) if count_row else {}
        open_count = _int(counts.get("total"), 0)
        total_elapsed = _num(counts.get("total_elapsed_min"), 0.0) or 0.0

        return {
            "data": data,
            "total": open_count,
            "summary": {
                "open_count": open_count,
                "total_elapsed_min": _round(total_elapsed),
                "max_elapsed_min": _round(_num(counts.get("max_elapsed_min"), 0.0)),
                "avg_elapsed_min": _round(_safe_div(total_elapsed, open_count)),
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mech open breakdown board: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

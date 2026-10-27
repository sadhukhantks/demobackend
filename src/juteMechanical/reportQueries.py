"""Raw SQL builders for the Jute Mechanical & Maintenance report endpoints.

These queries replace the mill's hand-computed "MILL SIDE MECHANICAL BREAK DOWN
SUMMARY" workbook. The metric definitions are the consultant value of the module,
so they are applied identically in every builder here:

    Only downtime_type IN (FAILURE_DOWNTIME_TYPES) AND active = 1 counts as a
    failure. Planned PM, overhaul, no-material, no-power and changeover downtime
    are downtime but NOT failures -- counting them would inflate breakdown counts
    and wreck MTBF.

    breakdown_count = COUNT(*)
    total_downtime  = SUM(duration_min)
    MTTR (min)      = SUM(duration_min) / COUNT(*)        -- "duration per breakdown"
    available_min   = days_in_range * 1440
    MTBF (min)      = (available_min - SUM(duration_min)) / COUNT(*)
    availability_%  = (available_min - SUM(duration_min)) / available_min * 100
    unattributed_%  = downtime on reason_code 'NR' / total downtime * 100

SQL returns the raw counts/sums; every ratio is derived in Python (reports.py) so
a zero denominator yields None instead of a SQL error -- the house reports
convention (see src/inventory/reportQueries.py + reports.py).

Masters are REUSED, never duplicated. machine_mst carries no co_id and no
branch_id, so branch is always derived through the canonical join
(src/juteProduction/stoppage_query.py):

    FROM machine_mst m
    INNER JOIN dept_mst d ON d.dept_id = m.dept_id      -- d.branch_id
    LEFT  JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id

dept_mst has NO active column; machine_type_mst and machine_mst do.
"""

from sqlalchemy import text

from src.juteMechanical.constants import FAILURE_DOWNTIME_TYPES, NR_REASON_CODE

# ---------------------------------------------------------------------------
# Shared literals
# ---------------------------------------------------------------------------

# NR_REASON_CODE ("No Reason mentioned") is imported from constants.py alongside
# the other analytics tuning values; re-exported here because every query module
# in this package reads it from its own namespace.

# The failure downtime types are bound as named params (never interpolated) so
# the SQL keeps the house binding discipline.
_FAILURE_BIND_NAMES = tuple(
    f"failure_type_{i}" for i in range(len(FAILURE_DOWNTIME_TYPES))
)
_FAILURE_BINDS_SQL = ", ".join(f":{name}" for name in _FAILURE_BIND_NAMES)

#: Params every failure-filtered query must be executed with.
FAILURE_TYPE_PARAMS = {
    name: value for name, value in zip(_FAILURE_BIND_NAMES, FAILURE_DOWNTIME_TYPES)
}


# ---------------------------------------------------------------------------
# Shared fragments
# ---------------------------------------------------------------------------


def _bd_source(sfx: str = "", failure_only: bool = True, extra_where: str = "") -> str:
    """FROM + WHERE for the breakdown log with the common report filters.

    ``sfx`` suffixes every alias (b/m/d/mt/r/c) so the fragment can be nested
    inside a derived table alongside an outer copy of itself without clashing.

    ``failure_only`` applies the FAILURE_DOWNTIME_TYPES gate -- on for every
    analytic report, off only for the raw register and the live open board.
    """
    b, m, d, mt, r, c = (f"{a}{sfx}" for a in ("b", "m", "d", "mt", "r", "c"))
    failure_clause = (
        f"          AND {b}.downtime_type IN ({_FAILURE_BINDS_SQL})\n"
        if failure_only
        else ""
    )
    return f"""
        FROM jute_mech_breakdown {b}
        INNER JOIN machine_mst {m} ON {m}.machine_id = {b}.machine_id
        INNER JOIN dept_mst {d} ON {d}.dept_id = {m}.dept_id
        LEFT JOIN machine_type_mst {mt} ON {mt}.machine_type_id = {m}.machine_type_id
        LEFT JOIN jute_mech_reason_mst {r} ON {r}.reason_id = {b}.reason_id
        LEFT JOIN jute_mech_component_mst {c} ON {c}.component_id = {b}.component_id
        WHERE {b}.active = 1
          AND {b}.co_id = :co_id
          AND (:branch_id IS NULL OR COALESCE({b}.branch_id, {d}.branch_id) = :branch_id)
          AND (:date_from IS NULL OR {b}.tran_date >= :date_from)
          AND (:date_to IS NULL OR {b}.tran_date <= :date_to)
          AND (:machine_type_id IS NULL OR {m}.machine_type_id = :machine_type_id)
          AND (:machine_id IS NULL OR {b}.machine_id = :machine_id)
          AND (:discipline IS NULL OR {b}.discipline = :discipline)
{failure_clause}{extra_where}"""


def _with_extra_joins(sql: str, joins: str) -> str:
    """Splice extra display joins in just before the WHERE of a base source block.

    Keeps _bd_source() as the single definition of the filter set: a report that
    needs spell / sub-dept / machine-attr columns adds only its joins, never a
    second copy of the WHERE clause that could drift.
    """
    return sql.replace("        WHERE b.active = 1", joins + "        WHERE b.active = 1", 1)


# Display joins used by the row-level reports (the aggregates do not need them).
_SPELL_JOIN = "        LEFT JOIN spell_mst sp ON sp.spell_id = b.spell_id\n"
_SUB_DEPT_JOIN = (
    "        LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = b.attended_dept_id\n"
)
# The machine-attribute satellite is optional (a machine may have no attr row
# yet), hence LEFT JOIN scoped by :co_id.
_ATTR_JOIN = (
    "        LEFT JOIN jute_mech_machine_attr a\n"
    "            ON a.machine_id = m.machine_id AND a.co_id = :co_id AND a.active = 1\n"
)


# Who reported / fixed / reconfirmed an incident (register + live board).
_USER_JOIN = (
    "        LEFT JOIN user_mst ru ON ru.user_id = b.reported_by_user_id\n"
    "        LEFT JOIN user_mst fu ON fu.user_id = b.fixed_by_user_id\n"
    "        LEFT JOIN user_mst pu ON pu.user_id = b.prod_confirm_by\n"
)


# NR downtime / count expressions, reused by every summary-style builder.
_NR_DOWNTIME = (
    "COALESCE(SUM(CASE WHEN r.reason_code = :nr_reason_code "
    "THEN COALESCE(b.duration_min, 0) ELSE 0 END), 0)"
)
_NR_COUNT = (
    "COALESCE(SUM(CASE WHEN r.reason_code = :nr_reason_code THEN 1 ELSE 0 END), 0)"
)

# Still-open incidents. Same definition as _OPEN_EXTRA_WHERE below (never restarted
# AND no hand-entered duration), kept in one place so the two cannot drift.
_OPEN_COUNT = (
    "COALESCE(SUM(CASE WHEN b.restart_datetime IS NULL AND b.duration_min IS NULL "
    "THEN 1 ELSE 0 END), 0)"
)


# =============================================================================
# 0. Shared metrics block (drives `summary` + `unattributed_pct` everywhere)
# =============================================================================


def get_breakdown_metrics_query():
    """One-row failure metrics over the filtered range.

    Params: :co_id, :branch_id, :date_from, :date_to, :machine_type_id,
    :machine_id, :discipline, :nr_reason_code + FAILURE_TYPE_PARAMS.
    """
    sql = f"""
    SELECT
        COUNT(*) AS breakdown_count,
        COALESCE(SUM(b.duration_min), 0) AS total_downtime_min,
        {_NR_DOWNTIME} AS nr_downtime_min,
        {_NR_COUNT} AS nr_count,
        COUNT(DISTINCT b.machine_id) AS machine_count,
        MIN(b.tran_date) AS first_breakdown_date,
        MAX(b.tran_date) AS last_breakdown_date,
        COALESCE(SUM(b.response_min), 0) AS total_response_min,
        COALESCE(SUM(CASE WHEN b.response_min IS NOT NULL THEN 1 ELSE 0 END), 0)
            AS response_count,
        {_OPEN_COUNT} AS open_count,
        COALESCE(SUM(b.production_loss_qty), 0) AS production_loss_qty
    {_bd_source()}
    """
    return text(sql)


# =============================================================================
# 1. /breakdown-register -- raw filterable incident log
# =============================================================================

_REGISTER_EXTRA_WHERE = """          AND (:downtime_type IS NULL OR b.downtime_type = :downtime_type)
          AND (:status IS NULL OR b.status = :status)
          AND (
              :search_like IS NULL
              OR m.machine_name LIKE :search_like
              OR m.mech_code LIKE :search_like
              OR r.reason_code LIKE :search_like
              OR r.reason_desc LIKE :search_like
              OR b.attended_by LIKE :search_like
              OR b.reported_by LIKE :search_like
              OR b.remarks LIKE :search_like
          )
"""


def get_breakdown_register_query():
    """Raw incident log, newest stop first.

    NOT failure-filtered: the register is the audit trail and must show planned
    PM / no-material / changeover rows too (each row carries downtime_type so the
    grid can label them). Only the analytic reports gate on BREAKDOWN.

    Params: common filters + :downtime_type, :status, :search_like, :limit, :offset.
    """
    sql = f"""
    SELECT
        b.breakdown_id,
        b.tran_date,
        b.machine_id,
        m.machine_name,
        m.mech_code,
        m.machine_type_id,
        mt.machine_type_name,
        d.dept_id,
        d.dept_desc AS dept_name,
        COALESCE(b.branch_id, d.branch_id) AS branch_id,
        b.spell_id,
        sp.spell_code,
        b.stop_datetime,
        b.attended_datetime,
        b.restart_datetime,
        b.duration_min,
        b.response_min,
        b.reason_id,
        r.reason_code,
        r.reason_desc,
        b.component_id,
        c.component_name,
        c.component_group,
        b.discipline,
        b.downtime_type,
        b.attended_dept_id,
        sd.sub_dept_desc AS attended_dept_name,
        b.attended_by,
        b.reported_by,
        b.root_cause,
        b.action_taken,
        b.preventive_action,
        b.production_loss_qty,
        b.status,
        b.remarks,
        ru.name AS reported_by_user_name,
        b.reported_date_time,
        fu.name AS fixed_by_user_name,
        b.fixed_date_time,
        b.prod_confirm_status,
        pu.name AS prod_confirm_by_name,
        b.prod_confirm_date_time,
        b.prod_restart_datetime,
        b.prod_confirm_remarks
    {_bd_source(failure_only=False, extra_where=_REGISTER_EXTRA_WHERE)}
    ORDER BY b.stop_datetime DESC, b.breakdown_id DESC
    LIMIT :limit OFFSET :offset
    """
    return text(_with_extra_joins(sql, _SUB_DEPT_JOIN + _SPELL_JOIN + _USER_JOIN))


def get_breakdown_register_count_query():
    """Row count behind get_breakdown_register_query (same filters, no paging)."""
    sql = f"""
    SELECT COUNT(*) AS total
    {_bd_source(failure_only=False, extra_where=_REGISTER_EXTRA_WHERE)}
    """
    return text(sql)


# =============================================================================
# 2. /machine-summary + 6. /reliability -- per machine aggregates
# =============================================================================


def _machine_agg_sql(order_by: str, paged: bool = True) -> str:
    """Per-machine failure aggregate. Shared by machine-summary and reliability.

    The two reports differ only in the derived fields Python computes on top and
    in ordering, so they read the same aggregate rather than drifting apart.
    """
    paging = "    LIMIT :limit OFFSET :offset" if paged else ""
    return f"""
    SELECT
        m.machine_id,
        m.machine_name,
        m.mech_code,
        m.machine_type_id,
        mt.machine_type_name,
        d.dept_id,
        d.dept_desc AS dept_name,
        d.branch_id,
        a.criticality,
        COUNT(*) AS breakdown_count,
        COALESCE(SUM(b.duration_min), 0) AS total_downtime_min,
        {_NR_DOWNTIME} AS nr_downtime_min,
        {_NR_COUNT} AS nr_count,
        COALESCE(SUM(b.response_min), 0) AS total_response_min,
        COALESCE(SUM(CASE WHEN b.response_min IS NOT NULL THEN 1 ELSE 0 END), 0)
            AS response_count,
        {_OPEN_COUNT} AS open_count,
        COALESCE(SUM(b.production_loss_qty), 0) AS production_loss_qty,
        MIN(b.tran_date) AS first_breakdown_date,
        MAX(b.tran_date) AS last_breakdown_date,
        SUM(COALESCE(SUM(b.duration_min), 0)) OVER () AS grand_downtime_min,
        SUM(COUNT(*)) OVER () AS grand_breakdown_count
    {_bd_source()}
    GROUP BY
        m.machine_id, m.machine_name, m.mech_code, m.machine_type_id,
        mt.machine_type_name, d.dept_id, d.dept_desc, d.branch_id, a.criticality
    ORDER BY {order_by}
{paging}
    """


def _with_attr_join(sql: str) -> str:
    """Insert the machine_attr LEFT JOIN (criticality) into a base source block."""
    return _with_extra_joins(sql, _ATTR_JOIN)


def get_machine_summary_query():
    """Per machine: breakdown count, total downtime, MTTR -- the workbook's main sheet.

    Params: common filters + :nr_reason_code, FAILURE_TYPE_PARAMS, :limit, :offset.
    """
    return text(_with_attr_join(_machine_agg_sql("total_downtime_min DESC, breakdown_count DESC, m.machine_name")))


def get_machine_summary_count_query():
    """Distinct machines with at least one failure in range."""
    sql = f"""
    SELECT COUNT(DISTINCT b.machine_id) AS total
    {_bd_source()}
    """
    return text(sql)


def get_reliability_query():
    """Per machine reliability: feeds MTBF / MTTR / availability / downtime per 1000 hrs."""
    return text(_with_attr_join(_machine_agg_sql("total_downtime_min DESC, m.machine_name")))


def get_reliability_count_query():
    """Distinct machines behind get_reliability_query."""
    return get_machine_summary_count_query()


# =============================================================================
# 3. /reason-pareto -- the workbook's reason block, made a real Pareto
# =============================================================================


def get_reason_pareto_query():
    """Per reason: count, total duration + running cumulative, ordered duration DESC.

    cum_downtime_min / grand_downtime_min come from window functions so the
    cumulative % stays correct on page 2 onwards (a Python-side running total
    would restart at each page). Python turns them into pct_share and
    cumulative_pct.

    Params: common filters + FAILURE_TYPE_PARAMS, :limit, :offset.
    """
    sql = f"""
    SELECT
        b.reason_id,
        r.reason_code,
        r.reason_desc,
        r.discipline,
        b.component_id,
        c.component_name,
        COUNT(*) AS breakdown_count,
        COALESCE(SUM(b.duration_min), 0) AS total_downtime_min,
        SUM(COALESCE(SUM(b.duration_min), 0)) OVER (
            ORDER BY COALESCE(SUM(b.duration_min), 0) DESC, b.reason_id
        ) AS cum_downtime_min,
        SUM(COALESCE(SUM(b.duration_min), 0)) OVER () AS grand_downtime_min,
        SUM(COUNT(*)) OVER () AS grand_breakdown_count,
        COUNT(DISTINCT b.machine_id) AS machine_count
    {_bd_source()}
    GROUP BY
        b.reason_id, r.reason_code, r.reason_desc, r.discipline,
        b.component_id, c.component_name
    ORDER BY total_downtime_min DESC, b.reason_id
    LIMIT :limit OFFSET :offset
    """
    return text(sql)


def get_reason_pareto_count_query():
    """Distinct reason codes behind the Pareto."""
    sql = f"""
    SELECT COUNT(DISTINCT b.reason_id) AS total
    {_bd_source()}
    """
    return text(sql)


# =============================================================================
# 4. /date-machine-grid -- exact replica of the sheet the mill keys today
# =============================================================================

# The paged window of dates. Paging must be on the DATE axis (one grid row per
# date), so the cell query joins to this derived table rather than limiting cells.
_GRID_PAGED_DATES = f"""
        INNER JOIN (
            SELECT DISTINCT b2.tran_date
            {_bd_source(sfx='2')}
            ORDER BY b2.tran_date DESC
            LIMIT :limit OFFSET :offset
        ) pd ON pd.tran_date = b.tran_date
"""


def get_date_machine_grid_query():
    """date x machine cells: reason codes joined with '/'.

    Multiple incidents on one machine on one date are GROUP_CONCATed in stop
    order with '/' -- exactly how the mill writes them into a single Excel cell
    today ("RF/DRJ", "DRJ/PB/BJ").

    Params: common filters + FAILURE_TYPE_PARAMS, :limit, :offset.
    """
    source = _bd_source()
    source = source.replace(
        "        WHERE b.active = 1", _GRID_PAGED_DATES + "        WHERE b.active = 1", 1
    )
    sql = f"""
    SELECT
        b.tran_date,
        b.machine_id,
        GROUP_CONCAT(r.reason_code ORDER BY b.stop_datetime SEPARATOR '/') AS reason_codes,
        COUNT(*) AS breakdown_count,
        COALESCE(SUM(b.duration_min), 0) AS total_downtime_min
    {source}
    GROUP BY b.tran_date, b.machine_id
    ORDER BY b.tran_date DESC, b.machine_id
    """
    return text(sql)


def get_date_machine_grid_columns_query():
    """Machines that appear anywhere in the filtered range -- the grid's columns."""
    sql = f"""
    SELECT DISTINCT
        m.machine_id,
        m.machine_name,
        m.mech_code,
        m.machine_type_id,
        mt.machine_type_name
    {_bd_source()}
    ORDER BY mt.machine_type_name, m.machine_name, m.machine_id
    """
    return text(sql)


def get_date_machine_grid_count_query():
    """Distinct dates in range -- the grid's row count."""
    sql = f"""
    SELECT COUNT(DISTINCT b.tran_date) AS total
    {_bd_source()}
    """
    return text(sql)


# =============================================================================
# 5. /department-summary -- dept + machine type share of mill downtime
# =============================================================================


def get_department_summary_query():
    """Per department + machine type: count, duration, machines, share of mill downtime."""
    sql = f"""
    SELECT
        d.dept_id,
        d.dept_desc AS dept_name,
        d.branch_id,
        m.machine_type_id,
        mt.machine_type_name,
        COUNT(*) AS breakdown_count,
        COALESCE(SUM(b.duration_min), 0) AS total_downtime_min,
        {_NR_DOWNTIME} AS nr_downtime_min,
        {_NR_COUNT} AS nr_count,
        COUNT(DISTINCT b.machine_id) AS machine_count,
        COALESCE(SUM(b.production_loss_qty), 0) AS production_loss_qty,
        SUM(COALESCE(SUM(b.duration_min), 0)) OVER () AS grand_downtime_min,
        SUM(COUNT(*)) OVER () AS grand_breakdown_count
    {_bd_source()}
    GROUP BY d.dept_id, d.dept_desc, d.branch_id, m.machine_type_id, mt.machine_type_name
    ORDER BY total_downtime_min DESC, d.dept_desc, mt.machine_type_name
    LIMIT :limit OFFSET :offset
    """
    return text(sql)


def get_department_summary_count_query():
    """Distinct dept x machine-type groups behind the department summary."""
    sql = f"""
    SELECT COUNT(*) AS total FROM (
        SELECT d.dept_id, m.machine_type_id
        {_bd_source()}
        GROUP BY d.dept_id, m.machine_type_id
    ) grp
    """
    return text(sql)


# =============================================================================
# 7. /reason-trend -- month x reason (the workbook's Summary sheets)
# =============================================================================


def get_reason_trend_query():
    """Month x reason counts and durations for the paged window of reasons.

    Reasons are ranked by total downtime and paged in a derived table, then all of
    their monthly rows are returned -- pivoting in Python needs every month of a
    reason, so paging cannot be applied to the long rows themselves.

    Params: common filters + FAILURE_TYPE_PARAMS, :limit, :offset.
    """
    ranked = f"""
        INNER JOIN (
            SELECT b2.reason_id
            {_bd_source(sfx='2')}
            GROUP BY b2.reason_id
            ORDER BY COALESCE(SUM(b2.duration_min), 0) DESC, b2.reason_id
            LIMIT :limit OFFSET :offset
        ) pr ON pr.reason_id = b.reason_id
"""
    source = _bd_source().replace(
        "        WHERE b.active = 1", ranked + "        WHERE b.active = 1", 1
    )
    sql = f"""
    SELECT
        DATE_FORMAT(b.tran_date, '%Y-%m') AS ym,
        b.reason_id,
        r.reason_code,
        r.reason_desc,
        r.discipline,
        c.component_name,
        COUNT(*) AS breakdown_count,
        COALESCE(SUM(b.duration_min), 0) AS total_downtime_min
    {source}
    GROUP BY
        DATE_FORMAT(b.tran_date, '%Y-%m'), b.reason_id, r.reason_code,
        r.reason_desc, r.discipline, c.component_name
    ORDER BY r.reason_code, ym
    """
    return text(sql)


def get_reason_trend_count_query():
    """Distinct reasons behind the trend pivot."""
    sql = f"""
    SELECT COUNT(DISTINCT b.reason_id) AS total
    {_bd_source()}
    """
    return text(sql)


# =============================================================================
# 8. /maintenance-due -- PM + overhaul due / overdue
# =============================================================================

_DUE_MACHINE_JOINS = """
        INNER JOIN machine_mst m ON m.machine_id = a.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
"""

_DUE_FILTERS = """
          AND a.active = 1
          AND a.co_id = :co_id
          AND m.active = 1
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
          AND (:machine_type_id IS NULL OR m.machine_type_id = :machine_type_id)
          AND (:machine_id IS NULL OR a.machine_id = :machine_id)
"""

# One row per DUE ITEM (a machine can appear twice: PM due and overhaul due).
_DUE_UNION = f"""
        SELECT
            'PM' AS due_type,
            a.machine_attr_id,
            a.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            d.dept_id,
            d.dept_desc AS dept_name,
            d.branch_id,
            a.criticality,
            a.next_pm_due AS due_date,
            a.last_pm_date AS last_done_date,
            a.pm_freq_days AS freq_days,
            NULL AS freq_months,
            a.make,
            a.model
        FROM jute_mech_machine_attr a
        {_DUE_MACHINE_JOINS}
        WHERE a.next_pm_due IS NOT NULL
          AND a.next_pm_due <= :due_cutoff
        {_DUE_FILTERS}
        UNION ALL
        SELECT
            'OVERHAUL' AS due_type,
            a.machine_attr_id,
            a.machine_id,
            m.machine_name,
            m.mech_code,
            m.machine_type_id,
            mt.machine_type_name,
            d.dept_id,
            d.dept_desc AS dept_name,
            d.branch_id,
            a.criticality,
            a.next_overhaul_due AS due_date,
            a.last_overhaul_date AS last_done_date,
            NULL AS freq_days,
            a.overhaul_freq_months AS freq_months,
            a.make,
            a.model
        FROM jute_mech_machine_attr a
        {_DUE_MACHINE_JOINS}
        WHERE a.next_overhaul_due IS NOT NULL
          AND a.next_overhaul_due <= :due_cutoff
        {_DUE_FILTERS}
"""


def get_maintenance_due_query():
    """PM + overhaul due/overdue rows, criticality A first then most overdue first.

    due_date ASC is days_overdue DESC -- the same ordering, expressed on the
    column the DB can index.

    Params: :co_id, :branch_id, :machine_type_id, :machine_id, :due_cutoff,
    :limit, :offset.
    """
    sql = f"""
    SELECT due.* FROM (
{_DUE_UNION}
    ) due
    ORDER BY
        CASE due.criticality
            WHEN 'A' THEN 1 WHEN 'B' THEN 2 WHEN 'C' THEN 3 ELSE 4
        END,
        due.due_date ASC,
        due.machine_name
    LIMIT :limit OFFSET :offset
    """
    return text(sql)


def get_maintenance_due_count_query():
    """Row count behind get_maintenance_due_query."""
    sql = f"""
    SELECT COUNT(*) AS total FROM (
{_DUE_UNION}
    ) due
    """
    return text(sql)


# =============================================================================
# 9. /spares-consumption -- spares qty/value + maintenance cost per machine
# =============================================================================

# Breakdown spares are discipline-filterable (the breakdown row carries the
# discipline); overhaul spares are not -- an overhaul has no discipline column,
# so it stays in the picture whatever the discipline filter says.
_SPARE_UNION = """
        SELECT
            b.machine_id AS machine_id,
            s.item_id AS item_id,
            'BREAKDOWN' AS source,
            COALESCE(s.qty, 0) AS qty,
            COALESCE(s.amount, 0) AS amount
        FROM jute_mech_breakdown_spare s
        INNER JOIN jute_mech_breakdown b ON b.breakdown_id = s.breakdown_id
        INNER JOIN machine_mst m ON m.machine_id = b.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE s.active = 1
          AND b.active = 1
          AND b.co_id = :co_id
          AND (:branch_id IS NULL OR COALESCE(b.branch_id, d.branch_id) = :branch_id)
          AND (:date_from IS NULL OR b.tran_date >= :date_from)
          AND (:date_to IS NULL OR b.tran_date <= :date_to)
          AND (:machine_type_id IS NULL OR m.machine_type_id = :machine_type_id)
          AND (:machine_id IS NULL OR b.machine_id = :machine_id)
          AND (:discipline IS NULL OR b.discipline = :discipline)
        UNION ALL
        SELECT
            o.machine_id AS machine_id,
            s.item_id AS item_id,
            'OVERHAUL' AS source,
            COALESCE(s.qty, 0) AS qty,
            COALESCE(s.amount, 0) AS amount
        FROM jute_mech_overhaul_spare s
        INNER JOIN jute_mech_overhaul o ON o.overhaul_id = s.overhaul_id
        INNER JOIN machine_mst m ON m.machine_id = o.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE s.active = 1
          AND o.active = 1
          AND o.co_id = :co_id
          AND (:branch_id IS NULL OR COALESCE(o.branch_id, d.branch_id) = :branch_id)
          AND (:date_from IS NULL OR o.from_date >= :date_from)
          AND (:date_to IS NULL OR o.from_date <= :date_to)
          AND (:machine_type_id IS NULL OR m.machine_type_id = :machine_type_id)
          AND (:machine_id IS NULL OR o.machine_id = :machine_id)
"""

_SPARE_CTE = f"""
    WITH sp AS (
{_SPARE_UNION}
    )"""

# The grouped body is kept separate from the CTE so the count query can hoist
# the CTE to the top level instead of nesting it inside a derived table.
_SPARE_GROUPED_BODY = """
    SELECT
        sp.machine_id,
        m.machine_name,
        m.mech_code,
        m.machine_type_id,
        mt.machine_type_name,
        d.dept_id,
        d.dept_desc AS dept_name,
        d.branch_id,
        sp.item_id,
        i.item_code,
        i.item_name,
        u.uom_name,
        COALESCE(SUM(CASE WHEN sp.source = 'BREAKDOWN' THEN sp.qty ELSE 0 END), 0)
            AS breakdown_qty,
        COALESCE(SUM(CASE WHEN sp.source = 'BREAKDOWN' THEN sp.amount ELSE 0 END), 0)
            AS breakdown_value,
        COALESCE(SUM(CASE WHEN sp.source = 'OVERHAUL' THEN sp.qty ELSE 0 END), 0)
            AS overhaul_qty,
        COALESCE(SUM(CASE WHEN sp.source = 'OVERHAUL' THEN sp.amount ELSE 0 END), 0)
            AS overhaul_value,
        COALESCE(SUM(sp.qty), 0) AS total_qty,
        COALESCE(SUM(sp.amount), 0) AS total_value,
        COUNT(*) AS line_count
    FROM sp
    INNER JOIN machine_mst m ON m.machine_id = sp.machine_id
    INNER JOIN dept_mst d ON d.dept_id = m.dept_id
    LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
    LEFT JOIN item_mst i ON i.item_id = sp.item_id
    LEFT JOIN uom_mst u ON u.uom_id = i.uom_id
    GROUP BY
        sp.machine_id, m.machine_name, m.mech_code, m.machine_type_id,
        mt.machine_type_name, d.dept_id, d.dept_desc, d.branch_id,
        sp.item_id, i.item_code, i.item_name, u.uom_name
"""


def get_spares_consumption_query():
    """Per machine + item: qty and value, split breakdown vs overhaul.

    Params: :co_id, :branch_id, :date_from, :date_to, :machine_type_id,
    :machine_id, :discipline, :limit, :offset.
    """
    sql = f"""
{_SPARE_CTE}
{_SPARE_GROUPED_BODY}
    ORDER BY total_value DESC, m.machine_name, i.item_name
    LIMIT :limit OFFSET :offset
    """
    return text(sql)


def get_spares_consumption_count_query():
    """Machine x item groups behind get_spares_consumption_query."""
    sql = f"""
{_SPARE_CTE}
    SELECT COUNT(*) AS total FROM (
{_SPARE_GROUPED_BODY}
    ) grp
    """
    return text(sql)


def get_spares_machine_cost_query():
    """Maintenance cost per machine: breakdown spares + overhaul spares + overhaul labour.

    Labour is folded in as a third synthetic cost row so "cost per machine" is the
    real maintenance spend, not just the parts bill.

    Params: same as get_spares_consumption_query plus :limit (no offset -- this is
    a companion block to the paged item rows).
    """
    sql = f"""
    WITH cost AS (
        SELECT
            b.machine_id AS machine_id,
            'BREAKDOWN_SPARE' AS cost_type,
            COALESCE(s.amount, 0) AS amount
        FROM jute_mech_breakdown_spare s
        INNER JOIN jute_mech_breakdown b ON b.breakdown_id = s.breakdown_id
        INNER JOIN machine_mst m ON m.machine_id = b.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE s.active = 1
          AND b.active = 1
          AND b.co_id = :co_id
          AND (:branch_id IS NULL OR COALESCE(b.branch_id, d.branch_id) = :branch_id)
          AND (:date_from IS NULL OR b.tran_date >= :date_from)
          AND (:date_to IS NULL OR b.tran_date <= :date_to)
          AND (:machine_type_id IS NULL OR m.machine_type_id = :machine_type_id)
          AND (:machine_id IS NULL OR b.machine_id = :machine_id)
          AND (:discipline IS NULL OR b.discipline = :discipline)
        UNION ALL
        SELECT
            o.machine_id AS machine_id,
            'OVERHAUL_SPARE' AS cost_type,
            COALESCE(s.amount, 0) AS amount
        FROM jute_mech_overhaul_spare s
        INNER JOIN jute_mech_overhaul o ON o.overhaul_id = s.overhaul_id
        INNER JOIN machine_mst m ON m.machine_id = o.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE s.active = 1
          AND o.active = 1
          AND o.co_id = :co_id
          AND (:branch_id IS NULL OR COALESCE(o.branch_id, d.branch_id) = :branch_id)
          AND (:date_from IS NULL OR o.from_date >= :date_from)
          AND (:date_to IS NULL OR o.from_date <= :date_to)
          AND (:machine_type_id IS NULL OR m.machine_type_id = :machine_type_id)
          AND (:machine_id IS NULL OR o.machine_id = :machine_id)
        UNION ALL
        SELECT
            o.machine_id AS machine_id,
            'OVERHAUL_LABOUR' AS cost_type,
            COALESCE(o.labour_cost, 0) AS amount
        FROM jute_mech_overhaul o
        INNER JOIN machine_mst m ON m.machine_id = o.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE o.active = 1
          AND o.co_id = :co_id
          AND (:branch_id IS NULL OR COALESCE(o.branch_id, d.branch_id) = :branch_id)
          AND (:date_from IS NULL OR o.from_date >= :date_from)
          AND (:date_to IS NULL OR o.from_date <= :date_to)
          AND (:machine_type_id IS NULL OR m.machine_type_id = :machine_type_id)
          AND (:machine_id IS NULL OR o.machine_id = :machine_id)
    )
    SELECT
        cost.machine_id,
        m.machine_name,
        m.mech_code,
        m.machine_type_id,
        mt.machine_type_name,
        d.dept_id,
        d.dept_desc AS dept_name,
        d.branch_id,
        COALESCE(SUM(CASE WHEN cost.cost_type = 'BREAKDOWN_SPARE'
                          THEN cost.amount ELSE 0 END), 0) AS breakdown_spare_value,
        COALESCE(SUM(CASE WHEN cost.cost_type = 'OVERHAUL_SPARE'
                          THEN cost.amount ELSE 0 END), 0) AS overhaul_spare_value,
        COALESCE(SUM(CASE WHEN cost.cost_type = 'OVERHAUL_LABOUR'
                          THEN cost.amount ELSE 0 END), 0) AS overhaul_labour_cost,
        COALESCE(SUM(cost.amount), 0) AS total_maintenance_cost
    FROM cost
    INNER JOIN machine_mst m ON m.machine_id = cost.machine_id
    INNER JOIN dept_mst d ON d.dept_id = m.dept_id
    LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
    GROUP BY
        cost.machine_id, m.machine_name, m.mech_code, m.machine_type_id,
        mt.machine_type_name, d.dept_id, d.dept_desc, d.branch_id
    ORDER BY total_maintenance_cost DESC, m.machine_name
    LIMIT :limit
    """
    return text(sql)


# =============================================================================
# 10. /top-problem-machines -- worst offenders + repeat failures
# =============================================================================


def get_top_machines_by_downtime_query():
    """Top-N machines by total downtime. Params: common + FAILURE_TYPE_PARAMS, :top_n."""
    return text(
        _with_attr_join(
            _machine_agg_sql("total_downtime_min DESC, breakdown_count DESC, m.machine_name", paged=False)
        )
        + "    LIMIT :top_n"
    )


def get_top_machines_by_count_query():
    """Top-N machines by breakdown count. Params: common + FAILURE_TYPE_PARAMS, :top_n."""
    return text(
        _with_attr_join(
            _machine_agg_sql("breakdown_count DESC, total_downtime_min DESC, m.machine_name", paged=False)
        )
        + "    LIMIT :top_n"
    )


def get_repeat_failures_query():
    """Same machine + component failing again within REPEAT_FAILURE_WINDOW_DAYS.

    The single most useful signal for telling "we fixed it" from "we patched it".
    LAG() over (machine_id, component_id) ordered by stop time gives each incident
    its predecessor on the same component; rows whose gap is within the window are
    the repeats. Incidents with no component tagged cannot be paired, so they are
    excluded rather than guessed at.

    Params: common filters + FAILURE_TYPE_PARAMS, :repeat_window_days, :top_n.
    """
    source = _bd_source(
        sfx="2", extra_where="          AND b2.component_id IS NOT NULL\n"
    )
    sql = f"""
    WITH seq AS (
        SELECT
            b2.breakdown_id,
            b2.machine_id,
            b2.component_id,
            b2.reason_id,
            b2.tran_date,
            b2.stop_datetime,
            b2.duration_min,
            LAG(b2.breakdown_id) OVER (
                PARTITION BY b2.machine_id, b2.component_id ORDER BY b2.stop_datetime
            ) AS prev_breakdown_id,
            LAG(b2.tran_date) OVER (
                PARTITION BY b2.machine_id, b2.component_id ORDER BY b2.stop_datetime
            ) AS prev_tran_date,
            LAG(b2.duration_min) OVER (
                PARTITION BY b2.machine_id, b2.component_id ORDER BY b2.stop_datetime
            ) AS prev_duration_min,
            LAG(b2.reason_id) OVER (
                PARTITION BY b2.machine_id, b2.component_id ORDER BY b2.stop_datetime
            ) AS prev_reason_id
        {source}
    )
    SELECT
        seq.breakdown_id,
        seq.machine_id,
        m.machine_name,
        m.mech_code,
        m.machine_type_id,
        mt.machine_type_name,
        d.dept_id,
        d.dept_desc AS dept_name,
        d.branch_id,
        seq.component_id,
        c.component_name,
        c.component_group,
        seq.reason_id,
        r.reason_code,
        r.reason_desc,
        seq.tran_date,
        seq.stop_datetime,
        seq.duration_min,
        seq.prev_breakdown_id,
        seq.prev_tran_date,
        seq.prev_duration_min,
        pr.reason_code AS prev_reason_code,
        DATEDIFF(seq.tran_date, seq.prev_tran_date) AS days_since_previous
    FROM seq
    INNER JOIN machine_mst m ON m.machine_id = seq.machine_id
    INNER JOIN dept_mst d ON d.dept_id = m.dept_id
    LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
    LEFT JOIN jute_mech_reason_mst r ON r.reason_id = seq.reason_id
    LEFT JOIN jute_mech_reason_mst pr ON pr.reason_id = seq.prev_reason_id
    LEFT JOIN jute_mech_component_mst c ON c.component_id = seq.component_id
    WHERE seq.prev_tran_date IS NOT NULL
      AND DATEDIFF(seq.tran_date, seq.prev_tran_date) <= :repeat_window_days
    ORDER BY days_since_previous ASC, seq.stop_datetime DESC
    LIMIT :top_n
    """
    return text(sql)


# =============================================================================
# 11. /machine-history -- one machine, everything
# =============================================================================


def get_machine_history_machine_query():
    """Machine header + maintenance attributes for the history page."""
    sql = """
    SELECT
        m.machine_id,
        m.machine_name,
        m.mech_code,
        m.machine_type_id,
        mt.machine_type_name,
        d.dept_id,
        d.dept_desc AS dept_name,
        d.branch_id,
        a.machine_attr_id,
        a.make,
        a.model,
        a.serial_no,
        a.commissioning_date,
        a.criticality,
        a.last_overhaul_date,
        a.overhaul_freq_months,
        a.next_overhaul_due,
        a.last_pm_date,
        a.pm_freq_days,
        a.next_pm_due,
        a.remarks
    FROM machine_mst m
    INNER JOIN dept_mst d ON d.dept_id = m.dept_id
    LEFT JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
    LEFT JOIN jute_mech_machine_attr a
        ON a.machine_id = m.machine_id AND a.co_id = :co_id AND a.active = 1
    WHERE m.machine_id = :machine_id
    """
    return text(sql)


def get_machine_history_breakdowns_query():
    """Every breakdown for one machine (all downtime types -- this is the history).

    Params: common filters + :limit, :offset.
    """
    sql = f"""
    SELECT
        b.breakdown_id,
        b.tran_date,
        b.spell_id,
        sp.spell_code,
        b.stop_datetime,
        b.attended_datetime,
        b.restart_datetime,
        b.duration_min,
        b.response_min,
        b.reason_id,
        r.reason_code,
        r.reason_desc,
        b.component_id,
        c.component_name,
        b.discipline,
        b.downtime_type,
        sd.sub_dept_desc AS attended_dept_name,
        b.attended_by,
        b.reported_by,
        b.root_cause,
        b.action_taken,
        b.preventive_action,
        b.production_loss_qty,
        b.status,
        b.remarks
    {_bd_source(failure_only=False)}
    ORDER BY b.stop_datetime DESC, b.breakdown_id DESC
    LIMIT :limit OFFSET :offset
    """
    # sub_dept / spell display joins, spliced into the shared source block.
    return text(_with_extra_joins(sql, _SUB_DEPT_JOIN + _SPELL_JOIN))


def get_machine_history_pm_query():
    """PM executions for one machine, with the task results rolled up."""
    sql = """
    SELECT
        p.pm_exec_id,
        p.pm_date,
        p.frequency,
        p.done_by,
        p.downtime_min,
        p.next_due_date,
        p.remarks,
        COALESCE(SUM(CASE WHEN pd.pm_exec_dtl_id IS NOT NULL THEN 1 ELSE 0 END), 0)
            AS task_count,
        COALESCE(SUM(CASE WHEN pd.result = 'NOT_OK' THEN 1 ELSE 0 END), 0)
            AS not_ok_count
    FROM jute_mech_pm_execution p
    INNER JOIN machine_mst m ON m.machine_id = p.machine_id
    INNER JOIN dept_mst d ON d.dept_id = m.dept_id
    LEFT JOIN jute_mech_pm_execution_dtl pd
        ON pd.pm_exec_id = p.pm_exec_id AND pd.active = 1
    WHERE p.active = 1
      AND p.co_id = :co_id
      AND p.machine_id = :machine_id
      AND (:branch_id IS NULL OR COALESCE(p.branch_id, d.branch_id) = :branch_id)
      AND (:date_from IS NULL OR p.pm_date >= :date_from)
      AND (:date_to IS NULL OR p.pm_date <= :date_to)
    GROUP BY
        p.pm_exec_id, p.pm_date, p.frequency, p.done_by, p.downtime_min,
        p.next_due_date, p.remarks
    ORDER BY p.pm_date DESC, p.pm_exec_id DESC
    LIMIT :limit
    """
    return text(sql)


def get_machine_history_overhaul_query():
    """Overhauls for one machine, vendor name resolved from party_mst."""
    sql = """
    SELECT
        o.overhaul_id,
        o.overhaul_type,
        o.from_date,
        o.to_date,
        o.downtime_days,
        o.work_done,
        o.done_by,
        o.party_id,
        pm.supp_name AS party_name,
        o.labour_cost,
        o.spare_cost,
        o.total_cost,
        o.next_due_date,
        o.remarks
    FROM jute_mech_overhaul o
    INNER JOIN machine_mst m ON m.machine_id = o.machine_id
    INNER JOIN dept_mst d ON d.dept_id = m.dept_id
    LEFT JOIN party_mst pm ON pm.party_id = o.party_id
    WHERE o.active = 1
      AND o.co_id = :co_id
      AND o.machine_id = :machine_id
      AND (:branch_id IS NULL OR COALESCE(o.branch_id, d.branch_id) = :branch_id)
      AND (:date_from IS NULL OR o.from_date >= :date_from)
      AND (:date_to IS NULL OR o.from_date <= :date_to)
    ORDER BY o.from_date DESC, o.overhaul_id DESC
    LIMIT :limit
    """
    return text(sql)


def get_machine_history_spares_query():
    """Every spare consumed on one machine, breakdown and overhaul alike."""
    sql = """
    SELECT
        'BREAKDOWN' AS source,
        b.breakdown_id AS document_id,
        b.tran_date AS document_date,
        s.item_id,
        i.item_code,
        i.item_name,
        u.uom_name,
        s.qty,
        s.rate,
        s.amount,
        s.remarks
    FROM jute_mech_breakdown_spare s
    INNER JOIN jute_mech_breakdown b ON b.breakdown_id = s.breakdown_id
    LEFT JOIN item_mst i ON i.item_id = s.item_id
    LEFT JOIN uom_mst u ON u.uom_id = i.uom_id
    WHERE s.active = 1
      AND b.active = 1
      AND b.co_id = :co_id
      AND b.machine_id = :machine_id
      AND (:date_from IS NULL OR b.tran_date >= :date_from)
      AND (:date_to IS NULL OR b.tran_date <= :date_to)
    UNION ALL
    SELECT
        'OVERHAUL' AS source,
        o.overhaul_id AS document_id,
        o.from_date AS document_date,
        s.item_id,
        i.item_code,
        i.item_name,
        u.uom_name,
        s.qty,
        s.rate,
        s.amount,
        s.remarks
    FROM jute_mech_overhaul_spare s
    INNER JOIN jute_mech_overhaul o ON o.overhaul_id = s.overhaul_id
    LEFT JOIN item_mst i ON i.item_id = s.item_id
    LEFT JOIN uom_mst u ON u.uom_id = i.uom_id
    WHERE s.active = 1
      AND o.active = 1
      AND o.co_id = :co_id
      AND o.machine_id = :machine_id
      AND (:date_from IS NULL OR o.from_date >= :date_from)
      AND (:date_to IS NULL OR o.from_date <= :date_to)
    ORDER BY document_date DESC
    LIMIT :limit
    """
    return text(sql)


def get_machine_history_breakdown_count_query():
    """Total breakdown rows for one machine (all downtime types)."""
    sql = f"""
    SELECT COUNT(*) AS total
    {_bd_source(failure_only=False)}
    """
    return text(sql)


# =============================================================================
# 12. /open-breakdown-board -- live board, longest stoppage first
# =============================================================================

# "Open" = never restarted AND no hand-entered duration: an incident closed by a
# typed duration_min alone is a finished repair and must leave the board.
_OPEN_EXTRA_WHERE = (
    "          AND b.restart_datetime IS NULL\n"
    "          AND b.duration_min IS NULL\n"
)


def get_open_breakdown_board_query():
    """Currently-open incidents with minutes elapsed to now, longest-first.

    Not failure-filtered: a machine stopped for a planned PM that has not
    restarted is still a stopped machine the board must show; downtime_type
    travels with the row so the UI can label it.

    Params: common filters + :limit, :offset.
    """
    sql = f"""
    SELECT
        b.breakdown_id,
        b.tran_date,
        b.machine_id,
        m.machine_name,
        m.mech_code,
        m.machine_type_id,
        mt.machine_type_name,
        d.dept_id,
        d.dept_desc AS dept_name,
        COALESCE(b.branch_id, d.branch_id) AS branch_id,
        a.criticality,
        b.spell_id,
        sp.spell_code,
        b.stop_datetime,
        b.attended_datetime,
        b.response_min,
        b.reason_id,
        r.reason_code,
        r.reason_desc,
        b.component_id,
        c.component_name,
        b.discipline,
        b.downtime_type,
        sd.sub_dept_desc AS attended_dept_name,
        b.attended_by,
        b.reported_by,
        ru.name AS reported_by_user_name,
        b.reported_date_time,
        b.status,
        b.remarks,
        TIMESTAMPDIFF(MINUTE, b.stop_datetime, NOW()) AS elapsed_min
    {_bd_source(failure_only=False, extra_where=_OPEN_EXTRA_WHERE)}
    ORDER BY elapsed_min DESC, b.stop_datetime ASC
    LIMIT :limit OFFSET :offset
    """
    return text(
        _with_extra_joins(sql, _SUB_DEPT_JOIN + _SPELL_JOIN + _ATTR_JOIN + _USER_JOIN)
    )


def get_open_breakdown_board_count_query():
    """Open incident count behind the board."""
    sql = f"""
    SELECT
        COUNT(*) AS total,
        COALESCE(SUM(TIMESTAMPDIFF(MINUTE, b.stop_datetime, NOW())), 0) AS total_elapsed_min,
        COALESCE(MAX(TIMESTAMPDIFF(MINUTE, b.stop_datetime, NOW())), 0) AS max_elapsed_min
    {_bd_source(failure_only=False, extra_where=_OPEN_EXTRA_WHERE)}
    """
    return text(sql)

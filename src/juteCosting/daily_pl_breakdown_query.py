"""Query builders for the Daily P&L lazy breakdown dialogs and the jute-issue
data check (ITM-027 §1.5 / §1.6; router daily_pl_breakdown.py).

Every builder returns a SMALL aggregated table (capped at the top N where the
dimension is open-ended) over exactly the scope of the matching day query in
daily_pl_query.py — the shared FROM / WHERE fragments
(JUTE_ISSUE_SCOPE_SQL, STORES_ISSUE_SCOPE_SQL, stores_category_sql) are
imported from there and extra JOINs are spliced in before their WHERE
(_jute_scope / _stores_scope), so a scope change applies to the P&L line and
its breakdown together. The one line-level pull is the jute-issue check
(get_jute_issue_check_rows_query), which returns the period's issue lines
with the MR / item / stock-balance context the flag rules need; the rules
themselves are pure Python (daily_pl_breakdown.flag_issue_rows).

All binds: :co_id, :branch_id (nullable), :date_from, :date_to.
``lines`` and ``rows`` are reserved words in MySQL 8, hence the back-ticked
aliases.
"""

from sqlalchemy import text

from src.inventory.query import lot_rate_sql
from src.juteCosting.daily_pl_query import (
    JUTE_ISSUE_SCOPE_SQL,
    JUTE_WASTE_PRED_SQL,
    RAW_JUTE_PRED_SQL,
    STORES_ISSUE_SCOPE_SQL,
    stores_category_sql,
)
from src.juteProcurement.constants import JUTE_ISSUE_EXCLUDED_STATUS

# Row caps of the open-ended dimensions (an aggregated table, never a dump).
TOP_MRS = 30
TOP_GROUPS = 25
TOP_DEPTS = 15
TOP_ITEMS = 25
TOP_DESIGNATIONS = 15

# Cancelled issue lines (status 6) are left out of the data check — they are
# not a data problem, they are a corrected one.
JUTE_ISSUE_CANCELLED_STATUS = 6

# Display-only tables (group / dept / expense type / top items) round in SQL;
# the CATEGORY table, whose sum is the dialog total, is left unrounded so the
# total is rounded ONCE in Python and matches the P&L stores line to the paisa
# (rounding each category first drifted the sls 1-19 Oct total by ₹0.01).
_STORES_VALUE_RAW = f"SUM(il.issue_qty * {lot_rate_sql('pid')}) AS value"
_STORES_VALUE = f"ROUND(SUM(il.issue_qty * {lot_rate_sql('pid')}), 2) AS value"

# The quality NAME of an issue line comes from item_mst via
# COALESCE(ji.item_id, mrli.actual_item_id) — jute_issue.jute_quality_id does
# not resolve in jute_quality_mst on sls (NULL on every 2026 line).
_QUALITY_ITEM_JOIN = (
    "        LEFT JOIN item_mst iq ON iq.item_id = COALESCE(ji.item_id, mrli.actual_item_id)\n"
)
_MR_HEADER_JOIN = "        LEFT JOIN jute_mr jm ON jm.jute_mr_id = mrli.jute_mr_id\n"


def _jute_scope(extra_joins: str = "") -> str:
    """JUTE_ISSUE_SCOPE_SQL with extra JOIN lines spliced in before its WHERE."""
    marker = "        WHERE b.co_id = :co_id"
    return JUTE_ISSUE_SCOPE_SQL.replace(marker, extra_joins + marker, 1)


def _stores_scope(extra_joins: str = "") -> str:
    """STORES_ISSUE_SCOPE_SQL with extra JOIN lines spliced in before its WHERE."""
    marker = "        WHERE (ih.active = 1 OR ih.active IS NULL)"
    return STORES_ISSUE_SCOPE_SQL.replace(marker, extra_joins + marker, 1)


# =============================================================================
# Raw material
# =============================================================================


def get_jute_issue_by_quality_query():
    """Raw jute issued in the range per quality (item): lines, kg, booked
    value, the kg that carries a value (for the average booked rate), the
    zero-value line count and what the MR rate would price those lines at.
    Returns item_id, item_name, lines, kg, value, valued_kg, zero_value_lines,
    mr_rated_value — ordered kg DESC."""
    return text(
        f"""
        SELECT COALESCE(ji.item_id, mrli.actual_item_id) AS item_id,
               iq.item_name,
               COUNT(*) AS `lines`,
               ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS kg,
               ROUND(SUM(COALESCE(ji.issue_value, 0)), 2) AS value,
               ROUND(SUM(CASE WHEN COALESCE(ji.issue_value, 0) > 0
                              THEN COALESCE(ji.weight, 0) ELSE 0 END), 3) AS valued_kg,
               SUM(CASE WHEN COALESCE(ji.issue_value, 0) = 0 THEN 1 ELSE 0 END) AS zero_value_lines,
               ROUND(SUM(CASE WHEN COALESCE(ji.issue_value, 0) = 0 AND COALESCE(mrli.rate, 0) > 0
                              THEN COALESCE(ji.weight, 0) / 100 * mrli.rate ELSE 0 END), 2) AS mr_rated_value
        {_jute_scope(_QUALITY_ITEM_JOIN)}
          AND {RAW_JUTE_PRED_SQL}
        GROUP BY 1, 2
        ORDER BY kg DESC, iq.item_name
        """
    )


def get_jute_issue_by_mr_query():
    """The MRs feeding the range, top TOP_MRS by kg — ONE row per MR (grouped
    on the MR header only, so the cap is TOP_MRS MRs, not MR x item x rate
    combinations): MR identity, date (the MR date, else the gate-entry date —
    a Pending chain root has no MR date yet), the issue lines aggregated —
    line count, distinct qualities (``items``) and their names joined
    (``item_name``, alphabetical, ', '-separated), the kg-weighted MR rate
    over the RATED lines (Σ weight x rate ÷ Σ weight, ₹/qtl; NULL when no
    line of the MR carries a rate), kg and booked value issued.
    ONLY_FULL_GROUP_BY safe (every selected non-aggregate is a GROUP BY
    expression). Returns
    jute_mr_id, branch_mr_no, jute_gate_entry_no, mr_date, lines, items,
    item_name, mr_rate_qtl, kg, value."""
    return text(
        f"""
        SELECT jm.jute_mr_id,
               jm.branch_mr_no,
               jm.jute_gate_entry_no,
               COALESCE(jm.jute_mr_date, jm.jute_gate_entry_date) AS mr_date,
               COUNT(*) AS `lines`,
               COUNT(DISTINCT COALESCE(ji.item_id, mrli.actual_item_id)) AS items,
               GROUP_CONCAT(DISTINCT iq.item_name ORDER BY iq.item_name SEPARATOR ', ') AS item_name,
               ROUND(SUM(CASE WHEN COALESCE(mrli.rate, 0) > 0
                              THEN COALESCE(ji.weight, 0) * mrli.rate ELSE 0 END)
                     / NULLIF(SUM(CASE WHEN COALESCE(mrli.rate, 0) > 0
                                       THEN COALESCE(ji.weight, 0) ELSE 0 END), 0), 2) AS mr_rate_qtl,
               ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS kg,
               ROUND(SUM(COALESCE(ji.issue_value, 0)), 2) AS value
        {_jute_scope(_MR_HEADER_JOIN + _QUALITY_ITEM_JOIN)}
          AND {RAW_JUTE_PRED_SQL}
        GROUP BY jm.jute_mr_id,
                 jm.branch_mr_no,
                 jm.jute_gate_entry_no,
                 COALESCE(jm.jute_mr_date, jm.jute_gate_entry_date)
        ORDER BY kg DESC, jm.jute_mr_id
        LIMIT {TOP_MRS}
        """
    )


def get_jute_issue_by_day_query():
    """Raw jute issued per day with the line counts: tran_date, kg, value,
    lines, zero_value_lines."""
    return text(
        f"""
        SELECT ji.issue_date AS tran_date,
               ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS kg,
               ROUND(SUM(COALESCE(ji.issue_value, 0)), 2) AS value,
               COUNT(*) AS `lines`,
               SUM(CASE WHEN COALESCE(ji.issue_value, 0) = 0 THEN 1 ELSE 0 END) AS zero_value_lines
        {_jute_scope()}
          AND {RAW_JUTE_PRED_SQL}
        GROUP BY ji.issue_date
        ORDER BY ji.issue_date
        """
    )


def get_jute_waste_issue_by_item_query():
    """Jute waste issued in the range per waste item: item_name, kg, value."""
    return text(
        f"""
        SELECT im.item_name,
               ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS kg,
               ROUND(SUM(COALESCE(ji.issue_value, 0)), 2) AS value
        {_jute_scope()}
          AND {JUTE_WASTE_PRED_SQL}
        GROUP BY im.item_name
        ORDER BY kg DESC
        """
    )


# =============================================================================
# Stores
# =============================================================================


def get_stores_by_category_query():
    """Stores issued in the range per category: category, value (NOT rounded
    — see _STORES_VALUE_RAW), lines."""
    return text(
        f"""
        SELECT {stores_category_sql('ig')} AS category,
               {_STORES_VALUE_RAW},
               COUNT(*) AS `lines`
        {_stores_scope()}
        GROUP BY 1
        ORDER BY value DESC
        """
    )


def get_stores_by_group_query():
    """Top TOP_GROUPS item groups by value: item_grp_name, category, value, lines."""
    return text(
        f"""
        SELECT ig.item_grp_name,
               {stores_category_sql('ig')} AS category,
               {_STORES_VALUE},
               COUNT(*) AS `lines`
        {_stores_scope()}
        GROUP BY 1, 2
        ORDER BY value DESC, ig.item_grp_name
        LIMIT {TOP_GROUPS}
        """
    )


def get_stores_by_dept_query():
    """Top TOP_DEPTS issuing departments (issue_hdr.dept_id -> dept_mst):
    dept_id, dept_desc, value, lines."""
    return text(
        f"""
        SELECT ih.dept_id,
               d.dept_desc,
               {_STORES_VALUE},
               COUNT(*) AS `lines`
        {_stores_scope("        LEFT JOIN dept_mst d ON d.dept_id = ih.dept_id\n")}
        GROUP BY 1, 2
        ORDER BY value DESC, d.dept_desc
        LIMIT {TOP_DEPTS}
        """
    )


def get_stores_by_expense_type_query():
    """Stores issued per expense type (issue_li.expense_type_id ->
    expense_type_mst): expense_type_id, expense_type_name, value, lines. Type
    7 "Capital" and 4 "Overhauling" are shown, not excluded — the owner may
    decide to move them out of stores later."""
    return text(
        f"""
        SELECT il.expense_type_id,
               et.expense_type_name,
               {_STORES_VALUE},
               COUNT(*) AS `lines`
        {_stores_scope("        LEFT JOIN expense_type_mst et ON et.expense_type_id = il.expense_type_id\n")}
        GROUP BY 1, 2
        ORDER BY value DESC
        """
    )


def get_stores_top_items_query():
    """Top TOP_ITEMS items by value: item_name, item_grp_name, qty, value."""
    return text(
        f"""
        SELECT im.item_name,
               ig.item_grp_name,
               ROUND(SUM(il.issue_qty), 3) AS qty,
               {_STORES_VALUE}
        {_stores_scope()}
        GROUP BY 1, 2
        ORDER BY value DESC, im.item_name
        LIMIT {TOP_ITEMS}
        """
    )


# =============================================================================
# Power
# =============================================================================

_POWER_FROM = """
        FROM jute_power_reading r
        LEFT JOIN jute_power_meter_mst m
               ON m.co_id = r.co_id
              AND m.meter_code = r.meter_code
              AND (r.branch_id IS NULL OR m.branch_id = r.branch_id)
        WHERE r.co_id = :co_id
          AND r.active = 1
          AND r.tran_date BETWEEN :date_from AND :date_to
          AND (:branch_id IS NULL OR r.branch_id = :branch_id)
"""


def get_power_by_meter_query():
    """Per meter over the range: meter_code, meter_name, meter_type, kwh,
    dg_hours, dg_litres, max_demand_kva, days (distinct reading dates).
    OPTIONAL table (ITM-004) — fetched through _fetch_optional."""
    return text(
        f"""
        SELECT r.meter_code,
               m.meter_name,
               UPPER(COALESCE(m.meter_type, 'OTHER')) AS meter_type,
               ROUND(SUM(r.kwh), 2) AS kwh,
               ROUND(SUM(COALESCE(r.dg_hours, 0)), 2) AS dg_hours,
               ROUND(SUM(COALESCE(r.dg_litres, 0)), 2) AS dg_litres,
               MAX(r.max_demand_kva) AS max_demand_kva,
               COUNT(DISTINCT r.tran_date) AS days
        {_POWER_FROM}
        GROUP BY 1, 2, 3
        ORDER BY kwh DESC, r.meter_code
        """
    )


def get_power_by_day_type_query():
    """kWh per day per meter type: tran_date, meter_type, kwh. OPTIONAL table."""
    return text(
        f"""
        SELECT r.tran_date,
               UPPER(COALESCE(m.meter_type, 'OTHER')) AS meter_type,
               ROUND(SUM(r.kwh), 2) AS kwh
        {_POWER_FROM}
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    )


# =============================================================================
# Wages (daily_attendance, the hands query's scope)
# =============================================================================

_HANDS_MEASURES = """
               COUNT(*) AS `rows`,
               ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)), 2) AS hours,
               ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)) / 8, 2) AS hands
"""
_HANDS_FROM = """
        FROM daily_attendance da
        INNER JOIN branch_mst bm ON bm.branch_id = da.branch_id
"""
_HANDS_WHERE = """
        WHERE da.is_active = 1
          AND da.status_id = 3
          AND da.attendance_date BETWEEN :date_from AND :date_to
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR da.branch_id = :branch_id)
"""


def get_hands_by_spell_query():
    """Hands per spell (daily_attendance.spell_id -> spell_mst, the DPR's
    join): spell_id, spell_name, rows, hours, hands."""
    return text(
        f"""
        SELECT da.spell_id,
               COALESCE(sp.spell_name, sp.spell_code, da.spell, '?') AS spell_name,
               {_HANDS_MEASURES}
        {_HANDS_FROM}
        LEFT JOIN spell_mst sp ON sp.spell_id = da.spell_id
        {_HANDS_WHERE}
        GROUP BY 1, 2
        ORDER BY hands DESC, spell_name
        """
    )


def get_hands_by_designation_query():
    """Top TOP_DESIGNATIONS designations by hands (worked_designation_id ->
    designation_mst.desig, else 'Not assigned'): designation, rows, hours, hands."""
    return text(
        f"""
        SELECT COALESCE(dm.desig, 'Not assigned') AS designation,
               {_HANDS_MEASURES}
        {_HANDS_FROM}
        LEFT JOIN designation_mst dm ON dm.designation_id = da.worked_designation_id
        {_HANDS_WHERE}
        GROUP BY 1
        ORDER BY hands DESC, designation
        LIMIT {TOP_DESIGNATIONS}
        """
    )


def get_hands_by_department_query():
    """Hands per department, routed through the worked DESIGNATION
    (worked_designation_id -> designation_mst.dept_id -> dept_mst.dept_desc,
    else 'Not assigned') — the route of dprQueries.get_dpr_hands_by_dept_query
    (TKT-185) and the Hands Report: daily_attendance.worked_department_id is a
    legacy id space that does not join dept_mst (94 % of sls 1-19 Oct rows
    read 'Not assigned' through it; 0 % through the designation).
    Returns department, rows, hours, hands."""
    return text(
        f"""
        SELECT COALESCE(dp.dept_desc, 'Not assigned') AS department,
               {_HANDS_MEASURES}
        {_HANDS_FROM}
        LEFT JOIN designation_mst dg ON dg.designation_id = da.worked_designation_id
        LEFT JOIN dept_mst dp ON dp.dept_id = dg.dept_id
        {_HANDS_WHERE}
        GROUP BY 1
        ORDER BY hands DESC, department
        """
    )


def get_hours_profile_query():
    """Attendance rows per working_hours value: working_hours, rows."""
    return text(
        f"""
        SELECT da.working_hours,
               COUNT(*) AS `rows`
        {_HANDS_FROM}
        {_HANDS_WHERE}
        GROUP BY 1
        ORDER BY 1 DESC
        """
    )


# =============================================================================
# Jute-issue data check (line level, one pull)
# =============================================================================


def get_jute_issue_check_rows_query():
    """Every jute-issue line of the range (raw jute AND waste), cancelled
    (status 6) and the ledger-excluded status left out, with the MR line, the
    MR header (status name from status_mst), the quality item name, the
    stock-view balance of the MR line (vw_jute_stock_outstanding.bal_weight,
    NULL when the view no longer lists the line) and the line's keyed-in time
    (jute_issue.update_date_time AS keyed_at) for the double-entry rule. The
    duplicate grouping (same date, MR line, weight) is done in Python over
    these rows — a duplicate shares the issue date, so it is always inside the
    range — rather than by a correlated subquery, which took 7 s on sls
    (no index on (issue_date, jute_mr_li_id)). The flag rules run on these
    rows in Python. Ordered issue_date, jute_issue_id.
    """
    live = f"({int(JUTE_ISSUE_EXCLUDED_STATUS)}, {JUTE_ISSUE_CANCELLED_STATUS})"
    return text(
        f"""
        SELECT ji.jute_issue_id,
               ji.issue_date,
               ji.status_id AS issue_status_id,
               mrli.jute_mr_id,
               ji.jute_mr_li_id,
               jm.branch_mr_no,
               jm.jute_gate_entry_no,
               jm.status_id AS mr_status_id,
               sm.status_name AS mr_status,
               COALESCE(ji.item_id, mrli.actual_item_id) AS item_id,
               iq.item_name,
               ji.item_id AS issue_item_id,
               mrli.actual_item_id AS mr_item_id,
               ji.weight AS weight_kg,
               ji.issue_value,
               mrli.rate AS mr_rate_qtl,
               vs.bal_weight AS mr_line_balance_kg,
               ji.update_date_time AS keyed_at
        FROM jute_issue ji
        INNER JOIN branch_mst b ON b.branch_id = ji.branch_id
        INNER JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id
        LEFT JOIN jute_mr jm ON jm.jute_mr_id = mrli.jute_mr_id
        LEFT JOIN status_mst sm ON sm.status_id = jm.status_id
{_QUALITY_ITEM_JOIN.rstrip()}
        LEFT JOIN vw_jute_stock_outstanding vs ON vs.jute_mr_li_id = ji.jute_mr_li_id
        WHERE b.co_id = :co_id
          AND (:branch_id IS NULL OR ji.branch_id = :branch_id)
          AND ji.issue_date BETWEEN :date_from AND :date_to
          AND ji.status_id NOT IN {live}
          AND ji.jute_mr_li_id IS NOT NULL
        ORDER BY ji.issue_date, ji.jute_issue_id
        """
    )


def get_jute_issue_waste_ledger_query():
    """The jute-issue lines the check above leaves out: the same scope (company
    via branch_mst, branch, range, status NOT IN (ledger-excluded, cancelled))
    but with NO MR line (jute_mr_li_id IS NULL) — on sls these are the
    waste-ledger lines (waste_entry_id set), booked at ₹0 by design (85 lines /
    11.8 t on 1-19 Oct 2026). Counted for the check's summary note only.
    Returns one row: lines, kg, value."""
    live = f"({int(JUTE_ISSUE_EXCLUDED_STATUS)}, {JUTE_ISSUE_CANCELLED_STATUS})"
    return text(
        f"""
        SELECT COUNT(*) AS `lines`,
               ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS kg,
               ROUND(SUM(COALESCE(ji.issue_value, 0)), 2) AS value
        FROM jute_issue ji
        INNER JOIN branch_mst b ON b.branch_id = ji.branch_id
        WHERE b.co_id = :co_id
          AND (:branch_id IS NULL OR ji.branch_id = :branch_id)
          AND ji.issue_date BETWEEN :date_from AND :date_to
          AND ji.status_id NOT IN {live}
          AND ji.jute_mr_li_id IS NULL
        """
    )

"""Range (per-day) query builders for the Daily P&L report (ITM-019 / ITM-027).

The DPR builders in src/juteProduction/dprQueries.py are single-date day+MTD
aggregates and cannot serve a date range, so the scope predicates of the
builders they are based on are COPIED VERBATIM here and only the grouping
changes to one row per tran_date. Each docstring names the source builder so a
scope change there can be mirrored (the DPR files themselves are never edited
by this module). The only DPR builder reused as-is is
dprQueries.get_dpr_spinning_daily_production_query (yarn kg per day), which
already takes a window.

All binds: :co_id, :branch_id (nullable, NULL = every branch of the company),
:date_from, :date_to unless a docstring says otherwise. Every weight is in the
unit of the source table (kg) and every value in rupees; the router converts
to tonnes.

ITM-027 added the per-category stores split (stores_category_sql), the
per-meter-type power split, the jute-waste stream and the two as-on capital
inputs (jute stock value from vw_jute_stock_outstanding; finished goods come
from bale_stock_query.get_bale_stock_summary_query, reused unchanged).
"""

from sqlalchemy import text

from src.inventory.query import lot_rate_sql
from src.juteProcurement.constants import (
    JUTE_ISSUE_EXCLUDED_STATUS,
    JUTE_WASTE_ITEM_TYPE_ID,
    YARN_DOC_STATUS_APPROVED,
    YARN_ISSUE_TYPE_RETURN,
)

# Stores categories (ITM-027 head 2). The CASE below maps item_grp_mst names
# to one of these; anything unknown is SPARES (machine parts are the bulk of
# the stores issues on sls, so the default is the most likely bucket).
STORES_SPARES = "SPARES"
STORES_CONSUMABLES = "CONSUMABLES"
STORES_PACKING = "PACKING"
STORES_GENERAL = "GENERAL"
STORES_CATEGORIES = (STORES_SPARES, STORES_CONSUMABLES, STORES_PACKING, STORES_GENERAL)

# Jute-issue scope shared by the day query, the waste query and the breakdown
# builders (daily_pl_breakdown_query.py): MR-sourced legs only, company via
# branch_mst, ledger status convention <> JUTE_ISSUE_EXCLUDED_STATUS. The
# jute-waste predicate is added by each builder.
JUTE_ISSUE_SCOPE_SQL = f"""
        FROM jute_issue ji
        INNER JOIN branch_mst b ON b.branch_id = ji.branch_id
        INNER JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id
        LEFT JOIN item_mst im ON im.item_id = mrli.actual_item_id
        LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE b.co_id = :co_id
          AND (:branch_id IS NULL OR ji.branch_id = :branch_id)
          AND ji.issue_date BETWEEN :date_from AND :date_to
          AND ji.status_id <> {int(JUTE_ISSUE_EXCLUDED_STATUS)}
          AND ji.jute_mr_li_id IS NOT NULL
"""
RAW_JUTE_PRED_SQL = f"COALESCE(ig.item_type_id, 0) <> {int(JUTE_WASTE_ITEM_TYPE_ID)}"
JUTE_WASTE_PRED_SQL = f"ig.item_type_id = {int(JUTE_WASTE_ITEM_TYPE_ID)}"

# Stores-issue scope shared by the day query and the stores breakdown: copy of
# inventory/reportQueries.get_issue_consumption_query's scope (header active =
# 1 or NULL, status_id IN (1, 3, 5, 20), company via branch_mst, optional
# branch) with the item group joined for the category CASE.
STORES_ISSUE_SCOPE_SQL = """
        FROM issue_li il
        INNER JOIN issue_hdr ih ON ih.issue_id = il.issue_id
        INNER JOIN branch_mst bm ON bm.branch_id = ih.branch_id
        LEFT JOIN proc_inward_dtl pid ON pid.inward_dtl_id = il.inward_dtl_id
        LEFT JOIN item_mst im ON im.item_id = il.item_id
        LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE (ih.active = 1 OR ih.active IS NULL)
          AND ih.status_id IN (1, 3, 5, 20)
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
          AND ih.issue_date BETWEEN :date_from AND :date_to
"""


def stores_category_sql(grp_alias: str) -> str:
    """CASE on UPPER(item_grp_mst.item_grp_name) (aliased ``grp_alias``) ->
    PACKING / CONSUMABLES / GENERAL / SPARES.

    Patterns (ITM-027 §1.2, checked against the sls group names): packing
    material; process consumables = lubricants (OIL/GREASE/LUBRICANTS holds
    ecofin / batching oil / emulsion on sls), chemicals, dyes; general stores
    and the service-like groups (misc expenses, other charges, other items,
    building material, stationery). Everything else — the long tail of
    machine-part groups — is SPARES, as is a line with no group (NULL name
    fails every LIKE and falls to ELSE).
    """
    n = f"UPPER({grp_alias}.item_grp_name)"
    return (
        f"CASE WHEN {n} LIKE 'PACKING%' THEN '{STORES_PACKING}' "
        f"WHEN {n} LIKE '%LUBRICANT%' OR {n} LIKE 'OIL/%' "
        f"OR {n} LIKE '%CHEMICAL%' OR {n} LIKE 'DYES%' THEN '{STORES_CONSUMABLES}' "
        f"WHEN {n} LIKE 'GENERAL STORE%' OR {n} LIKE 'MISC EXPNS%' "
        f"OR {n} LIKE 'OTHER CHARGES%' OR {n} LIKE 'OTHER ITEM%' "
        f"OR {n} LIKE 'BUILDING MATERIAL%' OR {n} LIKE 'STATIONERY%' THEN '{STORES_GENERAL}' "
        f"ELSE '{STORES_SPARES}' END"
    )


def get_cloth_by_quality_daily_query():
    """Cloth produced per day per weaving quality (tonnes).

    Copy of dprQueries.get_dpr_weaving_mtd_query with wd.tran_date added to the
    SELECT and GROUP BY (stored-jugar formula, active = 1, co_id scope, the
    NULL-tolerant branch idiom of weaving_day_slice_sql) and :report_date
    renamed :date_to. The quality name feeds the SACKING / HESSIAN split
    (daily_pl.product_of). Returns tran_date, weaving_quality_id,
    weaving_quality_code, weaving_quality_name, production_mt.
    """
    return text(
        """
        SELECT
            wd.tran_date,
            q.weaving_quality_id,
            q.weaving_quality_code,
            q.weaving_quality_name,
            ROUND(SUM(
                CASE WHEN COALESCE(q.no_of_jugar_per_cut, 0) > 0
                     THEN (wd.cuts * q.no_of_jugar_per_cut
                           + COALESCE(wd.close_jugar, 0)
                           - COALESCE(wd.open_jugar, 0)
                           - COALESCE(wd.less_production, 0))
                          * COALESCE(q.finished_length, 0) / q.no_of_jugar_per_cut
                     ELSE 0 END
                * COALESCE(q.ozs_yds, 0) * 28.35 / 1000 / 1000
            ), 3) AS production_mt
        FROM jute_prod_weaving_daily wd
        INNER JOIN jute_prod_weaving_quality q
                ON q.weaving_quality_id = wd.weaving_quality_id
        WHERE wd.active = 1
          AND wd.co_id = :co_id
          AND wd.tran_date BETWEEN :date_from AND :date_to
          -- branch NULL-tolerant by design (parity with weaving_day_slice_sql)
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
        GROUP BY wd.tran_date, q.weaving_quality_id, q.weaving_quality_code, q.weaving_quality_name
        ORDER BY wd.tran_date, q.weaving_quality_code
        """
    )


def get_hands_daily_query():
    """Worker hands per day: worked hours (net of idle) and hours / 8.

    Copy of dprQueries.get_dpr_hands_query's scope (daily_attendance joined to
    branch_mst for the company, is_active = 1, approved status_id = 3, the
    optional branch filter) grouped by attendance_date instead of shift bucket.
    Returns tran_date, hours, hands.
    """
    return text(
        """
        SELECT
            da.attendance_date AS tran_date,
            ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)), 2) AS hours,
            ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)) / 8, 2) AS hands
        FROM daily_attendance da
        INNER JOIN branch_mst bm ON bm.branch_id = da.branch_id
        WHERE da.is_active = 1
          AND da.status_id = 3
          AND da.attendance_date BETWEEN :date_from AND :date_to
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR da.branch_id = :branch_id)
        GROUP BY da.attendance_date
        ORDER BY da.attendance_date
        """
    )


def get_spindle_hours_daily_query():
    """Spindle-hours run per day from the spinning planning grid
    (jute_prod_spinning_daily: SUM(spindles x minutes) / 60).

    Scope is the grid's own (co_id, active = 1) with the NULL-tolerant branch
    idiom the grid's branch_id column needs. Returns tran_date, spindle_hours.
    """
    return text(
        """
        SELECT
            tran_date,
            ROUND(SUM(spindles * minutes) / 60, 2) AS spindle_hours
        FROM jute_prod_spinning_daily
        WHERE co_id = :co_id
          AND active = 1
          AND tran_date BETWEEN :date_from AND :date_to
          AND (:branch_id IS NULL OR branch_id = :branch_id OR branch_id IS NULL)
        GROUP BY tran_date
        ORDER BY tran_date
        """
    )


def get_jute_issue_daily_query():
    """Raw jute issued per day: weight (kg) and the issue value booked on it.

    Copy of wastage_report_query.get_selection_daily_weight_query's scope
    (MR-sourced legs only, company via branch_mst, ledger status convention
    <> JUTE_ISSUE_EXCLUDED_STATUS, type-3 jute-waste lines left out — see
    JUTE_ISSUE_SCOPE_SQL / RAW_JUTE_PRED_SQL) with issue_value summed beside
    the weight.

    issue_value is not reliably booked on every line (the batch cost report
    prefers mrli.rate for the same reason), so the lines whose value is NULL
    or 0 are also returned on their own: unvalued_kg (their weight), and
    the part of it the MR line rate can price — mr_rated_kg and
    mr_rate_value = Σ weight / 100 x mrli.rate (the rate is ₹/qtl, as the
    batch cost report's "accounts" basis reads it). The router prices
    unvalued_kg at the keyed JUTE_RATE_PER_KG, else at mr_rate_value.
    zero_value_lines counts the lines with no booked value (ITM-027 head 1
    KPI; the jute-issue data check lists them).
    Returns tran_date, issued_kg, issue_value, unvalued_kg, mr_rated_kg,
    mr_rate_value, zero_value_lines.
    """
    return text(
        f"""
        SELECT ji.issue_date AS tran_date,
               ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS issued_kg,
               ROUND(SUM(COALESCE(ji.issue_value, 0)), 2) AS issue_value,
               ROUND(SUM(CASE WHEN COALESCE(ji.issue_value, 0) = 0
                              THEN COALESCE(ji.weight, 0) ELSE 0 END), 3) AS unvalued_kg,
               ROUND(SUM(CASE WHEN COALESCE(ji.issue_value, 0) = 0 AND COALESCE(mrli.rate, 0) > 0
                              THEN COALESCE(ji.weight, 0) ELSE 0 END), 3) AS mr_rated_kg,
               ROUND(SUM(CASE WHEN COALESCE(ji.issue_value, 0) = 0 AND COALESCE(mrli.rate, 0) > 0
                              THEN COALESCE(ji.weight, 0) / 100 * mrli.rate ELSE 0 END), 2) AS mr_rate_value,
               SUM(CASE WHEN COALESCE(ji.issue_value, 0) = 0 THEN 1 ELSE 0 END) AS zero_value_lines
        {JUTE_ISSUE_SCOPE_SQL}
          AND {RAW_JUTE_PRED_SQL}
        GROUP BY ji.issue_date
        ORDER BY ji.issue_date
        """
    )


def get_jute_waste_issue_daily_query():
    """Jute WASTE issued per day (kg, booked value) — the lines the raw-jute
    query leaves out (item group of the jute-waste type,
    JUTE_WASTE_ITEM_TYPE_ID). Same scope otherwise. Booked issue_value only:
    waste carries no MR rate worth repricing by. A new stream in ITM-027
    (head 1 sub-line "jute waste"); 0 on sls in October 2026.
    Returns tran_date, waste_kg, waste_value.
    """
    return text(
        f"""
        SELECT ji.issue_date AS tran_date,
               ROUND(SUM(COALESCE(ji.weight, 0)), 3) AS waste_kg,
               ROUND(SUM(COALESCE(ji.issue_value, 0)), 2) AS waste_value
        {JUTE_ISSUE_SCOPE_SQL}
          AND {JUTE_WASTE_PRED_SQL}
        GROUP BY ji.issue_date
        ORDER BY ji.issue_date
        """
    )


def get_stores_value_daily_by_category_query():
    """Stores issued per day per category, valued at the receipt-lot rate.

    Valuation and scope of inventory/reportQueries.get_issue_consumption_query
    (issue_li x lot_rate_sql('pid') via proc_inward_dtl — STORES_ISSUE_SCOPE_SQL)
    grouped by issue_date and the stores_category_sql CASE on the line item's
    group (item_mst.item_grp_id -> item_grp_mst, LEFT joins: a line with no
    item or group lands in SPARES). The category sums are NOT rounded here:
    the day total is their sum rounded once in Python, which reproduces the
    pre-split query's ROUND(SUM(day), 2) to the paisa (rounding each category
    first drifted the sls October total by ₹0.02).
    Returns tran_date, category, stores_value.
    """
    return text(
        f"""
        SELECT ih.issue_date AS tran_date,
               {stores_category_sql('ig')} AS category,
               SUM(il.issue_qty * {lot_rate_sql('pid')}) AS stores_value
        {STORES_ISSUE_SCOPE_SQL}
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    )


def get_dust_daily_query():
    """Dust booked per day (kg) on the waste entries — the only waste that is
    sold rather than re-used (owner Q04).

    Joins from waste_query.get_waste_by_date_query (dept_mst INNER, item_mst for
    the item name, COALESCE(w.branch_id, d.branch_id) for legacy NULL branches).
    The item is the jute-waste item named exactly 'Dust' (sls item 335884,
    item_type JUTE_WASTE_ITEM_TYPE_ID): an exact name AND the waste item type,
    because a prefix match ('DUST%') also took the store items 'DUST MASK' /
    'DUST REMOVAL NOZZLE' (both tenants), and a waste entry mis-booked on one
    of those would have been credited as a dust sale. Returns tran_date,
    dust_kg.
    """
    return text(
        f"""
        SELECT w.tran_date,
               ROUND(SUM(w.waste_qty), 3) AS dust_kg
        FROM jute_prod_waste_entry w
        INNER JOIN dept_mst d ON d.dept_id = w.dept_id
        INNER JOIN item_mst im ON im.item_id = w.item_id
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE w.active = 1
          AND w.co_id = :co_id
          AND w.tran_date BETWEEN :date_from AND :date_to
          AND (:branch_id IS NULL OR COALESCE(w.branch_id, d.branch_id) = :branch_id)
          AND UPPER(TRIM(im.item_name)) = 'DUST'
          AND ig.item_type_id = {int(JUTE_WASTE_ITEM_TYPE_ID)}
        GROUP BY w.tran_date
        ORDER BY w.tran_date
        """
    )


def get_purchased_yarn_daily_query():
    """Purchased yarn issued to the floor per day, net of returns (kg).

    Range form of dprQueries.get_dpr_purchased_yarn_issue_query grouped by
    i.tran_date (approved documents only, RETURN lines negative, line
    active = 1). OPTIONAL table: jute_yarn_issue exists only on tenants that
    ran 20260925_yarn_purchase_issue_tables.sql — the caller goes through
    _fetch_optional and reads None as "not tracked". Returns tran_date,
    purchased_kg.
    """
    return text(
        f"""
        SELECT
            i.tran_date,
            ROUND(SUM(CASE WHEN i.issue_type = '{YARN_ISSUE_TYPE_RETURN}'
                           THEN -li.weight_kg ELSE li.weight_kg END), 3) AS purchased_kg
        FROM jute_yarn_issue i
        INNER JOIN jute_yarn_issue_li li
                ON li.yarn_issue_id = i.yarn_issue_id
               AND li.active = 1
        WHERE i.active = 1
          AND i.status_id = {int(YARN_DOC_STATUS_APPROVED)}
          AND i.co_id = :co_id
          AND (:branch_id IS NULL OR i.branch_id = :branch_id)
          AND i.tran_date BETWEEN :date_from AND :date_to
        GROUP BY i.tran_date
        ORDER BY i.tran_date
        """
    )


def get_power_daily_by_type_query():
    """Power consumed per day per METER TYPE (kWh) plus the DG running hours
    and diesel litres keyed on the readings.

    jute_power_reading is ITM-004's table, LEFT-joined to jute_power_meter_mst
    on (co_id, meter_code) — and branch when the reading carries one — for
    meter_type (GRID | DG | SOLAR); a reading whose meter is not in the master
    reads as OTHER and is priced at the grid rate. OPTIONAL table: absent on
    dev3 today, so the caller goes through _fetch_optional, where 1146 / 1054
    become power "not keyed" instead of a 500.
    Returns tran_date, meter_type, power_kwh, dg_hours, dg_litres.
    """
    return text(
        """
        SELECT r.tran_date,
               UPPER(COALESCE(m.meter_type, 'OTHER')) AS meter_type,
               ROUND(SUM(r.kwh), 2) AS power_kwh,
               ROUND(SUM(COALESCE(r.dg_hours, 0)), 2) AS dg_hours,
               ROUND(SUM(COALESCE(r.dg_litres, 0)), 2) AS dg_litres
        FROM jute_power_reading r
        LEFT JOIN jute_power_meter_mst m
               ON m.co_id = r.co_id
              AND m.meter_code = r.meter_code
              AND (r.branch_id IS NULL OR m.branch_id = r.branch_id)
        WHERE r.co_id = :co_id
          AND r.active = 1
          AND r.tran_date BETWEEN :date_from AND :date_to
          AND (:branch_id IS NULL OR r.branch_id = :branch_id)
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    )


def get_jute_stock_value_query():
    """Jute stock on hand NOW at MR rate — the first capital component
    (ITM-027 head 6). Σ bal_weight / 100 x rate (₹/qtl) and Σ bal_weight over
    vw_jute_stock_outstanding, which has no co_id of its own (branch_mst is
    joined for it). The view is a live position, not as-of-date, so the
    report says so in meta.notes. OPTIONAL (the view exists on sls; a tenant
    without it shows capital "not available"). Binds: :co_id, :branch_id.

    A line with no rate yet (NULL / 0 — typically an open MR) adds its kg to
    ``kg`` but ₹0 to ``value``, so its positive balance is returned on its
    own — unrated_kg and unrated_lines (on sls: 45,160 kg on 10 MR lines of
    632,854 kg, Oct 2026) — and the router either says so or prices those kg
    at the keyed JUTE_RATE_PER_KG (daily_pl.build_capital).
    Returns one row: value, kg, unrated_kg, unrated_lines.
    """
    return text(
        """
        SELECT ROUND(SUM(vs.bal_weight / 100 * COALESCE(vs.rate, 0)), 2) AS value,
               ROUND(SUM(vs.bal_weight), 3) AS kg,
               ROUND(SUM(CASE WHEN COALESCE(vs.rate, 0) = 0 AND vs.bal_weight > 0
                              THEN vs.bal_weight ELSE 0 END), 3) AS unrated_kg,
               SUM(CASE WHEN COALESCE(vs.rate, 0) = 0 AND vs.bal_weight > 0
                        THEN 1 ELSE 0 END) AS unrated_lines
        FROM vw_jute_stock_outstanding vs
        INNER JOIN branch_mst b ON b.branch_id = vs.branch_id
        WHERE b.co_id = :co_id
          AND (:branch_id IS NULL OR vs.branch_id = :branch_id)
        """
    )

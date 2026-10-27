"""
Jute Procurement Report Queries.
Contains SQL queries for jute stock and related reports.
"""

from sqlalchemy import bindparam, text

from src.juteProcurement.constants import (
    JUTE_MR_RECEIVED_WEIGHT_SQL,
    JUTE_ISSUE_EXCLUDED_STATUS,
    JUTE_WASTE_ITEM_TYPE_ID,
    JUTE_MR_LI_EXCLUDED_STATUSES,
    JUTE_MR_TALLY_STATUSES,
    JUTE_MR_STOCK_EXCLUDED_STATUSES,
    JUTE_PO_CLOSED_STATUS,
    JUTE_PO_INACTIVE_STATUSES,
    RAW_JUTE_INVOICE_TYPE_ID,
    SALES_APPROVED_STATUS_ID,
    SALES_MOVEMENT_EXCLUDED_STATUSES,
)
from src.juteProcurement.formatters import (
    get_jute_mr_number_sql_expression,
    get_jute_po_number_sql_expression,
)
from src.masters.tallyLinkQueries import LEDGER_DEFAULTS
from src.sales.reportQueries import _tally_name_join


def _group_path_cte():
    """
    Returns a recursive CTE that builds full hierarchical group paths.
    Creates a temp table `full_group_paths` with columns: item_grp_id, item_grp_name_path.
    Usage: prepend to any query, then JOIN full_group_paths fgp ON fgp.item_grp_id = ...
    and use COALESCE(fgp.item_grp_name_path, ig.item_grp_name) AS jute_group_name.
    """
    return """
        WITH RECURSIVE group_path AS (
            SELECT
                igm.item_grp_id AS target_id,
                igm.item_grp_id,
                igm.item_grp_name,
                igm.parent_grp_id,
                CAST(igm.item_grp_name AS CHAR(500)) AS item_grp_name_path
            FROM item_grp_mst igm

            UNION ALL

            SELECT
                child.target_id,
                p.item_grp_id,
                p.item_grp_name,
                p.parent_grp_id,
                CAST(CONCAT(p.item_grp_name, ' > ', child.item_grp_name_path) AS CHAR(500))
            FROM item_grp_mst p
            JOIN group_path child ON child.parent_grp_id = p.item_grp_id
        ),
        full_group_paths AS (
            SELECT target_id AS item_grp_id, item_grp_name_path
            FROM group_path
            WHERE parent_grp_id IS NULL
        )
    """


# ---------------------------------------------------------------------------
# Cluster A — balance reports computed from the base tables.
#
# Receipts and issues share ONE eligibility definition (below). When they
# disagree, material leaves stock that never arrived and closing drifts
# negative — that is exactly how sls branch 29 read -52,720 qtl against a
# true balance of 0.
#
# Stock has two sources (MR receipts + production waste) and two sinks
# (issues + approved raw-jute sales invoices). Every CTE builder here covers
# both sides symmetrically.
# ---------------------------------------------------------------------------

# jute_mr.status_id is an int; jute_mr_li.status is a VARCHAR.
_MR_EXCLUDED_SQL = ", ".join(str(s) for s in JUTE_MR_STOCK_EXCLUDED_STATUSES)
_MR_LI_EXCLUDED_SQL = ", ".join(f"'{s}'" for s in JUTE_MR_LI_EXCLUDED_STATUSES)

# The single stock-eligibility predicate. `jm` = jute_mr, `mrli` = jute_mr_li.
_MR_STOCK_ELIGIBLE = f"""
              jm.status_id NOT IN ({_MR_EXCLUDED_SQL})
              AND mrli.active = 1
              AND (mrli.status IS NULL OR mrli.status NOT IN ({_MR_LI_EXCLUDED_SQL}))
"""

# Receipt date for period bucketing only — never an eligibility gate. A NULL
# out_date (2,049 sls rows) means "not stamped", not "not received".
_MR_RECEIPT_DATE = "COALESCE(jm.out_date, jm.jute_mr_date, jm.jute_gate_entry_date)"

# Book (accounts-basis) weight of an MR line: approved weight, else the net
# weight when no approved weight was ever recorded. Never COALESCE straight to
# 0 — that drops the receipt while its issues still count.
_MR_ACCOUNTS_WEIGHT = "COALESCE(mrli.accepted_weight, mrli.actual_weight, 0)"

# The MR line's item group, for the FLAG below only — LEFT JOINed so a line
# whose item has no group is still booked (it just is not waste).
_MR_LINE_GROUP_JOIN = """
                LEFT JOIN item_mst     im ON im.item_id     = mrli.actual_item_id
                LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
"""


def _mr_qty_expr(unit_qty: str, kg: str) -> str:
    """Quantity-ledger expression for an MR line or one of its issues.

    Purchased WASTE (owner ruling 2026-09-25: an MR line whose item sits in a
    type-3 group) is kg-only. It is bought in bales/loose units — on sls not
    one of the 486 waste lines has actual_qty == actual_weight (line 45659:
    108 units against 10,042 kg) — but issued as kg slices from the grouped
    waste pool, whose quantity IS the kg. Booking the receipt in units and
    the issue in kg closed such an item at -392 in the qty column. So for a
    type-3 line the qty ledger is the weight ledger on BOTH sides; a raw-jute
    line keeps its unit count.
    """
    return f"CASE WHEN ig.item_type_id = {JUTE_WASTE_ITEM_TYPE_ID} THEN {kg} ELSE {unit_qty} END"


def _receipt_cte(period: str, basis: str = "actual") -> str:
    """
    Stock IN for one period: MR lines + production waste, per item.

    `period` is a filter template containing `{d}` for the date column, e.g.
    "{d} < :date_from". Emits item_id, wt, qty, val (floor waste carries no
    value; purchased waste — a type-3 MR line — is valued like any MR line and
    its qty is its kg, see `_mr_qty_expr`).

    basis="accounts" (decision D5, 2026-09-03): receipt dated on jute_mr_date,
    weight = accepted_weight, value = accepted_weight/100 * rate. The waste arm
    is unaffected (it carries no MR link).

    accepted_weight falls back to actual_weight when NULL (TKT-000155). It is
    NULL on every pre-FY26-27 sls line and decision D6 ruled "no backfill", so
    reading NULL as 0 booked no receipt while the issue side still drew the
    full weight — sls branch 29 showed 120+ items negative. Same fallback the
    MR money already uses (`calculate_mr_amounts` in mr.py).
    """
    if basis == "accounts":
        date_expr = "jm.jute_mr_date"
        wt_expr = _MR_ACCOUNTS_WEIGHT
        val_expr = f"({_MR_ACCOUNTS_WEIGHT} / 100) * COALESCE(mrli.rate, 0)"
    else:
        date_expr = _MR_RECEIPT_DATE
        wt_expr = "COALESCE(mrli.actual_weight, 0)"
        val_expr = "COALESCE(mrli.total_price, 0)"
    return f"""
            SELECT src.item_id AS item_id,
                   ROUND(SUM(src.wt), 3) AS wt,
                   ROUND(SUM(src.qty), 3) AS qty,
                   ROUND(SUM(src.val), 2) AS val
            FROM (
                SELECT mrli.actual_item_id AS item_id,
                       {wt_expr} AS wt,
                       {_mr_qty_expr("COALESCE(mrli.actual_qty, 0)", wt_expr)} AS qty,
                       {val_expr} AS val
                FROM jute_mr jm
                INNER JOIN jute_mr_li mrli ON mrli.jute_mr_id = jm.jute_mr_id
                {_MR_LINE_GROUP_JOIN}
                WHERE jm.branch_id = :branch_id
                  AND {period.format(d=date_expr)}
                  AND {_MR_STOCK_ELIGIBLE}

                UNION ALL

                -- Waste produced on the floor is the second stock source
                -- (branch resolution mirrors vw_jute_waste_stock_outstanding).
                SELECT w.item_id AS item_id,
                       COALESCE(w.waste_qty, 0) AS wt,
                       COALESCE(w.waste_qty, 0) AS qty,
                       0 AS val
                FROM jute_prod_waste_entry w
                INNER JOIN dept_mst d ON d.dept_id = w.dept_id
                WHERE COALESCE(w.branch_id, d.branch_id) = :branch_id
                  AND w.active = 1
                  AND {period.format(d='w.tran_date')}
            ) src
            GROUP BY src.item_id
    """


def _issue_rows(period: str, basis: str = "actual", with_dims: bool = False) -> str:
    """
    Stock OUT (issues) for one period, UNGROUPED: MR-backed issues + waste issues.

    The one definition of "an issue that left stock". `_issue_cte` groups it by
    item for the balance reports; the yarn-quality issue report (TKT-000214)
    groups the same rows by yarn quality, so the two can never disagree on a
    total.

    with_dims=True adds `yarn_type_id`, `issue_date` (the same date the period
    filter tests) and `is_waste` to both legs. Off by default, which leaves the
    SQL of every existing caller byte-for-byte unchanged.

    is_waste: 1 = wastage, 0 = raw jute. Wastage is a re-issue of process waste
    (the waste leg) or an MR line of a type-3 item — purchased waste, owner
    ruling 2026-09-25. The same line the Jute Stock report draws with its
    is_waste flag, and the complement of the wastage report's "raw jute issued"
    (get_selection_daily_weight_query): MR-sourced AND not a type-3 item.

    MR-backed issues INNER JOIN their line and header and must satisfy the same
    eligibility as the receipt side, so an issue can never outlive its receipt.
    Attribution is mrli.actual_item_id — NOT COALESCE(ji.item_id, ...), which
    produced a NULL group key on a missed join and silently deleted weight.

    basis="accounts" (decision D5): an issue dated before its MR's jute_mr_date
    is shifted to the MR date (a bale can't be issued before it was received on
    the books). The waste arm has no MR link and is unaffected.

    basis="accounts" also books the issue at BOOK weight (TKT-000155): the floor
    issues a line at its net weight (actual_weight) but the books received only
    the approved weight, so a fully issued line with a moisture/shortage claim
    closed negative by exactly the claim (sls line 45287: net 7,037, approved
    6,685, issued 7,037 -> -352). Scaling each issue by the line's
    approved/net ratio makes a fully issued line close at 0 by construction;
    with no claim (or no approved weight) the ratio is 1 and nothing changes.
    """
    if basis == "accounts":
        issue_date_expr = "GREATEST(ji.issue_date, jm.jute_mr_date)"
        mr_issue_wt = (
            "COALESCE(ji.weight, 0)"
            f" * COALESCE({_MR_ACCOUNTS_WEIGHT} / NULLIF(mrli.actual_weight, 0), 1)"
        )
    else:
        issue_date_expr = "ji.issue_date"
        mr_issue_wt = "COALESCE(ji.weight, 0)"
    mr_dims = waste_dims = ""
    if with_dims:
        mr_dims = f"""
                       ji.yarn_type_id AS yarn_type_id,
                       {issue_date_expr} AS issue_date,
                       CASE WHEN ig.item_type_id = {JUTE_WASTE_ITEM_TYPE_ID} THEN 1 ELSE 0 END AS is_waste,"""
        waste_dims = """
                       ji.yarn_type_id AS yarn_type_id,
                       ji.issue_date AS issue_date,
                       1 AS is_waste,"""
    return f"""
                SELECT mrli.actual_item_id AS item_id,{mr_dims}
                       {mr_issue_wt} AS wt,
                       {_mr_qty_expr("COALESCE(ji.quantity, 0)", mr_issue_wt)} AS qty,
                       COALESCE(ji.issue_value, 0) AS val
                FROM jute_issue ji
                INNER JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id
                INNER JOIN jute_mr jm ON jm.jute_mr_id = mrli.jute_mr_id
                {_MR_LINE_GROUP_JOIN}
                WHERE ji.branch_id = :branch_id
                  AND {period.format(d=issue_date_expr)}
                  AND ji.status_id <> {JUTE_ISSUE_EXCLUDED_STATUS}
                  AND {_MR_STOCK_ELIGIBLE}

                UNION ALL

                SELECT ji.item_id AS item_id,{waste_dims}
                       COALESCE(ji.weight, 0) AS wt,
                       COALESCE(ji.quantity, 0) AS qty,
                       COALESCE(ji.issue_value, 0) AS val
                FROM jute_issue ji
                INNER JOIN jute_prod_waste_entry w
                        ON w.waste_entry_id = ji.waste_entry_id AND w.active = 1
                WHERE ji.branch_id = :branch_id
                  AND {period.format(d='ji.issue_date')}
                  AND ji.status_id <> {JUTE_ISSUE_EXCLUDED_STATUS}
                  AND ji.waste_entry_id IS NOT NULL
                  AND ji.jute_mr_li_id IS NULL"""


def _issue_cte(period: str, basis: str = "actual") -> str:
    """Stock OUT (issues) for one period, per item — `_issue_rows` grouped by
    item_id. Emits item_id, wt, qty, val."""
    return f"""
            SELECT src.item_id AS item_id,
                   ROUND(SUM(src.wt), 3) AS wt,
                   ROUND(SUM(src.qty), 3) AS qty,
                   ROUND(SUM(src.val), 2) AS val
            FROM ({_issue_rows(period, basis)}
            ) src
            GROUP BY src.item_id
    """


def _sold_cte(period: str) -> str:
    """
    Stock OUT (sales) for one period: approved raw-jute invoices.

    Units are already quintal — same scale as jute_mr_li.actual_weight.

    Attribution: a sale line that names the MR line it drew from
    (sales_invoice_dtl.jute_mr_li_id, backfilled for sls branch 87 on 2026-08-23)
    is booked against THAT line's item, so receipt, issue and sale share one key.
    Keying on sid.item_id alone left 11 branch-87 lines (1,102 qtl) whose sale
    item differs from the MR line's item as offsetting +/- rows in the per-item
    report while the branch total was right. Unlinked lines keep sid.item_id.
    """
    return f"""
            SELECT COALESCE(mrli.actual_item_id, sid.item_id) AS item_id,
                   ROUND(SUM(COALESCE(NULLIF(sid.sales_weight, 0), sid.quantity)), 3) AS wt,
                   ROUND(SUM(COALESCE(sid.quantity, 0)), 3) AS qty
            FROM sales_invoice sinv
            INNER JOIN sales_invoice_dtl sid ON sid.invoice_id = sinv.invoice_id
            LEFT JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = sid.jute_mr_li_id
            WHERE sinv.branch_id = :branch_id
              AND sinv.invoice_type = :raw_jute_invoice_type_id
              AND sinv.status_id = :approved_status_id
              AND COALESCE(sinv.active, 1) = 1
              AND {period.format(d='sinv.invoice_date')}
            GROUP BY COALESCE(mrli.actual_item_id, sid.item_id)
    """


def _stock_text(sql: str):
    """text() with the sales-side ids pre-bound, so callers need no new params."""
    return text(sql).bindparams(
        bindparam("raw_jute_invoice_type_id", value=RAW_JUTE_INVOICE_TYPE_ID),
        bindparam("approved_status_id", value=SALES_APPROVED_STATUS_ID),
    )


def get_jute_stock_report_query(basis: str = "actual", mtd_rows: bool = True):
    """
    Query to calculate daily jute stock position grouped by item group and item.

    Returns opening stock, receipt, issue, sold, closing stock, and MTD
    receipt/issue for a given branch and report date.

    Stock in  = MR lines (jute_mr + jute_mr_li) + production waste.
    Stock out = jute_issue + approved raw-jute sales invoices.

    Every row also carries `is_waste`, from the item group's own item_type_id
    (3 = Jute Waste) rather than from a name match, so a caller can split any
    column into process waste vs purchased raw jute.
    Both sides share `_MR_STOCK_ELIGIBLE`; see the Cluster A header above.

    A row is suppressed only when EVERY figure it would print is zero: opening,
    receipt, issue, sold, closing AND the two MTD columns. The MTD pair was
    missing from this test from 2026-08-13 until 2026-10-05, so an item used
    up (or sold off) earlier in the month — nothing left, nothing moving on the
    report date — lost its row and took its MTD receipt/issue out of every
    total built on these rows: the report's Grand Total and cards, and the
    Daily Production Report's jute section (sls br 29, 2026-09-30: 7 items,
    MTD issue 360,660 shown against 455,631 actual).

    mtd_rows=False restores the position-only test (the five day figures) for
    a caller that reads the stock position alone — the Issue Planner's godown
    cover, which must not list a quality just because it moved this month.

    basis (decision D5, 2026-09-03): "actual" (default) = today's report,
    unchanged. "accounts" = receipt on jute_mr_date with accepted weight/value,
    issues shifted to the MR date when earlier — see `_receipt_cte`/`_issue_cte`.

    Parameters: :branch_id (int), :report_date (string 'YYYY-MM-DD')
    """
    before = "{d} < :report_date"
    on = "{d} = :report_date"
    mtd = (
        "{d} >= DATE_SUB(:report_date, INTERVAL DAYOFMONTH(:report_date) - 1 DAY)"
        " AND {d} <= :report_date"
    )

    opening = "(COALESCE(rb.wt, 0) - COALESCE(ib.wt, 0) - COALESCE(sb.wt, 0))"
    receipt = "COALESCE(ro.wt, 0)"
    issue = "COALESCE(io.wt, 0)"
    sold = "COALESCE(so.wt, 0)"
    closing = f"({opening} + {receipt} - {issue} - {sold})"
    mtd_present = ""
    if mtd_rows:
        mtd_present = """
            OR ROUND(COALESCE(rm.wt, 0), 2) <> 0
            OR ROUND(COALESCE(im2.wt, 0), 2) <> 0"""

    sql = _group_path_cte() + f""",
        receipt_before AS ({_receipt_cte(before, basis)}),
        issue_before AS ({_issue_cte(before, basis)}),
        sold_before AS ({_sold_cte(before)}),
        receipt_on AS ({_receipt_cte(on, basis)}),
        issue_on AS ({_issue_cte(on, basis)}),
        sold_on AS ({_sold_cte(on)}),
        receipt_mtd AS ({_receipt_cte(mtd, basis)}),
        issue_mtd AS ({_issue_cte(mtd, basis)})

        SELECT
            im.item_grp_id,
            COALESCE(fgp.item_grp_name_path, ig.item_grp_name) AS item_group_name,
            im.item_id,
            im.item_name,
            ROUND({opening}, 3) AS opening_weight,
            ROUND({receipt}, 3) AS receipt_weight,
            ROUND({issue}, 3) AS issue_weight,
            ROUND({sold}, 3) AS sold_weight,
            ROUND({closing}, 3) AS closing_weight,
            ROUND(COALESCE(rm.wt, 0), 3) AS mtd_receipt_weight,
            ROUND(COALESCE(im2.wt, 0), 3) AS mtd_issue_weight,
            CASE WHEN ig.item_type_id = {JUTE_WASTE_ITEM_TYPE_ID} THEN 1 ELSE 0 END AS is_waste
        FROM item_mst im
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        LEFT JOIN full_group_paths fgp ON fgp.item_grp_id = im.item_grp_id
        LEFT JOIN receipt_before rb ON rb.item_id = im.item_id
        LEFT JOIN issue_before ib ON ib.item_id = im.item_id
        LEFT JOIN sold_before sb ON sb.item_id = im.item_id
        LEFT JOIN receipt_on ro ON ro.item_id = im.item_id
        LEFT JOIN issue_on io ON io.item_id = im.item_id
        LEFT JOIN sold_on so ON so.item_id = im.item_id
        LEFT JOIN receipt_mtd rm ON rm.item_id = im.item_id
        LEFT JOIN issue_mtd im2 ON im2.item_id = im.item_id
        WHERE (
            ROUND({opening}, 2) <> 0
            OR ROUND({receipt}, 2) <> 0
            OR ROUND({issue}, 2) <> 0
            OR ROUND({sold}, 2) <> 0
            OR ROUND({closing}, 2) <> 0{mtd_present}
        )
        ORDER BY ig.item_grp_name, im.item_name
    """
    return _stock_text(sql)


# Batch daily assignment statuses that feed the planned weight (Open, Approved).
# Draft (21) and Pending approval (20) assignments are not a plan yet.
BATCH_ASSIGN_COUNTED_STATUSES = (1, 3)

# Jute waste re-issued into a batch (owner ruling 2026-10-08, option 3): it is
# NOT part of the batch cost — not in weight, value or cost per ton — and is
# reported only as a memo line under its yarn quality (rows flagged is_waste).
# It stays in the planned-weight base, because batch plans list waste
# qualities too. Waste =
# an item whose group is the jute-waste item type (floor waste and purchased
# waste alike). Needs `mrli` joined (the basis mr_join always does).
_BATCH_WASTE_JOIN = (
    "LEFT JOIN item_mst wim ON wim.item_id = COALESCE(ji.item_id, mrli.actual_item_id) "
    "LEFT JOIN item_grp_mst wig ON wig.item_grp_id = wim.item_grp_id"
)
_BATCH_IS_WASTE = f"(COALESCE(wig.item_type_id, 0) = {JUTE_WASTE_ITEM_TYPE_ID})"


def _batch_cost_basis_sql(basis: str):
    """(issue_status, mr_join, date_expr, value_expr) for the batch cost
    report's basis — shared by the report query and its plan-coverage query so
    both date an issue the same way."""
    issue_status = f"ji.status_id <> {JUTE_ISSUE_EXCLUDED_STATUS}"
    if basis == "accounts":
        mr_join = "LEFT JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id LEFT JOIN jute_mr jm ON jm.jute_mr_id = mrli.jute_mr_id"
        date_expr = "COALESCE(GREATEST(ji.issue_date, jm.jute_mr_date), ji.issue_date)"
        value_expr = "(ji.weight / 100) * COALESCE(mrli.rate, ji.issue_value / NULLIF(ji.weight, 0) * 100)"
    else:
        mr_join = "LEFT JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id"
        date_expr = "ji.issue_date"
        value_expr = "ji.issue_value"
    return issue_status, mr_join, date_expr, value_expr


def get_batch_cost_plan_coverage_query(basis: str = "actual"):
    """
    TKT-000206: why is planned weight 0? Per day x yarn type over the month so
    far (:month_start .. :report_date, same issue eligibility and basis dating
    as get_batch_cost_report_query):

    - every yarn type that had jute issued, with its issued weight and whether
      a batch plan was assigned to it that day — `counted` (Open/Approved, the
      only ones the report plans from), `pending` (20) or `draft` (21);
    - plus every yarn type with an assignment on the report date but nothing
      issued (weight 0) — planned weight is plan % x actual issue, so it is 0.

    Issues without a yarn type are left out: they can never have a plan.
    Assignments are matched on assign_date, as the report's planned CTE does.
    """
    issue_status, mr_join, date_expr, _value = _batch_cost_basis_sql(basis)
    waste_join, is_waste = _BATCH_WASTE_JOIN, _BATCH_IS_WASTE
    counted = ", ".join(str(s) for s in BATCH_ASSIGN_COUNTED_STATUSES)
    sql = f"""
        WITH issued AS (
            SELECT {date_expr} AS d, ji.yarn_type_id, SUM(ji.weight) AS weight
            FROM jute_issue ji
            {mr_join}
            {waste_join}
            WHERE ji.branch_id = :branch_id
              AND {date_expr} BETWEEN :month_start AND :report_date
              AND {issue_status}
              AND ji.yarn_type_id IS NOT NULL
              AND NOT {is_waste}
            GROUP BY {date_expr}, ji.yarn_type_id
        ),
        assigned AS (
            SELECT
                bda.assign_date AS d,
                bda.jute_yarn_id AS yarn_type_id,
                MAX(CASE WHEN bda.status_id IN ({counted}) THEN 1 ELSE 0 END) AS counted,
                MAX(CASE WHEN bda.status_id = 20 THEN 1 ELSE 0 END) AS pending,
                MAX(CASE WHEN bda.status_id = 21 THEN 1 ELSE 0 END) AS draft
            FROM jute_batch_daily_assign bda
            WHERE bda.branch_id = :branch_id
              AND bda.assign_date BETWEEN :month_start AND :report_date
            GROUP BY bda.assign_date, bda.jute_yarn_id
        ),
        facts AS (
            SELECT i.d, i.yarn_type_id, i.weight,
                   COALESCE(a.counted, 0) AS counted,
                   COALESCE(a.pending, 0) AS pending,
                   COALESCE(a.draft, 0) AS draft
            FROM issued i
            LEFT JOIN assigned a ON a.d = i.d AND a.yarn_type_id = i.yarn_type_id
            UNION ALL
            SELECT a.d, a.yarn_type_id, 0, a.counted, a.pending, a.draft
            FROM assigned a
            LEFT JOIN issued i ON i.d = a.d AND i.yarn_type_id = a.yarn_type_id
            WHERE a.d = :report_date AND i.yarn_type_id IS NULL
        )
        SELECT
            f.d,
            f.yarn_type_id,
            COALESCE(yim.item_name, jym.jute_yarn_name) AS yarn_type_name,
            f.weight,
            f.counted,
            f.pending,
            f.draft
        FROM facts f
        LEFT JOIN jute_yarn_mst jym ON jym.jute_yarn_id = f.yarn_type_id
        LEFT JOIN item_mst yim ON yim.item_id = jym.item_id
        ORDER BY f.d, yarn_type_name
    """
    return text(sql)


def get_batch_cost_report_query(basis: str = "actual"):
    """
    Query to calculate yarn quality-wise planned vs actual jute issue
    for a given branch and date (batch cost report), for the day AND
    month-to-date (TKT-000213, owner rulings 2026-10-05).

    Day columns (the report date only) — unchanged meaning:
    - planned_weight: (batch_plan_li percentage / 100) * that day's total actual
      issue weight for the yarn type
    - actual_weight: sum of jute_issue weight grouped by yarn_type + item
    - actual_rate: weighted (SUM(issue_value)/SUM(weight)*100), NOT AVG(actual_rate) —
      actual_rate is NULL on ~99.5% of lines, so an unweighted AVG silently zeroed
      the report for almost everyone.
    - issue_value: sum of the issue value expression (basis-dependent, below)
    - variance: actual_weight - planned_weight

    MTD columns (1st of the report date's month through the report date,
    inclusive; same eligibility, item fallback and date expression as the day):
    - mtd_actual_weight: SUM(jute_issue.weight) over the range
    - mtd_actual_rate: mtd_issue_value / mtd_actual_weight * 100 — weighted,
      never an average of daily rates. On the "actual" basis this is exactly
      the day rate's formula. On "accounts" the day rate (kept as it was) still
      divides the STORED issue_value, which is 0 on many sls lines, while the
      MTD rate divides the accounts value, so MTD rate x weight = MTD value.
    - mtd_issue_value: the day value expression summed over the range
    - mtd_planned_weight: planned is computed PER DAY (that day's assignment %
      x that day's yarn total), then summed — a month-level % x month total
      would mis-weight days that ran a different batch plan
    - mtd_variance: mtd_actual_weight - mtd_planned_weight

    Rows: every yarn type x jute quality issued or planned on ANY day of the
    month so far; the day columns read 0 when nothing moved on the report
    date, and the MTD column totals equal the month's total issue.

    Issues and plan lines are stacked into one fact list and grouped once by
    (yarn type, quality) — a null-safe FULL OUTER JOIN without MySQL's missing
    FULL JOIN or a UNION that silently collapses identical rows.

    Fixes (verified against sls 2026-09-03, decision D5):
    1. status filter was `ji.status_id IN (1, 3)`, hiding every Draft issue while
       the stock view (vw_jute_stock_outstanding) counts them — aligned to `<> 4`
       (JUTE_ISSUE_EXCLUDED_STATUS), same rule as the stock report.
    2. `AVG(mrli.actual_rate)` replaced by the weighted rate above.
    3. NULL yarn_type_id/item rows now surface as '(unassigned)' instead of a
       blank bucket.

    basis (decision D5, 2026-09-03): "actual" (default) = issue_date, issue_value
    as stored. "accounts" = an issue dated before its MR's jute_mr_date is shifted
    to the MR date (the month range tests that same shifted date), and the value
    takes mrli.rate, falling back to the issue's own rate when the line has none.

    Parameters: :branch_id (int), :month_start, :report_date ('YYYY-MM-DD' —
    the router derives month_start, so the SQL carries no date arithmetic).
    """
    issue_status, mr_join, date_expr, value_expr = _batch_cost_basis_sql(basis)
    is_day = "ji.d = :report_date"
    waste_join, is_waste = _BATCH_WASTE_JOIN, _BATCH_IS_WASTE

    sql = f"""
        -- CTE 1: every eligible issue line of the month so far, dated by the basis
        WITH issue_rows AS (
            SELECT
                ji.yarn_type_id,
                COALESCE(ji.item_id, mrli.actual_item_id) AS item_id,
                {date_expr} AS d,
                ji.weight,
                ji.issue_value,
                {value_expr} AS val,
                CASE WHEN {is_waste} THEN 1 ELSE 0 END AS is_waste
            FROM jute_issue ji
            {mr_join}
            {waste_join}
            WHERE ji.branch_id = :branch_id
              AND {date_expr} BETWEEN :month_start AND :report_date
              AND {issue_status}
        ),

        -- CTE 2: total actual issue weight per yarn type per day (the plan % base).
        -- Waste stays IN the base: batch plans carry waste qualities too, so a
        -- plan's percentages are shares of jute + waste (the cost excludes waste)
        yarn_totals AS (
            SELECT
                yarn_type_id,
                d,
                ROUND(COALESCE(SUM(weight), 0), 3) AS total_actual_weight
            FROM issue_rows
            GROUP BY yarn_type_id, d
        ),

        -- CTE 3: planned weight per yarn type x quality per day, from that day's
        -- batch daily assignment + batch plan line items
        planned AS (
            SELECT
                bda.jute_yarn_id AS yarn_type_id,
                bpl.jute_quality_id AS item_id,
                bda.assign_date AS d,
                ROUND((bpl.percentage / 100.0) * COALESCE(yt.total_actual_weight, 0), 3) AS planned_weight,
                CASE WHEN COALESCE(pig.item_type_id, 0) = {JUTE_WASTE_ITEM_TYPE_ID} THEN 1 ELSE 0 END AS is_waste
            FROM jute_batch_daily_assign bda
            INNER JOIN jute_batch_plan_li bpl ON bpl.batch_plan_id = bda.batch_plan_id
            LEFT JOIN item_mst pim ON pim.item_id = bpl.jute_quality_id
            LEFT JOIN item_grp_mst pig ON pig.item_grp_id = pim.item_grp_id
            LEFT JOIN yarn_totals yt ON yt.yarn_type_id = bda.jute_yarn_id AND yt.d = bda.assign_date
            WHERE bda.branch_id = :branch_id
              AND bda.assign_date BETWEEN :month_start AND :report_date
              AND bda.status_id IN (1, 3)
        ),

        -- CTE 4: day + MTD figures per yarn type x quality. `ji` = the month's
        -- issue lines stacked with its plan lines (whose weight/value are NULL).
        agg AS (
            SELECT
                ji.yarn_type_id,
                ji.item_id,
                ROUND(COALESCE(SUM(CASE WHEN {is_day} THEN ji.planned_weight END), 0), 3) AS planned_weight,
                ROUND(COALESCE(SUM(CASE WHEN {is_day} THEN ji.weight END), 0), 3) AS actual_weight,
                ROUND(CASE WHEN SUM(CASE WHEN {is_day} THEN ji.weight END) > 0
                      THEN SUM(CASE WHEN {is_day} THEN COALESCE(ji.issue_value, 0) END)
                           / SUM(CASE WHEN {is_day} THEN ji.weight END) * 100
                      ELSE 0 END, 2) AS actual_rate,
                ROUND(COALESCE(SUM(CASE WHEN {is_day} THEN ji.val END), 0), 2) AS issue_value,
                ROUND(COALESCE(SUM(ji.planned_weight), 0), 3) AS mtd_planned_weight,
                ROUND(COALESCE(SUM(ji.weight), 0), 3) AS mtd_actual_weight,
                ROUND(CASE WHEN SUM(ji.weight) > 0 THEN SUM(COALESCE(ji.val, 0)) / SUM(ji.weight) * 100 ELSE 0 END, 2) AS mtd_actual_rate,
                ROUND(COALESCE(SUM(ji.val), 0), 2) AS mtd_issue_value,
                MAX(ji.is_waste) AS is_waste
            FROM (
                SELECT yarn_type_id, item_id, d, weight, issue_value, val, NULL AS planned_weight, is_waste
                FROM issue_rows
                UNION ALL
                SELECT yarn_type_id, item_id, d, NULL, NULL, NULL, planned_weight, is_waste
                FROM planned
            ) ji
            GROUP BY ji.yarn_type_id, ji.item_id
        )

        SELECT
            a.yarn_type_id,
            COALESCE(yim.item_name, jym.jute_yarn_name, '(unassigned)') AS yarn_type_name,
            a.item_id,
            im.item_name,
            im.item_grp_id,
            igm.item_grp_name AS item_group_name,
            a.planned_weight,
            a.actual_weight,
            a.actual_rate,
            a.issue_value,
            ROUND(a.actual_weight - a.planned_weight, 3) AS variance,
            a.mtd_planned_weight,
            a.mtd_actual_weight,
            a.mtd_actual_rate,
            a.mtd_issue_value,
            ROUND(a.mtd_actual_weight - a.mtd_planned_weight, 3) AS mtd_variance,
            -- 1 = jute waste re-issued: a memo line, not part of the batch cost
            a.is_waste
        FROM agg a
        LEFT JOIN jute_yarn_mst jym ON jym.jute_yarn_id = a.yarn_type_id
        LEFT JOIN item_mst yim ON yim.item_id = jym.item_id
        LEFT JOIN item_mst im ON im.item_id = a.item_id
        LEFT JOIN item_grp_mst igm ON igm.item_grp_id = im.item_grp_id
        ORDER BY yarn_type_name, im.item_name
    """
    return text(sql)


def get_jute_issue_yarn_wise_query():
    """
    Jute Issue — Yarn Quality Wise (TKT-000214): per yarn quality, the jute
    issued ON the report date and month-to-date, in KG (jute_issue.weight is
    kg; the caller divides by 1000 for MT).

    Rows come from `_issue_rows` — the same legs, eligibility and dating as the
    Jute Stock (Production) report's Issue column and the winding DPR's "jute
    issue" figure (get_jute_issue_weight_query) — so the grand totals tie out
    to both, day and month-to-date. That means: rejected issues (status 4) are
    out, Draft issues count, waste re-issues count under the yarn quality they
    were issued for.

    day_issue_kg / mtd_issue_kg are raw jute + wastage; day_waste_kg /
    mtd_waste_kg are the wastage part of each (`_issue_rows.is_waste`), so raw
    jute = issue - waste and the page can exclude wastage without a second
    request.

    One row per yarn quality with any issue in the month so far; a quality
    with nothing on the day itself still shows, at 0 for the day. Issues with
    no yarn quality surface as '(unassigned)' rather than vanishing.

    Parameters: :branch_id (int), :month_start, :report_date ('YYYY-MM-DD' —
    the router derives month_start, so the SQL carries no date arithmetic).
    """
    mtd = "{d} BETWEEN :month_start AND :report_date"
    sql = f"""
        SELECT
            src.yarn_type_id AS yarn_type_id,
            COALESCE(yim.item_name, jym.jute_yarn_name, '(unassigned)') AS yarn_type_name,
            ROUND(SUM(CASE WHEN src.issue_date = :report_date THEN src.wt ELSE 0 END), 3) AS day_issue_kg,
            ROUND(SUM(src.wt), 3) AS mtd_issue_kg,
            ROUND(SUM(CASE WHEN src.is_waste = 1 AND src.issue_date = :report_date THEN src.wt ELSE 0 END), 3) AS day_waste_kg,
            ROUND(SUM(CASE WHEN src.is_waste = 1 THEN src.wt ELSE 0 END), 3) AS mtd_waste_kg
        FROM ({_issue_rows(mtd, with_dims=True)}
        ) src
        LEFT JOIN jute_yarn_mst jym ON jym.jute_yarn_id = src.yarn_type_id
        LEFT JOIN item_mst yim ON yim.item_id = jym.item_id
        GROUP BY src.yarn_type_id, yim.item_name, jym.jute_yarn_name
        ORDER BY yarn_type_name
    """
    return text(sql)


# The date an MR is listed under in the MR list: its MR date once it has one
# (approve / finalize), else its gate-entry date (Open, Pending, ...).
MR_LIST_ANY_DATE_SQL = "COALESCE(jm.jute_mr_date, jm.jute_gate_entry_date)"


def _mr_list_base_from_clause(status_ids: tuple = ()):
    """
    Shared FROM + JOIN + WHERE clauses for the MR list report and its count query.
    Note: the caller supplies the SELECT and pagination/ORDER BY.

    MRs are placed by MR_LIST_ANY_DATE_SQL, so lorries that are Open or out on
    a transfer chain (Pending, no MR date yet) fall in the range too.
    status_ids: the statuses ticked on the report (ints, inlined); empty = every
    status.

    Parameters (bind):
        :co_id (int, required)
        :branch_id (int or NULL) - optional branch filter
        :date_from (str YYYY-MM-DD, required)
        :date_to (str YYYY-MM-DD, required)
        :search_like (str or NULL) - LIKE pattern for party name, vehicle_no,
                                     challan_no, and formatted MR number
    """
    mr_num_expr = get_jute_mr_number_sql_expression()
    status_sql = (
        f"AND jm.status_id IN ({', '.join(str(int(s)) for s in status_ids)})"
        if status_ids else ""
    )
    return f"""
        FROM jute_mr jm
        INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id
        INNER JOIN co_mst cm ON cm.co_id = bm.co_id
        LEFT JOIN jute_supplier_mst jsm ON jsm.supplier_id = jm.jute_supplier_id
        LEFT JOIN party_mst pm ON pm.party_id = jm.party_id
        LEFT JOIN status_mst sm ON sm.status_id = jm.status_id
        WHERE bm.co_id = :co_id
          {status_sql}
          AND {MR_LIST_ANY_DATE_SQL} BETWEEN :date_from AND :date_to
          AND (:branch_id IS NULL OR jm.branch_id = :branch_id)
          AND (
              :search_like IS NULL
              OR COALESCE(pm.supp_name, '') LIKE :search_like
              OR COALESCE(jsm.supplier_name, '') LIKE :search_like
              OR COALESCE(jm.vehicle_no, '') LIKE :search_like
              OR COALESCE(jm.challan_no, '') LIKE :search_like
              OR {mr_num_expr} LIKE :search_like
          )
    """


def get_mr_list_query(status_ids: tuple = ()):
    """
    Query to list Jute MR headers for the MR list report, filtered to the
    ticked statuses (see _mr_list_base_from_clause).

    Returns one row per MR header with supplier/party, branch, challan, weights,
    totals, and status info. Uses jute_mr_li.total_price as a fallback when
    jute_mr.total_amount / jute_mr.net_total are NULL.

    Parameters (bind):
        :co_id (int, required)
        :branch_id (int or NULL)
        :date_from (str YYYY-MM-DD, required)
        :date_to (str YYYY-MM-DD, required)
        :search_like (str or NULL)
        :limit (int)
        :offset (int)
    """
    mr_num_expr = get_jute_mr_number_sql_expression()
    from_clause = _mr_list_base_from_clause(status_ids)

    sql = f"""
        SELECT
            jm.jute_mr_id,
            {mr_num_expr} AS mr_number,
            jm.jute_mr_date,
            jm.jute_gate_entry_date,
            jm.jute_gate_entry_no,
            jm.is_returned,
            jm.branch_id,
            bm.branch_name,
            jm.jute_supplier_id AS party_id,
            COALESCE(pm.supp_name, jsm.supplier_name) AS party_name,
            jm.vehicle_no,
            jm.challan_no,
            jm.challan_date,
            jm.gross_weight,
            jm.net_weight,
            jm.actual_weight,
            COALESCE(
                jm.total_amount,
                (
                    SELECT COALESCE(SUM(mrli.total_price), 0)
                    FROM jute_mr_li mrli
                    WHERE mrli.jute_mr_id = jm.jute_mr_id
                      AND (mrli.active = 1 OR mrli.active IS NULL)
                )
            ) AS total_amount,
            COALESCE(
                jm.net_total,
                (
                    SELECT COALESCE(SUM(mrli.total_price), 0)
                    FROM jute_mr_li mrli
                    WHERE mrli.jute_mr_id = jm.jute_mr_id
                      AND (mrli.active = 1 OR mrli.active IS NULL)
                )
            ) AS net_total,
            jm.status_id,
            COALESCE(
                sm.status_name,
                CASE jm.status_id
                    WHEN 3 THEN 'Approved'
                    WHEN 13 THEN 'Finalised'
                    ELSE CAST(jm.status_id AS CHAR)
                END
            ) AS status_name
        {from_clause}
        ORDER BY {MR_LIST_ANY_DATE_SQL} DESC, jm.jute_mr_id DESC
        LIMIT :limit OFFSET :offset
    """
    return text(sql)


def get_mr_list_count_query(status_ids: tuple = ()):
    """
    Count query for the Jute MR list report (matches get_mr_list_query filters).

    Parameters (bind): same filter params as get_mr_list_query (no limit/offset).
    """
    from_clause = _mr_list_base_from_clause(status_ids)
    sql = f"""
        SELECT COUNT(*) AS total
        {from_clause}
    """
    return text(sql)


def get_mr_list_statuses_query():
    """
    Status options for the MR list report's status multi-select: the statuses
    the company's MRs actually carry (they differ per tenant).

    Parameters (bind):
        :co_id (int, required)
    """
    sql = """
        SELECT jm.status_id,
               COALESCE(MAX(sm.status_name), CONCAT('Status ', jm.status_id)) AS status_name
        FROM jute_mr jm
        INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id
        LEFT JOIN status_mst sm ON sm.status_id = jm.status_id
        WHERE bm.co_id = :co_id
          AND jm.status_id IS NOT NULL
        GROUP BY jm.status_id
        ORDER BY jm.status_id
    """
    return text(sql)


_JUTE_PURCHASE_TALLY_SCOPE = """
        FROM jute_mr jm
        INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id
        INNER JOIN co_mst cm ON cm.co_id = bm.co_id"""

_JUTE_PURCHASE_TALLY_WHERE = f"""
        WHERE bm.co_id = :co_id
          AND jm.status_id IN ({", ".join(str(s) for s in JUTE_MR_TALLY_STATUSES)})
          AND jm.jute_mr_date BETWEEN :date_from AND :date_to
          AND (:branch_id IS NULL OR jm.branch_id = :branch_id)"""


def get_jute_purchase_tally_header_query():
    """Jute Purchase Tally export -- one row per approved MR (voucher header data).

    Money is not aggregated here: the builder (reports.build_purchase_tally_rows)
    sums it from the line query. Tally names come from tally_link_mst and fall
    back to the ERP name (godown to blank, ledgers to LEDGER_DEFAULTS). Supplier
    details are the MR's party branch; consignee is the own company at the
    receiving branch. tds_amount stays NULL when the MR never stored one.

    Bind params: :co_id, :branch_id (or NULL), :date_from, :date_to (MR date).
    """
    mr_num_expr = get_jute_mr_number_sql_expression()
    sql = f"""
        SELECT
            jm.jute_mr_id,
            {mr_num_expr}                                              AS mr_no,
            jm.jute_mr_date,
            jm.branch_mr_no,
            jm.party_id,
            jm.invoice_no,
            jm.invoice_date,
            jm.challan_no,
            jm.challan_date,
            jm.unit_conversion,
            jm.tds_amount,
            COALESCE(tlp.tally_name, pm.supp_name, jsm.supplier_name, '') AS party_name,
            pb.address                                                 AS party_address,
            pb.address_additional                                      AS party_address2,
            pb.zip_code                                                AS party_zip,
            pb.gst_no                                                  AS party_gstin,
            COALESCE(pst.state, '')                                    AS party_state,
            cm.co_name                                                 AS consignee_name,
            bm.branch_address1,
            bm.branch_address2,
            bm.branch_zipcode,
            COALESCE(bst.state, '')                                    AS branch_state,
            COALESCE(tlg.tally_name, '')                               AS godown_name,
            COALESCE(tlpl.tally_name, '{LEDGER_DEFAULTS["PURCHASE_LEDGER"]}') AS purchase_ledger,
            COALESCE(tlcl.tally_name, '{LEDGER_DEFAULTS["PURCHASE_CLAIM"]}')  AS claim_ledger,
            COALESCE(tltd.tally_name, '{LEDGER_DEFAULTS["TDS_194Q"]}')        AS tds_ledger,
            COALESCE(tlro.tally_name, '{LEDGER_DEFAULTS["ROUND_OFF"]}')       AS round_off_ledger
        {_JUTE_PURCHASE_TALLY_SCOPE}
        LEFT JOIN party_mst pm ON pm.party_id = jm.party_id
        LEFT JOIN jute_supplier_mst jsm ON jsm.supplier_id = jm.jute_supplier_id
        LEFT JOIN party_branch_mst pb ON pb.party_mst_branch_id = jm.party_branch_id
        LEFT JOIN state_mst pst ON pst.state_id = pb.state_id
        LEFT JOIN state_mst bst ON bst.state_id = bm.state_id
        {_tally_name_join("tlp", "PARTY", "jm.party_id")}
        {_tally_name_join("tlg", "GODOWN", "0")}
        {_tally_name_join("tlpl", "PURCHASE_LEDGER", "0")}
        {_tally_name_join("tlcl", "PURCHASE_CLAIM", "0")}
        {_tally_name_join("tltd", "TDS_194Q", "0")}
        {_tally_name_join("tlro", "ROUND_OFF", "0")}
        {_JUTE_PURCHASE_TALLY_WHERE}
        ORDER BY COALESCE(jm.invoice_date, jm.jute_mr_date), jm.jute_mr_date,
                 jm.branch_mr_no, jm.jute_mr_id
    """
    return text(sql)


def get_jute_purchase_tally_line_query():
    """Jute Purchase Tally export -- one row per live line of the approved MRs.

    accepted_weight is kg and rate / claim_rate are per quintal (FY 26-27 on);
    older imported MRs have no accepted_weight and are skipped by the builder.

    Bind params: same as get_jute_purchase_tally_header_query.
    """
    sql = f"""
        SELECT
            mrli.jute_mr_id,
            mrli.jute_mr_li_id,
            COALESCE(tli.tally_name, im.item_name, '')                AS item_name,
            mrli.accepted_weight,
            mrli.actual_qty,
            mrli.rate,
            mrli.claim_rate
        {_JUTE_PURCHASE_TALLY_SCOPE}
        INNER JOIN jute_mr_li mrli ON mrli.jute_mr_id = jm.jute_mr_id
        LEFT JOIN item_mst im ON im.item_id = mrli.actual_item_id
        {_tally_name_join("tli", "ITEM", "mrli.actual_item_id")}
        {_JUTE_PURCHASE_TALLY_WHERE}
          AND (mrli.active = 1 OR mrli.active IS NULL)
          AND (mrli.status IS NULL OR mrli.status NOT IN ({_MR_LI_EXCLUDED_SQL}))
        ORDER BY mrli.jute_mr_id, mrli.jute_mr_li_id
    """
    return text(sql)


def get_jute_qty_wise_report_query():
    """
    Jute Quantity Wise report (#2) — opening / receipt / issue / closing per
    jute quality (item) over a date range, in both weight (kg) and quantity.

    Also backs the Inventory Snapshot (#14): pass an early :date_from (e.g. the
    financial-year start) and :date_to = the as-of date.

    Stock in  = MR lines + production waste; stock out = issues + approved
    raw-jute sales. Both sides share `_MR_STOCK_ELIGIBLE` — see the Cluster A
    header. All-zero rows are suppressed.

    Parameters: :branch_id (int), :date_from (str 'YYYY-MM-DD'),
                :date_to (str 'YYYY-MM-DD')
    """
    before = "{d} < :date_from"
    rng = "{d} BETWEEN :date_from AND :date_to"

    open_wt = "(COALESCE(rb.wt, 0) - COALESCE(ib.wt, 0) - COALESCE(sb.wt, 0))"
    open_qty = "(COALESCE(rb.qty, 0) - COALESCE(ib.qty, 0) - COALESCE(sb.qty, 0))"
    close_wt = f"({open_wt} + COALESCE(rr.wt, 0) - COALESCE(ir.wt, 0) - COALESCE(sr.wt, 0))"
    close_qty = f"({open_qty} + COALESCE(rr.qty, 0) - COALESCE(ir.qty, 0) - COALESCE(sr.qty, 0))"

    sql = _group_path_cte() + f""",
        receipt_before AS ({_receipt_cte(before)}),
        issue_before AS ({_issue_cte(before)}),
        sold_before AS ({_sold_cte(before)}),
        receipt_range AS ({_receipt_cte(rng)}),
        issue_range AS ({_issue_cte(rng)}),
        sold_range AS ({_sold_cte(rng)})

        SELECT
            im.item_grp_id,
            COALESCE(fgp.item_grp_name_path, ig.item_grp_name) AS item_group_name,
            im.item_id,
            im.item_name,
            ROUND({open_wt}, 3) AS opening_weight,
            ROUND({open_qty}, 3) AS opening_qty,
            ROUND(COALESCE(rr.wt, 0), 3) AS receipt_weight,
            ROUND(COALESCE(rr.qty, 0), 3) AS receipt_qty,
            ROUND(COALESCE(ir.wt, 0), 3) AS issue_weight,
            ROUND(COALESCE(ir.qty, 0), 3) AS issue_qty,
            ROUND(COALESCE(sr.wt, 0), 3) AS sold_weight,
            ROUND(COALESCE(sr.qty, 0), 3) AS sold_qty,
            ROUND({close_wt}, 3) AS closing_weight,
            ROUND({close_qty}, 3) AS closing_qty
        FROM item_mst im
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        LEFT JOIN full_group_paths fgp ON fgp.item_grp_id = im.item_grp_id
        LEFT JOIN receipt_before rb ON rb.item_id = im.item_id
        LEFT JOIN issue_before ib ON ib.item_id = im.item_id
        LEFT JOIN sold_before sb ON sb.item_id = im.item_id
        LEFT JOIN receipt_range rr ON rr.item_id = im.item_id
        LEFT JOIN issue_range ir ON ir.item_id = im.item_id
        LEFT JOIN sold_range sr ON sr.item_id = im.item_id
        WHERE (
            ROUND({open_wt}, 2) <> 0 OR ROUND({open_qty}, 2) <> 0
            OR ROUND(COALESCE(rr.wt, 0), 2) <> 0 OR ROUND(COALESCE(rr.qty, 0), 2) <> 0
            OR ROUND(COALESCE(ir.wt, 0), 2) <> 0 OR ROUND(COALESCE(ir.qty, 0), 2) <> 0
            OR ROUND(COALESCE(sr.wt, 0), 2) <> 0 OR ROUND(COALESCE(sr.qty, 0), 2) <> 0
            OR ROUND({close_wt}, 2) <> 0 OR ROUND({close_qty}, 2) <> 0
        )
        ORDER BY ig.item_grp_name, im.item_name
    """
    return _stock_text(sql)


def get_jute_percent_claims_query():
    """
    Jute Percent Claims (#7) — per-supplier pass/claim counts and percentages.

    An MR is a "claim" if ANY of its active lines has shortage_kgs > 0 OR
    claim_rate > 0; otherwise it "passes". Percentages are by MR count.

    Parameters: :branch_id (int), :date_from, :date_to (str 'YYYY-MM-DD')
    """
    sql = """
        WITH mr_claim AS (
            SELECT
                jm.jute_mr_id,
                jm.jute_supplier_id,
                MAX(CASE
                    WHEN COALESCE(mrli.shortage_kgs, 0) > 0
                      OR COALESCE(mrli.claim_rate, 0) > 0
                    THEN 1 ELSE 0 END) AS is_claim
            FROM jute_mr jm
            INNER JOIN jute_mr_li mrli ON mrli.jute_mr_id = jm.jute_mr_id
            WHERE jm.branch_id = :branch_id
              AND jm.jute_mr_date BETWEEN :date_from AND :date_to
              AND jm.status_id IN (1, 3, 13, 48)
              AND (mrli.active = 1 OR mrli.active IS NULL)
            GROUP BY jm.jute_mr_id, jm.jute_supplier_id
        )
        SELECT
            mc.jute_supplier_id AS supplier_id,
            COALESCE(jsm.supplier_name, CAST(mc.jute_supplier_id AS CHAR)) AS supplier_name,
            COUNT(*) AS total_mr,
            SUM(CASE WHEN mc.is_claim = 0 THEN 1 ELSE 0 END) AS pass_count,
            SUM(mc.is_claim) AS claim_count,
            ROUND(100 * SUM(CASE WHEN mc.is_claim = 0 THEN 1 ELSE 0 END) / COUNT(*), 2) AS pass_percent,
            ROUND(100 * SUM(mc.is_claim) / COUNT(*), 2) AS claim_percent
        FROM mr_claim mc
        LEFT JOIN jute_supplier_mst jsm ON jsm.supplier_id = mc.jute_supplier_id
        GROUP BY mc.jute_supplier_id, supplier_name
        ORDER BY supplier_name
    """
    return text(sql)


def get_jute_mukham_moisture_query():
    """
    Mukham Moisture (#9) — per supplier + mukam, average supplied moisture vs
    allowed moisture and the deviation.

    Supplied moisture = AVG(jute_moisture_rdg.moisture_percentage) (authoritative
    numeric reading); allowed = AVG(jute_mr_li.allowable_moisture).

    Parameters: :branch_id (int), :date_from, :date_to (str 'YYYY-MM-DD')
    """
    sql = """
        SELECT
            jm.jute_supplier_id AS supplier_id,
            COALESCE(jsm.supplier_name, CAST(jm.jute_supplier_id AS CHAR)) AS supplier_name,
            COALESCE(mk.mukam_name, CAST(jm.mukam_id AS CHAR)) AS mukam_name,
            ROUND(AVG(jmr.moisture_percentage), 2) AS avg_supplied_moisture,
            ROUND(AVG(mrli.allowable_moisture), 2) AS avg_allowed_moisture,
            ROUND(AVG(jmr.moisture_percentage) - AVG(mrli.allowable_moisture), 2) AS deviation
        FROM jute_mr jm
        INNER JOIN jute_mr_li mrli ON mrli.jute_mr_id = jm.jute_mr_id
        INNER JOIN jute_moisture_rdg jmr ON jmr.jute_mr_li_id = mrli.jute_mr_li_id
        LEFT JOIN jute_supplier_mst jsm ON jsm.supplier_id = jm.jute_supplier_id
        LEFT JOIN jute_mukam_mst mk ON mk.mukam_id = jm.mukam_id
        WHERE jm.branch_id = :branch_id
          AND jm.jute_mr_date BETWEEN :date_from AND :date_to
          AND jm.status_id IN (1, 3, 13, 48)
          AND (mrli.active = 1 OR mrli.active IS NULL)
        GROUP BY jm.jute_supplier_id, supplier_name, jm.mukam_id, mukam_name
        ORDER BY supplier_name, mukam_name
    """
    return text(sql)


def get_jute_txn_summary_query(txn_type: str = "ALL"):
    """
    Jute Issue & Receipt Summary (#3) — transaction-level rows (one per MR line
    for receipts, one per issue for issues) over a date range.

    txn_type: 'R' (receipts only), 'I' (issues only), or 'ALL' (both).
    Issue rate has no source column (jute_issue has no rate) -> NULL.
    # ponytail: issue 'rate' left NULL — derive from issue_value/weight later if needed.

    Parameters: :branch_id (int), :date_from, :date_to (str 'YYYY-MM-DD'),
                :search_like (str or NULL — matches formatted MR number)
    """
    mr_num = get_jute_mr_number_sql_expression()

    receipts = f"""
        SELECT
            'R' AS txn_type,
            jm.jute_mr_date AS txn_date,
            {mr_num} AS mr_number,
            im.item_name AS quality,
            ROUND(mrli.actual_weight, 3) AS weight,
            ROUND(mrli.actual_qty, 3) AS qty,
            ROUND(mrli.actual_rate, 2) AS rate,
            ROUND(mrli.total_price, 2) AS value,
            COALESCE(sm.status_name, CAST(jm.status_id AS CHAR)) AS status_name
        FROM jute_mr jm
        INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id
        INNER JOIN co_mst cm ON cm.co_id = bm.co_id
        INNER JOIN jute_mr_li mrli ON mrli.jute_mr_id = jm.jute_mr_id
        LEFT JOIN item_mst im ON im.item_id = mrli.actual_item_id
        LEFT JOIN status_mst sm ON sm.status_id = jm.status_id
        WHERE jm.branch_id = :branch_id
          AND jm.jute_mr_date BETWEEN :date_from AND :date_to
          AND jm.status_id IN (1, 3, 13, 48)
          AND (mrli.active = 1 OR mrli.active IS NULL)
          AND (:search_like IS NULL OR {mr_num} LIKE :search_like)
    """

    issues = f"""
        SELECT
            'I' AS txn_type,
            ji.issue_date AS txn_date,
            {mr_num} AS mr_number,
            COALESCE(im.item_name, im2.item_name) AS quality,
            ROUND(ji.weight, 3) AS weight,
            ROUND(ji.quantity, 3) AS qty,
            NULL AS rate,
            ROUND(ji.issue_value, 2) AS value,
            COALESCE(sm.status_name, CAST(ji.status_id AS CHAR)) AS status_name
        FROM jute_issue ji
        LEFT JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id
        LEFT JOIN jute_mr jm ON jm.jute_mr_id = mrli.jute_mr_id
        LEFT JOIN branch_mst bm ON bm.branch_id = jm.branch_id
        LEFT JOIN co_mst cm ON cm.co_id = bm.co_id
        LEFT JOIN item_mst im ON im.item_id = ji.item_id
        LEFT JOIN item_mst im2 ON im2.item_id = mrli.actual_item_id
        LEFT JOIN status_mst sm ON sm.status_id = ji.status_id
        WHERE ji.branch_id = :branch_id
          AND ji.issue_date BETWEEN :date_from AND :date_to
          AND ji.status_id IN (1, 3)
          AND (:search_like IS NULL OR {mr_num} LIKE :search_like)
    """

    t = (txn_type or "ALL").upper()
    if t == "R":
        sql = receipts + "\n        ORDER BY txn_date, mr_number"
    elif t == "I":
        sql = issues + "\n        ORDER BY txn_date, mr_number"
    else:
        sql = f"""
        SELECT * FROM (
            {receipts}
            UNION ALL
            {issues}
        ) t
        ORDER BY t.txn_date, t.txn_type, t.mr_number
        """
    return text(sql)


def get_jute_period_wise_query(period: str = "month"):
    """
    Jute Month Wise (#10) / Day Wise (#11) — receipt vs issue weight & qty
    aggregated per period over a date range.

    period: 'month' -> 'YYYY-MM', 'day' -> 'YYYY-MM-DD' buckets.
    Receipts keyed on jute_mr.out_date; issues on jute_issue.issue_date.

    Parameters: :branch_id (int), :date_from, :date_to (str 'YYYY-MM-DD')
    """
    if (period or "month").lower() == "day":
        recv_p = "CAST(DATE(jm.out_date) AS CHAR)"
        iss_p = "CAST(DATE(ji.issue_date) AS CHAR)"
    else:
        recv_p = "CONCAT(YEAR(jm.out_date), '-', LPAD(MONTH(jm.out_date), 2, '0'))"
        iss_p = "CONCAT(YEAR(ji.issue_date), '-', LPAD(MONTH(ji.issue_date), 2, '0'))"

    sql = f"""
        WITH recv AS (
            SELECT {recv_p} AS period,
                   ROUND(COALESCE(SUM(mrli.actual_weight), 0), 3) AS wt,
                   ROUND(COALESCE(SUM(mrli.actual_qty), 0), 3) AS qty
            FROM jute_mr jm
            INNER JOIN jute_mr_li mrli ON mrli.jute_mr_id = jm.jute_mr_id
            WHERE jm.branch_id = :branch_id
              AND jm.out_date BETWEEN :date_from AND :date_to
              AND jm.status_id IN (1, 3, 13, 48)
              AND (mrli.active = 1 OR mrli.active IS NULL)
            GROUP BY period
        ),
        iss AS (
            SELECT {iss_p} AS period,
                   ROUND(COALESCE(SUM(ji.weight), 0), 3) AS wt,
                   ROUND(COALESCE(SUM(ji.quantity), 0), 3) AS qty
            FROM jute_issue ji
            WHERE ji.branch_id = :branch_id
              AND ji.issue_date BETWEEN :date_from AND :date_to
              AND ji.status_id IN (1, 3)
            GROUP BY period
        )
        SELECT
            p.period,
            ROUND(COALESCE(r.wt, 0), 3) AS receipt_weight,
            ROUND(COALESCE(r.qty, 0), 3) AS receipt_qty,
            ROUND(COALESCE(i.wt, 0), 3) AS issue_weight,
            ROUND(COALESCE(i.qty, 0), 3) AS issue_qty
        FROM (SELECT period FROM recv UNION SELECT period FROM iss) p
        LEFT JOIN recv r ON r.period = p.period
        LEFT JOIN iss i ON i.period = p.period
        ORDER BY p.period
    """
    return text(sql)


# ---------------------------------------------------------------------------
# Cluster B — outstanding-balance reports over vw_jute_stock_outstanding.
# The view nets ALL issues per MR line (no as-of date, no status filter) AND
# every approved raw-jute sale linked to the line via
# sales_invoice_dtl.jute_mr_li_id (since 2026-08-23; sold_qty / sold_weight are
# exposed), so these are CURRENT-position reports keyed on branch only.
# ponytail: view ignores issue status + as-of date; acceptable as the
# established stock source — add a status-aware view if exactness is needed.
# ---------------------------------------------------------------------------

def _outstanding_base_from_where():
    """Shared FROM/JOIN/WHERE for the Cluster B reports. Needs :branch_id."""
    return """
        FROM vw_jute_stock_outstanding v
        JOIN jute_mr_li li ON li.jute_mr_li_id = v.jute_mr_li_id
        JOIN jute_mr jm ON jm.jute_mr_id = li.jute_mr_id
        INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id
        INNER JOIN co_mst cm ON cm.co_id = bm.co_id
        LEFT JOIN item_mst im ON im.item_id = li.actual_item_id
        WHERE v.branch_id = :branch_id
          AND jm.status_id IN (1, 3, 13, 48)
    """


def get_jute_mr_in_stock_query():
    """
    Jute MR in Stock (#4) — per MR-line outstanding balance, only lines still
    holding stock. Parameters: :branch_id (int).
    """
    mr_num = get_jute_mr_number_sql_expression()
    sql = f"""
        SELECT
            v.jute_mr_li_id,
            {mr_num} AS mr_number,
            jm.jute_mr_date,
            v.jute_gate_entry_no,
            v.warehouse_name,
            COALESCE(im.item_name, v.actual_quality) AS quality,
            ROUND(v.actual_qty, 3) AS received_qty,
            ROUND(v.actual_weight, 3) AS received_weight,
            ROUND(v.bal_qty, 3) AS balance_qty,
            ROUND(v.bal_weight, 3) AS balance_weight
        {_outstanding_base_from_where()}
          AND (ROUND(v.bal_qty, 0) != 0 OR ROUND(v.bal_weight, 0) != 0)
        ORDER BY jm.jute_mr_date DESC, v.jute_mr_li_id
    """
    return text(sql)


def get_jute_mr_wise_query():
    """
    MR Wise (#6) — per MR-line received vs issued vs sold vs balance (all lines).
    Parameters: :branch_id (int).

    balance = received - issued - sold, so `issued` is derived net of the
    line-linked sales the view now subtracts (not `received - balance`, which
    would fold sold stock into issued — on sls branch 87 that is 85,111 qtl of
    raw-jute sales on 1,075 lines that were never issued at all).
    """
    mr_num = get_jute_mr_number_sql_expression()
    sql = f"""
        SELECT
            v.jute_mr_li_id,
            {mr_num} AS mr_number,
            jm.jute_mr_date,
            v.warehouse_name,
            COALESCE(im.item_name, v.actual_quality) AS quality,
            ROUND(v.actual_qty, 3) AS received_qty,
            ROUND(v.actual_weight, 3) AS received_weight,
            ROUND(v.actual_qty - v.bal_qty - v.sold_qty, 3) AS issued_qty,
            ROUND(v.actual_weight - v.bal_weight - v.sold_weight, 3) AS issued_weight,
            ROUND(v.sold_qty, 3) AS sold_qty,
            ROUND(v.sold_weight, 3) AS sold_weight,
            ROUND(v.bal_qty, 3) AS balance_qty,
            ROUND(v.bal_weight, 3) AS balance_weight
        {_outstanding_base_from_where()}
        ORDER BY jm.jute_mr_date DESC, v.jute_mr_li_id
    """
    return text(sql)


def get_jute_godown_wise_query():
    """
    Godown Wise Stock (#5) — outstanding balance grouped by warehouse + quality.
    Parameters: :branch_id (int).
    """
    sql = f"""
        SELECT
            COALESCE(v.warehouse_name, 'Unknown') AS warehouse_name,
            COALESCE(im.item_name, v.actual_quality) AS quality,
            ROUND(SUM(v.bal_qty), 3) AS balance_qty,
            ROUND(SUM(v.bal_weight), 3) AS balance_weight
        {_outstanding_base_from_where()}
        GROUP BY COALESCE(v.warehouse_name, 'Unknown'), COALESCE(im.item_name, v.actual_quality)
        HAVING ROUND(SUM(v.bal_qty), 0) != 0 OR ROUND(SUM(v.bal_weight), 0) != 0
        ORDER BY warehouse_name, quality
    """
    return text(sql)


def get_jute_with_value_query():
    """
    Jute with Value (#1) — opening/receipt/issue/sold/closing per quality over a
    date range, plus receipt value, issue value and average issue rate.

    Sold = approved raw-jute sales invoices (invoice_type 5), already in
    quintal. Stock in also includes production waste. Both sides share
    `_MR_STOCK_ELIGIBLE` — see the Cluster A header. All-zero rows suppressed.

    Parameters: :branch_id (int), :date_from, :date_to (str 'YYYY-MM-DD')
    """
    before = "{d} < :date_from"
    rng = "{d} BETWEEN :date_from AND :date_to"

    open_wt = "(COALESCE(rb.wt, 0) - COALESCE(ib.wt, 0) - COALESCE(sb.wt, 0))"
    open_qty = "(COALESCE(rb.qty, 0) - COALESCE(ib.qty, 0) - COALESCE(sb.qty, 0))"
    close_wt = f"({open_wt} + COALESCE(rr.wt, 0) - COALESCE(ir.wt, 0) - COALESCE(sr.wt, 0))"
    close_qty = f"({open_qty} + COALESCE(rr.qty, 0) - COALESCE(ir.qty, 0) - COALESCE(sr.qty, 0))"

    sql = _group_path_cte() + f""",
        receipt_before AS ({_receipt_cte(before)}),
        issue_before AS ({_issue_cte(before)}),
        sold_before AS ({_sold_cte(before)}),
        receipt_range AS ({_receipt_cte(rng)}),
        issue_range AS ({_issue_cte(rng)}),
        sold_range AS ({_sold_cte(rng)})

        SELECT
            im.item_grp_id,
            COALESCE(fgp.item_grp_name_path, ig.item_grp_name) AS item_group_name,
            im.item_id,
            im.item_name,
            ROUND({open_wt}, 3) AS opening_weight,
            ROUND({open_qty}, 3) AS opening_qty,
            ROUND(COALESCE(rr.wt, 0), 3) AS receipt_weight,
            ROUND(COALESCE(rr.qty, 0), 3) AS receipt_qty,
            ROUND(COALESCE(rr.val, 0), 2) AS receipt_value,
            ROUND(COALESCE(ir.wt, 0), 3) AS issue_weight,
            ROUND(COALESCE(ir.qty, 0), 3) AS issue_qty,
            ROUND(COALESCE(ir.val, 0), 2) AS issue_value,
            ROUND(COALESCE(sr.wt, 0), 3) AS sold_weight,
            ROUND(COALESCE(sr.qty, 0), 3) AS sold_qty,
            ROUND({close_wt}, 3) AS closing_weight,
            ROUND({close_qty}, 3) AS closing_qty,
            ROUND(COALESCE(ir.val, 0) / NULLIF(COALESCE(ir.wt, 0) / 100, 0), 2) AS avg_issue_rate
        FROM item_mst im
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        LEFT JOIN full_group_paths fgp ON fgp.item_grp_id = im.item_grp_id
        LEFT JOIN receipt_before rb ON rb.item_id = im.item_id
        LEFT JOIN issue_before ib ON ib.item_id = im.item_id
        LEFT JOIN sold_before sb ON sb.item_id = im.item_id
        LEFT JOIN receipt_range rr ON rr.item_id = im.item_id
        LEFT JOIN issue_range ir ON ir.item_id = im.item_id
        LEFT JOIN sold_range sr ON sr.item_id = im.item_id
        WHERE (
            ROUND({open_wt}, 2) <> 0 OR ROUND({open_qty}, 2) <> 0
            OR ROUND(COALESCE(rr.wt, 0), 2) <> 0 OR ROUND(COALESCE(rr.qty, 0), 2) <> 0
            OR ROUND(COALESCE(ir.wt, 0), 2) <> 0 OR ROUND(COALESCE(ir.qty, 0), 2) <> 0
            OR ROUND(COALESCE(sr.wt, 0), 2) <> 0 OR ROUND(COALESCE(sr.qty, 0), 2) <> 0
            OR ROUND({close_wt}, 2) <> 0 OR ROUND({close_qty}, 2) <> 0
        )
        ORDER BY ig.item_grp_name, im.item_name
    """
    return _stock_text(sql)


# ---------------------------------------------------------------------------
# MR-wise Issue Detail — flat MR-line x issue grid.
#
# The link is two-hop: jute_issue.jute_mr_li_id -> jute_mr_li -> jute_mr.
# Waste issues carry a NULL jute_mr_li_id, so driving from the MR line drops
# them without an extra guard.
#
# Neither jute_mr nor jute_issue has co_id — company scope comes through
# branch_mst. Eligibility is the shared `_MR_STOCK_ELIGIBLE` predicate so the
# figures reconcile with the stock reports and the issue picker.
#
# Weights are passed through EXACTLY as stored: actual_weight / ji.weight hold
# KG on dev3 but QUINTAL on sls, with no column to tell them apart. No /100,
# no unit in any alias. Only `quantity` is consistent across tenants.
# ---------------------------------------------------------------------------

# Dates come from the module-wide _MR_RECEIPT_DATE (defined above). A second,
# differently-ordered COALESCE here would bucket the same MR into a different
# period than every sibling jute report and the balances would not tie out.


def _mr_number_exprs():
    """
    (mr_number, mr_label) SQL expressions for a `jm`/`bm`/`cm` join.

    branch_mr_no is only stamped on approval and is NULL on 82/162 dev3 MRs, so
    the formatted number is NULL for half of them — same guard as
    get_jute_issues_by_date_query. `mr_label` is the never-null display string;
    an unlabelled dropdown row cannot be selected.
    """
    mr_num = get_jute_mr_number_sql_expression(mr_date_column=_MR_RECEIPT_DATE)
    number = f"CASE WHEN jm.branch_mr_no IS NOT NULL THEN {mr_num} END"
    label = f"""COALESCE(
            {number},
            CONCAT('GE-', jm.jute_gate_entry_no),
            CONCAT('MR-', jm.jute_mr_id)
        )"""
    return number, label


def get_jute_mr_options_query(has_date_filter: bool):
    """
    MR picker options — header level only, no line join, no aggregation.

    Deliberately NOT get_mr_list_query(): that one paginates and filters by
    the report's status selection and gate-entry date, not the MR receipt date.

    Parameters (bind):
        :co_id (int), :branch_id (int)
        :date_from, :date_to (str 'YYYY-MM-DD') — only when has_date_filter
    """
    number, label = _mr_number_exprs()
    date_clause = (
        f"AND {_MR_RECEIPT_DATE} BETWEEN :date_from AND :date_to"
        if has_date_filter else ""
    )
    sql = f"""
        SELECT
            jm.jute_mr_id,
            {number} AS mr_number,
            {label} AS mr_label,
            {_MR_RECEIPT_DATE} AS mr_date,
            jm.jute_mr_date,
            jm.out_date,
            jm.jute_gate_entry_no,
            COALESCE(pm.supp_name, jsm.supplier_name) AS party_name
        FROM jute_mr jm
        INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id
        INNER JOIN co_mst cm ON cm.co_id = bm.co_id
        LEFT JOIN party_mst pm ON pm.party_id = CAST(jm.party_id AS UNSIGNED)
        LEFT JOIN jute_supplier_mst jsm ON jsm.supplier_id = jm.jute_supplier_id
        WHERE bm.co_id = :co_id
          AND jm.branch_id = :branch_id
          AND jm.status_id NOT IN ({_MR_EXCLUDED_SQL})
          {date_clause}
        ORDER BY {_MR_RECEIPT_DATE} DESC, jm.jute_mr_id DESC
    """
    return text(sql)


def get_jute_mr_issue_detail_query(has_mr_filter: bool, has_date_filter: bool):
    """
    MR-wise Issue Detail — one row per jute_issue, LEFT JOINed from the MR line
    so MR lines with zero issues still appear (issue columns NULL). MR columns
    repeat across an MR's issue rows; balance_qty/balance_weight are per MR
    LINE and repeat too — do not sum them. The balance nets issues and any
    approved sale line-linked to the MR line (sales_invoice_dtl.jute_mr_li_id),
    so a sold-out line shows balance 0 with no issue rows under it.

    The issue-status exclusion sits in the LEFT JOIN's ON clause, never the
    WHERE: in the WHERE it would collapse the left join to an inner join and
    the zero-issue lines would vanish. The predicate is `<> 4` (rejected only),
    matching vw_jute_stock_outstanding's netting subquery, so DRAFT and OPEN
    issues DO consume stock here. Tightening it to approved-only would stop the
    report reconciling with the stock picker.

    Parameters (bind):
        :co_id (int), :branch_id (int)
        :mr_id (int) — only when has_mr_filter
        :date_from, :date_to (str 'YYYY-MM-DD') — only when has_date_filter
    """
    number, label = _mr_number_exprs()
    mr_clause = "AND mrli.jute_mr_id = :mr_id" if has_mr_filter else ""
    date_clause = (
        f"AND {_MR_RECEIPT_DATE} BETWEEN :date_from AND :date_to"
        if has_date_filter else ""
    )
    sql = f"""
        SELECT
            jm.jute_mr_id,
            mrli.jute_mr_li_id,
            {number} AS mr_number,
            {label} AS mr_label,
            {_MR_RECEIPT_DATE} AS mr_date,
            jm.jute_mr_date,
            jm.out_date,
            jm.jute_gate_entry_no,
            COALESCE(pm.supp_name, jsm.supplier_name) AS party_name,
            wm.warehouse_name,
            COALESCE(qim.item_name, mrli.actual_quality) AS quality,
            mrli.marka,
            mrli.crop_year,
            ROUND(mrli.actual_qty, 3) AS received_qty,
            ROUND(mrli.actual_weight, 3) AS received_weight,
            ROUND(mrli.actual_rate, 2) AS actual_rate,
            ROUND(mrli.accepted_weight, 3) AS accepted_weight,
            ROUND(mrli.actual_weight / NULLIF(mrli.actual_qty, 0), 4) AS unit_weight,
            agg.line_issue_count,
            agg.total_issued_weight,
            agg.expected_issued_weight,
            ROUND(agg.total_issued_weight - agg.expected_issued_weight, 3) AS weight_diff,
            ji.jute_issue_id,
            ji.issue_date,
            ROUND(ji.quantity, 3) AS issued_qty,
            ROUND(ji.weight, 3) AS issued_weight,
            ROUND(ji.issue_value, 2) AS issue_value,
            CASE WHEN ji.jute_issue_id IS NULL THEN NULL
                 ELSE COALESCE(iim.item_name, qim.item_name) END AS issue_item_name,
            COALESCE(yim.item_name, jym.jute_yarn_name) AS yarn_type_name,
            CASE WHEN ji.jute_issue_id IS NULL THEN NULL
                 ELSE COALESCE(sm.status_name, 'Draft') END AS issue_status,
            ji.updated_by,
            ji.update_date_time,
            ROUND(v.bal_qty, 3) AS balance_qty,
            ROUND(v.bal_weight, 3) AS balance_weight
        FROM jute_mr_li mrli
        INNER JOIN jute_mr jm ON jm.jute_mr_id = mrli.jute_mr_id
        INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id
        INNER JOIN co_mst cm ON cm.co_id = bm.co_id
        LEFT JOIN party_mst pm ON pm.party_id = CAST(jm.party_id AS UNSIGNED)
        LEFT JOIN jute_supplier_mst jsm ON jsm.supplier_id = jm.jute_supplier_id
        LEFT JOIN warehouse_mst wm ON wm.warehouse_id = mrli.warehouse_id
        LEFT JOIN item_mst qim ON qim.item_id = mrli.actual_item_id
        LEFT JOIN vw_jute_stock_outstanding v ON v.jute_mr_li_id = mrli.jute_mr_li_id
        LEFT JOIN (
            SELECT ji2.jute_mr_li_id,
                   COUNT(*) AS line_issue_count,
                   ROUND(SUM(ji2.weight), 3) AS total_issued_weight,
                   ROUND(SUM(ji2.quantity * l2.actual_weight / NULLIF(l2.actual_qty, 0)), 3) AS expected_issued_weight
            FROM jute_issue ji2
            JOIN jute_mr_li l2 ON l2.jute_mr_li_id = ji2.jute_mr_li_id
            WHERE ji2.status_id <> {JUTE_ISSUE_EXCLUDED_STATUS}
            GROUP BY ji2.jute_mr_li_id
        ) agg ON agg.jute_mr_li_id = mrli.jute_mr_li_id
        LEFT JOIN jute_issue ji
               ON ji.jute_mr_li_id = mrli.jute_mr_li_id
              AND ji.status_id <> {JUTE_ISSUE_EXCLUDED_STATUS}
        LEFT JOIN status_mst sm ON sm.status_id = ji.status_id
        LEFT JOIN item_mst iim ON iim.item_id = ji.item_id
        LEFT JOIN jute_yarn_mst jym ON jym.jute_yarn_id = ji.yarn_type_id
        LEFT JOIN item_mst yim ON yim.item_id = jym.item_id
        WHERE bm.co_id = :co_id
          AND jm.branch_id = :branch_id
          AND {_MR_STOCK_ELIGIBLE}
          {mr_clause}
          {date_clause}
        ORDER BY {_MR_RECEIPT_DATE}, jm.jute_mr_id, ji.issue_date, ji.jute_issue_id
    """
    return text(sql)


# Sales invoices carry no line-level MR link — `sales_invoice_jute.mr_id` is a
# header field, one MR per invoice, and on sls it is mostly wrong (5,673 of the
# 5,885 resolvable ids point at an MR in a different branch; only 100 of 12,251
# invoice lines name an item the referenced MR actually held). So an invoice
# line is only filed under an MR line when all three agree: the id resolves,
# the MR is in the same branch, and that MR line carries the same item. Sales
# failing the guard are NOT dropped — they come back as `row_group = 1` with
# the MR columns NULL and `claimed_mr_id` / `claimed_mr_no` showing what the
# invoice asserted, so totals stay honest and the mismatch stays visible.
_SALES_MOVEMENT_EXCLUDED_SQL = ", ".join(
    str(s) for s in SALES_MOVEMENT_EXCLUDED_STATUSES
)


def get_jute_mr_movement_detail_query(has_mr_filter: bool, has_date_filter: bool):
    """
    MR-wise Movement Detail — the Issue Detail report plus raw-jute sales.

    One row per MOVEMENT, never one row per pair: `movement_type` is 'Issue',
    'Sale' or 'Sale (unattributed)', and only that movement's own columns are
    populated. An MR line with neither an issue nor a sale still appears once
    with `movement_type` NULL. Nothing is multiplied, so issued_weight and
    sold_weight can both be summed across the grid.

    Scope, deliberately asymmetric and matching the sibling Issue Detail report:
    the date range filters MRs by RECEIPT date, and every issue and every
    attributed sale against an in-scope MR line then comes back whatever its own
    date. Unattributed sales have no MR to date them by, so those alone are
    filtered on `invoice_date`. With an MR picked the range is unused entirely
    and unattributed sales are suppressed — the user asked about one MR.

    Two divergences, both intentional:
    - `balance_qty` / `balance_weight` come from vw_jute_stock_outstanding,
      which nets issues plus only those APPROVED sales that are line-linked via
      sales_invoice_dtl.jute_mr_li_id (backfilled for sls branch 87 on
      2026-08-23; unlinked sales do not move that balance). Same column and
      same meaning as in the Issue Detail report.
    - Sold figures include non-approved invoices (only rejected and cancelled
      are excluded), mirroring the issue side's `<> 4`. The Jute Stock report's
      `sold_weight` counts approved invoices only, so the two differ by the
      open/draft rows. `invoice_status` is emitted so that stays visible.

    Weight columns are kg on dev3 and quintal on sls with no discriminator —
    never label one with a unit and never convert.

    Parameters (bind):
        :co_id (int), :branch_id (int)
        :mr_id (int) — only when has_mr_filter
        :date_from, :date_to (str 'YYYY-MM-DD') — only when has_date_filter
    """
    number, label = _mr_number_exprs()
    mr_clause = "AND mrli.jute_mr_id = :mr_id" if has_mr_filter else ""
    date_clause = (
        f"AND {_MR_RECEIPT_DATE} BETWEEN :date_from AND :date_to"
        if has_date_filter else ""
    )
    # An unattributed sale has no MR to inherit a period from. In MR mode there
    # is no range bound at all, and the question is about one MR, so drop them.
    sale_scope = (
        "AND (mi.jute_mr_li_id IS NOT NULL"
        " OR sinv.invoice_date BETWEEN :date_from AND :date_to)"
        if has_date_filter
        else "AND mi.jute_mr_li_id IS NOT NULL"
    )

    # NULL padding for the half of the movement columns each UNION branch does
    # not fill. Kept as named blocks so the two branches cannot drift.
    issue_nulls = """
            NULL AS jute_issue_id,
            NULL AS issue_date,
            NULL AS issued_qty,
            NULL AS issued_weight,
            NULL AS issue_value,
            NULL AS issue_item_name,
            NULL AS yarn_type_name,
            NULL AS issue_status"""
    sale_nulls = """
            NULL AS invoice_id,
            NULL AS invoice_line_item_id,
            NULL AS invoice_no,
            NULL AS invoice_date,
            NULL AS customer_name,
            NULL AS sale_item_name,
            NULL AS sold_qty,
            NULL AS sold_weight,
            NULL AS sale_rate,
            NULL AS sale_value,
            NULL AS invoice_status,
            NULL AS claimed_mr_id,
            NULL AS claimed_mr_no"""
    mr_nulls = """
            NULL AS jute_mr_id,
            NULL AS jute_mr_li_id,
            NULL AS mr_number,
            NULL AS mr_label,
            NULL AS mr_date,
            NULL AS jute_mr_date,
            NULL AS out_date,
            NULL AS jute_gate_entry_no,
            NULL AS party_name,
            NULL AS warehouse_name,
            NULL AS quality,
            NULL AS marka,
            NULL AS crop_year,
            NULL AS received_qty,
            NULL AS received_weight,
            NULL AS actual_rate"""
    mr_cols = """
            ml.jute_mr_id,
            ml.jute_mr_li_id,
            ml.mr_number,
            ml.mr_label,
            ml.mr_date,
            ml.jute_mr_date,
            ml.out_date,
            ml.jute_gate_entry_no,
            ml.party_name,
            ml.warehouse_name,
            ml.quality,
            ml.marka,
            ml.crop_year,
            ml.received_qty,
            ml.received_weight,
            ml.actual_rate"""
    movement_cols = """
            mv.movement_date,
            mv.jute_issue_id,
            mv.issue_date,
            mv.issued_qty,
            mv.issued_weight,
            mv.issue_value,
            mv.issue_item_name,
            mv.yarn_type_name,
            mv.issue_status,
            mv.invoice_id,
            mv.invoice_line_item_id,
            mv.invoice_no,
            mv.invoice_date,
            mv.customer_name,
            mv.sale_item_name,
            mv.sold_qty,
            mv.sold_weight,
            mv.sale_rate,
            mv.sale_value,
            mv.invoice_status,
            mv.claimed_mr_id,
            mv.claimed_mr_no,
            mv.updated_by,
            mv.update_date_time"""

    sql = f"""
        WITH mr_line AS (
            SELECT
                mrli.jute_mr_li_id,
                mrli.jute_mr_id,
                mrli.actual_item_id,
                {number} AS mr_number,
                {label} AS mr_label,
                {_MR_RECEIPT_DATE} AS mr_date,
                jm.jute_mr_date,
                jm.out_date,
                jm.jute_gate_entry_no,
                COALESCE(pm.supp_name, jsm.supplier_name) AS party_name,
                wm.warehouse_name,
                COALESCE(qim.item_name, mrli.actual_quality) AS quality,
                mrli.marka,
                mrli.crop_year,
                ROUND(mrli.actual_qty, 3) AS received_qty,
                ROUND(mrli.actual_weight, 3) AS received_weight,
                ROUND(mrli.actual_rate, 2) AS actual_rate,
                ROUND(v.bal_qty, 3) AS balance_qty,
                ROUND(v.bal_weight, 3) AS balance_weight
            FROM jute_mr_li mrli
            INNER JOIN jute_mr jm ON jm.jute_mr_id = mrli.jute_mr_id
            INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id
            INNER JOIN co_mst cm ON cm.co_id = bm.co_id
            LEFT JOIN party_mst pm ON pm.party_id = CAST(jm.party_id AS UNSIGNED)
            LEFT JOIN jute_supplier_mst jsm ON jsm.supplier_id = jm.jute_supplier_id
            LEFT JOIN warehouse_mst wm ON wm.warehouse_id = mrli.warehouse_id
            LEFT JOIN item_mst qim ON qim.item_id = mrli.actual_item_id
            LEFT JOIN vw_jute_stock_outstanding v
                   ON v.jute_mr_li_id = mrli.jute_mr_li_id
            WHERE bm.co_id = :co_id
              AND jm.branch_id = :branch_id
              AND {_MR_STOCK_ELIGIBLE}
              {mr_clause}
              {date_clause}
        ),
        -- One MR line per (MR, item). 810 sls MRs carry two lines of the same
        -- item; without the MIN one invoice line would attach to both and the
        -- sold weight would double.
        mr_item AS (
            SELECT jute_mr_id, actual_item_id, MIN(jute_mr_li_id) AS jute_mr_li_id
            FROM mr_line
            WHERE actual_item_id IS NOT NULL
            GROUP BY jute_mr_id, actual_item_id
        ),
        sale_line AS (
            SELECT
                mi.jute_mr_li_id,
                sinv.invoice_id,
                sid.invoice_line_item_id,
                sinv.invoice_no,
                sinv.invoice_date,
                cpm.supp_name AS customer_name,
                sim.item_name AS sale_item_name,
                ROUND(sid.quantity, 3) AS sold_qty,
                ROUND(sid.sales_weight, 3) AS sold_weight,
                ROUND(sid.rate, 2) AS sale_rate,
                ROUND(sid.amount_without_tax, 2) AS sale_value,
                COALESCE(ssm.status_name, 'Draft') AS invoice_status,
                sij.mr_id AS claimed_mr_id,
                sij.mr_no AS claimed_mr_no,
                sinv.updated_by,
                sinv.updated_date_time AS update_date_time
            FROM sales_invoice sinv
            INNER JOIN sales_invoice_dtl sid ON sid.invoice_id = sinv.invoice_id
            LEFT JOIN sales_invoice_jute sij ON sij.invoice_id = sinv.invoice_id
            LEFT JOIN mr_item mi
                   ON mi.jute_mr_id = sij.mr_id
                  AND mi.actual_item_id = sid.item_id
            LEFT JOIN party_mst cpm ON cpm.party_id = sinv.party_id
            LEFT JOIN item_mst sim ON sim.item_id = sid.item_id
            LEFT JOIN status_mst ssm ON ssm.status_id = sinv.status_id
            WHERE sinv.branch_id = :branch_id
              AND sinv.invoice_type = {RAW_JUTE_INVOICE_TYPE_ID}
              AND COALESCE(sinv.active, 1) = 1
              AND (sinv.status_id IS NULL
                   OR sinv.status_id NOT IN ({_SALES_MOVEMENT_EXCLUDED_SQL}))
              {sale_scope}
        ),
        movement AS (
            SELECT
                ml.jute_mr_li_id,
                'Issue' AS movement_type,
                ji.issue_date AS movement_date,
                ji.jute_issue_id,
                ji.issue_date,
                ROUND(ji.quantity, 3) AS issued_qty,
                ROUND(ji.weight, 3) AS issued_weight,
                ROUND(ji.issue_value, 2) AS issue_value,
                COALESCE(iim.item_name, ml.quality) AS issue_item_name,
                COALESCE(yim.item_name, jym.jute_yarn_name) AS yarn_type_name,
                COALESCE(sm.status_name, 'Draft') AS issue_status,
                {sale_nulls},
                ji.updated_by,
                ji.update_date_time
            FROM mr_line ml
            INNER JOIN jute_issue ji
                    ON ji.jute_mr_li_id = ml.jute_mr_li_id
                   AND ji.status_id <> {JUTE_ISSUE_EXCLUDED_STATUS}
            LEFT JOIN status_mst sm ON sm.status_id = ji.status_id
            LEFT JOIN item_mst iim ON iim.item_id = ji.item_id
            LEFT JOIN jute_yarn_mst jym ON jym.jute_yarn_id = ji.yarn_type_id
            LEFT JOIN item_mst yim ON yim.item_id = jym.item_id

            UNION ALL

            SELECT
                sl.jute_mr_li_id,
                'Sale' AS movement_type,
                sl.invoice_date AS movement_date,
                {issue_nulls},
                sl.invoice_id,
                sl.invoice_line_item_id,
                sl.invoice_no,
                sl.invoice_date,
                sl.customer_name,
                sl.sale_item_name,
                sl.sold_qty,
                sl.sold_weight,
                sl.sale_rate,
                sl.sale_value,
                sl.invoice_status,
                sl.claimed_mr_id,
                sl.claimed_mr_no,
                sl.updated_by,
                sl.update_date_time
            FROM sale_line sl
        )
        SELECT
            0 AS row_group,
            {mr_cols},
            mv.movement_type,
            {movement_cols},
            ml.balance_qty,
            ml.balance_weight
        FROM mr_line ml
        LEFT JOIN movement mv ON mv.jute_mr_li_id = ml.jute_mr_li_id

        UNION ALL

        SELECT
            1 AS row_group,
            {mr_nulls},
            'Sale (unattributed)' AS movement_type,
            {movement_cols},
            NULL AS balance_qty,
            NULL AS balance_weight
        FROM movement mv
        WHERE mv.jute_mr_li_id IS NULL

        ORDER BY row_group, mr_date, jute_mr_id, jute_mr_li_id,
                 movement_date, jute_issue_id, invoice_id, invoice_line_item_id
    """
    return text(sql)


# ---------------------------------------------------------------------------
# PO-wise reports (TKT-000115) — PO <-> MR receipt comparison + Pending PO.
#
# jute_po_li has NO reliable line link from jute_mr_li: jute_mr_li.jute_po_li_id
# is populated on 3 of 40,852 sls rows (verified 2026-08-31), so every MR line
# is matched to its PO line BY ITEM through jute_mr.po_id + actual/challan
# item_id. A PO can hold several lines with the same item (1,863 sls cases) —
# po lines are therefore aggregated per (po, item) so an MR line matches
# exactly one ordered row and nothing multiplies.
# ---------------------------------------------------------------------------

_PO_INACTIVE_SQL = ", ".join(str(s) for s in JUTE_PO_INACTIVE_STATUSES)
_PO_PENDING_EXCLUDED_SQL = ", ".join(
    str(s) for s in JUTE_PO_INACTIVE_STATUSES + (JUTE_PO_CLOSED_STATUS,)
)


def _po_number_exprs():
    """
    (po_number, po_label) SQL expressions for a `jp`/`bm`/`cm` join — same
    guard pattern as _mr_number_exprs: the formatted number needs both po_no
    and po_date (the FY comes from the date), so it is NULL without them and
    `po_label` falls back to PO-<id> to stay selectable.
    """
    po_num = get_jute_po_number_sql_expression()
    number = (
        "CASE WHEN jp.po_no IS NOT NULL AND jp.po_date IS NOT NULL "
        f"THEN {po_num} END"
    )
    label = f"COALESCE({number}, CONCAT('PO-', jp.jute_po_id))"
    return number, label


# One aggregated ordered row per (po, item) — see module note above.
_PO_ITEMS_CTE = """
        po_items AS (
            SELECT jpli.jute_po_id,
                   jpli.item_id,
                   SUM(jpli.quantity) AS ordered_qty,
                   MAX(jpli.rate) AS po_rate,
                   MAX(jpli.jute_uom) AS po_uom
            FROM jute_po_li jpli
            WHERE (jpli.active = 1 OR jpli.active IS NULL)
            GROUP BY jpli.jute_po_id, jpli.item_id
        )
"""


def get_jute_po_options_query(has_date_filter: bool):
    """
    PO picker options for the PO <-> MR receipt report — header level only.

    Drafts, rejected and cancelled POs are excluded; CLOSED stays in (its
    receipts are exactly what the user compares).

    Parameters (bind):
        :co_id (int), :branch_id (int)
        :date_from, :date_to (str 'YYYY-MM-DD') — only when has_date_filter
    """
    number, label = _po_number_exprs()
    date_clause = (
        "AND jp.po_date BETWEEN :date_from AND :date_to"
        if has_date_filter else ""
    )
    sql = f"""
        SELECT
            jp.jute_po_id,
            {number} AS po_number,
            {label} AS po_label,
            jp.po_date,
            COALESCE(jsm.supplier_name, pm.supp_name) AS party_name,
            jp.weight AS po_weight,
            COALESCE(sm.status_name, jp.status_id) AS po_status
        FROM jute_po jp
        INNER JOIN branch_mst bm ON bm.branch_id = jp.branch_id
        INNER JOIN co_mst cm ON cm.co_id = bm.co_id
        LEFT JOIN jute_supplier_mst jsm ON jsm.supplier_id = jp.supplier_id
        LEFT JOIN party_mst pm ON pm.party_id = jp.party_id
        LEFT JOIN status_mst sm ON sm.status_id = jp.status_id
        WHERE bm.co_id = :co_id
          AND jp.branch_id = :branch_id
          AND jp.status_id NOT IN ({_PO_INACTIVE_SQL})
          {date_clause}
        ORDER BY jp.po_date DESC, jp.jute_po_id DESC
    """
    return text(sql)


def _po_receipts_cte():
    """
    One row per eligible MR LINE received against a PO, with the MR identity
    columns resolved. Eligibility is _MR_STOCK_ELIGIBLE — the same predicate
    as every stock report, so "received" here ties out with stock IN there.
    The MR's own branch is NOT filtered: the PO is the anchor and historical
    branch consolidations moved MRs across branches.
    """
    number, label = _mr_number_exprs()
    return f"""
        receipts AS (
            SELECT jm.po_id,
                   COALESCE(mrli.actual_item_id, mrli.challan_item_id) AS item_id,
                   jm.jute_mr_id,
                   mrli.jute_mr_li_id,
                   {label} AS mr_label,
                   {_MR_RECEIPT_DATE} AS mr_date,
                   jm.jute_gate_entry_no,
                   jm.status_id AS mr_status_id,
                   mrli.challan_quantity,
                   mrli.challan_weight,
                   mrli.actual_qty,
                   mrli.actual_weight,
                   mrli.accepted_weight,
                   mrli.rate AS mr_rate,
                   mrli.shortage_kgs
            FROM jute_mr jm
            INNER JOIN jute_mr_li mrli ON mrli.jute_mr_id = jm.jute_mr_id
            INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id
            INNER JOIN co_mst cm ON cm.co_id = bm.co_id
            WHERE jm.po_id IS NOT NULL
              AND {_MR_STOCK_ELIGIBLE}
        )
"""


def get_jute_po_mr_receipt_detail_query(has_po_filter: bool, has_date_filter: bool):
    """
    PO-wise MR Receipt Detail — every MR line received against each PO item,
    flat, for ordered-vs-received comparison.

    Row shape (`row_group`):
      0 — one row per (PO item x MR line matched by item). A PO item with no
          receipts still appears once with the MR columns NULL, so un-arrived
          orders are visible.
      1 — an MR line under an in-scope PO whose item matches NO PO item: the
          ordered columns are NULL. Nothing is silently dropped — a receipt
          booked against the wrong item stays visible instead of vanishing.

    Weight/qty columns are emitted raw, never unit-converted (dev3 kg vs sls
    quintal ambiguity — same rule as the MR movement report). ordered_qty and
    received_qty share the drums/bales unit of jute_po_li.quantity /
    jute_mr_li.actual_qty; the *_weight columns are the stored weights.

    Parameters (bind):
        :co_id (int), :branch_id (int)
        :po_id (int) — only when has_po_filter (date range then unused)
        :date_from, :date_to — only when has_date_filter, bound on jp.po_date
    """
    _, po_label = _po_number_exprs()
    if has_po_filter:
        scope_clause = "AND jp.jute_po_id = :po_id"
    else:
        # Range mode skips drafts/rejected/cancelled; a picked PO is trusted
        # (the picker never offers those anyway).
        scope_clause = f"""AND jp.po_date BETWEEN :date_from AND :date_to
          AND jp.status_id NOT IN ({_PO_INACTIVE_SQL})"""

    po_head_cols = f"""
            jp.jute_po_id,
            {po_label} AS po_label,
            jp.po_date,
            COALESCE(sm.status_name, jp.status_id) AS po_status,
            COALESCE(jsm.supplier_name, pm.supp_name) AS party_name,
            jmm.mukam_name AS mukam"""
    po_head_joins = """
        LEFT JOIN jute_supplier_mst jsm ON jsm.supplier_id = jp.supplier_id
        LEFT JOIN party_mst pm ON pm.party_id = jp.party_id
        LEFT JOIN jute_mukam_mst jmm ON jmm.mukam_id = jp.jute_mukam_id
        LEFT JOIN status_mst sm ON sm.status_id = jp.status_id"""
    mr_cols = """
            r.jute_mr_id,
            r.jute_mr_li_id,
            r.mr_label,
            r.mr_date,
            r.jute_gate_entry_no,
            COALESCE(msm.status_name, r.mr_status_id) AS mr_status,
            ROUND(r.challan_quantity, 3) AS challan_qty,
            ROUND(r.challan_weight, 3) AS challan_weight,
            ROUND(r.actual_qty, 3) AS received_qty,
            ROUND(r.actual_weight, 3) AS received_weight,
            ROUND(r.accepted_weight, 3) AS accepted_weight,
            ROUND(r.mr_rate, 2) AS mr_rate,
            r.shortage_kgs"""

    sql = f"""
        WITH {_PO_ITEMS_CTE.strip()},
        {_po_receipts_cte().strip()}
        SELECT
            0 AS row_group,{po_head_cols},
            pi.item_id,
            -- Item-<id> fallback: the sls raw-jute cleanup hard-deleted some
            -- old item_mst rows that POs still reference.
            COALESCE(pim.item_name, CONCAT('Item-', pi.item_id)) AS item_name,
            ROUND(pi.ordered_qty, 3) AS ordered_qty,
            pi.po_uom,
            ROUND(pi.po_rate, 2) AS po_rate,{mr_cols}
        FROM jute_po jp
        INNER JOIN branch_mst bm ON bm.branch_id = jp.branch_id
        INNER JOIN co_mst cm ON cm.co_id = bm.co_id
        INNER JOIN po_items pi ON pi.jute_po_id = jp.jute_po_id
        LEFT JOIN item_mst pim ON pim.item_id = pi.item_id
        LEFT JOIN receipts r ON r.po_id = jp.jute_po_id AND r.item_id = pi.item_id
        LEFT JOIN status_mst msm ON msm.status_id = r.mr_status_id{po_head_joins}
        WHERE bm.co_id = :co_id
          AND jp.branch_id = :branch_id
          {scope_clause}

        UNION ALL

        SELECT
            1 AS row_group,{po_head_cols},
            r.item_id,
            COALESCE(rim.item_name, CONCAT('Item-', r.item_id)) AS item_name,
            NULL AS ordered_qty,
            NULL AS po_uom,
            NULL AS po_rate,{mr_cols}
        FROM jute_po jp
        INNER JOIN branch_mst bm ON bm.branch_id = jp.branch_id
        INNER JOIN co_mst cm ON cm.co_id = bm.co_id
        INNER JOIN receipts r ON r.po_id = jp.jute_po_id
        LEFT JOIN item_mst rim ON rim.item_id = r.item_id
        LEFT JOIN status_mst msm ON msm.status_id = r.mr_status_id{po_head_joins}
        WHERE bm.co_id = :co_id
          AND jp.branch_id = :branch_id
          {scope_clause}
          AND NOT EXISTS (
              SELECT 1 FROM po_items pi2
              WHERE pi2.jute_po_id = jp.jute_po_id AND pi2.item_id = r.item_id
          )

        ORDER BY po_date DESC, jute_po_id, row_group, item_name,
                 mr_date, jute_mr_li_id
    """
    return text(sql)


def get_jute_po_pending_query():
    """
    Pending PO report — one row per PO ITEM of every PO that is still live
    (not closed/rejected/cancelled/draft) and has arrival pending at header
    level: SUM(net_weight) of its gate-entry MRs < jp.weight. Both weights
    follow the PO-list query's convention (same subtraction, raw values, no
    unit conversion), so this report's pending ties out with the PO list page.

    The po_* header columns (weights, lorries) REPEAT across a PO's item rows
    — sum the line columns (ordered_qty / received_qty / pending_qty), never
    the po_* ones. Line received comes from the same item-matched,
    stock-eligible MR lines as the receipt-detail report; the header receipt
    deliberately mirrors the PO list instead (any MR with a gate entry), so
    the two can differ by in-flight/draft MRs.

    Parameters (bind): :co_id (int), :branch_id (int)
    """
    _, po_label = _po_number_exprs()
    sql = f"""
        WITH {_PO_ITEMS_CTE.strip()},
        received AS (
            SELECT jm.po_id,
                   COALESCE(mrli.actual_item_id, mrli.challan_item_id) AS item_id,
                   SUM(COALESCE(mrli.actual_qty, 0)) AS received_qty,
                   SUM(COALESCE(mrli.actual_weight, 0)) AS received_weight
            FROM jute_mr jm
            INNER JOIN jute_mr_li mrli ON mrli.jute_mr_id = jm.jute_mr_id
            WHERE jm.po_id IS NOT NULL
              AND {_MR_STOCK_ELIGIBLE}
            GROUP BY jm.po_id, COALESCE(mrli.actual_item_id, mrli.challan_item_id)
        ),
        gate AS (
            SELECT jm.po_id,
                   COUNT(*) AS received_lorries,
                   COALESCE(SUM(jm.net_weight), 0) AS received_weight
            FROM jute_mr jm
            WHERE jm.po_id IS NOT NULL
              AND jm.jute_gate_entry_no IS NOT NULL
            GROUP BY jm.po_id
        )
        SELECT
            jp.jute_po_id,
            {po_label} AS po_label,
            jp.po_date,
            COALESCE(sm.status_name, jp.status_id) AS po_status,
            COALESCE(jsm.supplier_name, pm.supp_name) AS party_name,
            jmm.mukam_name AS mukam,
            jp.delivery_days,
            CASE WHEN jp.delivery_days IS NOT NULL AND jp.po_date IS NOT NULL
                 THEN DATE_ADD(jp.po_date, INTERVAL jp.delivery_days DAY)
            END AS due_date,
            jp.vehicle_quantity,
            COALESCE(g.received_lorries, 0) AS received_lorries,
            GREATEST(
                COALESCE(jp.vehicle_quantity, 0) - COALESCE(g.received_lorries, 0),
                0) AS pending_lorries,
            ROUND(jp.weight, 3) AS po_weight,
            ROUND(COALESCE(g.received_weight, 0), 3) AS po_received_weight,
            ROUND(GREATEST(
                COALESCE(jp.weight, 0) - COALESCE(g.received_weight, 0),
                0), 3) AS po_pending_weight,
            pi.item_id,
            COALESCE(pim.item_name, CONCAT('Item-', pi.item_id)) AS item_name,
            pi.po_uom,
            ROUND(pi.po_rate, 2) AS po_rate,
            ROUND(pi.ordered_qty, 3) AS ordered_qty,
            ROUND(COALESCE(rc.received_qty, 0), 3) AS received_qty,
            ROUND(GREATEST(
                COALESCE(pi.ordered_qty, 0) - COALESCE(rc.received_qty, 0),
                0), 3) AS pending_qty,
            ROUND(COALESCE(rc.received_weight, 0), 3) AS received_weight
        FROM jute_po jp
        INNER JOIN branch_mst bm ON bm.branch_id = jp.branch_id
        INNER JOIN co_mst cm ON cm.co_id = bm.co_id
        INNER JOIN po_items pi ON pi.jute_po_id = jp.jute_po_id
        LEFT JOIN item_mst pim ON pim.item_id = pi.item_id
        LEFT JOIN received rc ON rc.po_id = jp.jute_po_id AND rc.item_id = pi.item_id
        LEFT JOIN gate g ON g.po_id = jp.jute_po_id
        LEFT JOIN jute_supplier_mst jsm ON jsm.supplier_id = jp.supplier_id
        LEFT JOIN party_mst pm ON pm.party_id = jp.party_id
        LEFT JOIN jute_mukam_mst jmm ON jmm.mukam_id = jp.jute_mukam_id
        LEFT JOIN status_mst sm ON sm.status_id = jp.status_id
        WHERE bm.co_id = :co_id
          AND jp.branch_id = :branch_id
          AND jp.status_id NOT IN ({_PO_PENDING_EXCLUDED_SQL})
          AND COALESCE(jp.weight, 0) - COALESCE(g.received_weight, 0) > 0
        ORDER BY jp.po_date, jp.jute_po_id, pim.item_name
    """
    return text(sql)


# Arrival date of an MR for the as-on cut of the PO position: the gate entry
# is when the lorry physically arrived; fall back to the stock reports'
# receipt date for MRs that never got a gate stamp.
_MR_ARRIVAL_DATE = f"COALESCE(jm.jute_gate_entry_date, {_MR_RECEIPT_DATE})"


def get_jute_po_outstanding_position_query():
    """
    Outstanding PO Position (jute production reports) — one row per PO ITEM
    of every live PO (not draft/rejected/cancelled/closed) dated on or before
    :as_on, with what had arrived against it by :as_on. The row is NOT
    filtered on balance here: the handler derives ordered/balance weight and
    value in Python (``build_po_position``) and drops the fully-received
    items there, so the arithmetic is unit-tested without a database.

    Weight is never unit-converted (dev3 kg vs legacy-sls quintal). Instead
    each item's ORDERED weight is its share of the PO header weight:
        weight_share = percentage/100 when the PO's lines carry percentages,
                       else quantity / PO total quantity,
                       else 1 for a single-line PO (quantity never entered).
    Ordered value is the line's stored value (SUM over same-item lines), so
    the balance value can be pro-rated from it without assuming a rate unit.

    Received = stock-eligible MR lines (same predicate as every stock report)
    matched BY ITEM through jute_mr.po_id (see the module note above the PO
    reports), arrived on or before :as_on. Header-level gate figures (lorries
    and net weight of every stock-eligible MR with a gate entry by :as_on —
    the settlement engine's receipt predicate) repeat across a PO's item rows.

    Parameters (bind): :co_id (int), :branch_id (int), :as_on (date)
    """
    _, po_label = _po_number_exprs()
    sql = f"""
        WITH po_lines AS (
            SELECT jpli.jute_po_id,
                   jpli.item_id,
                   jpli.quantity,
                   jpli.percentage,
                   jpli.value,
                   jpli.rate,
                   jpli.jute_uom,
                   SUM(jpli.quantity) OVER (PARTITION BY jpli.jute_po_id) AS po_total_qty,
                   SUM(jpli.percentage) OVER (PARTITION BY jpli.jute_po_id) AS po_total_pct,
                   COUNT(*) OVER (PARTITION BY jpli.jute_po_id) AS po_line_count
            FROM jute_po_li jpli
            WHERE (jpli.active = 1 OR jpli.active IS NULL)
        ),
        po_items AS (
            SELECT pl.jute_po_id,
                   pl.item_id,
                   SUM(pl.quantity) AS ordered_qty,
                   SUM(pl.value) AS ordered_value,
                   MAX(pl.rate) AS po_rate,
                   -- Lowest rate over the item lines. Legacy POs carry the same
                   -- quality on several lines at different rates, so the PO
                   -- Detail grid shows a range rather than just the MAX (TKT-208).
                   MIN(pl.rate) AS po_rate_min,
                   MAX(pl.jute_uom) AS po_uom,
                   SUM(CASE
                           WHEN pl.po_total_pct > 0 THEN COALESCE(pl.percentage, 0) / 100
                           WHEN pl.po_total_qty > 0 THEN COALESCE(pl.quantity, 0) / pl.po_total_qty
                           WHEN pl.po_line_count = 1 THEN 1
                       END) AS weight_share
            FROM po_lines pl
            GROUP BY pl.jute_po_id, pl.item_id
        ),
        received AS (
            SELECT jm.po_id,
                   COALESCE(mrli.actual_item_id, mrli.challan_item_id) AS item_id,
                   SUM(COALESCE(mrli.actual_qty, 0)) AS received_qty,
                   SUM(COALESCE(mrli.actual_weight, 0)) AS received_wt,
                   COUNT(DISTINCT jm.jute_mr_id) AS mr_count,
                   MAX({_MR_ARRIVAL_DATE}) AS last_receipt_date
            FROM jute_mr jm
            INNER JOIN jute_mr_li mrli ON mrli.jute_mr_id = jm.jute_mr_id
            WHERE jm.po_id IS NOT NULL
              AND {_MR_STOCK_ELIGIBLE}
              AND ({_MR_ARRIVAL_DATE} IS NULL OR {_MR_ARRIVAL_DATE} <= :as_on)
            GROUP BY jm.po_id, COALESCE(mrli.actual_item_id, mrli.challan_item_id)
        ),
        gate AS (
            -- Header receipts = the settlement engine's predicate (po_settlement.py,
            -- spec section 2): a gate entry that is not rejected / cancelled /
            -- draft / returned. Same statuses as _MR_STOCK_ELIGIBLE, so the
            -- report's settle_suggest agrees with what recompute_settlement does.
            -- Returned lorries (48) are reported separately (spec section 11.2).
            SELECT jm.po_id,
                   COALESCE(SUM(CASE WHEN jm.status_id NOT IN ({_MR_EXCLUDED_SQL}) THEN 1 ELSE 0 END), 0) AS received_lorries,
                   COALESCE(SUM(CASE WHEN jm.status_id NOT IN ({_MR_EXCLUDED_SQL}) THEN {JUTE_MR_RECEIVED_WEIGHT_SQL} END), 0) AS received_weight,
                   COALESCE(SUM(CASE WHEN jm.is_returned = 1 AND jm.status_id NOT IN ({_MR_EXCLUDED_SQL}) THEN 1 ELSE 0 END), 0) AS returned_lorries,
                   COALESCE(SUM(CASE WHEN jm.is_returned = 1 AND jm.status_id NOT IN ({_MR_EXCLUDED_SQL}) THEN jm.net_weight END), 0) AS returned_weight
            FROM jute_mr jm
            WHERE jm.po_id IS NOT NULL
              AND jm.jute_gate_entry_no IS NOT NULL
              AND (jm.jute_gate_entry_date IS NULL OR jm.jute_gate_entry_date <= :as_on)
            GROUP BY jm.po_id
        )
        SELECT
            jp.jute_po_id,
            {po_label} AS po_label,
            jp.po_date,
            jp.delivery_days,
            COALESCE(sm.status_name, jp.status_id) AS po_status,
            COALESCE(jsm.supplier_name, pm.supp_name) AS party_name,
            jmm.mukam_name AS mukam,
            jp.vehicle_quantity,
            COALESCE(g.received_lorries, 0) AS received_lorries,
            jp.weight AS po_weight,
            COALESCE(g.received_weight, 0) AS po_received_weight,
            COALESCE(g.returned_lorries, 0) AS returned_lorries,
            COALESCE(g.returned_weight, 0) AS returned_weight,
            pi.item_id,
            pim.item_code,
            -- Item-<id> fallback: the sls raw-jute cleanup hard-deleted some
            -- old item_mst rows that POs still reference.
            COALESCE(pim.item_name, CONCAT('Item-', pi.item_id)) AS item_name,
            pi.po_uom,
            pi.po_rate,
            pi.po_rate_min,
            pi.ordered_qty,
            pi.ordered_value,
            pi.weight_share,
            COALESCE(rc.received_qty, 0) AS received_qty,
            COALESCE(rc.received_wt, 0) AS received_wt,
            COALESCE(rc.mr_count, 0) AS mr_count,
            rc.last_receipt_date
        FROM jute_po jp
        INNER JOIN branch_mst bm ON bm.branch_id = jp.branch_id
        INNER JOIN co_mst cm ON cm.co_id = bm.co_id
        INNER JOIN po_items pi ON pi.jute_po_id = jp.jute_po_id
        LEFT JOIN item_mst pim ON pim.item_id = pi.item_id
        LEFT JOIN received rc ON rc.po_id = jp.jute_po_id AND rc.item_id = pi.item_id
        LEFT JOIN gate g ON g.po_id = jp.jute_po_id
        LEFT JOIN jute_supplier_mst jsm ON jsm.supplier_id = jp.supplier_id
        LEFT JOIN party_mst pm ON pm.party_id = jp.party_id
        LEFT JOIN jute_mukam_mst jmm ON jmm.mukam_id = jp.jute_mukam_id
        LEFT JOIN status_mst sm ON sm.status_id = jp.status_id
        WHERE bm.co_id = :co_id
          AND jp.branch_id = :branch_id
          AND jp.status_id NOT IN ({_PO_PENDING_EXCLUDED_SQL})
          AND (jp.po_date IS NULL OR jp.po_date <= :as_on)
        ORDER BY jp.po_date, jp.jute_po_id, pim.item_name
    """
    return text(sql)

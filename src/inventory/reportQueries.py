"""
Inventory Report Queries.
Contains SQL queries for inventory reports (stock position, item-wise issue).
"""

from sqlalchemy import text

from src.inventory.query import lot_rate_sql


def get_inventory_stock_report_query():
    """
    Inventory stock position report query.

    Returns one row per item with opening, receipt, issue, return, closing
    quantities over a date range. Opening = all receipts before date_from minus
    all issues and supplier returns before date_from. Closing = opening +
    receipts_in_range - issues_in_range - returns_in_range.

    Only considers approved inwards (sr_status = 3), active issues, and approved
    DR-note returns (vw_drcr_return_qty — see
    dbqueries/migrations/20260823_drcr_return_reduces_stock.sql).

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional branch filter
        :item_grp_id (int or NULL) - optional item group filter
        :date_from (str) - range start (YYYY-MM-DD), required
        :date_to (str) - range end (YYYY-MM-DD), required
        :search_like (str or NULL) - LIKE pattern for item_name, item_grp_name
        :limit (int) - pagination limit
        :offset (int) - pagination offset
    """
    sql = f"""
    WITH receipts_before AS (
        SELECT
            pid.item_id,
            COALESCE(SUM(pid.approved_qty), 0) AS total_qty,
            COALESCE(SUM(pid.approved_qty
                * {lot_rate_sql('pid')}), 0) AS total_val
        FROM proc_inward_dtl AS pid
        INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
        WHERE pid.active = 1
            AND pi.sr_status = 3
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR pi.branch_id = :branch_id)
            AND pi.inward_date < :date_from
        GROUP BY pid.item_id
    ),
    issues_before AS (
        SELECT
            il.item_id,
            COALESCE(SUM(il.issue_qty), 0) AS total_qty,
            COALESCE(SUM(il.issue_qty
                * {lot_rate_sql('lot')}), 0) AS total_val
        FROM issue_li AS il
        INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
        LEFT JOIN proc_inward_dtl AS lot ON lot.inward_dtl_id = il.inward_dtl_id
        WHERE (ih.active = 1 OR ih.active IS NULL)
            AND ih.status_id IN (1, 3, 5, 20)
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
            AND ih.issue_date < :date_from
        GROUP BY il.item_id
    ),
    returns_before AS (
        SELECT
            r.item_id,
            COALESCE(SUM(r.return_qty), 0) AS total_qty,
            COALESCE(SUM(r.return_qty
                * {lot_rate_sql('lot')}), 0) AS total_val
        FROM vw_drcr_return_qty AS r
        INNER JOIN proc_inward_dtl AS lot ON lot.inward_dtl_id = r.inward_dtl_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = r.branch_id
        WHERE bm.co_id = :co_id
            AND (:branch_id IS NULL OR r.branch_id = :branch_id)
            AND r.note_date < :date_from
        GROUP BY r.item_id
    ),
    receipts_in_range AS (
        SELECT
            pid.item_id,
            COALESCE(SUM(pid.approved_qty), 0) AS total_qty,
            COALESCE(SUM(pid.approved_qty
                * {lot_rate_sql('pid')}), 0) AS total_val
        FROM proc_inward_dtl AS pid
        INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
        WHERE pid.active = 1
            AND pi.sr_status = 3
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR pi.branch_id = :branch_id)
            AND pi.inward_date >= :date_from
            AND pi.inward_date <= :date_to
        GROUP BY pid.item_id
    ),
    issues_in_range AS (
        SELECT
            il.item_id,
            COALESCE(SUM(il.issue_qty), 0) AS total_qty,
            COALESCE(SUM(il.issue_qty
                * {lot_rate_sql('lot')}), 0) AS total_val
        FROM issue_li AS il
        INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
        LEFT JOIN proc_inward_dtl AS lot ON lot.inward_dtl_id = il.inward_dtl_id
        WHERE (ih.active = 1 OR ih.active IS NULL)
            AND ih.status_id IN (1, 3, 5, 20)
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
            AND ih.issue_date >= :date_from
            AND ih.issue_date <= :date_to
        GROUP BY il.item_id
    ),
    returns_in_range AS (
        SELECT
            r.item_id,
            COALESCE(SUM(r.return_qty), 0) AS total_qty,
            COALESCE(SUM(r.return_qty
                * {lot_rate_sql('lot')}), 0) AS total_val
        FROM vw_drcr_return_qty AS r
        INNER JOIN proc_inward_dtl AS lot ON lot.inward_dtl_id = r.inward_dtl_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = r.branch_id
        WHERE bm.co_id = :co_id
            AND (:branch_id IS NULL OR r.branch_id = :branch_id)
            AND r.note_date >= :date_from
            AND r.note_date <= :date_to
        GROUP BY r.item_id
    ),
    last_receipt AS (
        SELECT pid.item_id, MAX(pi.inward_date) AS last_date
        FROM proc_inward_dtl AS pid
        INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
        WHERE pid.active = 1
            AND pi.sr_status = 3
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR pi.branch_id = :branch_id)
            AND pi.inward_date <= :date_to
        GROUP BY pid.item_id
    ),
    last_issue AS (
        SELECT il.item_id, MAX(ih.issue_date) AS last_date
        FROM issue_li AS il
        INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
        WHERE (ih.active = 1 OR ih.active IS NULL)
            AND ih.status_id IN (1, 3, 5, 20)
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
            AND ih.issue_date <= :date_to
        GROUP BY il.item_id
    ),
    all_items AS (
        SELECT item_id FROM receipts_before
        UNION
        SELECT item_id FROM issues_before
        UNION
        SELECT item_id FROM receipts_in_range
        UNION
        SELECT item_id FROM issues_in_range
        UNION
        SELECT item_id FROM returns_in_range
    )
    SELECT
        im.item_id,
        vip.full_item_code AS item_code,
        im.item_name,
        igm.item_grp_name,
        um.uom_name,
        (COALESCE(rb.total_qty, 0) - COALESCE(ib.total_qty, 0)
         - COALESCE(rtb.total_qty, 0)) AS opening_qty,
        (COALESCE(rb.total_val, 0) - COALESCE(ib.total_val, 0)
         - COALESCE(rtb.total_val, 0)) AS opening_val,
        COALESCE(rir.total_qty, 0) AS receipt_qty,
        COALESCE(rir.total_val, 0) AS receipt_val,
        COALESCE(iir.total_qty, 0) AS issue_qty,
        COALESCE(iir.total_val, 0) AS issue_val,
        COALESCE(rtr.total_qty, 0) AS return_qty,
        COALESCE(rtr.total_val, 0) AS return_val,
        (COALESCE(rb.total_qty, 0) - COALESCE(ib.total_qty, 0) - COALESCE(rtb.total_qty, 0)
         + COALESCE(rir.total_qty, 0) - COALESCE(iir.total_qty, 0)
         - COALESCE(rtr.total_qty, 0)) AS closing_qty,
        (COALESCE(rb.total_val, 0) - COALESCE(ib.total_val, 0) - COALESCE(rtb.total_val, 0)
         + COALESCE(rir.total_val, 0) - COALESCE(iir.total_val, 0)
         - COALESCE(rtr.total_val, 0)) AS closing_val,
        lr.last_date AS last_receipt_date,
        li.last_date AS last_issue_date
    FROM all_items AS ai
    INNER JOIN item_mst AS im ON im.item_id = ai.item_id
    LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = ai.item_id
    LEFT JOIN item_grp_mst AS igm ON igm.item_grp_id = im.item_grp_id
    LEFT JOIN uom_mst AS um ON um.uom_id = im.uom_id
    LEFT JOIN receipts_before AS rb ON rb.item_id = ai.item_id
    LEFT JOIN issues_before AS ib ON ib.item_id = ai.item_id
    LEFT JOIN receipts_in_range AS rir ON rir.item_id = ai.item_id
    LEFT JOIN issues_in_range AS iir ON iir.item_id = ai.item_id
    LEFT JOIN returns_before AS rtb ON rtb.item_id = ai.item_id
    LEFT JOIN returns_in_range AS rtr ON rtr.item_id = ai.item_id
    LEFT JOIN last_receipt AS lr ON lr.item_id = ai.item_id
    LEFT JOIN last_issue AS li ON li.item_id = ai.item_id
    WHERE (:item_grp_id IS NULL OR im.item_grp_id = :item_grp_id)
        AND (
            :search_like IS NULL
            OR im.item_name LIKE :search_like
            OR igm.item_grp_name LIKE :search_like
        )
    ORDER BY igm.item_grp_name, im.item_name
    LIMIT :limit OFFSET :offset;
    """
    return text(sql)


def get_inventory_stock_report_count_query():
    """
    Count query for inventory stock report pagination.
    """
    sql = """
    WITH receipts_before AS (
        SELECT DISTINCT pid.item_id
        FROM proc_inward_dtl AS pid
        INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
        WHERE pid.active = 1
            AND pi.sr_status = 3
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR pi.branch_id = :branch_id)
            AND pi.inward_date < :date_from
    ),
    issues_before AS (
        SELECT DISTINCT il.item_id
        FROM issue_li AS il
        INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
        WHERE (ih.active = 1 OR ih.active IS NULL)
            AND ih.status_id IN (1, 3, 5, 20)
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
            AND ih.issue_date < :date_from
    ),
    receipts_in_range AS (
        SELECT DISTINCT pid.item_id
        FROM proc_inward_dtl AS pid
        INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
        WHERE pid.active = 1
            AND pi.sr_status = 3
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR pi.branch_id = :branch_id)
            AND pi.inward_date >= :date_from
            AND pi.inward_date <= :date_to
    ),
    issues_in_range AS (
        SELECT DISTINCT il.item_id
        FROM issue_li AS il
        INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
        WHERE (ih.active = 1 OR ih.active IS NULL)
            AND ih.status_id IN (1, 3, 5, 20)
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
            AND ih.issue_date >= :date_from
            AND ih.issue_date <= :date_to
    ),
    returns_in_range AS (
        SELECT DISTINCT r.item_id
        FROM vw_drcr_return_qty AS r
        LEFT JOIN branch_mst AS bm ON bm.branch_id = r.branch_id
        WHERE bm.co_id = :co_id
            AND (:branch_id IS NULL OR r.branch_id = :branch_id)
            AND r.note_date >= :date_from
            AND r.note_date <= :date_to
    ),
    all_items AS (
        SELECT item_id FROM receipts_before
        UNION
        SELECT item_id FROM issues_before
        UNION
        SELECT item_id FROM receipts_in_range
        UNION
        SELECT item_id FROM issues_in_range
        UNION
        SELECT item_id FROM returns_in_range
    )
    SELECT COUNT(1) AS total
    FROM all_items AS ai
    INNER JOIN item_mst AS im ON im.item_id = ai.item_id
    LEFT JOIN item_grp_mst AS igm ON igm.item_grp_id = im.item_grp_id
    WHERE (:item_grp_id IS NULL OR im.item_grp_id = :item_grp_id)
        AND (
            :search_like IS NULL
            OR im.item_name LIKE :search_like
            OR igm.item_grp_name LIKE :search_like
        );
    """
    return text(sql)


def get_issue_itemwise_report_query():
    """
    Item-wise issue report query.

    Returns one row per issue line item with issue header info,
    item details, and department/cost factor/machine info.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional branch filter
        :item_grp_id (int or NULL) - optional item group filter
        :date_from (str or NULL) - date range start (YYYY-MM-DD)
        :date_to (str or NULL) - date range end (YYYY-MM-DD)
        :search_like (str or NULL) - LIKE pattern for item_name, branch_name, item_grp_name
        :limit (int) - pagination limit
        :offset (int) - pagination offset
    """
    sql = f"""
    SELECT
        il.issue_li_id,
        ih.issue_id,
        ih.issue_pass_no,
        ih.issue_pass_print_no,
        ih.issue_date,
        bm.branch_name,
        bm.branch_prefix,
        cm.co_prefix,
        dm.dept_desc AS department,
        vip.full_item_code AS item_code,
        im.item_name,
        igm.item_grp_name,
        um.uom_name,
        il.req_quantity,
        il.issue_qty,
        ROUND(il.issue_qty * {lot_rate_sql('pid')}, 2) AS issue_value,
        pi.sr_no,
        etm.expense_type_name,
        cfm.cost_factor_name,
        mm.machine_name,
        sm.status_name
    FROM issue_li AS il
    INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
    LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
    LEFT JOIN co_mst AS cm ON cm.co_id = bm.co_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = ih.dept_id
    LEFT JOIN item_mst AS im ON im.item_id = il.item_id
    LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = il.item_id
    LEFT JOIN item_grp_mst AS igm ON igm.item_grp_id = im.item_grp_id
    LEFT JOIN uom_mst AS um ON um.uom_id = il.uom_id
    LEFT JOIN proc_inward_dtl AS pid ON pid.inward_dtl_id = il.inward_dtl_id
    LEFT JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
    LEFT JOIN expense_type_mst AS etm ON etm.expense_type_id = il.expense_type_id
    LEFT JOIN cost_factor_mst AS cfm ON cfm.cost_factor_id = il.cost_factor_id
    LEFT JOIN machine_mst AS mm ON mm.machine_id = il.machine_id
    LEFT JOIN status_mst AS sm ON sm.status_id = ih.status_id
    -- No status_id filter: every issue is listed whatever its status (the
    -- report has a Status column). Only deleted issues (active = 0) drop out.
    WHERE (ih.active = 1 OR ih.active IS NULL)
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
        AND (:item_grp_id IS NULL OR im.item_grp_id = :item_grp_id)
        AND (:date_from IS NULL OR ih.issue_date >= :date_from)
        AND (:date_to IS NULL OR ih.issue_date <= :date_to)
        -- Drill-down filters: the consumption reports (IS01/02/03/06) pass the
        -- ids of the row the user opened, so this returns exactly the issue
        -- lines that made up that figure.
        AND (:dept_id IS NULL OR ih.dept_id = :dept_id)
        AND (:cost_factor_id IS NULL OR il.cost_factor_id = :cost_factor_id)
        AND (:item_id IS NULL OR il.item_id = :item_id)
        AND (
            :search_like IS NULL
            OR im.item_name LIKE :search_like
            OR bm.branch_name LIKE :search_like
            OR igm.item_grp_name LIKE :search_like
            OR dm.dept_desc LIKE :search_like
        )
    ORDER BY ih.issue_date DESC, ih.issue_id DESC, il.issue_li_id
    LIMIT :limit OFFSET :offset;
    """
    return text(sql)


def get_item_ledger_opening_query():
    """
    Opening balance (quantity + value) for one item before ``date_from``.

    Opening = approved receipts before date_from - active issues before
    date_from - approved supplier returns before date_from, scoped to the
    company (and optional branch). Same movement tables and status rules as the
    stock report. Values use the receipt-lot rate
    (lot_rate_sql: COALESCE(accepted_rate, rate) net of the SR line discount).

    Parameters:
        :co_id (int) - required
        :item_id (int) - required
        :branch_id (int or NULL) - optional branch filter
        :date_from (str) - range start (YYYY-MM-DD), required
    """
    sql = f"""
    SELECT
        (r.qty - i.qty - dr.qty) AS opening_qty,
        (r.val - i.val - dr.val) AS opening_val
    FROM
        (
            SELECT
                COALESCE(SUM(pid.approved_qty), 0) AS qty,
                COALESCE(SUM(pid.approved_qty
                    * {lot_rate_sql('pid')}), 0) AS val
            FROM proc_inward_dtl AS pid
            INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
            LEFT JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
            WHERE pid.active = 1
                AND pi.sr_status = 3
                AND pid.item_id = :item_id
                AND bm.co_id = :co_id
                AND (:branch_id IS NULL OR pi.branch_id = :branch_id)
                AND pi.inward_date < :date_from
        ) AS r,
        (
            SELECT
                COALESCE(SUM(il.issue_qty), 0) AS qty,
                COALESCE(SUM(il.issue_qty
                    * {lot_rate_sql('lot')}), 0) AS val
            FROM issue_li AS il
            INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
            LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
            LEFT JOIN proc_inward_dtl AS lot ON lot.inward_dtl_id = il.inward_dtl_id
            WHERE (ih.active = 1 OR ih.active IS NULL)
                AND ih.status_id IN (1, 3, 5, 20)
                AND il.item_id = :item_id
                AND bm.co_id = :co_id
                AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
                AND ih.issue_date < :date_from
        ) AS i,
        (
            SELECT
                COALESCE(SUM(r.return_qty), 0) AS qty,
                COALESCE(SUM(r.return_qty
                    * {lot_rate_sql('lot')}), 0) AS val
            FROM vw_drcr_return_qty AS r
            INNER JOIN proc_inward_dtl AS lot ON lot.inward_dtl_id = r.inward_dtl_id
            LEFT JOIN branch_mst AS bm ON bm.branch_id = r.branch_id
            WHERE r.item_id = :item_id
                AND bm.co_id = :co_id
                AND (:branch_id IS NULL OR r.branch_id = :branch_id)
                AND r.note_date < :date_from
        ) AS dr;
    """
    return text(sql)


def get_item_ledger_txns_query():
    """
    Transaction rows (receipts + issues + supplier returns) for one item within
    a date range, one row per source document, ordered chronologically. The running balance
    is computed by the caller from the opening balance.

    Parameters:
        :co_id (int) - required
        :item_id (int) - required
        :branch_id (int or NULL) - optional branch filter
        :date_from (str) - range start (YYYY-MM-DD), required
        :date_to (str) - range end (YYYY-MM-DD), required
    """
    sql = f"""
    SELECT txn_date, txn_type, ref_no, in_qty, in_val, out_qty, out_val
    FROM (
        SELECT
            pi.inward_date AS txn_date,
            'Receipt' AS txn_type,
            -- Full SR number (LC/FACTORY/26-27/000065), same value the issue
            -- item-wise report shows; falls back to the raw sequence when a
            -- receipt predates SR-no minting.
            COALESCE(pi.sr_no,
                CAST(COALESCE(pi.inward_sequence_no, pi.inward_id) AS CHAR)) AS ref_no,
            COALESCE(SUM(pid.approved_qty), 0) AS in_qty,
            COALESCE(SUM(pid.approved_qty
                * {lot_rate_sql('pid')}), 0) AS in_val,
            0 AS out_qty,
            0 AS out_val
        FROM proc_inward_dtl AS pid
        INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
        WHERE pid.active = 1
            AND pi.sr_status = 3
            AND pid.item_id = :item_id
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR pi.branch_id = :branch_id)
            AND pi.inward_date >= :date_from
            AND pi.inward_date <= :date_to
        GROUP BY pi.inward_id

        UNION ALL

        SELECT
            ih.issue_date AS txn_date,
            'Issue' AS txn_type,
            -- Legacy Data-Portal format: SR no of the lot(s) consumed, with the
            -- issue pass no in brackets -- concat(sr_print_no,'(',hdr_id,')').
            CONCAT(
                COALESCE(GROUP_CONCAT(DISTINCT lot_sr.sr_no SEPARATOR ', '), ''),
                '(',
                COALESCE(ih.issue_pass_print_no, CAST(ih.issue_pass_no AS CHAR), ''),
                ')'
            ) AS ref_no,
            0 AS in_qty,
            0 AS in_val,
            COALESCE(SUM(il.issue_qty), 0) AS out_qty,
            COALESCE(SUM(il.issue_qty
                * {lot_rate_sql('lot')}), 0) AS out_val
        FROM issue_li AS il
        INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
        LEFT JOIN proc_inward_dtl AS lot ON lot.inward_dtl_id = il.inward_dtl_id
        LEFT JOIN proc_inward AS lot_sr ON lot_sr.inward_id = lot.inward_id
        WHERE (ih.active = 1 OR ih.active IS NULL)
            AND ih.status_id IN (1, 3, 5, 20)
            AND il.item_id = :item_id
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
            AND ih.issue_date >= :date_from
            AND ih.issue_date <= :date_to
        GROUP BY ih.issue_id

        UNION ALL

        SELECT
            r.note_date AS txn_date,
            'Return' AS txn_type,
            -- SR no of the lot(s) sent back, with the DR-note id in brackets.
            CONCAT(
                COALESCE(GROUP_CONCAT(DISTINCT lot_sr.sr_no SEPARATOR ', '), ''),
                '(DN', r.debit_credit_note_id, ')'
            ) AS ref_no,
            0 AS in_qty,
            0 AS in_val,
            COALESCE(SUM(r.return_qty), 0) AS out_qty,
            COALESCE(SUM(r.return_qty
                * {lot_rate_sql('lot')}), 0) AS out_val
        FROM vw_drcr_return_qty AS r
        INNER JOIN proc_inward_dtl AS lot ON lot.inward_dtl_id = r.inward_dtl_id
        LEFT JOIN proc_inward AS lot_sr ON lot_sr.inward_id = lot.inward_id
        LEFT JOIN branch_mst AS bm ON bm.branch_id = r.branch_id
        WHERE r.item_id = :item_id
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR r.branch_id = :branch_id)
            AND r.note_date >= :date_from
            AND r.note_date <= :date_to
        GROUP BY r.debit_credit_note_id, r.note_date
    ) AS ledger
    ORDER BY txn_date, txn_type DESC, ref_no;
    """
    return text(sql)


def get_issue_itemwise_report_count_query():
    """
    Count query for item-wise issue report pagination.
    """
    sql = """
    SELECT COUNT(1) AS total
    FROM issue_li AS il
    INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
    LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = ih.dept_id
    LEFT JOIN item_mst AS im ON im.item_id = il.item_id
    LEFT JOIN item_grp_mst AS igm ON igm.item_grp_id = im.item_grp_id
    WHERE (ih.active = 1 OR ih.active IS NULL)
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
        AND (:item_grp_id IS NULL OR im.item_grp_id = :item_grp_id)
        AND (:date_from IS NULL OR ih.issue_date >= :date_from)
        AND (:date_to IS NULL OR ih.issue_date <= :date_to)
        -- Drill-down filters: the consumption reports (IS01/02/03/06) pass the
        -- ids of the row the user opened, so this returns exactly the issue
        -- lines that made up that figure.
        AND (:dept_id IS NULL OR ih.dept_id = :dept_id)
        AND (:cost_factor_id IS NULL OR il.cost_factor_id = :cost_factor_id)
        AND (:item_id IS NULL OR il.item_id = :item_id)
        AND (
            :search_like IS NULL
            OR im.item_name LIKE :search_like
            OR bm.branch_name LIKE :search_like
            OR igm.item_grp_name LIKE :search_like
            OR dm.dept_desc LIKE :search_like
        );
    """
    return text(sql)


def get_inventory_minmax_report_query():
    """
    Stores min-max report: each item that has an active min-max record for the
    branch, with its min/max/reorder levels and current stock as of :date_to.

    branch_id is required (min-max levels are per branch). Current stock =
    approved receipts up to date_to minus active issues and approved supplier
    returns up to date_to.

    current_qty is also split into two buckets by the PO's expense type
    (TKT-000114): stock_oc_qty = Overhauling/Capital, stock_gpm_qty =
    everything else (General/Production/Maintenance + unclassified). The two
    always sum back to current_qty, and below_min_qty measures the
    General/Production/Maintenance bucket only.

    Parameters:
        :co_id (int) - required
        :branch_id (int) - required
        :date_to (str) - as-on date (YYYY-MM-DD), required
        :item_grp_id (int or NULL) - optional item group filter
        :search_like (str or NULL) - LIKE pattern for item_name, item_grp_name
        :limit (int), :offset (int) - pagination
    """
    sql = """
    WITH stock AS (
        SELECT item_id, SUM(qty) AS net FROM (
            SELECT pid.item_id AS item_id, SUM(pid.approved_qty) AS qty
            FROM proc_inward_dtl AS pid
            INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
            INNER JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
            WHERE pid.active = 1 AND pi.sr_status = 3
                AND bm.co_id = :co_id AND pi.branch_id = :branch_id
                AND pi.inward_date <= :date_to
            GROUP BY pid.item_id
            UNION ALL
            SELECT il.item_id AS item_id, -SUM(il.issue_qty) AS qty
            FROM issue_li AS il
            INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
            INNER JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
            WHERE (ih.active = 1 OR ih.active IS NULL)
                AND ih.status_id IN (1, 3, 5, 20)
                AND bm.co_id = :co_id AND ih.branch_id = :branch_id
                AND ih.issue_date <= :date_to
            GROUP BY il.item_id
            UNION ALL
            SELECT r.item_id AS item_id, -SUM(r.return_qty) AS qty
            FROM vw_drcr_return_qty AS r
            INNER JOIN branch_mst AS bm ON bm.branch_id = r.branch_id
            WHERE bm.co_id = :co_id AND r.branch_id = :branch_id
                AND r.note_date <= :date_to
            GROUP BY r.item_id
        ) AS t GROUP BY item_id
    ),
    -- The Overhauling/Capital slice of the same stock (TKT-000114). Matched on
    -- the expense type NAME by prefix, not id: tenant masters carry spelling
    -- drift ("Mantainence"), and an unmatched name degrades into the
    -- General/Production/Maintenance bucket rather than into wrong data.
    -- Issues are classified by the RECEIPT line's PO expense type (via
    -- inward_dtl_id), NOT issue_li.expense_type_id — an issue booked to a
    -- different expense type than its receipt would otherwise drive this
    -- bucket negative and push the same qty twice into the other bucket.
    stock_oc AS (
        SELECT item_id, SUM(qty) AS net FROM (
            SELECT pid.item_id AS item_id, SUM(pid.approved_qty) AS qty
            FROM proc_inward_dtl AS pid
            INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
            INNER JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
            INNER JOIN proc_po_dtl AS ppd ON ppd.po_dtl_id = pid.po_dtl_id
            INNER JOIN proc_po AS pp ON pp.po_id = ppd.po_id
            INNER JOIN expense_type_mst AS et
                ON et.expense_type_id = pp.expense_type_id
            WHERE pid.active = 1 AND pi.sr_status = 3
                AND bm.co_id = :co_id AND pi.branch_id = :branch_id
                AND pi.inward_date <= :date_to
                AND (LOWER(et.expense_type_name) LIKE 'overhaul%'
                     OR LOWER(et.expense_type_name) LIKE 'capital%')
            GROUP BY pid.item_id
            UNION ALL
            SELECT il.item_id AS item_id, -SUM(il.issue_qty) AS qty
            FROM issue_li AS il
            INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
            INNER JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
            INNER JOIN proc_inward_dtl AS pid
                ON pid.inward_dtl_id = il.inward_dtl_id
            INNER JOIN proc_po_dtl AS ppd ON ppd.po_dtl_id = pid.po_dtl_id
            INNER JOIN proc_po AS pp ON pp.po_id = ppd.po_id
            INNER JOIN expense_type_mst AS et
                ON et.expense_type_id = pp.expense_type_id
            WHERE (ih.active = 1 OR ih.active IS NULL)
                AND ih.status_id IN (1, 3, 5, 20)
                AND bm.co_id = :co_id AND ih.branch_id = :branch_id
                AND ih.issue_date <= :date_to
                AND (LOWER(et.expense_type_name) LIKE 'overhaul%'
                     OR LOWER(et.expense_type_name) LIKE 'capital%')
            GROUP BY il.item_id
        ) AS t GROUP BY item_id
    ),
    pending_indent AS (
        SELECT pid.item_id, SUM(oi.bal_ind_qty) AS qty
        FROM proc_indent_dtl AS pid
        INNER JOIN proc_indent AS pi ON pi.indent_id = pid.indent_id
        INNER JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
        INNER JOIN vw_proc_indent_outstanding_new AS oi
            ON oi.indent_dtl_id = pid.indent_dtl_id
        WHERE pi.active = 1 AND pid.active = 1
            AND pi.status_id = 3
            AND bm.co_id = :co_id AND pi.branch_id = :branch_id
            AND oi.bal_ind_qty > 0
        GROUP BY pid.item_id
    ),
    pending_po AS (
        SELECT ppd.item_id, SUM(opo.bal_po_qty) AS qty
        FROM proc_po_dtl AS ppd
        INNER JOIN proc_po AS pp ON pp.po_id = ppd.po_id
        INNER JOIN branch_mst AS bm ON bm.branch_id = pp.branch_id
        INNER JOIN vw_proc_po_outstanding_new AS opo
            ON opo.po_dtl_id = ppd.po_dtl_id
        WHERE ppd.active = 1
            AND pp.status_id = 3
            AND bm.co_id = :co_id AND pp.branch_id = :branch_id
            AND opo.bal_po_qty > 0
        GROUP BY ppd.item_id
    )
    SELECT
        im.item_id,
        vip.full_item_code AS item_code,
        im.item_name,
        igm.item_grp_name,
        um.uom_name,
        mm.minqty,
        mm.maxqty,
        mm.min_order_qty,
        mm.lead_time,
        COALESCE(st.net, 0) AS current_qty,
        -- Expense-type split of current_qty (TKT-000114). B is the matched
        -- Overhauling/Capital slice, A is the remainder, so A + B = total by
        -- construction — DR/CR supplier returns have no inward link and so
        -- always land in A.
        COALESCE(oc.net, 0) AS stock_oc_qty,
        COALESCE(st.net, 0) - COALESCE(oc.net, 0) AS stock_gpm_qty,
        COALESCE(pin.qty, 0) AS pending_indent_qty,
        COALESCE(ppo.qty, 0) AS pending_po_qty,
        -- How far below min the item is, 0 when at/above min or when no min is
        -- set (TKT-000114). One expression serves the column AND the default
        -- order, so "below min first, biggest shortfall first" survives LIMIT —
        -- sorting the page in Python would only reorder whatever page 1 held.
        -- No min set is NOT a shortfall, and the stock CTE sums DOUBLEs, so a
        -- fully-issued item lands on -2.4e-07 rather than 0 — ROUND collapses
        -- that noise instead of floating a phantom shortfall to the top.
        -- Measured against the General/Production/Maintenance bucket only:
        -- Overhauling/Capital stock is earmarked and does not cover a
        -- production shortfall.
        CASE WHEN mm.minqty IS NULL THEN 0
             ELSE ROUND(GREATEST(
                 mm.minqty - (COALESCE(st.net, 0) - COALESCE(oc.net, 0)), 0
             ), 3)
        END AS below_min_qty
    FROM item_minmax_mst AS mm
    INNER JOIN item_mst AS im ON im.item_id = mm.item_id
    LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = mm.item_id
    LEFT JOIN item_grp_mst AS igm ON igm.item_grp_id = im.item_grp_id
    LEFT JOIN uom_mst AS um ON um.uom_id = im.uom_id
    INNER JOIN branch_mst AS bm ON bm.branch_id = mm.branch_id
    LEFT JOIN stock AS st ON st.item_id = mm.item_id
    LEFT JOIN stock_oc AS oc ON oc.item_id = mm.item_id
    LEFT JOIN pending_indent AS pin ON pin.item_id = mm.item_id
    LEFT JOIN pending_po AS ppo ON ppo.item_id = mm.item_id
    WHERE mm.active = 1
        AND bm.co_id = :co_id
        AND mm.branch_id = :branch_id
        AND (:item_grp_id IS NULL OR im.item_grp_id = :item_grp_id)
        AND (
            :search_like IS NULL
            OR im.item_name LIKE :search_like
            OR igm.item_grp_name LIKE :search_like
        )
    -- Shortest-of-stock first (TKT-000114): the mill reads this sheet to decide
    -- what to indent, so the items needing the most come first, and everything
    -- at/above min keeps the old group/name order behind them.
    ORDER BY below_min_qty DESC, igm.item_grp_name, im.item_name
    LIMIT :limit OFFSET :offset;
    """
    return text(sql)


def get_item_monthwise_report_query():
    """
    Item month-wise consumption (issue qty) grouped by item and calendar month
    within a date range. The endpoint pivots these flat rows into one row per
    item with a column per month.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional branch filter
        :date_from (str) - range start (YYYY-MM-DD), required
        :date_to (str) - range end (YYYY-MM-DD), required
    """
    sql = """
    SELECT
        il.item_id,
        im.item_name,
        igm.item_grp_name,
        um.uom_name,
        CONCAT(YEAR(ih.issue_date), '-', LPAD(MONTH(ih.issue_date), 2, '0')) AS ym,
        COALESCE(SUM(il.issue_qty), 0) AS qty
    FROM issue_li AS il
    INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
    INNER JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
    INNER JOIN item_mst AS im ON im.item_id = il.item_id
    LEFT JOIN item_grp_mst AS igm ON igm.item_grp_id = im.item_grp_id
    LEFT JOIN uom_mst AS um ON um.uom_id = im.uom_id
    WHERE (ih.active = 1 OR ih.active IS NULL)
        AND ih.status_id IN (1, 3, 5, 20)
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
        AND ih.issue_date >= :date_from
        AND ih.issue_date <= :date_to
    GROUP BY il.item_id, ym
    ORDER BY igm.item_grp_name, im.item_name;
    """
    return text(sql)


# Grouping dimensions for the issue-consumption pivot (IS01/IS02/IS03/IS06).
# Each: (SELECT fragment, GROUP BY fragment, ordered list of key field names).
# dept_order = dept_mst.order_id: the department sequence the mill reads the
# report in (process order, not A-Z). The endpoint sorts on it, not displayed.
_CONSUMPTION_DIMENSIONS = {
    "dept": (
        "dm.dept_desc AS department, dm.order_id AS dept_order",
        "dm.dept_id, dm.dept_desc, dm.order_id",
        ["department"],
    ),
    "dept_cost": (
        "dm.dept_desc AS department, dm.order_id AS dept_order, "
        "cfm.cost_factor_name AS cost_factor",
        "dm.dept_id, dm.dept_desc, dm.order_id, cfm.cost_factor_id, "
        "cfm.cost_factor_name",
        ["department", "cost_factor"],
    ),
    "dept_cost_item": (
        "dm.dept_desc AS department, dm.order_id AS dept_order, "
        "cfm.cost_factor_name AS cost_factor, "
        "vip.full_item_code AS item_code, im.item_name AS item_name, "
        # Carried for the row drill-down, not displayed as columns. item_code is
        # not unique across items, so the id is what identifies the row.
        "dm.dept_id AS dept_id, cfm.cost_factor_id AS cost_factor_id, "
        "im.item_id AS item_id",
        "dm.dept_id, dm.dept_desc, dm.order_id, cfm.cost_factor_id, "
        "cfm.cost_factor_name, im.item_id, vip.full_item_code, im.item_name",
        ["department", "cost_factor", "item_code", "item_name"],
    ),
    "item_group": (
        "igm.item_grp_code AS item_group_code, igm.item_grp_name AS item_group",
        "igm.item_grp_id, igm.item_grp_code, igm.item_grp_name",
        ["item_group_code", "item_group"],
    ),
}


def consumption_dimension_keys(dimension: str) -> list[str]:
    """Ordered leading key fields for a consumption dimension."""
    return _CONSUMPTION_DIMENSIONS[dimension][2]


def get_issue_consumption_query(dimension: str):
    print(f"DEBUG: get_issue_consumption_query dimension={dimension}")
    """
    Issue consumption value grouped by ``dimension`` and expense category. Issues
    are valued at their receipt-lot rate via issue_li.inward_dtl_id, taking
    lot_rate_sql (COALESCE(accepted_rate, rate) net of the SR line discount) — a receipt carries its price in exactly one of
    those two columns, and reading only `rate` silently valued the
    accepted_rate-priced lots at zero. Same expression as the stock, ledger,
    issue-checklist and machine-wise reports. Returns flat (group cols, category,
    value) rows; the endpoint pivots category into fixed columns.

    dimension: 'dept' | 'dept_cost' | 'item_group'.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
        :item_code / :item_code_like (str or NULL) - optional item code prefix
    """
    if dimension not in _CONSUMPTION_DIMENSIONS:
        raise ValueError(f"Unknown consumption dimension: {dimension}")
    group_select, group_by, _ = _CONSUMPTION_DIMENSIONS[dimension]
    sql = f"""
    SELECT
        {group_select},
        etm.expense_type_name AS category,
        ROUND(SUM(il.issue_qty * {lot_rate_sql('pid')}), 2) AS value
    FROM issue_li AS il
    INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
    INNER JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
    LEFT JOIN proc_inward_dtl AS pid ON pid.inward_dtl_id = il.inward_dtl_id
    LEFT JOIN expense_type_mst AS etm ON etm.expense_type_id = il.expense_type_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = ih.dept_id
    LEFT JOIN cost_factor_mst AS cfm ON cfm.cost_factor_id = il.cost_factor_id
    LEFT JOIN item_mst AS im ON im.item_id = il.item_id
    LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = il.item_id
    LEFT JOIN item_grp_mst AS igm ON igm.item_grp_id = im.item_grp_id
    WHERE (ih.active = 1 OR ih.active IS NULL)
        AND ih.status_id IN (1, 3, 5, 20)
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
        AND ih.issue_date >= :date_from
        AND ih.issue_date <= :date_to
        AND (:item_code IS NULL OR vip.full_item_code LIKE :item_code_like)
    GROUP BY {group_by}, etm.expense_type_name;
    """
    #print(f"DEBUG: get_issue_consumption_query SQL:\n{sql}")
    return text(sql)


def get_sr_exception_report_query():
    """
    SR exception report: SR line items whose remaining stock is negative —
    issued + returned more than was received. One row per proc_inward_dtl.

    Balance = approved receipt qty - issues against the lot (issue_li by
    inward_dtl_id, statuses 1/3/5/20) - approved DR-note returns
    (vw_drcr_return_qty). Same movement rules as the stock report; only rows
    with balance < -0.01 are returned (dust threshold), worst first.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional branch filter
        :search_like (str or NULL) - LIKE pattern for sr_no, item_name,
            item_grp_name
    """
    sql = """
    SELECT
        pid.inward_dtl_id,
        pi.sr_no,
        pi.inward_date,
        bm.branch_name,
        vip.full_item_code AS item_code,
        im.item_name,
        igm.item_grp_name,
        um.uom_name,
        ROUND(COALESCE(pid.approved_qty, 0), 3) AS received_qty,
        ROUND(COALESCE(iss.qty, 0), 3) AS issued_qty,
        ROUND(COALESCE(ret.qty, 0), 3) AS return_qty,
        ROUND(COALESCE(pid.approved_qty, 0) - COALESCE(iss.qty, 0)
         - COALESCE(ret.qty, 0), 3) AS balance_qty
    FROM proc_inward_dtl AS pid
    INNER JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
    INNER JOIN branch_mst AS bm ON bm.branch_id = pi.branch_id
    LEFT JOIN item_mst AS im ON im.item_id = pid.item_id
    LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = pid.item_id
    LEFT JOIN item_grp_mst AS igm ON igm.item_grp_id = im.item_grp_id
    LEFT JOIN uom_mst AS um ON um.uom_id = im.uom_id
    LEFT JOIN (
        SELECT il.inward_dtl_id, SUM(il.issue_qty) AS qty
        FROM issue_li AS il
        INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
        WHERE (ih.active = 1 OR ih.active IS NULL)
            AND ih.status_id IN (1, 3, 5, 20)
            AND il.inward_dtl_id IS NOT NULL
        GROUP BY il.inward_dtl_id
    ) AS iss ON iss.inward_dtl_id = pid.inward_dtl_id
    LEFT JOIN (
        SELECT r.inward_dtl_id, SUM(r.return_qty) AS qty
        FROM vw_drcr_return_qty AS r
        GROUP BY r.inward_dtl_id
    ) AS ret ON ret.inward_dtl_id = pid.inward_dtl_id
    WHERE pid.active = 1
        AND pi.sr_status = 3
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR pi.branch_id = :branch_id)
        -- ponytail: -0.01 threshold hides fractional-dust rows (e.g. a lot
        -- over-issued by 0.004 L of oil); tighten if sub-0.01 gaps matter.
        AND (COALESCE(pid.approved_qty, 0) - COALESCE(iss.qty, 0)
             - COALESCE(ret.qty, 0)) < -0.01
        AND (
            :search_like IS NULL
            OR pi.sr_no LIKE :search_like
            OR im.item_name LIKE :search_like
            OR igm.item_grp_name LIKE :search_like
        )
    ORDER BY balance_qty ASC, pi.inward_date DESC;
    """
    return text(sql)


def get_issue_machinewise_query():
    """
    Machine-wise item consumption (IS05): issue qty + value per machine and item.
    Only issues tagged to a machine. Value = issue_qty * receipt-lot rate.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = f"""
    SELECT
        mm.machine_name,
        dm.dept_desc AS department,
        vip.full_item_code AS item_code,
        im.item_name,
        igm.item_grp_name,
        um.uom_name,
        etm.expense_type_name,
        ROUND(SUM(il.issue_qty), 3) AS issue_qty,
        ROUND(SUM(il.issue_qty * {lot_rate_sql('pid')}), 2)
            AS issue_value
    FROM issue_li AS il
    INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
    INNER JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
    LEFT JOIN proc_inward_dtl AS pid ON pid.inward_dtl_id = il.inward_dtl_id
    LEFT JOIN machine_mst AS mm ON mm.machine_id = il.machine_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = ih.dept_id
    LEFT JOIN item_mst AS im ON im.item_id = il.item_id
    LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = il.item_id
    LEFT JOIN item_grp_mst AS igm ON igm.item_grp_id = im.item_grp_id
    LEFT JOIN uom_mst AS um ON um.uom_id = im.uom_id
    LEFT JOIN expense_type_mst AS etm ON etm.expense_type_id = il.expense_type_id
    WHERE (ih.active = 1 OR ih.active IS NULL)
        AND ih.status_id IN (1, 3, 5, 20)
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR ih.branch_id = :branch_id)
        AND ih.issue_date >= :date_from
        AND ih.issue_date <= :date_to
        AND il.machine_id IS NOT NULL
        AND il.machine_id > 0
    GROUP BY mm.machine_id, mm.machine_name, dm.dept_id, dm.dept_desc,
             im.item_id, vip.full_item_code, im.item_name, igm.item_grp_name,
             um.uom_name, etm.expense_type_id, etm.expense_type_name
    ORDER BY mm.machine_name, im.item_name;
    """
    return text(sql)

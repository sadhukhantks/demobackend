"""
SQL queries for inventory issue module.
Based on actual database schema from 'sls' database.
"""
from sqlalchemy.sql import text


def lot_rate_sql(alias: str) -> str:
    """
    Per-unit valuation rate of a receipt lot (proc_inward_dtl aliased ``alias``),
    NET of the SR line discount — the same price the SR screen, bill pass and
    accounting posting use (qty * COALESCE(accepted_rate, rate) - discount_amount).
    A receipt carries its price in exactly one of accepted_rate / rate;
    discount_amount is the whole-line discount on approved_qty, so it is spread
    per unit here (NULLIF keeps fully-rejected lines at the gross rate * 0 qty).
    Every inventory valuation (stock, ledger, issue reports, issue pickers) must
    use this one expression so values agree across reports (TKT-000149).
    """
    return (
        f"(COALESCE({alias}.accepted_rate, {alias}.rate, 0)"
        f" - COALESCE({alias}.discount_amount / NULLIF({alias}.approved_qty, 0), 0))"
    )


def get_issue_table_query():
    """Get paginated list of issues for the index table."""
    sql = """SELECT
        ih.issue_id,
        ih.issue_pass_no AS issue_no,
        ih.issue_pass_print_no,
        ih.issue_date,
        ih.branch_id,
        bm.branch_name,
        bm.branch_prefix,
        bm.co_id,
        cm.co_prefix,
        dm.dept_desc AS department,
        ih.issued_to,
        ih.req_by,
        ih.internal_note,
        sm.status_name AS status
    FROM issue_hdr AS ih
    LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
    LEFT JOIN co_mst AS cm ON cm.co_id = bm.co_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = ih.dept_id
    LEFT JOIN status_mst AS sm ON sm.status_id = ih.status_id
    WHERE (ih.active = 1 OR ih.active IS NULL)
        AND (:co_id IS NULL OR bm.co_id = :co_id)
        AND (
            :search_like IS NULL
            OR CAST(ih.issue_pass_no AS CHAR) LIKE :search_like
            OR ih.issue_pass_print_no LIKE :search_like
            OR bm.branch_name LIKE :search_like
            OR dm.dept_desc LIKE :search_like
            OR ih.issued_to LIKE :search_like
        )
    ORDER BY ih.issue_date DESC, ih.issue_id DESC
    LIMIT :limit OFFSET :offset;"""
    return text(sql)


def get_issue_table_count_query():
    """Get total count of issues for pagination."""
    sql = """SELECT COUNT(1) AS total
    FROM issue_hdr AS ih
    LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = ih.dept_id
    WHERE (ih.active = 1 OR ih.active IS NULL)
        AND (:co_id IS NULL OR bm.co_id = :co_id)
        AND (
            :search_like IS NULL
            OR CAST(ih.issue_pass_no AS CHAR) LIKE :search_like
            OR ih.issue_pass_print_no LIKE :search_like
            OR bm.branch_name LIKE :search_like
            OR dm.dept_desc LIKE :search_like
            OR ih.issued_to LIKE :search_like
        );"""
    return text(sql)


def get_issue_by_id_query():
    """Get issue header by ID with company and branch print-header fields."""
    sql = """SELECT
        ih.issue_id,
        ih.issue_pass_no AS issue_no,
        ih.issue_pass_print_no,
        ih.issue_date,
        ih.branch_id,
        bm.branch_name,
        bm.branch_address1,
        bm.branch_address2,
        bm.branch_zipcode,
        bm.gst_no AS branch_gst_no,
        ih.dept_id,
        dm.dept_desc AS department,
        ih.project_id,
        pm.prj_name AS project_name,
        ih.customer_id,
        ih.issued_to,
        ih.req_by,
        ih.internal_note,
        ih.status_id,
        ih.approval_level,
        sm.status_name AS status,
        ih.approved_by,
        ih.approved_date,
        ih.updated_by,
        ih.updated_date_time,
        um.name AS updated_by_name,
        cm.co_id,
        cm.co_name,
        cm.co_logo,
        cm.co_address1,
        cm.co_address2,
        cm.co_zipcode,
        cm.co_cin_no,
        cm.co_pan_no,
        cm.co_email_id
    FROM issue_hdr AS ih
    LEFT JOIN branch_mst AS bm ON bm.branch_id = ih.branch_id
    LEFT JOIN co_mst AS cm ON cm.co_id = bm.co_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = ih.dept_id
    LEFT JOIN project_mst AS pm ON pm.project_id = ih.project_id
    LEFT JOIN status_mst AS sm ON sm.status_id = ih.status_id
    LEFT JOIN user_mst AS um ON um.user_id = ih.updated_by
    WHERE ih.issue_id = :issue_id
        AND (ih.active = 1 OR ih.active IS NULL);"""
    return text(sql)


def get_issue_details_query():
    """Get issue line items by issue ID.
    Uses vw_item_with_group_path for hierarchical item group path display."""
    sql = f"""SELECT
        il.issue_li_id,
        il.issue_id,
        il.item_id,
        vip.item_name,
        vip.item_code,
        vip.item_grp_id,
        vip.item_group_name_display AS item_grp_name,
        vip.item_group_code_display AS item_grp_code,
        vip.full_item_code,
        il.uom_id,
        um.uom_name,
        il.req_quantity,
        il.issue_qty,
        il.expense_type_id,
        etm.expense_type_name,
        il.cost_factor_id,
        cfm.cost_factor_name,
        il.machine_id,
        mm.machine_name,
        il.inward_dtl_id,
        il.remarks,
        pi.sr_no,
        {lot_rate_sql('pid')} AS rate
    FROM issue_li AS il
    LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = il.item_id
    LEFT JOIN uom_mst AS um ON um.uom_id = il.uom_id
    LEFT JOIN expense_type_mst AS etm ON etm.expense_type_id = il.expense_type_id
    LEFT JOIN cost_factor_mst AS cfm ON cfm.cost_factor_id = il.cost_factor_id
    LEFT JOIN machine_mst AS mm ON mm.machine_id = il.machine_id
    LEFT JOIN proc_inward_dtl AS pid ON pid.inward_dtl_id = il.inward_dtl_id
    LEFT JOIN proc_inward AS pi ON pi.inward_id = pid.inward_id
    WHERE il.issue_id = :issue_id
    ORDER BY il.issue_li_id;"""
    return text(sql)


def insert_issue_hdr():
    """Insert a new issue header."""
    sql = """INSERT INTO issue_hdr (
        branch_id,
        dept_id,
        issue_pass_no,
        issue_pass_print_no,
        active,
        issue_date,
        item_id,
        status_id,
        issued_to,
        req_by,
        project_id,
        customer_id,
        internal_note,
        updated_by,
        updated_date_time
    ) VALUES (
        :branch_id,
        :dept_id,
        :issue_pass_no,
        :issue_pass_print_no,
        :active,
        :issue_date,
        :item_id,
        :status_id,
        :issued_to,
        :req_by,
        :project_id,
        :customer_id,
        :internal_note,
        :updated_by,
        :updated_date_time
    );"""
    return text(sql)


def insert_issue_li():
    """Insert a new issue line item."""
    sql = """INSERT INTO issue_li (
        issue_id,
        item_id,
        uom_id,
        req_quantity,
        issue_qty,
        expense_type_id,
        cost_factor_id,
        machine_id,
        inward_dtl_id,
        remarks,
        updated_by,
        updated_date_time
    ) VALUES (
        :issue_id,
        :item_id,
        :uom_id,
        :req_quantity,
        :issue_qty,
        :expense_type_id,
        :cost_factor_id,
        :machine_id,
        :inward_dtl_id,
        :remarks,
        :updated_by,
        :updated_date_time
    );"""
    return text(sql)


def update_issue_hdr():
    """Update an existing issue header."""
    sql = """UPDATE issue_hdr SET
        branch_id = :branch_id,
        dept_id = :dept_id,
        issue_date = :issue_date,
        issued_to = :issued_to,
        req_by = :req_by,
        project_id = :project_id,
        customer_id = :customer_id,
        internal_note = :internal_note,
        status_id = :status_id,
        updated_by = :updated_by,
        updated_date_time = :updated_date_time
    WHERE issue_id = :issue_id;"""
    return text(sql)


def delete_issue_li():
    """Delete issue line items for a given issue (before re-inserting)."""
    sql = """DELETE FROM issue_li WHERE issue_id = :issue_id;"""
    return text(sql)


def get_max_issue_pass_no_for_branch():
    """Get the maximum issue_pass_no for a branch WITHIN a financial year.

    issue_pass_no is a per-financial-year sequence (legacy behaviour carried
    over from the old system). The caller passes the FY window
    [:fy_start, :fy_end] (Indian FY: 1 Apr .. 31 Mar) derived from the new
    issue's issue_date, so the next number = MAX(this branch, this FY, active) + 1.

    Without the FY filter a stale prior-year maximum (e.g. branch 29 reaching
    8900 in FY2023-24) poisons the current year and the number jumps
    (8901 instead of 35). issue_date is DATE (no time component), so the
    inclusive <= :fy_end bound correctly includes 31 Mar.
    """
    sql = """SELECT COALESCE(MAX(ih.issue_pass_no), 0) AS max_issue_no
    FROM issue_hdr ih
    WHERE ih.branch_id = :branch_id
        AND ih.issue_date >= :fy_start
        AND ih.issue_date <= :fy_end
        AND (ih.active = 1 OR ih.active IS NULL);"""
    return text(sql)


def update_issue_status():
    """Update the status of an issue."""
    sql = """UPDATE issue_hdr SET
        status_id = :status_id,
        approval_level = :approval_level,
        approved_by = :approved_by,
        approved_date = :approved_date,
        updated_by = :updated_by,
        updated_date_time = :updated_date_time
    WHERE issue_id = :issue_id;"""
    return text(sql)


# Quantity on each lot that DRAFTED (status 21) issues have already committed.
#
# TKT-000143: the picker and the Item Ledger disagree on what a draft means.
# vw_approved_inward_qty.balance_qty subtracts every issue line whose header is
# not cancelled (status <> 6) — drafts included — while the Item Ledger and the
# Stock Report count only status 1/3/5/20. So a drafted issue silently eats
# picker stock while staying invisible in the ledger, and a fully draft-held lot
# vanished from the picker entirely ("ledger says 1 PC, picker shows nothing").
#
# Neither definition is being changed (the view is per-tenant DDL, the ledger is
# correct to ignore unapproved paper). Instead the picker joins the draft hold
# and SHOWS it: available_qty stays draft-exclusive (what may be issued now),
# ledger_qty is what the ledger shows, and draft_held_refs names the issues to
# chase. Approving or cancelling those issues resolves the difference.
#
# :issue_id excludes the draft being edited from its own hold — the same
# self-exclusion get_lot_availability_query does with own_qty. Without it,
# re-picking a lot inside draft 1433 reports it "held by draft 1433". Bind None
# when creating; every query carrying this join must bind it either way.
_DRAFT_HELD_JOIN = """
    LEFT JOIN (
        SELECT il.inward_dtl_id,
               SUM(il.issue_qty) AS draft_qty,
               GROUP_CONCAT(DISTINCT COALESCE(ih.issue_pass_print_no,
                                              CAST(ih.issue_pass_no AS CHAR))
                            ORDER BY ih.issue_pass_no SEPARATOR ', ') AS draft_refs
        FROM issue_li AS il
        INNER JOIN issue_hdr AS ih ON ih.issue_id = il.issue_id
        WHERE (ih.active = 1 OR ih.active IS NULL)
          AND ih.status_id = 21
          AND (:issue_id IS NULL OR ih.issue_id <> :issue_id)
        GROUP BY il.inward_dtl_id
    ) AS dh ON dh.inward_dtl_id = v.inward_dtl_id"""

# Same visibility rule in all three picker queries — rows and count must agree,
# or pagination totals drift from the rows returned.
_PICKER_VISIBLE = "(v.balance_qty > 0 OR COALESCE(dh.draft_qty, 0) > 0)"


def get_available_inward_inventory_query():
    """
    Get available inventory from inward details for issuing.
    Returns items with available qty (approved_qty - already issued qty).
    Used for selecting SR line items when creating an issue.
    Uses the vw_approved_inward_qty view for approved inwards.
    Uses vw_item_with_group_path for hierarchical item group path display.

    sr_no is the stored, already-formatted SR number (co/branch/SR/FY/seq) —
    minted at SR approval, and the view only returns sr_status = 3 rows.

    Joins _DRAFT_HELD_JOIN so a lot held by a DRAFTED (21) issue is shown with
    its hold explained instead of silently disappearing — see that comment and
    TKT-000143. available_qty keeps its meaning exactly: v.balance_qty, what may
    be issued right now.

    Binds :issue_id — the draft being edited (None when creating), excluded from
    its own draft hold.
    """
    sql = f"""SELECT
        v.inward_dtl_id,
        v.inward_id,
        pi.inward_sequence_no,
        pi.sr_no,
        v.inward_date,
        v.branch_id,
        bm.branch_name,
        v.item_id,
        vip.item_name,
        vip.item_code,
        vip.item_grp_id,
        vip.item_group_name_display AS item_grp_name,
        vip.item_group_code_display AS item_grp_code,
        vip.full_item_code,
        pid.item_make_id,
        imk.item_make_name,
        v.uom_id,
        um.uom_name,
        v.approved_qty,
        v.issue_qty,
        v.balance_qty AS available_qty,
        COALESCE(dh.draft_qty, 0) AS draft_held_qty,
        dh.draft_refs AS draft_held_refs,
        ROUND(v.balance_qty + COALESCE(dh.draft_qty, 0), 3) AS ledger_qty,
        {lot_rate_sql('pid')} AS rate,
        pid.warehouse_id,
        wm.warehouse_name
    FROM vw_approved_inward_qty AS v
    INNER JOIN proc_inward AS pi ON pi.inward_id = v.inward_id
    INNER JOIN proc_inward_dtl AS pid ON pid.inward_dtl_id = v.inward_dtl_id
    LEFT JOIN branch_mst AS bm ON bm.branch_id = v.branch_id
    LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = v.item_id
    LEFT JOIN item_make AS imk ON imk.item_make_id = pid.item_make_id
    LEFT JOIN uom_mst AS um ON um.uom_id = v.uom_id
    LEFT JOIN warehouse_mst AS wm ON wm.warehouse_id = pid.warehouse_id{_DRAFT_HELD_JOIN}
    WHERE v.branch_id = :branch_id
        AND {_PICKER_VISIBLE}
        AND (:item_id IS NULL OR v.item_id = :item_id)
        AND (:item_grp_id IS NULL OR vip.item_grp_id = :item_grp_id)
    ORDER BY v.inward_date ASC, v.inward_dtl_id ASC;"""
    return text(sql)


def get_searchable_inventory_list_query():
    """
    Get paginated searchable inventory list from approved inwards.
    Supports search by item group code/name, item code/name, full_item_code, and SR number.
    Uses vw_item_with_group_path for hierarchical item group path display and search.
    Used for the inventory search table in create issue page.

    sr_no is the stored, already-formatted SR number (co/branch/SR/FY/seq).

    Joins _DRAFT_HELD_JOIN so a lot held by a DRAFTED (21) issue stays visible
    with its hold explained rather than vanishing from the search (TKT-000143:
    the view counts drafts against stock, the Item Ledger does not).

    Binds :issue_id — the draft being edited (None when creating), excluded from
    its own draft hold.
    """
    sql = f"""SELECT
        v.inward_dtl_id,
        v.inward_id,
        pi.inward_sequence_no,
        pi.sr_no,
        v.inward_date,
        v.branch_id,
        bm.branch_name,
        v.item_id,
        vip.item_name,
        vip.item_code,
        vip.item_grp_id,
        vip.item_group_name_display AS item_grp_name,
        vip.item_group_code_display AS item_grp_code,
        vip.full_item_code,
        pid.item_make_id,
        imk.item_make_name,
        v.uom_id,
        um.uom_name,
        v.approved_qty,
        v.issue_qty,
        v.balance_qty AS available_qty,
        COALESCE(dh.draft_qty, 0) AS draft_held_qty,
        dh.draft_refs AS draft_held_refs,
        ROUND(v.balance_qty + COALESCE(dh.draft_qty, 0), 3) AS ledger_qty,
        {lot_rate_sql('pid')} AS rate,
        pid.warehouse_id,
        wm.warehouse_name
    FROM vw_approved_inward_qty AS v
    INNER JOIN proc_inward AS pi ON pi.inward_id = v.inward_id
    INNER JOIN proc_inward_dtl AS pid ON pid.inward_dtl_id = v.inward_dtl_id
    LEFT JOIN branch_mst AS bm ON bm.branch_id = v.branch_id
    LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = v.item_id
    LEFT JOIN item_make AS imk ON imk.item_make_id = pid.item_make_id
    LEFT JOIN uom_mst AS um ON um.uom_id = v.uom_id
    LEFT JOIN warehouse_mst AS wm ON wm.warehouse_id = pid.warehouse_id{_DRAFT_HELD_JOIN}
    WHERE v.branch_id = :branch_id
        AND {_PICKER_VISIBLE}
        AND (
            :search_like IS NULL
            OR vip.item_code LIKE :search_like
            OR vip.item_name LIKE :search_like
            OR vip.item_group_code_display LIKE :search_like
            OR vip.item_group_name_display LIKE :search_like
            OR vip.full_item_code LIKE :search_like
            OR pi.sr_no LIKE :search_like
        )
    ORDER BY v.inward_date DESC, v.inward_dtl_id DESC
    LIMIT :limit OFFSET :offset;"""
    return text(sql)


def get_searchable_inventory_count_query():
    """
    Get total count for searchable inventory list.
    Uses vw_item_with_group_path for hierarchical search.

    Carries the same _DRAFT_HELD_JOIN (including its :issue_id bind) and the same
    visibility filter as the list query — draft-held lots are rows there
    (TKT-000143), so they must be counted here or pagination totals no longer
    match the rows.
    """
    sql = f"""SELECT COUNT(1) AS total
    FROM vw_approved_inward_qty AS v
    INNER JOIN proc_inward AS pi ON pi.inward_id = v.inward_id
    INNER JOIN proc_inward_dtl AS pid ON pid.inward_dtl_id = v.inward_dtl_id
    LEFT JOIN vw_item_with_group_path AS vip ON vip.item_id = v.item_id{_DRAFT_HELD_JOIN}
    WHERE v.branch_id = :branch_id
        AND {_PICKER_VISIBLE}
        AND (
            :search_like IS NULL
            OR vip.item_code LIKE :search_like
            OR vip.item_name LIKE :search_like
            OR vip.item_group_code_display LIKE :search_like
            OR vip.item_group_name_display LIKE :search_like
            OR vip.full_item_code LIKE :search_like
            OR pi.sr_no LIKE :search_like
        );"""
    return text(sql)


def get_cost_factors_by_branch_query():
    """Get cost factors for a branch."""
    sql = """SELECT
        cf.cost_factor_id,
        cf.cost_factor_name,
        cf.cost_factor_desc,
        cf.branch_id,
        cf.dept_id
    FROM cost_factor_mst AS cf
    WHERE cf.branch_id = :branch_id
    ORDER BY cf.cost_factor_name;"""
    return text(sql)


def get_machines_by_dept_query():
    """Get machines for a department."""
    sql = """SELECT
        m.machine_id,
        m.machine_name,
        m.dept_id,
        m.machine_type_id,
        mt.machine_type_name,
        m.mech_code
    FROM machine_mst AS m
    LEFT JOIN machine_type_mst AS mt ON mt.machine_type_id = m.machine_type_id
    WHERE m.dept_id = :dept_id
        AND m.active = 1
    ORDER BY m.machine_name;"""
    return text(sql)


def get_machines_by_branch_query():
    """
    Get all machines for a branch (across all departments).
    Machines are linked to departments, and departments have branch_id.
    Frontend will filter by selected department.
    """
    sql = """SELECT
        m.machine_id,
        m.machine_name,
        m.dept_id,
        dm.dept_desc AS dept_name,
        m.machine_type_id,
        mt.machine_type_name,
        m.mech_code
    FROM machine_mst AS m
    LEFT JOIN machine_type_mst AS mt ON mt.machine_type_id = m.machine_type_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = m.dept_id
    WHERE dm.branch_id = :branch_id
        AND m.active = 1
    ORDER BY m.machine_name;"""
    return text(sql)


def get_lot_availability_query(dtl_count: int, exclude_issue: bool):
    """
    Availability of specific inward lots, for the server-side over-issue guard.

    ``balance_qty`` from vw_approved_inward_qty is already net of issues AND of
    approved supplier returns (DR notes) — see
    dbqueries/migrations/20260823_drcr_return_reduces_stock.sql. When editing an
    existing issue, its own current lines are still in issue_li, so they are
    added back as ``own_qty``: available = balance_qty + own_qty.

    A requested lot missing from the result is NOT issuable (SR not approved, or
    the inward line was deactivated) — the caller must reject it.

    Parameters:
        :d0..:dN (int) - inward_dtl_ids
        :issue_id (int) - only when exclude_issue is True
    """
    placeholders = ", ".join(f":d{i}" for i in range(dtl_count))
    own = (
        """
    LEFT JOIN (
        SELECT inward_dtl_id, SUM(issue_qty) AS qty
        FROM issue_li WHERE issue_id = :issue_id GROUP BY inward_dtl_id
    ) AS own ON own.inward_dtl_id = v.inward_dtl_id"""
        if exclude_issue else ""
    )
    own_col = "COALESCE(own.qty, 0)" if exclude_issue else "0"
    sql = f"""SELECT
        v.inward_dtl_id,
        v.balance_qty + {own_col} AS available_qty,
        im.item_name,
        pi.sr_no
    FROM vw_approved_inward_qty AS v
    LEFT JOIN item_mst AS im ON im.item_id = v.item_id
    LEFT JOIN proc_inward AS pi ON pi.inward_id = v.inward_id{own}
    WHERE v.inward_dtl_id IN ({placeholders});"""
    return text(sql)

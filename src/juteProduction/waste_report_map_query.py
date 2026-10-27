"""SQL for the Wastage Report Mapping master (jute_waste_report_map) — TKT-000183.

Schema: dbqueries/migrations/20260825_jute_waste_report_map.sql (ORM model in
wastage_report_models.JuteWasteReportMap). Group-level fields (group_name,
day_denom, total_denom, display_order) are repeated on every row of a group;
the Wastage Report takes the first active row by (display_order,
waste_report_map_id) as the group definition, so the setup's group list below
resolves a group the same way.

item_mst carries no co_id — company scope lives on item_grp_mst.co_id, which is
also where item_type_id hangs (3 = 'jute wastage'). dept_mst has no co_id and no
active column; a department belongs to the company through its branch.
"""

from sqlalchemy import text


# =============================================================================
# Setup (pickers)
# =============================================================================


def get_waste_items_for_map_query():
    """Waste-item picker: active items whose group is item type :item_type_id
    (3 = jute wastage) and belongs to the company."""
    return text(
        """
        SELECT im.item_id, im.item_code, im.item_name
        FROM item_mst im
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE im.active = 1
          AND ig.item_type_id = :item_type_id
          AND ig.co_id = :co_id
        ORDER BY im.item_name
        """
    )


def get_company_departments_query():
    """Department picker: every department of the company's active branches,
    with the branch name (department names repeat across branches)."""
    return text(
        """
        SELECT d.dept_id, d.dept_desc, d.dept_code, d.branch_id, b.branch_name
        FROM dept_mst d
        INNER JOIN branch_mst b ON b.branch_id = d.branch_id
        WHERE b.co_id = :co_id
          AND b.active = 1
        ORDER BY d.dept_desc, b.branch_name
        """
    )


def get_existing_groups_query():
    """One row per active group_code of the company: the group definition the
    report would use (first row by display_order, then id) plus how many active
    rows the group has."""
    return text(
        """
        SELECT g.group_code, g.group_name, g.day_denom, g.total_denom,
               g.display_order, g.item_count
        FROM (
            SELECT m.group_code, m.group_name, m.day_denom, m.total_denom,
                   m.display_order,
                   COUNT(*) OVER (PARTITION BY m.group_code) AS item_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY m.group_code
                       ORDER BY m.display_order, m.waste_report_map_id
                   ) AS rn
            FROM jute_waste_report_map m
            WHERE m.co_id = :co_id
              AND m.active = 1
        ) g
        WHERE g.rn = 1
        ORDER BY g.display_order, g.group_code
        """
    )


# =============================================================================
# List
# =============================================================================


def get_waste_report_map_list_query():
    """Active mapping rows of the company with item, department and branch
    names. dept_id NULL = the item maps from any department (LEFT JOINs)."""
    return text(
        """
        SELECT
            m.waste_report_map_id,
            m.co_id,
            m.group_code,
            m.group_name,
            m.day_denom,
            m.total_denom,
            m.item_id,
            im.item_code,
            im.item_name,
            m.dept_id,
            d.dept_desc,
            d.branch_id,
            b.branch_name,
            m.col_label,
            m.display_order,
            m.active,
            m.updated_by,
            m.updated_date_time
        FROM jute_waste_report_map m
        LEFT JOIN item_mst im ON im.item_id = m.item_id
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN branch_mst b ON b.branch_id = d.branch_id
        WHERE m.co_id = :co_id
          AND m.active = 1
        ORDER BY m.display_order, m.group_code, im.item_name, m.waste_report_map_id
        """
    )


# =============================================================================
# Write guards
# =============================================================================


def get_waste_report_map_row_query():
    """One ACTIVE mapping row by id (update / deactivate existence + company check)."""
    return text(
        """
        SELECT waste_report_map_id, co_id, group_code, item_id, dept_id, active
        FROM jute_waste_report_map
        WHERE waste_report_map_id = :id
          AND active = 1
        """
    )


def verify_map_item_query():
    """Item guard: the item exists, its group is item type :item_type_id and
    the group belongs to the company. Not gated on im.active so an existing row
    whose item was later retired can still be edited (the picker only offers
    active items)."""
    return text(
        """
        SELECT im.item_id
        FROM item_mst im
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE im.item_id = :item_id
          AND ig.item_type_id = :item_type_id
          AND ig.co_id = :co_id
        """
    )


def verify_map_dept_query():
    """Department guard: the department exists and its branch belongs to the company."""
    return text(
        """
        SELECT d.dept_id
        FROM dept_mst d
        INNER JOIN branch_mst b ON b.branch_id = d.branch_id
        WHERE d.dept_id = :dept_id
          AND b.co_id = :co_id
        """
    )


def find_duplicate_map_row_query():
    """Another ACTIVE row for the same (co_id, item_id, dept_id). dept_id uses
    the NULL-safe <=> so two "any department" rows collide too; :exclude_id
    leaves out the row being updated (NULL on create)."""
    return text(
        """
        SELECT m.waste_report_map_id, m.group_code, m.group_name
        FROM jute_waste_report_map m
        WHERE m.co_id = :co_id
          AND m.item_id = :item_id
          AND m.dept_id <=> :dept_id
          AND m.active = 1
          AND (:exclude_id IS NULL OR m.waste_report_map_id <> :exclude_id)
        LIMIT 1
        """
    )


# =============================================================================
# Writes
# =============================================================================


def insert_waste_report_map_query():
    """Insert one active mapping row."""
    return text(
        """
        INSERT INTO jute_waste_report_map
            (co_id, group_code, group_name, day_denom, total_denom, item_id,
             dept_id, col_label, display_order, active, updated_by)
        VALUES
            (:co_id, :group_code, :group_name, :day_denom, :total_denom, :item_id,
             :dept_id, :col_label, :display_order, 1, :updated_by)
        """
    )


def update_waste_report_map_query():
    """Rewrite every editable field of one mapping row."""
    return text(
        """
        UPDATE jute_waste_report_map
        SET group_code = :group_code,
            group_name = :group_name,
            day_denom = :day_denom,
            total_denom = :total_denom,
            item_id = :item_id,
            dept_id = :dept_id,
            col_label = :col_label,
            display_order = :display_order,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE waste_report_map_id = :id
        """
    )


def propagate_group_fields_query():
    """Copy the group-level fields onto every OTHER active row of the same
    (co_id, group_code) that differs, so the group stays consistent whichever
    row the report reads as its definition. Rows that already match are left
    untouched (their audit columns don't churn). group_name is compared as
    BINARY because the table collation (utf8mb4_0900_ai_ci) treats a case-only
    rename ('Sweep' -> 'SWEEP') as equal."""
    return text(
        """
        UPDATE jute_waste_report_map
        SET group_name = :group_name,
            day_denom = :day_denom,
            total_denom = :total_denom,
            display_order = :display_order,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE co_id = :co_id
          AND group_code = :group_code
          AND active = 1
          AND waste_report_map_id <> :id
          AND (CAST(group_name AS BINARY) <> CAST(:group_name AS BINARY)
               OR day_denom <> :day_denom
               OR total_denom <> :total_denom
               OR display_order <> :display_order)
        """
    )


def deactivate_waste_report_map_query():
    """Soft-delete one mapping row."""
    return text(
        """
        UPDATE jute_waste_report_map
        SET active = 0,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE waste_report_map_id = :id
        """
    )

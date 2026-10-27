"""SQL for the Loom->Quality map bulk tools (TKT-000119).

Two operator chores the ticket asks to remove:
  1. "Fill from previous": the map is stored per (tran_date, spell_id, loom) and every
     downstream reader (entry capture stamp, day-slice, planning grid, Process) needs a
     persisted row, so an unchanged map still had to be re-saved for every spell. The
     carry-forward endpoint writes the missing rows in one action, copying each loom's
     previous mapping. It only INSERTS rows for looms with no active row in a cell;
     existing rows are never changed.
  2. Bulk select: "select by group" reads the existing grouping masters (Hands Norms
     machine groups and the Loom Groups master of the Loom Modification register),
     restricted to Loom-type machines, so no new grouping concept is introduced.

All queries are tenant-DB, read-only except insert_carry_forward_map_rows_query.
"""

from sqlalchemy import text


def get_branch_looms_query():
    """Active Loom-type machines of one branch (same loom set as the map grid)."""
    return text(
        """
        SELECT m.machine_id
        FROM machine_mst m
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE m.active = 1
          AND mt.active = 1
          AND mt.machine_type_name = :loom_type
          AND d.branch_id = :branch_id
        """
    )


def get_map_rows_in_range_query():
    """Active map rows of a company for a date range (idx_wqm_key range scan).

    Includes every spell (not only the target ones) so the planner can see a
    same-date earlier spell as the previous mapping. Callers filter machine_id
    to the branch's looms in Python (map rows carry co_id; looms are branch-bound).
    """
    return text(
        """
        SELECT tran_date, spell_id, machine_id, weaving_quality_id, weaving_quality_map_id
        FROM jute_prod_weaving_quality_map
        WHERE co_id = :co_id
          AND tran_date BETWEEN :from_date AND :to_date
          AND active = 1
          AND weaving_quality_id IS NOT NULL
        """
    )


def get_current_quality_pointers_query():
    """The maintained latest-mapping pointer per loom (weaving_loom_current_quality).

    Used only as the seed fallback for looms with no map row inside the look-back
    window, and only when the pointer is strictly before the fill's first date.
    """
    return text(
        """
        SELECT machine_id, weaving_quality_id, tran_date, spell_id
        FROM weaving_loom_current_quality
        WHERE co_id = :co_id
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        """
    )


def insert_carry_forward_map_rows_query():
    """Insert one active map row (executemany-friendly). Same columns as the
    Save Map insert, stamped with the acting user."""
    return text(
        """
        INSERT INTO jute_prod_weaving_quality_map
            (co_id, branch_id, tran_date, spell_id, machine_id, weaving_quality_id,
             active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :spell_id, :machine_id, :weaving_quality_id,
             1, :updated_by)
        """
    )


def get_production_rows_in_range_query():
    """Active production rows of a branch for a date range (idx_wd_co_branch_date).

    Production captured before the map existed is stored with weaving_quality_id
    NULL; the fill re-stamps those (same rule as Save Map) so the day-slice stops
    computing zero for them. A row that already carries a quality decides the
    map row written for that loom/cell.
    """
    return text(
        """
        SELECT weaving_daily_id, tran_date, spell_id, machine_id, weaving_quality_id
        FROM jute_prod_weaving_daily
        WHERE co_id = :co_id
          AND branch_id = :branch_id
          AND tran_date BETWEEN :from_date AND :to_date
          AND active = 1
        """
    )


def get_machine_group_looms_query():
    """Hands Norms machine groups (jute_prod_machine_group_mst/_dtl) -> loom members.

    Only Loom-type members of the branch are returned; groups without any such
    member drop out naturally (inner joins).
    """
    return text(
        """
        SELECT g.machine_group_id AS group_id, g.group_code, g.group_name,
               gd.machine_id
        FROM jute_prod_machine_group_mst g
        INNER JOIN jute_prod_machine_group_dtl gd
                ON gd.machine_group_id = g.machine_group_id AND gd.active = 1
        INNER JOIN machine_mst m ON m.machine_id = gd.machine_id AND m.active = 1
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE g.co_id = :co_id
          AND g.active = 1
          AND mt.machine_type_name = :loom_type
          AND d.branch_id = :branch_id
        ORDER BY g.group_code, gd.machine_id
        """
    )


def get_loom_group_looms_query():
    """Loom Groups master (jute_prod_loom_group/_dtl, one weaver's set of looms)
    -> loom members of the branch."""
    return text(
        """
        SELECT g.loom_group_id AS group_id, g.group_code, g.group_name,
               gd.machine_id
        FROM jute_prod_loom_group g
        INNER JOIN jute_prod_loom_group_dtl gd
                ON gd.loom_group_id = g.loom_group_id AND gd.active = 1
        INNER JOIN machine_mst m ON m.machine_id = gd.machine_id AND m.active = 1
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE g.co_id = :co_id
          AND g.active = 1
          AND d.branch_id = :branch_id
        ORDER BY g.group_code, gd.machine_id
        """
    )

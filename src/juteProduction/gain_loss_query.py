"""SQL builders for the Jute Gain/Loss Sheet (/api/juteGainLoss).

Arg-less functions returning sqlalchemy.text() with named binds, following the
winding_stock_query conventions. Range queries join the header so one round
trip fetches a whole financial year of a detail table (needed for the To-Date
column, which the Excel used to hardcode month-by-month).
"""

from sqlalchemy import text


# =============================================================================
# Masters
# =============================================================================


def get_gl_rows_query():
    """Active figure rows of the statement for a company."""
    return text(
        """
        SELECT gl_row_id, row_code, row_name, section, calc_sign,
               in_fng, in_wvg, in_wdg, display_order
        FROM jute_gl_row_mst
        WHERE co_id = :co_id AND active = 1
        ORDER BY section, display_order, gl_row_id
        """
    )


def get_gl_depts_query():
    """Active process-stock departments for a company."""
    return text(
        """
        SELECT gl_dept_id, dept_code, dept_name, in_wvg, in_wdg, display_order
        FROM jute_gl_dept_mst
        WHERE co_id = :co_id AND active = 1
        ORDER BY display_order, gl_dept_id
        """
    )


def get_quality_suggestions_query():
    """Finishing-quality item names by type, offered as quality suggestions.

    DISTINCT because one item can carry several finishing rows (per process
    params) — mirrors get_weaving_quality_type_map_query's caution.
    """
    return text(
        """
        SELECT DISTINCT fq.quality_type, im.item_name AS quality_name
        FROM jute_prod_finishing_quality fq
        JOIN item_mst im ON im.item_id = fq.item_id
        WHERE fq.co_id = :co_id AND fq.active = 1
        ORDER BY fq.quality_type, im.item_name
        """
    )


def get_used_quality_names_query():
    """Quality names already used on earlier sheets of this unit, so a name
    typed free-hand once keeps auto-completing afterwards."""
    return text(
        """
        SELECT DISTINCT p.statement, p.quality_type, p.quality_name
        FROM jute_gain_loss_prod_dtl p
        JOIN jute_gain_loss_hdr h ON h.gain_loss_id = p.gain_loss_id
        WHERE h.co_id = :co_id AND h.branch_id = :branch_id
          AND h.active = 1 AND p.active = 1
        ORDER BY p.statement, p.quality_type, p.quality_name
        """
    )


# =============================================================================
# Header
# =============================================================================


def get_hdr_query():
    return text(
        """
        SELECT gain_loss_id, co_id, branch_id, month_date, remarks,
               is_locked, locked_by, locked_date_time
        FROM jute_gain_loss_hdr
        WHERE co_id = :co_id AND branch_id = :branch_id
          AND month_date = :month_date AND active = 1
        LIMIT 1
        """
    )


def get_hdrs_in_range_query():
    """Headers of every saved month in [:date_from, :date_to] — drives the
    To-Date sum and the missing-months note."""
    return text(
        """
        SELECT gain_loss_id, month_date, remarks, is_locked
        FROM jute_gain_loss_hdr
        WHERE co_id = :co_id AND branch_id = :branch_id
          AND month_date BETWEEN :date_from AND :date_to AND active = 1
        ORDER BY month_date
        """
    )


def insert_hdr_query():
    return text(
        """
        INSERT INTO jute_gain_loss_hdr
            (co_id, branch_id, month_date, remarks, updated_by)
        VALUES (:co_id, :branch_id, :month_date, :remarks, :updated_by)
        """
    )


def update_hdr_query():
    return text(
        """
        UPDATE jute_gain_loss_hdr
        SET remarks = :remarks, updated_by = :updated_by
        WHERE gain_loss_id = :gain_loss_id AND co_id = :co_id AND active = 1
        """
    )


def set_hdr_lock_query():
    """:is_locked 1 locks, 0 unlocks; locked_by/locked_date_time follow."""
    return text(
        """
        UPDATE jute_gain_loss_hdr
        SET is_locked = :is_locked,
            locked_by = CASE WHEN :is_locked = 1 THEN :user_id ELSE locked_by END,
            locked_date_time = CASE WHEN :is_locked = 1 THEN CURRENT_TIMESTAMP ELSE locked_date_time END,
            updated_by = :user_id
        WHERE gain_loss_id = :gain_loss_id AND co_id = :co_id AND active = 1
        """
    )


# =============================================================================
# Details of one sheet
# =============================================================================


def get_dtl_query():
    return text(
        """
        SELECT d.gain_loss_dtl_id, d.gl_row_id, d.hess_qty, d.sack_qty
        FROM jute_gain_loss_dtl d
        WHERE d.gain_loss_id = :gain_loss_id AND d.active = 1
        """
    )


def get_prod_query():
    return text(
        """
        SELECT p.gain_loss_prod_id, p.statement, p.quality_type,
               p.quality_name, p.qty, p.display_order
        FROM jute_gain_loss_prod_dtl p
        WHERE p.gain_loss_id = :gain_loss_id AND p.active = 1
        ORDER BY p.statement, p.quality_type, p.display_order, p.gain_loss_prod_id
        """
    )


def get_ps_query():
    return text(
        """
        SELECT s.gain_loss_ps_id, s.gl_dept_id,
               s.opening_hess, s.opening_sack, s.closing_hess, s.closing_sack
        FROM jute_gain_loss_process_stock s
        WHERE s.gain_loss_id = :gain_loss_id AND s.active = 1
        """
    )


# =============================================================================
# Details across a month range (To-Date / prev-month reads, one trip each)
# =============================================================================


def get_dtl_in_range_query():
    return text(
        """
        SELECT h.month_date, d.gl_row_id, d.hess_qty, d.sack_qty
        FROM jute_gain_loss_dtl d
        JOIN jute_gain_loss_hdr h ON h.gain_loss_id = d.gain_loss_id
        WHERE h.co_id = :co_id AND h.branch_id = :branch_id
          AND h.month_date BETWEEN :date_from AND :date_to
          AND h.active = 1 AND d.active = 1
        """
    )


def get_prod_in_range_query():
    return text(
        """
        SELECT h.month_date, p.statement, p.quality_type, p.quality_name,
               p.qty, p.display_order
        FROM jute_gain_loss_prod_dtl p
        JOIN jute_gain_loss_hdr h ON h.gain_loss_id = p.gain_loss_id
        WHERE h.co_id = :co_id AND h.branch_id = :branch_id
          AND h.month_date BETWEEN :date_from AND :date_to
          AND h.active = 1 AND p.active = 1
        """
    )


def get_ps_in_range_query():
    return text(
        """
        SELECT h.month_date, s.gl_dept_id,
               s.opening_hess, s.opening_sack, s.closing_hess, s.closing_sack
        FROM jute_gain_loss_process_stock s
        JOIN jute_gain_loss_hdr h ON h.gain_loss_id = s.gain_loss_id
        WHERE h.co_id = :co_id AND h.branch_id = :branch_id
          AND h.month_date BETWEEN :date_from AND :date_to
          AND h.active = 1 AND s.active = 1
        """
    )


# =============================================================================
# Save (full-sheet replace: soft-delete actives, then insert fresh)
# =============================================================================


def soft_delete_dtl_query():
    return text(
        """
        UPDATE jute_gain_loss_dtl
        SET active = 0, updated_by = :updated_by
        WHERE gain_loss_id = :gain_loss_id AND active = 1
        """
    )


def soft_delete_prod_query():
    return text(
        """
        UPDATE jute_gain_loss_prod_dtl
        SET active = 0, updated_by = :updated_by
        WHERE gain_loss_id = :gain_loss_id AND active = 1
        """
    )


def soft_delete_ps_query():
    return text(
        """
        UPDATE jute_gain_loss_process_stock
        SET active = 0, updated_by = :updated_by
        WHERE gain_loss_id = :gain_loss_id AND active = 1
        """
    )


def insert_dtl_query():
    return text(
        """
        INSERT INTO jute_gain_loss_dtl
            (gain_loss_id, gl_row_id, hess_qty, sack_qty, updated_by)
        VALUES (:gain_loss_id, :gl_row_id, :hess_qty, :sack_qty, :updated_by)
        """
    )


def insert_prod_query():
    return text(
        """
        INSERT INTO jute_gain_loss_prod_dtl
            (gain_loss_id, statement, quality_type, quality_name, qty,
             display_order, updated_by)
        VALUES (:gain_loss_id, :statement, :quality_type, :quality_name, :qty,
                :display_order, :updated_by)
        """
    )


def insert_ps_query():
    return text(
        """
        INSERT INTO jute_gain_loss_process_stock
            (gain_loss_id, gl_dept_id, opening_hess, opening_sack,
             closing_hess, closing_sack, updated_by)
        VALUES (:gain_loss_id, :gl_dept_id, :opening_hess, :opening_sack,
                :closing_hess, :closing_sack, :updated_by)
        """
    )


# =============================================================================
# Validation helpers
# =============================================================================


def verify_gl_row_ids_query():
    """Count of the posted row ids that really are active rows of this company
    — compared against the distinct posted count in the handler."""
    return text(
        """
        SELECT COUNT(DISTINCT gl_row_id) AS n
        FROM jute_gl_row_mst
        WHERE co_id = :co_id AND active = 1
          AND FIND_IN_SET(gl_row_id, :ids)
        """
    )


def verify_gl_dept_ids_query():
    return text(
        """
        SELECT COUNT(DISTINCT gl_dept_id) AS n
        FROM jute_gl_dept_mst
        WHERE co_id = :co_id AND active = 1
          AND FIND_IN_SET(gl_dept_id, :ids)
        """
    )


def verify_branch_co_query():
    """The (branch, co) pair must exist — mirrors dpr_reports' guard."""
    return text(
        """
        SELECT branch_id FROM branch_mst
        WHERE branch_id = :branch_id AND co_id = :co_id AND active = 1
        LIMIT 1
        """
    )

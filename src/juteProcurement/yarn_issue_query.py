"""SQL for the Yarn Issue / Return document (jute_yarn_issue / jute_yarn_issue_li).

Design: docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-design.md §3.2
Schema: dbqueries/migrations/20260925_yarn_purchase_issue_tables.sql
Models: src/juteProcurement/yarn_models.py

Every function returns a sqlalchemy ``text()`` with named binds; the router
supplies the values. Kilograms are the only unit.

The godown balance per lot comes from ``vw_yarn_godown_outstanding``, which
already nets every non-draft / non-rejected / non-cancelled issue and return.
When an existing open document is being edited its own lines are already
inside that balance, so the lot queries take an ``:exclude_issue_id`` and add
that document's movement back (ISSUE lines +kg, RETURN lines -kg) — the user
then sees the balance as if their document were not yet saved.

The department floor book balance per lot has no view: it is the sum of that
department's ISSUE line kg minus its RETURN line kg over the same statuses.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.juteProcurement.constants import (
    YARN_DOC_STOCK_EXCLUDED_STATUSES,
    YARN_ISSUE_TYPE_ISSUE,
    YARN_PURCHASE_ISSUABLE_STATUSES,
)
from src.juteProcurement.formatters import get_financial_year_sql_expression

_EXCLUDED = ", ".join(str(s) for s in YARN_DOC_STOCK_EXCLUDED_STATUSES)
_ISSUABLE = ", ".join(str(s) for s in YARN_PURCHASE_ISSUABLE_STATUSES)

# This document's own active movement per lot (only rows the view already
# counts, i.e. status not in the excluded tuple). Binds: :exclude_issue_id.
# NULL matches nothing, so a /create call adds back nothing.
_OWN_MOVEMENT_SUBQUERY = f"""
            SELECT il.yarn_purchase_li_id,
                   SUM(CASE WHEN i.issue_type = '{YARN_ISSUE_TYPE_ISSUE}'
                            THEN il.weight_kg ELSE -il.weight_kg END) AS own_kg,
                   SUM(CASE WHEN i.issue_type = '{YARN_ISSUE_TYPE_ISSUE}'
                            THEN IFNULL(il.no_of_packages, 0)
                            ELSE -IFNULL(il.no_of_packages, 0) END)   AS own_pkgs
            FROM jute_yarn_issue_li il
            INNER JOIN jute_yarn_issue i ON i.yarn_issue_id = il.yarn_issue_id
            WHERE i.yarn_issue_id = :exclude_issue_id
              AND i.active = 1
              AND il.active = 1
              AND i.status_id NOT IN ({_EXCLUDED})
            GROUP BY il.yarn_purchase_li_id
"""


# =============================================================================
# SETUP
# =============================================================================


def get_yarn_issue_departments_query():
    """Departments of the branch for the header picker. dept_mst has NO
    active column (see juteMechanical/masters_query.py), so every row of the
    branch is offered. Binds: :branch_id."""
    return text(
        """
        SELECT d.dept_id,
               d.dept_desc,
               d.dept_code
        FROM dept_mst d
        WHERE d.branch_id = :branch_id
        ORDER BY d.order_id, d.dept_desc
        """
    )


def verify_department_query():
    """One-row check that dept_id is a department of the branch.
    Binds: :dept_id, :branch_id."""
    return text(
        """
        SELECT d.dept_id
        FROM dept_mst d
        WHERE d.dept_id = :dept_id
          AND d.branch_id = :branch_id
        LIMIT 1
        """
    )


# =============================================================================
# LOT PICKERS
# =============================================================================


def get_godown_lots_query():
    """Lots a new ISSUE may draw from: approved purchases with a positive
    godown balance, oldest first (FIFO).
    Binds: :co_id, :branch_id, :item_id (NULL = all), :exclude_issue_id (NULL
    on create)."""
    return text(
        f"""
        SELECT v.yarn_purchase_li_id,
               v.yarn_purchase_id,
               v.purchase_no,
               v.tran_date,
               pm.supp_name,
               v.item_id,
               im.item_name,
               im.item_code,
               v.package_type,
               v.received_kg,
               ROUND(v.bal_kg + IFNULL(own.own_kg, 0), 3)     AS bal_kg,
               ROUND(v.bal_pkgs + IFNULL(own.own_pkgs, 0), 3) AS bal_pkgs
        FROM vw_yarn_godown_outstanding v
        LEFT JOIN item_mst im ON im.item_id = v.item_id
        LEFT JOIN party_mst pm ON pm.party_id = v.party_id
        LEFT JOIN ({_OWN_MOVEMENT_SUBQUERY}
        ) own ON own.yarn_purchase_li_id = v.yarn_purchase_li_id
        WHERE v.co_id = :co_id
          AND v.branch_id = :branch_id
          AND v.purchase_status_id IN ({_ISSUABLE})
          AND (:item_id IS NULL OR v.item_id = :item_id)
          AND (v.bal_kg + IFNULL(own.own_kg, 0)) > 0
        ORDER BY v.tran_date ASC, v.yarn_purchase_li_id ASC
        """
    )


def get_yarn_lot_query():
    """One lot by id, unfiltered by balance or purchase status — the
    save-time guard reads co/branch/item, purchase_status_id and the godown
    balance from it. Binds: :yarn_purchase_li_id, :exclude_issue_id."""
    return text(
        f"""
        SELECT v.yarn_purchase_li_id,
               v.yarn_purchase_id,
               v.purchase_no,
               v.tran_date,
               v.co_id,
               v.branch_id,
               v.purchase_status_id,
               v.item_id,
               im.item_name,
               im.item_code,
               ROUND(v.bal_kg + IFNULL(own.own_kg, 0), 3) AS bal_kg
        FROM vw_yarn_godown_outstanding v
        LEFT JOIN item_mst im ON im.item_id = v.item_id
        LEFT JOIN ({_OWN_MOVEMENT_SUBQUERY}
        ) own ON own.yarn_purchase_li_id = v.yarn_purchase_li_id
        WHERE v.yarn_purchase_li_id = :yarn_purchase_li_id
        """
    )


def get_dept_lots_query():
    """Lots a RETURN may give back: per (lot, department) floor book balance
    = ISSUE kg - RETURN kg of that department over the counted statuses,
    positive only, oldest first. Binds: :co_id, :branch_id, :dept_id,
    :item_id (NULL = all), :exclude_issue_id (NULL on create)."""
    return text(
        f"""
        SELECT v.yarn_purchase_li_id,
               v.yarn_purchase_id,
               v.purchase_no,
               v.tran_date,
               pm.supp_name,
               v.item_id,
               im.item_name,
               im.item_code,
               v.package_type,
               v.received_kg,
               v.bal_kg,
               v.bal_pkgs,
               ROUND(fb.floor_kg, 3)   AS floor_bal_kg,
               ROUND(fb.floor_pkgs, 3) AS floor_bal_pkgs
        FROM (
            SELECT il.yarn_purchase_li_id,
                   SUM(CASE WHEN i.issue_type = '{YARN_ISSUE_TYPE_ISSUE}'
                            THEN il.weight_kg ELSE -il.weight_kg END) AS floor_kg,
                   SUM(CASE WHEN i.issue_type = '{YARN_ISSUE_TYPE_ISSUE}'
                            THEN IFNULL(il.no_of_packages, 0)
                            ELSE -IFNULL(il.no_of_packages, 0) END)   AS floor_pkgs
            FROM jute_yarn_issue_li il
            INNER JOIN jute_yarn_issue i ON i.yarn_issue_id = il.yarn_issue_id
            WHERE i.co_id = :co_id
              AND i.branch_id = :branch_id
              AND i.dept_id = :dept_id
              AND i.active = 1
              AND il.active = 1
              AND i.status_id NOT IN ({_EXCLUDED})
              AND (:exclude_issue_id IS NULL OR i.yarn_issue_id <> :exclude_issue_id)
            GROUP BY il.yarn_purchase_li_id
        ) fb
        INNER JOIN vw_yarn_godown_outstanding v ON v.yarn_purchase_li_id = fb.yarn_purchase_li_id
        LEFT JOIN item_mst im ON im.item_id = v.item_id
        LEFT JOIN party_mst pm ON pm.party_id = v.party_id
        WHERE fb.floor_kg > 0
          AND (:item_id IS NULL OR v.item_id = :item_id)
        ORDER BY v.tran_date ASC, v.yarn_purchase_li_id ASC
        """
    )


# =============================================================================
# LIST / GET
# =============================================================================


def get_yarn_issue_list_query():
    """Header list with department, status and line totals.
    Binds: :co_id, :branch_id, :from_date, :to_date, :status_id, :issue_type
    (all but the first two may be NULL)."""
    return text(
        """
        SELECT h.yarn_issue_id,
               h.issue_no,
               h.tran_date,
               h.issue_type,
               h.dept_id,
               d.dept_desc,
               h.status_id,
               sm.status_name,
               h.approval_level,
               COALESCE(li.line_count, 0) AS line_count,
               COALESCE(li.total_kg, 0) AS total_kg,
               h.updated_date_time
        FROM jute_yarn_issue h
        LEFT JOIN dept_mst d ON d.dept_id = h.dept_id
        LEFT JOIN status_mst sm ON sm.status_id = h.status_id
        LEFT JOIN (
            SELECT yarn_issue_id,
                   COUNT(*) AS line_count,
                   SUM(weight_kg) AS total_kg
            FROM jute_yarn_issue_li
            WHERE active = 1
            GROUP BY yarn_issue_id
        ) li ON li.yarn_issue_id = h.yarn_issue_id
        WHERE h.active = 1
          AND h.co_id = :co_id
          AND h.branch_id = :branch_id
          AND (:from_date IS NULL OR h.tran_date >= :from_date)
          AND (:to_date IS NULL OR h.tran_date <= :to_date)
          AND (:status_id IS NULL OR h.status_id = :status_id)
          AND (:issue_type IS NULL OR h.issue_type = :issue_type)
        ORDER BY h.tran_date DESC, h.yarn_issue_id DESC
        """
    )


def get_yarn_issue_header_query():
    """Full header with department and status names. Binds: :yarn_issue_id."""
    return text(
        """
        SELECT h.yarn_issue_id,
               h.co_id,
               h.branch_id,
               h.tran_date,
               h.issue_no,
               h.issue_type,
               h.dept_id,
               d.dept_desc,
               h.status_id,
               sm.status_name,
               h.approval_level,
               h.remarks,
               h.active,
               h.updated_by,
               h.updated_date_time
        FROM jute_yarn_issue h
        LEFT JOIN dept_mst d ON d.dept_id = h.dept_id
        LEFT JOIN status_mst sm ON sm.status_id = h.status_id
        WHERE h.yarn_issue_id = :yarn_issue_id
          AND h.active = 1
        """
    )


def get_yarn_issue_lines_query():
    """Active lines with item names, the lot's purchase number / date and the
    lot's CURRENT godown balance (NULL when the purchase has since left the
    view). Binds: :yarn_issue_id."""
    return text(
        """
        SELECT li.yarn_issue_li_id,
               li.yarn_issue_id,
               li.item_id,
               im.item_name,
               im.item_code,
               li.yarn_purchase_li_id,
               p.purchase_no,
               p.tran_date AS lot_tran_date,
               v.bal_kg AS lot_bal_kg,
               li.no_of_packages,
               li.weight_kg,
               li.remarks,
               li.active,
               li.updated_by,
               li.updated_date_time
        FROM jute_yarn_issue_li li
        LEFT JOIN item_mst im ON im.item_id = li.item_id
        LEFT JOIN jute_yarn_purchase_li pl ON pl.yarn_purchase_li_id = li.yarn_purchase_li_id
        LEFT JOIN jute_yarn_purchase p ON p.yarn_purchase_id = pl.yarn_purchase_id
        LEFT JOIN vw_yarn_godown_outstanding v ON v.yarn_purchase_li_id = li.yarn_purchase_li_id
        WHERE li.yarn_issue_id = :yarn_issue_id
          AND li.active = 1
        ORDER BY li.yarn_issue_li_id
        """
    )


def get_yarn_issue_line_totals_query():
    """Count and smallest weight of the active lines — the send-for-approval
    guard. Binds: :yarn_issue_id."""
    return text(
        """
        SELECT COUNT(*) AS line_count,
               MIN(weight_kg) AS min_kg
        FROM jute_yarn_issue_li
        WHERE yarn_issue_id = :yarn_issue_id
          AND active = 1
        """
    )


def get_yarn_issue_active_line_ids_query():
    """Ids of the active lines, for the replace-lines diff on update.
    Binds: :yarn_issue_id."""
    return text(
        """
        SELECT yarn_issue_li_id
        FROM jute_yarn_issue_li
        WHERE yarn_issue_id = :yarn_issue_id
          AND active = 1
        """
    )


# =============================================================================
# APPROVAL (shape required by src/common/approval_utils.py)
# =============================================================================


def get_yarn_issue_with_approval_info():
    """status_id / approval_level / branch_id (+ what /open needs).
    Binds: :yarn_issue_id."""
    return text(
        """
        SELECT h.yarn_issue_id,
               h.co_id,
               h.branch_id,
               h.status_id,
               h.approval_level,
               h.tran_date,
               h.issue_no,
               h.issue_type
        FROM jute_yarn_issue h
        WHERE h.yarn_issue_id = :yarn_issue_id
          AND h.active = 1
        """
    )


def update_yarn_issue_status():
    """Status transition. issue_no is written only when a non-NULL value is
    bound (the /open path); every other transition binds NULL and keeps it.
    Binds: :yarn_issue_id, :status_id, :approval_level, :updated_by,
    :updated_date_time, :issue_no."""
    return text(
        """
        UPDATE jute_yarn_issue
        SET status_id = :status_id,
            approval_level = :approval_level,
            updated_by = :updated_by,
            updated_date_time = :updated_date_time,
            issue_no = CASE
                WHEN :issue_no IS NOT NULL THEN :issue_no
                ELSE issue_no
            END
        WHERE yarn_issue_id = :yarn_issue_id
        """
    )


def get_next_yarn_issue_no_for_fy(db: Session, branch_id: int, tran_date: date, issue_type: str) -> int:
    """Next issue_no for the branch in the financial year (April–March) of
    tran_date — one series per issue_type (ISSUE and RETURN are numbered
    independently). Same shape as get_next_yarn_purchase_no_for_fy."""
    fy_expr = get_financial_year_sql_expression("tran_date")
    fy_start_year = tran_date.year if tran_date.month >= 4 else tran_date.year - 1
    fy_string = f"{fy_start_year % 100:02d}-{(fy_start_year + 1) % 100:02d}"

    query = text(
        f"""
        SELECT COALESCE(MAX(issue_no), 0) + 1 AS next_no
        FROM jute_yarn_issue
        WHERE branch_id = :branch_id
          AND issue_type = :issue_type
          AND issue_no IS NOT NULL
          AND {fy_expr} = :fy_string
        """
    )
    result = db.execute(
        query,
        {"branch_id": int(branch_id), "issue_type": str(issue_type), "fy_string": fy_string},
    ).fetchone()
    return int(result[0]) if result and result[0] is not None else 1


# =============================================================================
# WRITES
# =============================================================================


def insert_yarn_issue_header_query():
    """New header in Draft (21). Binds: :co_id, :branch_id, :tran_date,
    :issue_type, :dept_id, :remarks, :status_id, :updated_by,
    :updated_date_time."""
    return text(
        """
        INSERT INTO jute_yarn_issue (
            co_id, branch_id, tran_date, issue_no, issue_type, dept_id,
            status_id, approval_level, remarks, active,
            updated_by, updated_date_time
        ) VALUES (
            :co_id, :branch_id, :tran_date, NULL, :issue_type, :dept_id,
            :status_id, NULL, :remarks, 1,
            :updated_by, :updated_date_time
        )
        """
    )


def update_yarn_issue_header_query():
    """Editable header fields (identity columns co_id / branch_id and the
    status columns are never touched here). Binds: :yarn_issue_id,
    :tran_date, :issue_type, :dept_id, :remarks, :updated_by,
    :updated_date_time."""
    return text(
        """
        UPDATE jute_yarn_issue
        SET tran_date = :tran_date,
            issue_type = :issue_type,
            dept_id = :dept_id,
            remarks = :remarks,
            updated_by = :updated_by,
            updated_date_time = :updated_date_time
        WHERE yarn_issue_id = :yarn_issue_id
        """
    )


def insert_yarn_issue_line_query():
    """Binds: :yarn_issue_id, :item_id, :yarn_purchase_li_id,
    :no_of_packages, :weight_kg, :remarks, :updated_by, :updated_date_time."""
    return text(
        """
        INSERT INTO jute_yarn_issue_li (
            yarn_issue_id, item_id, yarn_purchase_li_id,
            no_of_packages, weight_kg, remarks,
            active, updated_by, updated_date_time
        ) VALUES (
            :yarn_issue_id, :item_id, :yarn_purchase_li_id,
            :no_of_packages, :weight_kg, :remarks,
            1, :updated_by, :updated_date_time
        )
        """
    )


def update_yarn_issue_line_query():
    """Binds: :yarn_issue_li_id, :yarn_issue_id, :item_id,
    :yarn_purchase_li_id, :no_of_packages, :weight_kg, :remarks,
    :updated_by, :updated_date_time."""
    return text(
        """
        UPDATE jute_yarn_issue_li
        SET item_id = :item_id,
            yarn_purchase_li_id = :yarn_purchase_li_id,
            no_of_packages = :no_of_packages,
            weight_kg = :weight_kg,
            remarks = :remarks,
            updated_by = :updated_by,
            updated_date_time = :updated_date_time
        WHERE yarn_issue_li_id = :yarn_issue_li_id
          AND yarn_issue_id = :yarn_issue_id
        """
    )


def soft_delete_yarn_issue_line_query():
    """Line removed on update: active = 0, row kept for audit.
    Binds: :yarn_issue_li_id, :yarn_issue_id, :updated_by,
    :updated_date_time."""
    return text(
        """
        UPDATE jute_yarn_issue_li
        SET active = 0,
            updated_by = :updated_by,
            updated_date_time = :updated_date_time
        WHERE yarn_issue_li_id = :yarn_issue_li_id
          AND yarn_issue_id = :yarn_issue_id
        """
    )

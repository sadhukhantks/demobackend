"""SQL for the Yarn Purchase document (jute_yarn_purchase / jute_yarn_purchase_li).

Design: docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-design.md §3.1
Schema: dbqueries/migrations/20260925_yarn_purchase_issue_tables.sql
Models: src/juteProcurement/yarn_models.py

Every function returns a sqlalchemy ``text()`` with named binds; the router
supplies the values. Kilograms are the only unit — nothing here reads or
writes a UOM.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.juteProcurement.formatters import get_financial_year_sql_expression
from src.juteProduction.constants import PLANNING_YARN_ITEM_TYPE_ID


# =============================================================================
# SETUP
# =============================================================================


def get_yarn_purchase_menu_id_query():
    """menu_mst.menu_id for a page path. Binds: :menu_path."""
    return text(
        """
        SELECT menu_id
        FROM menu_mst
        WHERE menu_path = :menu_path
          AND active = 1
        ORDER BY menu_id
        LIMIT 1
        """
    )


def get_yarn_purchase_parties_query():
    """Active parties of the company tagged with the 'Yarn Supplier' party type.

    party_mst.party_type_id is a set-like string ('{1}', '2,3', '{2,3,1}'), so
    membership is FIND_IN_SET after stripping the braces, as in procurement and
    sales. The type id is resolved by name (it is seeded per tenant by
    dbqueries/migrations/20260925_yarn_supplier_party_type.sql), never hardcoded.
    Binds: :co_id, :party_type_name.
    """
    return text(
        """
        SELECT pm.party_id,
               pm.supp_name
        FROM party_mst pm
        WHERE pm.co_id = :co_id
          AND pm.active = 1
          AND FIND_IN_SET(
                (SELECT CAST(ptm.party_types_mst_id AS CHAR)
                   FROM party_type_mst ptm
                  WHERE ptm.party_types_mst_name = :party_type_name
                  LIMIT 1),
                REPLACE(REPLACE(pm.party_type_id, '{', ''), '}', '')
              ) > 0
        ORDER BY pm.supp_name
        """
    )


def verify_yarn_item_query():
    """One-row check that item_id is an active yarn item of this company
    (item_grp_mst.item_type_id = 4, company scope on item_grp_mst.co_id).
    Binds: :item_id, :co_id."""
    return text(
        f"""
        SELECT im.item_id
        FROM item_mst im
        INNER JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE im.item_id = :item_id
          AND im.active = 1
          AND ig.co_id = :co_id
          AND ig.item_type_id = {PLANNING_YARN_ITEM_TYPE_ID}
        LIMIT 1
        """
    )


# =============================================================================
# LIST / GET
# =============================================================================


def get_yarn_purchase_list_query():
    """Header list with supplier, status and line totals.
    Binds: :co_id, :branch_id, :from_date, :to_date, :status_id (all but the
    first two may be NULL)."""
    return text(
        """
        SELECT h.yarn_purchase_id,
               h.purchase_no,
               h.tran_date,
               h.party_id,
               pm.supp_name,
               h.challan_no,
               h.vehicle_no,
               h.status_id,
               sm.status_name,
               h.approval_level,
               COALESCE(li.line_count, 0) AS line_count,
               COALESCE(li.total_kg, 0) AS total_kg,
               h.updated_date_time
        FROM jute_yarn_purchase h
        LEFT JOIN party_mst pm ON pm.party_id = h.party_id
        LEFT JOIN status_mst sm ON sm.status_id = h.status_id
        LEFT JOIN (
            SELECT yarn_purchase_id,
                   COUNT(*) AS line_count,
                   SUM(weight_kg) AS total_kg
            FROM jute_yarn_purchase_li
            WHERE active = 1
            GROUP BY yarn_purchase_id
        ) li ON li.yarn_purchase_id = h.yarn_purchase_id
        WHERE h.active = 1
          AND h.co_id = :co_id
          AND h.branch_id = :branch_id
          AND (:from_date IS NULL OR h.tran_date >= :from_date)
          AND (:to_date IS NULL OR h.tran_date <= :to_date)
          AND (:status_id IS NULL OR h.status_id = :status_id)
        ORDER BY h.tran_date DESC, h.yarn_purchase_id DESC
        """
    )


def get_yarn_purchase_header_query():
    """Full header with supplier and status names. Binds: :yarn_purchase_id."""
    return text(
        """
        SELECT h.yarn_purchase_id,
               h.co_id,
               h.branch_id,
               h.tran_date,
               h.purchase_no,
               h.party_id,
               pm.supp_name,
               h.challan_no,
               h.challan_date,
               h.vehicle_no,
               h.inward_id,
               h.status_id,
               sm.status_name,
               h.approval_level,
               h.remarks,
               h.active,
               h.updated_by,
               h.updated_date_time
        FROM jute_yarn_purchase h
        LEFT JOIN party_mst pm ON pm.party_id = h.party_id
        LEFT JOIN status_mst sm ON sm.status_id = h.status_id
        WHERE h.yarn_purchase_id = :yarn_purchase_id
          AND h.active = 1
        """
    )


def get_yarn_purchase_lines_query():
    """Active lines with item and quality names. Binds: :yarn_purchase_id."""
    return text(
        """
        SELECT li.yarn_purchase_li_id,
               li.yarn_purchase_id,
               li.item_id,
               im.item_name,
               im.item_code,
               ig.item_grp_name AS quality_name,
               li.package_type,
               li.no_of_packages,
               li.wt_per_package,
               li.weight_kg,
               li.rate_per_kg,
               li.amount,
               li.remarks,
               li.active,
               li.updated_by,
               li.updated_date_time
        FROM jute_yarn_purchase_li li
        LEFT JOIN item_mst im ON im.item_id = li.item_id
        LEFT JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE li.yarn_purchase_id = :yarn_purchase_id
          AND li.active = 1
        ORDER BY li.yarn_purchase_li_id
        """
    )


def get_yarn_purchase_line_totals_query():
    """Count and smallest weight of the active lines — the send-for-approval
    guard. Binds: :yarn_purchase_id."""
    return text(
        """
        SELECT COUNT(*) AS line_count,
               MIN(weight_kg) AS min_kg
        FROM jute_yarn_purchase_li
        WHERE yarn_purchase_id = :yarn_purchase_id
          AND active = 1
        """
    )


def get_yarn_purchase_active_line_ids_query():
    """Ids of the active lines, for the replace-lines diff on update.
    Binds: :yarn_purchase_id."""
    return text(
        """
        SELECT yarn_purchase_li_id
        FROM jute_yarn_purchase_li
        WHERE yarn_purchase_id = :yarn_purchase_id
          AND active = 1
        """
    )


# =============================================================================
# APPROVAL (shape required by src/common/approval_utils.py)
# =============================================================================


def get_yarn_purchase_with_approval_info():
    """status_id / approval_level / branch_id (+ what /open needs).
    Binds: :yarn_purchase_id."""
    return text(
        """
        SELECT h.yarn_purchase_id,
               h.co_id,
               h.branch_id,
               h.status_id,
               h.approval_level,
               h.tran_date,
               h.purchase_no
        FROM jute_yarn_purchase h
        WHERE h.yarn_purchase_id = :yarn_purchase_id
          AND h.active = 1
        """
    )


def update_yarn_purchase_status():
    """Status transition. purchase_no is written only when a non-NULL value is
    bound (the /open path); every other transition binds NULL and keeps it.
    Binds: :yarn_purchase_id, :status_id, :approval_level, :updated_by,
    :updated_date_time, :purchase_no."""
    return text(
        """
        UPDATE jute_yarn_purchase
        SET status_id = :status_id,
            approval_level = :approval_level,
            updated_by = :updated_by,
            updated_date_time = :updated_date_time,
            purchase_no = CASE
                WHEN :purchase_no IS NOT NULL THEN :purchase_no
                ELSE purchase_no
            END
        WHERE yarn_purchase_id = :yarn_purchase_id
        """
    )


def get_next_yarn_purchase_no_for_fy(db: Session, branch_id: int, tran_date: date) -> int:
    """Next purchase_no for the branch in the financial year (April–March) of
    tran_date. Same shape as mr.get_next_bill_pass_no_for_fy."""
    fy_expr = get_financial_year_sql_expression("tran_date")
    fy_start_year = tran_date.year if tran_date.month >= 4 else tran_date.year - 1
    fy_string = f"{fy_start_year % 100:02d}-{(fy_start_year + 1) % 100:02d}"

    query = text(
        f"""
        SELECT COALESCE(MAX(purchase_no), 0) + 1 AS next_no
        FROM jute_yarn_purchase
        WHERE branch_id = :branch_id
          AND purchase_no IS NOT NULL
          AND {fy_expr} = :fy_string
        """
    )
    result = db.execute(query, {"branch_id": int(branch_id), "fy_string": fy_string}).fetchone()
    return int(result[0]) if result and result[0] is not None else 1


# =============================================================================
# WRITES
# =============================================================================


def insert_yarn_purchase_header_query():
    """New header in Draft (21). Binds: :co_id, :branch_id, :tran_date,
    :party_id, :challan_no, :challan_date, :vehicle_no, :inward_id, :remarks,
    :status_id, :updated_by, :updated_date_time."""
    return text(
        """
        INSERT INTO jute_yarn_purchase (
            co_id, branch_id, tran_date, purchase_no, party_id,
            challan_no, challan_date, vehicle_no, inward_id,
            status_id, approval_level, remarks, active,
            updated_by, updated_date_time
        ) VALUES (
            :co_id, :branch_id, :tran_date, NULL, :party_id,
            :challan_no, :challan_date, :vehicle_no, :inward_id,
            :status_id, NULL, :remarks, 1,
            :updated_by, :updated_date_time
        )
        """
    )


def update_yarn_purchase_header_query():
    """Editable header fields (identity columns co_id / branch_id and the
    status columns are never touched here). Binds: :yarn_purchase_id,
    :tran_date, :party_id, :challan_no, :challan_date, :vehicle_no,
    :inward_id, :remarks, :updated_by, :updated_date_time."""
    return text(
        """
        UPDATE jute_yarn_purchase
        SET tran_date = :tran_date,
            party_id = :party_id,
            challan_no = :challan_no,
            challan_date = :challan_date,
            vehicle_no = :vehicle_no,
            inward_id = :inward_id,
            remarks = :remarks,
            updated_by = :updated_by,
            updated_date_time = :updated_date_time
        WHERE yarn_purchase_id = :yarn_purchase_id
        """
    )


def insert_yarn_purchase_line_query():
    """Binds: :yarn_purchase_id, :item_id, :package_type, :no_of_packages,
    :wt_per_package, :weight_kg, :rate_per_kg, :amount, :remarks,
    :updated_by, :updated_date_time."""
    return text(
        """
        INSERT INTO jute_yarn_purchase_li (
            yarn_purchase_id, item_id, package_type, no_of_packages,
            wt_per_package, weight_kg, rate_per_kg, amount, remarks,
            active, updated_by, updated_date_time
        ) VALUES (
            :yarn_purchase_id, :item_id, :package_type, :no_of_packages,
            :wt_per_package, :weight_kg, :rate_per_kg, :amount, :remarks,
            1, :updated_by, :updated_date_time
        )
        """
    )


def update_yarn_purchase_line_query():
    """Binds: :yarn_purchase_li_id, :yarn_purchase_id, :item_id,
    :package_type, :no_of_packages, :wt_per_package, :weight_kg,
    :rate_per_kg, :amount, :remarks, :updated_by, :updated_date_time."""
    return text(
        """
        UPDATE jute_yarn_purchase_li
        SET item_id = :item_id,
            package_type = :package_type,
            no_of_packages = :no_of_packages,
            wt_per_package = :wt_per_package,
            weight_kg = :weight_kg,
            rate_per_kg = :rate_per_kg,
            amount = :amount,
            remarks = :remarks,
            updated_by = :updated_by,
            updated_date_time = :updated_date_time
        WHERE yarn_purchase_li_id = :yarn_purchase_li_id
          AND yarn_purchase_id = :yarn_purchase_id
        """
    )


def soft_delete_yarn_purchase_line_query():
    """Line removed on update: active = 0, row kept for audit.
    Binds: :yarn_purchase_li_id, :yarn_purchase_id, :updated_by,
    :updated_date_time."""
    return text(
        """
        UPDATE jute_yarn_purchase_li
        SET active = 0,
            updated_by = :updated_by,
            updated_date_time = :updated_date_time
        WHERE yarn_purchase_li_id = :yarn_purchase_li_id
          AND yarn_purchase_id = :yarn_purchase_id
        """
    )

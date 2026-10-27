"""Raw SQL builders for the costing parameters master (jute_cost_param).

Effective-dated EAV with LAST-DATE resolution: for a (co_id, branch scope,
param_code, product) the row in force on a date is the active one with
MAX(effective_date) <= that date. The CRUD builders here mirror
spng_target_map_query.py (named binds, the ``:x IS NULL OR ...`` optional-filter
idiom, soft delete via active). Resolution itself is done in Python
(params.resolve_params) over the candidate rows get_cost_param_rows_for_resolution_query
returns, because scope precedence (branch beats all-branch even when older)
is a tuple comparison, not an ORDER BY.
"""

from typing import List

from sqlalchemy import text


def get_cost_param_list_query():
    """Parameter rows of a company with optional branch / code / product / active filters.

    branch_id NULL lists company-wide rows AND every branch's rows; a given
    branch_id lists that branch's rows plus the NULL-branch (all-branch) rows.
    active NULL lists both active and inactive rows. branch_mst is LEFT-joined
    for a display name. Binds: :co_id, :branch_id, :param_code, :product, :active.
    """
    return text(
        """
        SELECT
            p.cost_param_id,
            p.co_id,
            p.branch_id,
            b.branch_name,
            p.effective_date,
            p.param_code,
            p.param_value,
            p.uom,
            p.product,
            p.source,
            p.remarks,
            p.active
        FROM jute_cost_param p
        LEFT JOIN branch_mst b ON b.branch_id = p.branch_id
        WHERE p.co_id = :co_id
          AND (:branch_id IS NULL OR p.branch_id = :branch_id OR p.branch_id IS NULL)
          AND (:param_code IS NULL OR p.param_code = :param_code)
          AND (:product IS NULL OR p.product = :product)
          AND (:active IS NULL OR p.active = :active)
        ORDER BY p.param_code, p.product, p.effective_date DESC, p.cost_param_id DESC
        """
    )


def get_cost_param_row_query():
    """One parameter row by id within the company (active or not). Binds: :id, :co_id."""
    return text(
        """
        SELECT
            p.cost_param_id,
            p.co_id,
            p.branch_id,
            b.branch_name,
            p.effective_date,
            p.param_code,
            p.param_value,
            p.uom,
            p.product,
            p.source,
            p.remarks,
            p.active
        FROM jute_cost_param p
        LEFT JOIN branch_mst b ON b.branch_id = p.branch_id
        WHERE p.cost_param_id = :id
          AND p.co_id = :co_id
        """
    )


def get_cost_param_rows_for_resolution_query():
    """Candidate rows for resolution on or before :on_date — Python picks the winner.

    Active rows of the company effective on or before :on_date, either
    all-branch (branch_id NULL) or of :branch_id. With :branch_id NULL only the
    all-branch rows come back (company-wide view). Newest first so a plain scan
    sees the latest rows early, but params.resolve_params does the actual pick.
    Binds: :co_id, :branch_id (nullable), :on_date.
    """
    return text(
        """
        SELECT
            cost_param_id,
            co_id,
            branch_id,
            effective_date,
            param_code,
            param_value,
            uom,
            product,
            source,
            remarks,
            active
        FROM jute_cost_param
        WHERE co_id = :co_id
          AND active = 1
          AND effective_date <= :on_date
          AND (branch_id IS NULL OR branch_id = :branch_id)
        ORDER BY effective_date DESC, cost_param_id DESC
        """
    )


def check_cost_param_duplicate_query():
    """COUNT of active rows at the exact key (NULL-safe on branch_id / product).

    MySQL's UNIQUE key treats NULLs as distinct, so an all-branch or all-product
    duplicate would slip past uq_jute_cost_param — this is the guard the API
    runs before every insert. :id excludes the row being edited (NULL on create).
    Binds: :co_id, :branch_id, :effective_date, :param_code, :product, :id.
    """
    return text(
        """
        SELECT COUNT(*) AS cnt
        FROM jute_cost_param
        WHERE co_id = :co_id
          AND (branch_id <=> :branch_id)
          AND effective_date = :effective_date
          AND param_code = :param_code
          AND (product <=> :product)
          AND active = 1
          AND (:id IS NULL OR cost_param_id <> :id)
        """
    )


def insert_cost_param_query():
    """Insert a fresh active parameter row."""
    return text(
        """
        INSERT INTO jute_cost_param
            (co_id, branch_id, effective_date, param_code, param_value,
             uom, product, source, remarks, active, updated_by, updated_date_time)
        VALUES
            (:co_id, :branch_id, :effective_date, :param_code, :param_value,
             :uom, :product, :source, :remarks, 1, :updated_by, CURRENT_TIMESTAMP)
        """
    )


def update_cost_param_query(sets: List[str]):
    """UPDATE in place with a caller-built SET list (``"param_value = :param_value"`` ...).

    Dynamic SET like spng_target_map's target_map_edit; the caller passes only
    the fields present in the request. updated_by is always stamped.
    Binds: :updated_by, :id, :co_id plus one per SET entry.
    """
    return text(
        f"UPDATE jute_cost_param SET {', '.join(sets)}, updated_by = :updated_by "
        "WHERE cost_param_id = :id AND co_id = :co_id"
    )


def soft_delete_cost_param_query():
    """Deactivate one active row (rowcount 0 = not found or already inactive)."""
    return text(
        """
        UPDATE jute_cost_param
        SET active = 0,
            updated_by = :updated_by
        WHERE cost_param_id = :id
          AND co_id = :co_id
          AND active = 1
        """
    )

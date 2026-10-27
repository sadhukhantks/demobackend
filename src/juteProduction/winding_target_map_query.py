"""SQL for the Winding Standards / Targets Map (jute_prod_winding_target_map).

Clone of weaving_query.py's "PAGE B" section against the winding table, minus the
machine dimension: winding is QUALITY-ONLY, id_type is always 'qid' and ref_id is
the yarn ITEM (item_mst.item_id). The grid's ref list is NOT built here — it reuses
spinning_query.get_yarn_qualities_query() (the one yarn-item picker the whole
module already shares), so the ref label comes from item_mst.item_code/item_name.

Resolution rule everywhere below (and in whatever report reads these values): the
active row with MAX(effective_date) <= on_date, newest winding_target_map_id
breaking ties, with NO branch filter.
"""

from sqlalchemy import text


def get_winding_target_map_list_query():
    """Active winding target-map rows with optional id_type / ref_id / value_role /
    param filters. Newest effective_date first. The ref label comes from item_mst
    (ref_id = yarn item_id); the :id_type bind is kept for call-shape parity with
    weaving but in practice is always 'qid'."""
    return text(
        """
        SELECT
            tm.winding_target_map_id,
            tm.co_id,
            tm.branch_id,
            tm.effective_date,
            tm.ref_id,
            tm.id_type,
            tm.value_role,
            tm.param,
            tm.value,
            tm.active,
            im.item_code AS ref_code,
            im.item_name AS ref_name
        FROM jute_prod_winding_target_map tm
        LEFT JOIN item_mst im ON im.item_id = tm.ref_id
        WHERE tm.co_id = :co_id
          AND tm.active = 1
          AND (:branch_id IS NULL OR tm.branch_id = :branch_id OR tm.branch_id IS NULL)
          AND (:id_type IS NULL OR tm.id_type = :id_type)
          AND (:ref_id IS NULL OR tm.ref_id = :ref_id)
          AND (:value_role IS NULL OR tm.value_role = :value_role)
          AND (:param IS NULL OR tm.param = :param)
        ORDER BY tm.effective_date DESC, tm.winding_target_map_id DESC
        """
    )


def get_winding_target_map_row_query():
    """A single active winding target-map row by id (for edit / delete existence check)."""
    return text(
        """
        SELECT winding_target_map_id, co_id, branch_id, effective_date, ref_id,
               id_type, value_role, param, value, active
        FROM jute_prod_winding_target_map
        WHERE winding_target_map_id = :id
          AND active = 1
        """
    )


def insert_winding_target_map_query():
    """Insert a fresh active winding standards/targets row (id_type always 'qid')."""
    return text(
        """
        INSERT INTO jute_prod_winding_target_map
            (co_id, branch_id, effective_date, ref_id, id_type, value_role,
             param, value, active, updated_by)
        VALUES
            (:co_id, :branch_id, :effective_date, :ref_id, :id_type, :value_role,
             :param, :value, 1, :updated_by)
        """
    )


def resolve_winding_grid_cells_batch_query():
    """LAST-DATE resolution for the WHOLE grid in ONE statement (no refs x params N+1).

    One ranked pass over jute_prod_winding_target_map for (co_id, id_type,
    value_role): rn=1 per (ref_id, param) ordered by effective_date DESC,
    winding_target_map_id DESC, with NO branch filter — the exact rule production
    reads by. The router filters params to grid_params_for() and omits NULL values.
    """
    return text(
        """
        SELECT t.ref_id, t.param, t.value, t.effective_date
        FROM (
            SELECT tm.ref_id, tm.param, tm.value, tm.effective_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY tm.ref_id, tm.param
                       ORDER BY tm.effective_date DESC, tm.winding_target_map_id DESC
                   ) AS rn
            FROM jute_prod_winding_target_map tm
            WHERE tm.co_id = :co_id
              AND tm.id_type = :id_type
              AND tm.value_role = :value_role
              AND tm.active = 1
              AND tm.effective_date <= :on_date
        ) t
        WHERE t.rn = 1
        """
    )


def find_exact_winding_grid_row_query():
    """Active row at the EXACT save key, BRANCH-AGNOSTIC (mirrors grid resolution).

    Used by target_map_bulk_save to decide insert-vs-update-vs-clear. Applies NO
    branch filter so save targets the SAME row the grid prefilled; the newest-id
    tiebreak matches the grid's is_exact ORDER BY.
    """
    return text(
        """
        SELECT winding_target_map_id, value
        FROM jute_prod_winding_target_map
        WHERE co_id = :co_id
          AND ref_id = :ref_id
          AND id_type = :id_type
          AND value_role = :value_role
          AND param = :param
          AND effective_date = :effective_date
          AND active = 1
        ORDER BY winding_target_map_id DESC
        LIMIT 1
        """
    )


def update_winding_grid_value_query():
    """Set value (+ audit cols) on one active grid row by id."""
    return text(
        """
        UPDATE jute_prod_winding_target_map
        SET value = :value,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE winding_target_map_id = :id
        """
    )


def clear_winding_grid_value_query():
    """Soft-delete (active=0) one grid row by id, clearing the cell."""
    return text(
        """
        UPDATE jute_prod_winding_target_map
        SET active = 0,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE winding_target_map_id = :id
        """
    )

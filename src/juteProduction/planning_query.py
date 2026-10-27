"""Raw SQL builders for Jute Production Planning (SPEC §2 / §3).

Mirrors the module conventions from spng_target_map_query.py / weaving_query.py:
named binds only, the ``:x IS NULL OR ...`` optional-filter idiom, soft-delete via
``active``, and de-duplication left to the caller.

Two families live here:

  * the PLAN document — header listing with totals, header/line reads, and the
    write statements used by planning_save (wholesale child replace in one
    transaction);
  * the SETUP / NORMS reads — cloth qualities (item_type_id=5), yarn items
    (item_type_id=4 LEFT JOIN jute_yarn_mst for the count), machine counts per
    machine-type name (case-insensitive), and the LAST-DATE norm resolution
    (MAX(effective_date) <= :on_date among active rows), which is identical in
    spirit to every *_target_map resolution in this module.
"""

from sqlalchemy import text


# =============================================================================
# Plan document — reads
# =============================================================================


def get_planning_table_query():
    """Plan headers for the list page, newest first, with the two headline totals.

    total_cloth_kg = SUM(prod_a + prod_b + prod_c) over the plan's active loom lines
    (the grand total cloth kg/day); raw_jute_kg = the BATCHING stage's input_kg_day
    (the headline raw-jute requirement per day, SPEC §1). Both come from derived
    tables so a plan with no lines still lists, with 0 totals.

    Optional filters: branch_id, from_date, to_date (all NULL-tolerant).
    """
    return text(
        """
        SELECT
            p.planning_id,
            p.plan_no,
            p.plan_date,
            p.plan_name,
            p.remarks,
            p.branch_id,
            b.branch_name,
            p.status_id,
            p.updated_by,
            u.name AS updated_by_name,
            p.updated_date_time,
            COALESCE(l.total_cloth_kg, 0) AS total_cloth_kg,
            COALESCE(d.raw_jute_kg, 0) AS raw_jute_kg
        FROM jute_prod_planning p
        LEFT JOIN branch_mst b ON b.branch_id = p.branch_id
        LEFT JOIN user_mst u ON u.user_id = p.updated_by
        LEFT JOIN (
            SELECT planning_id,
                   SUM(COALESCE(prod_a, 0) + COALESCE(prod_b, 0) + COALESCE(prod_c, 0))
                       AS total_cloth_kg
            FROM jute_prod_planning_loom
            WHERE active = 1
            GROUP BY planning_id
        ) l ON l.planning_id = p.planning_id
        LEFT JOIN (
            SELECT planning_id, SUM(COALESCE(input_kg_day, 0)) AS raw_jute_kg
            FROM jute_prod_planning_dept
            WHERE active = 1 AND stage_code = 'BATCHING'
            GROUP BY planning_id
        ) d ON d.planning_id = p.planning_id
        WHERE p.co_id = :co_id
          AND p.active = 1
          AND (:branch_id IS NULL OR p.branch_id = :branch_id)
          AND (:from_date IS NULL OR p.plan_date >= :from_date)
          AND (:to_date IS NULL OR p.plan_date <= :to_date)
        ORDER BY p.plan_date DESC, p.planning_id DESC
        """
    )


def get_planning_header_query():
    """One active plan header by id, company-scoped (404 when it returns nothing)."""
    return text(
        """
        SELECT
            p.planning_id, p.co_id, p.branch_id, p.plan_date, p.plan_no,
            p.plan_name, p.remarks, p.status_id, p.active,
            p.updated_by, u.name AS updated_by_name, p.updated_date_time,
            b.branch_name
        FROM jute_prod_planning p
        LEFT JOIN branch_mst b ON b.branch_id = p.branch_id
        LEFT JOIN user_mst u ON u.user_id = p.updated_by
        WHERE p.planning_id = :planning_id
          AND p.co_id = :co_id
          AND p.active = 1
        """
    )


def get_planning_latest_header_query():
    """Newest active plan header with plan_date <= :before_date ("Copy latest plan").

    branch_id is an optional filter; :before_date is NULL-tolerant so an unbounded
    call simply returns the newest plan.
    """
    return text(
        """
        SELECT
            p.planning_id, p.co_id, p.branch_id, p.plan_date, p.plan_no,
            p.plan_name, p.remarks, p.status_id, p.active,
            p.updated_by, u.name AS updated_by_name, p.updated_date_time,
            b.branch_name
        FROM jute_prod_planning p
        LEFT JOIN branch_mst b ON b.branch_id = p.branch_id
        LEFT JOIN user_mst u ON u.user_id = p.updated_by
        WHERE p.co_id = :co_id
          AND p.active = 1
          AND (:branch_id IS NULL OR p.branch_id = :branch_id)
          AND (:before_date IS NULL OR p.plan_date <= :before_date)
        ORDER BY p.plan_date DESC, p.planning_id DESC
        LIMIT 1
        """
    )


def find_duplicate_plan_query():
    """Another ACTIVE header for the same (co_id, branch_id, plan_date).

    Uniqueness guard for planning_save (SPEC §3): the row currently being updated is
    excluded via :planning_id (NULL on insert). branch_id is compared NULL-safely so
    two branch-less plans on one date still collide.
    """
    return text(
        """
        SELECT planning_id
        FROM jute_prod_planning
        WHERE co_id = :co_id
          AND active = 1
          AND plan_date = :plan_date
          AND ((:branch_id IS NULL AND branch_id IS NULL) OR branch_id = :branch_id)
          AND (:planning_id IS NULL OR planning_id <> :planning_id)
        LIMIT 1
        """
    )


def get_planning_loom_lines_query():
    """Active Section-1 lines of one plan, in line_order, with the quality label."""
    return text(
        """
        SELECT
            pl.planning_loom_id, pl.planning_id, pl.product_group,
            pl.quality_item_id, pl.quality_desc,
            im.item_code AS quality_item_code, im.item_name AS quality_item_name,
            pl.looms_a, pl.looms_b, pl.looms_c,
            pl.kg_per_loom_100, pl.eff_pct, pl.kg_per_loom,
            pl.prod_a, pl.prod_b, pl.prod_c,
            pl.warp_yarn_item_id, pl.weft_yarn_item_id, pl.warp_pct, pl.weft_pct,
            pl.line_order, pl.remarks
        FROM jute_prod_planning_loom pl
        LEFT JOIN item_mst im ON im.item_id = pl.quality_item_id
        WHERE pl.planning_id = :planning_id
          AND pl.active = 1
        ORDER BY pl.line_order ASC, pl.planning_loom_id ASC
        """
    )


def get_planning_yarn_lines_query():
    """Active Section-2 lines of one plan, in line_order, with the yarn label."""
    return text(
        """
        SELECT
            py.planning_yarn_id, py.planning_id, py.line_kind, py.section,
            py.yarn_item_id, py.yarn_desc,
            im.item_code AS yarn_item_code, im.item_name AS yarn_item_name,
            py.warp_weft, py.pct_of_prod,
            py.req_kg_a, py.req_kg_b, py.req_kg_c, py.req_kg_day,
            py.kg_per_frame_100, py.eff_pct, py.kg_per_frame,
            py.frames_required, py.frames_a, py.frames_b, py.frames_c,
            py.priority, py.order_mt, py.days_needed, py.delivery_date,
            py.line_order, py.remarks
        FROM jute_prod_planning_yarn py
        LEFT JOIN item_mst im ON im.item_id = py.yarn_item_id
        WHERE py.planning_id = :planning_id
          AND py.active = 1
        ORDER BY py.line_order ASC, py.planning_yarn_id ASC
        """
    )


def get_planning_dept_lines_query():
    """Active Section-3 stage rows of one plan, in line_order (then stage_seq)."""
    return text(
        """
        SELECT
            pd.planning_dept_id, pd.planning_id, pd.stage_seq, pd.stage_code,
            pd.stage_label, pd.output_kg_day, pd.waste_pct, pd.input_kg_day,
            pd.kg_per_machine, pd.machines_required,
            pd.machines_a, pd.machines_b, pd.machines_c,
            pd.machines_installed, pd.machines_maint, pd.machines_ready,
            pd.line_order, pd.remarks
        FROM jute_prod_planning_dept pd
        WHERE pd.planning_id = :planning_id
          AND pd.active = 1
        ORDER BY pd.line_order ASC, pd.stage_seq ASC, pd.planning_dept_id ASC
        """
    )


# =============================================================================
# Plan document — writes (planning_save)
# =============================================================================


def insert_planning_header_query():
    """Insert a plan header. plan_no is stamped afterwards from the new id."""
    return text(
        """
        INSERT INTO jute_prod_planning
            (co_id, branch_id, plan_date, plan_name, remarks, status_id, active, updated_by)
        VALUES
            (:co_id, :branch_id, :plan_date, :plan_name, :remarks, :status_id, 1, :updated_by)
        """
    )


def stamp_plan_no_query():
    """Stamp plan_no = JPP-00001 style from the auto-increment id (SPEC §3)."""
    return text(
        """
        UPDATE jute_prod_planning
        SET plan_no = CONCAT(
                'JPP-',
                LPAD(planning_id, GREATEST(5, CHAR_LENGTH(CAST(planning_id AS CHAR))), '0')
            ),
            updated_by = :updated_by
        WHERE planning_id = :planning_id
        """
    )


def update_planning_header_query():
    """Update an existing plan header (company-scoped)."""
    return text(
        """
        UPDATE jute_prod_planning
        SET branch_id = :branch_id,
            plan_date = :plan_date,
            plan_name = :plan_name,
            remarks = :remarks,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE planning_id = :planning_id
          AND co_id = :co_id
        """
    )


def soft_delete_planning_query():
    """Soft-delete a plan header (active = 0), company-scoped."""
    return text(
        """
        UPDATE jute_prod_planning
        SET active = 0,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE planning_id = :planning_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def delete_planning_loom_lines_query():
    """Wholesale child replace: drop Section-1 lines before re-inserting them."""
    return text("DELETE FROM jute_prod_planning_loom WHERE planning_id = :planning_id")


def delete_planning_yarn_lines_query():
    """Wholesale child replace: drop Section-2 lines before re-inserting them."""
    return text("DELETE FROM jute_prod_planning_yarn WHERE planning_id = :planning_id")


def delete_planning_dept_lines_query():
    """Wholesale child replace: drop Section-3 rows before re-inserting them."""
    return text("DELETE FROM jute_prod_planning_dept WHERE planning_id = :planning_id")


def insert_planning_loom_line_query():
    return text(
        """
        INSERT INTO jute_prod_planning_loom
            (planning_id, product_group, quality_item_id, quality_desc,
             looms_a, looms_b, looms_c,
             kg_per_loom_100, eff_pct, kg_per_loom,
             prod_a, prod_b, prod_c,
             warp_yarn_item_id, weft_yarn_item_id, warp_pct, weft_pct,
             line_order, remarks, active, updated_by)
        VALUES
            (:planning_id, :product_group, :quality_item_id, :quality_desc,
             :looms_a, :looms_b, :looms_c,
             :kg_per_loom_100, :eff_pct, :kg_per_loom,
             :prod_a, :prod_b, :prod_c,
             :warp_yarn_item_id, :weft_yarn_item_id, :warp_pct, :weft_pct,
             :line_order, :remarks, 1, :updated_by)
        """
    )


def insert_planning_yarn_line_query():
    return text(
        """
        INSERT INTO jute_prod_planning_yarn
            (planning_id, line_kind, section, yarn_item_id, yarn_desc, warp_weft,
             pct_of_prod, req_kg_a, req_kg_b, req_kg_c, req_kg_day,
             kg_per_frame_100, eff_pct, kg_per_frame, frames_required,
             frames_a, frames_b, frames_c,
             priority, order_mt, days_needed, delivery_date,
             line_order, remarks, active, updated_by)
        VALUES
            (:planning_id, :line_kind, :section, :yarn_item_id, :yarn_desc, :warp_weft,
             :pct_of_prod, :req_kg_a, :req_kg_b, :req_kg_c, :req_kg_day,
             :kg_per_frame_100, :eff_pct, :kg_per_frame, :frames_required,
             :frames_a, :frames_b, :frames_c,
             :priority, :order_mt, :days_needed, :delivery_date,
             :line_order, :remarks, 1, :updated_by)
        """
    )


def insert_planning_dept_line_query():
    return text(
        """
        INSERT INTO jute_prod_planning_dept
            (planning_id, stage_seq, stage_code, stage_label,
             output_kg_day, waste_pct, input_kg_day,
             kg_per_machine, machines_required,
             machines_a, machines_b, machines_c,
             machines_installed, machines_maint, machines_ready,
             line_order, remarks, active, updated_by)
        VALUES
            (:planning_id, :stage_seq, :stage_code, :stage_label,
             :output_kg_day, :waste_pct, :input_kg_day,
             :kg_per_machine, :machines_required,
             :machines_a, :machines_b, :machines_c,
             :machines_installed, :machines_maint, :machines_ready,
             :line_order, :remarks, 1, :updated_by)
        """
    )


# =============================================================================
# Setup reads (cloth qualities / yarn items / machine counts)
# =============================================================================


def get_planning_cloth_qualities_query():
    """Active cloth-quality items — item_grp_mst.item_type_id = 5 ('Jute Cloth').

    Same source as Beaming/Weaving/Finishing pickers. Company scope lives on
    item_grp_mst.co_id (item_mst has no co_id by design), so the join filter is
    mandatory — otherwise items sharing a code across companies appear duplicated.
    """
    return text(
        """
        SELECT i.item_id, i.item_code, i.item_name,
               g.item_grp_id, g.item_grp_name AS grp_name, g.item_type_id
        FROM item_mst i
        INNER JOIN item_grp_mst g ON g.item_grp_id = i.item_grp_id
        WHERE i.active = 1
          AND g.co_id = :co_id
          AND g.item_type_id = :item_type_id
        ORDER BY g.item_grp_name, i.item_name
        """
    )


def get_planning_yarn_items_query():
    """Active yarn items — item_grp_mst.item_type_id = 4, count from jute_yarn_mst.

    A yarn IS an item (item_mst); jute_yarn_mst only carries the extra yarn data, so
    the join is a LEFT JOIN — a yarn item with no jute_yarn_mst row still lists, with
    yarn_count NULL.
    """
    return text(
        """
        SELECT i.item_id, i.item_code, i.item_name,
               g.item_grp_id, g.item_grp_name AS grp_name,
               ym.jute_yarn_count AS yarn_count
        FROM item_mst i
        INNER JOIN item_grp_mst g ON g.item_grp_id = i.item_grp_id
        LEFT JOIN jute_yarn_mst ym ON ym.item_id = i.item_id
        WHERE i.active = 1
          AND g.co_id = :co_id
          AND g.item_type_id = :item_type_id
        ORDER BY i.item_name
        """
    )


def get_planning_machine_counts_query():
    """Installed active machines per machine TYPE for the company (optionally branch).

    Returns the lower-cased type name so the caller can match PLANNING_STAGES'
    machine_type_name case-insensitively (resolution is by NAME everywhere in this
    module). A stage whose machine type does not exist in the tenant simply has no
    row here and resolves to 0 (e.g. 'Carding' in dev3).

    Company scope follows the machine -> dept -> branch -> branch_mst.co_id chain
    used by every other juteProduction machine query.
    """
    return text(
        """
        SELECT LOWER(t.machine_type_name) AS machine_type_name,
               COUNT(*) AS machine_count
        FROM machine_mst m
        INNER JOIN machine_type_mst t ON t.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        INNER JOIN branch_mst b ON b.branch_id = d.branch_id AND b.co_id = :co_id
        WHERE m.active = 1
          AND t.active = 1
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        GROUP BY LOWER(t.machine_type_name)
        """
    )


# =============================================================================
# Norms (jute_prod_planning_norm)
# =============================================================================


def get_resolved_planning_norms_query():
    """Every norm value effective on :on_date, one row per (id_type, ref_id, param).

    LAST-DATE resolution exactly as SPEC §2.5 / every *_target_map: among active=1
    rows for the key, the row with MAX(effective_date) <= :on_date wins. The derived
    table picks that date; ties on the same date are broken by the caller, which
    walks the ASC-ordered rows and keeps the LAST one seen per key (= highest
    planning_norm_id, matching ``ORDER BY effective_date DESC, id DESC LIMIT 1``).

    :id_type is an optional filter (norms_grid passes one; planning_setup does not).
    Like every target-map resolution, NO branch filter is applied.
    """
    return text(
        """
        SELECT n.id_type, n.ref_id, n.param, n.value, n.effective_date,
               n.planning_norm_id
        FROM jute_prod_planning_norm n
        INNER JOIN (
            SELECT id_type, ref_id, param, MAX(effective_date) AS eff_dt
            FROM jute_prod_planning_norm
            WHERE co_id = :co_id
              AND active = 1
              AND effective_date <= :on_date
              AND (:id_type IS NULL OR id_type = :id_type)
            GROUP BY id_type, ref_id, param
        ) x ON x.id_type = n.id_type
           AND x.ref_id = n.ref_id
           AND x.param = n.param
           AND x.eff_dt = n.effective_date
        WHERE n.co_id = :co_id
          AND n.active = 1
          AND (:id_type IS NULL OR n.id_type = :id_type)
        ORDER BY n.id_type, n.ref_id, n.param, n.planning_norm_id ASC
        """
    )


def resolve_planning_norm_query():
    """LAST-DATE resolution for ONE norm key (co_id, id_type, ref_id, param).

    The single-cell twin of get_resolved_planning_norms_query, used by the upsert
    path (norms_bulk_save / planning_save's update_norms) to decide whether a value
    actually changed. No branch filter — resolution is branch-agnostic.
    """
    return text(
        """
        SELECT planning_norm_id, value, effective_date
        FROM jute_prod_planning_norm
        WHERE co_id = :co_id
          AND id_type = :id_type
          AND ref_id = :ref_id
          AND param = :param
          AND active = 1
          AND effective_date <= :on_date
        ORDER BY effective_date DESC, planning_norm_id DESC
        LIMIT 1
        """
    )


def insert_planning_norm_query():
    """Insert a fresh active norm row at :effective_date."""
    return text(
        """
        INSERT INTO jute_prod_planning_norm
            (co_id, branch_id, effective_date, ref_id, id_type, value_role,
             param, value, active, updated_by)
        VALUES
            (:co_id, :branch_id, :effective_date, :ref_id, :id_type, :value_role,
             :param, :value, 1, :updated_by)
        """
    )


def get_planning_norms_history_query():
    """Raw active norm history for one ref (audit / history view).

    id_type and ref_id are optional filters so the same builder serves "all history
    for a dimension" and "history for one ref". Newest effective_date first.
    """
    return text(
        """
        SELECT planning_norm_id, co_id, branch_id, effective_date, ref_id,
               id_type, value_role, param, value, active,
               updated_by, updated_date_time
        FROM jute_prod_planning_norm
        WHERE co_id = :co_id
          AND active = 1
          AND (:id_type IS NULL OR id_type = :id_type)
          AND (:ref_id IS NULL OR ref_id = :ref_id)
          AND (:param IS NULL OR param = :param)
        ORDER BY effective_date DESC, planning_norm_id DESC
        """
    )


def soft_delete_planning_norm_query():
    """Soft-delete one norm row (active = 0), company-scoped."""
    return text(
        """
        UPDATE jute_prod_planning_norm
        SET active = 0,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE planning_norm_id = :planning_norm_id
          AND co_id = :co_id
          AND active = 1
        """
    )

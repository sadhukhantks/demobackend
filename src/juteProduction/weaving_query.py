"""Raw SQL builders for the three Weaving pages (jute production module).

The SINGLE shared query module imported by all three weaving routers, exactly as
``beaming_query.py`` is shared by the beaming routers:

* **Page A — Weaving Quality Master** (``jute_prod_weaving_quality``): woven-cloth item
  list, jute-yarn picker, quality list / row / duplicate-check, insert / update /
  soft-delete, plus the composite-warp component (``jute_prod_weaving_quality_dtl``)
  helpers.
* **Page B — Weaving Standards/Targets Map** (``jute_prod_weaving_target_map``): a clone
  of ``beaming_query``'s target-map section, but **QID-ONLY** (Q5): ``id_type`` is always
  ``'qid'`` (``ref_id = weaving_quality_id``); there are NO loom (``mcid``) standards, so
  the machine-ref join is dropped. Setup quality refs, flat list, single-row CRUD,
  LAST-DATE grid prefill + bulk-save exact-key lookups (all branch-agnostic).
* **Page C — Weaving Production Entry** (``jute_prod_weaving_daily`` + the spinning-style
  ``jute_prod_weaving_quality_map`` Loom->Quality map + ``jute_prod_weaving_beam_map``
  beam-change map): create-setup lookups, entries-by-date day grid (quality read from the
  STORED ``wd.weaving_quality_id`` — stamped from the map at entry save and RE-STAMPED by
  quality_map_save on remap; the day-slice does NOT coalesce to the map at read time, so a
  NULL/stale stored quality computes zero production — Process blocks on it), the
  machine-standards resolution, the
  planning-grid driver select (cloned from ``get_spinning_plan_driver_query`` but driven
  by the active quality map), the Loom->Quality map get/save/mapped, and the beam-map
  get/save.

Mirrors ``beaming_query.py`` / ``spinning_query.py`` conventions throughout: named binds,
the ``:x IS NULL OR ...`` optional-filter idiom, soft-delete via active=1, and
de-duplication left to the router. ``machine_mst`` / ``machine_type_mst`` filter
``active = 1``; ``spell_mst`` filters ``status = 1`` (NOT active).

The Weaving (Loom) machine type resolves against ``machine_type_mst.machine_type_name`` =
'Loom' (case-insensitive under MySQL's default collation; dev3 machine_type_id 6 'LOOM'),
bound at call time via :loom_type so no constant is imported here. Column names/types
match weaving_models.py exactly.
"""

from sqlalchemy import text

from src.juteProduction.constants import WEAVING_CHAIN_MAX_GAP_DAYS


# =============================================================================
# PAGE A — Weaving Quality Master (jute_prod_weaving_quality)
# =============================================================================


def get_weaving_cloth_items_query():
    """Active woven-cloth items for the company — item_grp_mst.item_type_id = 5.

    Adapted from beaming_query.get_beaming_cloth_items_query (Jute Cloth = 5,
    WEAVING_ITEM_TYPE_IDS). Company scope lives on item_grp_mst.co_id (item_mst has
    no co_id by design).
    """
    return text(
        """
        SELECT i.item_id, i.item_code, i.item_name,
               g.item_grp_id, g.item_grp_name, g.item_type_id
        FROM item_mst i
        INNER JOIN item_grp_mst g ON g.item_grp_id = i.item_grp_id
        WHERE i.active = 1
          AND g.co_id = :co_id
          AND g.item_type_id = 5
        ORDER BY i.item_name
        """
    )


def get_weaving_yarns_query():
    """Active jute yarns for the composite-warp count picker (item_type_id = 4).

    Mirrors beaming_query.get_beaming_yarns_query: a yarn IS an item (item_mst); its
    count lives on jute_yarn_mst.jute_yarn_count. Returns item_id (canonical key),
    item_code/item_name, and jute_yarn_count so a component dialog can auto-fill the
    count from the chosen yarn. Company scope is on item_grp_mst.co_id.
    """
    return text(
        """
        SELECT
            ym.item_id,
            im.item_code,
            im.item_name,
            ym.jute_yarn_count
        FROM jute_yarn_mst ym
        JOIN item_mst im ON im.item_id = ym.item_id
        JOIN item_grp_mst ig ON ig.item_grp_id = im.item_grp_id
        WHERE ig.item_type_id = 4
          AND ig.co_id = :co_id
        ORDER BY im.item_name
        """
    )


def get_weaving_quality_list_query():
    """Active weaving-quality rows for a company, with item label join.

    Optional :item_id filter (one item's qualities); optional :search on code/name;
    optional branch filter that tolerates NULL branch rows (company-scoped master).
    Newest first.
    """
    return text(
        """
        SELECT
            q.weaving_quality_id,
            q.co_id,
            q.branch_id,
            q.item_id,
            im.item_code,
            im.item_name,
            q.weaving_quality_code,
            q.weaving_quality_name,
            q.ends,
            q.finished_length,
            q.ozs_yds,
            q.std_ozs_yds,
            q.no_of_jugar_per_cut,
            q.width,
            q.ports,
            q.reed_porter,
            q.shrinkage_pct,
            q.shots,
            q.mc_teeth,
            q.jbo_rbo,
            q.reed_space,
            q.tpi,
            q.yarn_count,
            q.is_composite,
            q.active
        FROM jute_prod_weaving_quality q
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        WHERE q.co_id = :co_id
          AND q.active = 1
          AND (:item_id IS NULL OR q.item_id = :item_id)
          AND (:branch_id IS NULL OR q.branch_id = :branch_id OR q.branch_id IS NULL)
          AND (:search IS NULL
               OR q.weaving_quality_code LIKE :search
               OR q.weaving_quality_name LIKE :search)
        ORDER BY q.weaving_quality_id DESC
        """
    )


def get_weaving_quality_row_query():
    """A single active weaving-quality row by id (edit / delete existence check)."""
    return text(
        """
        SELECT weaving_quality_id, co_id, branch_id, item_id, weaving_quality_code,
               weaving_quality_name, ends, finished_length, ozs_yds, std_ozs_yds,
               no_of_jugar_per_cut, width, ports, reed_porter, shrinkage_pct, shots,
               mc_teeth, jbo_rbo, reed_space, tpi, yarn_count, is_composite, active
        FROM jute_prod_weaving_quality
        WHERE weaving_quality_id = :weaving_quality_id
          AND active = 1
        """
    )


def check_weaving_quality_duplicate_query():
    """Active duplicate guard on (co_id, item_id, weaving_quality_code).

    Optional :exclude_id excludes the row being edited so an update to itself is not
    flagged as a duplicate. Mirrors check_bm_quality_duplicate_query.
    """
    return text(
        """
        SELECT weaving_quality_id
        FROM jute_prod_weaving_quality
        WHERE co_id = :co_id
          AND item_id = :item_id
          AND weaving_quality_code = :weaving_quality_code
          AND active = 1
          AND (:exclude_id IS NULL OR weaving_quality_id <> :exclude_id)
        LIMIT 1
        """
    )


def insert_weaving_quality_query():
    """Insert a fresh active weaving-quality row (no created_* — trigger-based audit)."""
    return text(
        """
        INSERT INTO jute_prod_weaving_quality
            (co_id, branch_id, item_id, weaving_quality_code, weaving_quality_name,
             ends, finished_length, ozs_yds, std_ozs_yds, no_of_jugar_per_cut,
             width, ports, reed_porter, shrinkage_pct, shots, mc_teeth, jbo_rbo,
             reed_space, tpi, yarn_count, is_composite, active, updated_by)
        VALUES
            (:co_id, :branch_id, :item_id, :weaving_quality_code, :weaving_quality_name,
             :ends, :finished_length, :ozs_yds, :std_ozs_yds, :no_of_jugar_per_cut,
             :width, :ports, :reed_porter, :shrinkage_pct, :shots, :mc_teeth, :jbo_rbo,
             :reed_space, :tpi, :yarn_count, :is_composite, 1, :updated_by)
        """
    )


def update_weaving_quality_query():
    """Patch update one weaving-quality row by id.

    COALESCE keeps the existing value when a bind is NULL, so the router can pass only
    the fields the client sent (mirrors the beaming edit pattern). active is updatable so
    a soft-deleted row can be reactivated via edit if needed.
    """
    return text(
        """
        UPDATE jute_prod_weaving_quality
        SET weaving_quality_code = COALESCE(:weaving_quality_code, weaving_quality_code),
            weaving_quality_name = COALESCE(:weaving_quality_name, weaving_quality_name),
            ends                 = COALESCE(:ends, ends),
            finished_length      = COALESCE(:finished_length, finished_length),
            ozs_yds              = COALESCE(:ozs_yds, ozs_yds),
            std_ozs_yds          = COALESCE(:std_ozs_yds, std_ozs_yds),
            no_of_jugar_per_cut  = COALESCE(:no_of_jugar_per_cut, no_of_jugar_per_cut),
            width                = COALESCE(:width, width),
            ports                = COALESCE(:ports, ports),
            reed_porter          = COALESCE(:reed_porter, reed_porter),
            shrinkage_pct        = COALESCE(:shrinkage_pct, shrinkage_pct),
            shots                = COALESCE(:shots, shots),
            mc_teeth             = COALESCE(:mc_teeth, mc_teeth),
            jbo_rbo              = COALESCE(:jbo_rbo, jbo_rbo),
            reed_space           = COALESCE(:reed_space, reed_space),
            tpi                  = COALESCE(:tpi, tpi),
            yarn_count           = COALESCE(:yarn_count, yarn_count),
            is_composite         = COALESCE(:is_composite, is_composite),
            active               = COALESCE(:active, active),
            updated_by           = :updated_by,
            updated_date_time    = CURRENT_TIMESTAMP
        WHERE weaving_quality_id = :weaving_quality_id
        """
    )


def soft_delete_weaving_quality_query():
    """Soft-delete (active=0) one weaving-quality row by id."""
    return text(
        """
        UPDATE jute_prod_weaving_quality
        SET active = 0,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_quality_id = :weaving_quality_id
        """
    )


def get_weaving_quality_detail_query():
    """A single active weaving-quality parent row by id + co_id (edit dialog, Q6).

    Co-scoped variant of get_weaving_quality_row_query for the
    weaving_quality_detail/{id} endpoint — the router fetches the component rows
    separately via get_weaving_quality_components_query and nests them under
    ``components``.
    """
    return text(
        """
        SELECT weaving_quality_id, co_id, branch_id, item_id, weaving_quality_code,
               weaving_quality_name, ends, finished_length, ozs_yds, std_ozs_yds,
               no_of_jugar_per_cut, width, ports, reed_porter, shrinkage_pct, shots,
               mc_teeth, jbo_rbo, reed_space, tpi, yarn_count, is_composite, active
        FROM jute_prod_weaving_quality
        WHERE weaving_quality_id = :weaving_quality_id
          AND co_id = :co_id
          AND active = 1
        """
    )


def get_weaving_quality_components_query():
    """Active component rows for a composite weaving quality (Q6, mirror beaming).

    Returns the real (ends, count) warp-component pairs ordered by component_no for
    the edit dialog. Only populated when the parent is_composite=1 (>=2 rows);
    non-composite qualities have no rows here.
    """
    return text(
        """
        SELECT weaving_quality_dtl_id, component_no, ends, yarn_item_id, count
        FROM jute_prod_weaving_quality_dtl
        WHERE weaving_quality_id = :weaving_quality_id
          AND active = 1
        ORDER BY component_no
        """
    )


def insert_weaving_quality_component_query():
    """Insert one active component row for a composite weaving quality (Q6)."""
    return text(
        """
        INSERT INTO jute_prod_weaving_quality_dtl
            (weaving_quality_id, component_no, ends, yarn_item_id, count, active)
        VALUES
            (:weaving_quality_id, :component_no, :ends, :yarn_item_id, :count, 1)
        """
    )


def soft_delete_weaving_quality_components_query():
    """Soft-delete (active=0) ALL components of a weaving quality (replace-on-edit, Q6).

    Edit re-inserts the full component set, so the router clears the existing set
    first via this builder, then re-inserts via insert_weaving_quality_component_query.
    """
    return text(
        """
        UPDATE jute_prod_weaving_quality_dtl
        SET active = 0
        WHERE weaving_quality_id = :weaving_quality_id
          AND active = 1
        """
    )


# =============================================================================
# PAGE B — Weaving Standards/Targets Map (jute_prod_weaving_target_map)
# TWO-DIMENSIONAL clone of beaming_query's target-map section. id_type is 'mcid'
# (ref_id = machine_id, a LOOM) or 'qid' (ref_id =
# jute_prod_weaving_quality.weaving_quality_id). Loom (mcid) refs reuse PAGE C's
# get_weaving_entry_machines_query(); the queries below cover the qid refs + the
# id_type-agnostic list / row / grid resolve / bulk-save shape.
# =============================================================================


def get_weaving_target_qualities_query():
    """Active Weaving-Quality rows for the target-map QID grid refs (qid-only, Q5).

    Mirrors beaming_query.get_beaming_target_qualities_query: the grid ref is the
    weaving QUALITY (jute_prod_weaving_quality). Returns ref_id = weaving_quality_id,
    ref_code = weaving_quality_code, ref_name = weaving_quality_name so the router can
    build {ref_id, ref_code, ref_name} ref rows. Company-scoped (co_id); :branch_id
    is accepted for call-shape parity but quality is branch-agnostic (NULL-tolerant).
    Newest first.
    """
    return text(
        """
        SELECT
            q.weaving_quality_id,
            q.weaving_quality_code,
            q.weaving_quality_name,
            q.branch_id
        FROM jute_prod_weaving_quality q
        WHERE q.co_id = :co_id
          AND q.active = 1
          AND (:branch_id IS NULL OR q.branch_id = :branch_id OR q.branch_id IS NULL)
        ORDER BY q.weaving_quality_id DESC
        """
    )


def get_weaving_target_map_list_query():
    """Active weaving target-map rows with optional id_type / ref_id / value_role /
    param filters. Newest effective_date first. QID-ONLY: the ref label comes from the
    Weaving Quality Master (jute_prod_weaving_quality, ref_id = weaving_quality_id).
    The :id_type bind is accepted for call-shape parity with beaming but in practice is
    always 'qid'."""
    return text(
        """
        SELECT
            tm.weaving_target_map_id,
            tm.co_id,
            tm.branch_id,
            tm.effective_date,
            tm.ref_id,
            tm.id_type,
            tm.value_role,
            tm.param,
            tm.value,
            tm.active,
            q.weaving_quality_code AS ref_code,
            q.weaving_quality_name AS ref_name
        FROM jute_prod_weaving_target_map tm
        LEFT JOIN jute_prod_weaving_quality q
               ON q.weaving_quality_id = tm.ref_id
        WHERE tm.co_id = :co_id
          AND tm.active = 1
          AND (:branch_id IS NULL OR tm.branch_id = :branch_id OR tm.branch_id IS NULL)
          AND (:id_type IS NULL OR tm.id_type = :id_type)
          AND (:ref_id IS NULL OR tm.ref_id = :ref_id)
          AND (:value_role IS NULL OR tm.value_role = :value_role)
          AND (:param IS NULL OR tm.param = :param)
        ORDER BY tm.effective_date DESC, tm.weaving_target_map_id DESC
        """
    )


def get_weaving_target_map_row_query():
    """A single active weaving target-map row by id (for edit / delete existence check)."""
    return text(
        """
        SELECT weaving_target_map_id, co_id, branch_id, effective_date, ref_id,
               id_type, value_role, param, value, active
        FROM jute_prod_weaving_target_map
        WHERE weaving_target_map_id = :id
          AND active = 1
        """
    )


def insert_weaving_target_map_query():
    """Insert a fresh active weaving standards/targets row (id_type always 'qid')."""
    return text(
        """
        INSERT INTO jute_prod_weaving_target_map
            (co_id, branch_id, effective_date, ref_id, id_type, value_role,
             param, value, active, updated_by)
        VALUES
            (:co_id, :branch_id, :effective_date, :ref_id, :id_type, :value_role,
             :param, :value, 1, :updated_by)
        """
    )


def update_weaving_target_map_query():
    """Patch update one weaving target-map row by id (value + effective_date + audit)."""
    return text(
        """
        UPDATE jute_prod_weaving_target_map
        SET value = COALESCE(:value, value),
            effective_date = COALESCE(:effective_date, effective_date),
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_target_map_id = :id
        """
    )


def soft_delete_weaving_target_map_query():
    """Soft-delete (active=0) one weaving target-map row by id."""
    return text(
        """
        UPDATE jute_prod_weaving_target_map
        SET active = 0,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_target_map_id = :id
        """
    )


def resolve_weaving_target_value_query():
    """LAST-DATE resolution: the value effective on :on_date for one resolution key.

    MAX(effective_date) <= :on_date among active rows for
    (co_id, ref_id, id_type, value_role, param). Branch-agnostic (mirrors beaming /
    resolve_param). Consumed by the Page C standards snapshot builder
    (services/weaving_standards.py).
    """
    return text(
        """
        SELECT value, effective_date
        FROM jute_prod_weaving_target_map
        WHERE co_id = :co_id
          AND ref_id = :ref_id
          AND id_type = :id_type
          AND value_role = :value_role
          AND param = :param
          AND active = 1
          AND effective_date <= :on_date
        ORDER BY effective_date DESC, weaving_target_map_id DESC
        LIMIT 1
        """
    )


def resolve_weaving_grid_cells_batch_query():
    """LAST-DATE resolution for the WHOLE grid in ONE statement (batch form of the
    old per-cell resolve_weaving_grid_cell_query — kills target_map_grid's
    refs x params N+1).

    One ranked pass over jute_prod_weaving_target_map for (co_id, id_type,
    value_role): rn=1 per (ref_id, param) with the SAME ORDER BY effective_date
    DESC, weaving_target_map_id DESC tiebreak and NO branch filter as the per-cell
    probe (mirrors resolve_param / production resolution), returning
    ref_id/param/value/effective_date so the router can dict-key the cells. The
    router filters params to grid_params_for() and omits NULL values — cell output
    identical to the per-cell probe, one execute instead of refs x params.
    """
    return text(
        """
        SELECT t.ref_id, t.param, t.value, t.effective_date
        FROM (
            SELECT tm.ref_id, tm.param, tm.value, tm.effective_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY tm.ref_id, tm.param
                       ORDER BY tm.effective_date DESC, tm.weaving_target_map_id DESC
                   ) AS rn
            FROM jute_prod_weaving_target_map tm
            WHERE tm.co_id = :co_id
              AND tm.id_type = :id_type
              AND tm.value_role = :value_role
              AND tm.active = 1
              AND tm.effective_date <= :on_date
        ) t
        WHERE t.rn = 1
        """
    )


def find_exact_weaving_grid_row_query():
    """Active row at the EXACT save key, BRANCH-AGNOSTIC (mirrors grid resolution).

    Used by target_map_bulk_save to decide insert-vs-update-vs-clear. Applies NO branch
    filter so save targets the SAME row the grid prefilled
    (resolve_weaving_grid_cells_batch_query ignores branch_id); the newest-id tiebreak
    matches the grid's is_exact ORDER BY.
    """
    return text(
        """
        SELECT weaving_target_map_id, value
        FROM jute_prod_weaving_target_map
        WHERE co_id = :co_id
          AND ref_id = :ref_id
          AND id_type = :id_type
          AND value_role = :value_role
          AND param = :param
          AND effective_date = :effective_date
          AND active = 1
        ORDER BY weaving_target_map_id DESC
        LIMIT 1
        """
    )


def update_weaving_grid_value_query():
    """Set value (+ audit cols) on one active grid row by id."""
    return text(
        """
        UPDATE jute_prod_weaving_target_map
        SET value = :value,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_target_map_id = :id
        """
    )


def clear_weaving_grid_value_query():
    """Soft-delete (active=0) one grid row by id, clearing the cell."""
    return text(
        """
        UPDATE jute_prod_weaving_target_map
        SET active = 0,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_target_map_id = :id
        """
    )


# =============================================================================
# PAGE C — Weaving Production Entry (jute_prod_weaving_daily)
# create_setup lookups + entries_by_date day grid + planning-grid driver select.
# Looms resolve by machine_type_name 'Loom' (:loom_type, case-insensitive).
# =============================================================================


def get_weaving_entry_machines_query():
    """Loom-type machines for the entry create-setup (resolve by NAME 'Loom').

    Machine identity only; quality is inherited from the §6.6 quality map, standards
    resolve per-date from the qid target map. machine_mst/machine_type_mst filter
    active=1. line_no is the dev3 line column (NOT line_number).
    """
    return text(
        """
        SELECT
            m.machine_id,
            m.machine_name,
            m.mech_code,
            m.dept_id,
            m.line_no,
            d.dept_desc AS dept_name,
            d.branch_id
        FROM machine_mst m
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE m.active = 1
          AND mt.active = 1
          AND mt.machine_type_name = :loom_type
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY m.mech_code
        """
    )


def get_weaving_spells_query():
    """Active spells with working hours for the entry header.

    spell_mst filters status = 1 (NOT active); shift_mst filters status = 1. Returns
    spell_id (daily table stores INT spell_id) plus the code/name/hours. Callers must
    de-dup by spell_code in Python (branch fanout), keeping the first row per code.
    """
    return text(
        """
        SELECT sp.spell_id, sp.spell_code, sp.spell_name, sp.working_hours,
               sp.starting_time, sp.is_overnight, sp.shift_id
        FROM spell_mst sp
        INNER JOIN shift_mst sh ON sh.shift_id = sp.shift_id
        WHERE sp.status = 1
          AND sh.status = 1
          AND (:branch_id IS NULL OR sh.branch_id = :branch_id)
        ORDER BY sp.starting_time
        """
    )


def get_weaving_eb_list_query():
    """Active employee (eb) list for display joins / pickers.

    The eb identity is the HRMS employee (hrms_ed_personal_details, keyed by eb_id);
    there is no eb_master table. Joins hrms_ed_official_details (active=1) for emp_code
    and branch scope. Branch-scoped, NULL-tolerant. On the weaving screen EB is NOT
    entered — Sync/Process stamps it from attendance; this list is kept for parity and
    any eb label lookups.
    """
    return text(
        """
        SELECT
            p.eb_id,
            o.emp_code,
            CONCAT(p.first_name, ' ', COALESCE(p.last_name, '')) AS eb_name,
            o.branch_id
        FROM hrms_ed_personal_details p
        LEFT JOIN hrms_ed_official_details o ON o.eb_id = p.eb_id AND o.active = 1
        WHERE p.active = 1
          AND (:branch_id IS NULL OR o.branch_id = :branch_id OR o.branch_id IS NULL)
        ORDER BY o.emp_code, p.first_name
        """
    )


def get_weaving_entry_qualities_query():
    """All active weaving qualities for the company (item->quality reference / picker).

    Returns the item label and the quality's construction attrs (ends, finished_length,
    ozs_yds, std_ozs_yds, no_of_jugar_per_cut, is_composite) so the FE can render the
    mapped quality read-only and the compute layer can snapshot standards. Quality is
    MAPPED (Loom->Quality map), not selected inline on the production grid. Optional
    :item_id filter.
    """
    return text(
        """
        SELECT
            q.weaving_quality_id,
            q.item_id,
            im.item_code,
            im.item_name,
            q.weaving_quality_code,
            q.weaving_quality_name,
            q.ends,
            q.finished_length,
            q.ozs_yds,
            q.std_ozs_yds,
            q.no_of_jugar_per_cut,
            q.is_composite
        FROM jute_prod_weaving_quality q
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        WHERE q.co_id = :co_id
          AND q.active = 1
          AND (:item_id IS NULL OR q.item_id = :item_id)
        ORDER BY im.item_name, q.weaving_quality_code
        """
    )


def _spell_rank_case(alias: str) -> str:
    """Spell order for the jugar chain: A1 -> B1 -> A2 -> B2 -> C, or A -> B -> C
    (A ranks with A1, B with B1 — a branch uses one scheme or the other). Else 99."""
    return (
        f"CASE {alias}.spell_code WHEN 'A1' THEN 1 WHEN 'A' THEN 1 "
        f"WHEN 'B1' THEN 2 WHEN 'B' THEN 2 "
        f"WHEN 'A2' THEN 3 WHEN 'B2' THEN 4 WHEN 'C' THEN 5 ELSE 99 END"
    )


def _chain_eff_start_sql(wd: str, sp: str) -> str:
    """Chain START TIMESTAMP of a daily row = tran_date + the spell's starting_time.

    Ruling 2026-09-16 (TKT-000150/156): the jugar chain follows clock time on the
    TRANSACTION DATE. An overnight spell (spell_mst.is_overnight = 1) is dated by
    the day it STARTS - C dated 14-Sep = night 14-Sep 22:00 -> 15-Sep 06:00 - so
    it sorts last on its own date and the morning shift of the next date follows
    it. is_overnight only says the shift ENDS next day; it never moves a row to
    another date. (The 2026-09-15 variant that shifted overnight rows to D-1 was
    rejected by the crew's own data-entry order.) A vanished spell row sorts last
    in its day (23:59:59), mirroring the rank CASE's ELSE 99."""
    return f"TIMESTAMP({wd}.tran_date, COALESCE({sp}.starting_time, '23:59:59'))"


def _open_jugar_probe_sql() -> str:
    """Two-probe open_jugar expression (LAG replacement) -- WRITE-TIME ONLY since
    Phase 1b (2026-07-07): open_jugar is now STORED on jute_prod_weaving_daily and
    the read paths (day-slice + entry-inputs) read COALESCE(wd.open_jugar, 0). The
    sole consumers are resolve_weaving_open_jugar_for_row_query() (per written row
    + its single chain successor) and get_weaving_unit_open_jugar_query() (Sync).

    CHAIN ORDER (2026-09-16, TKT-000150/156): (tran_date, start time, spell_rank,
    weaving_daily_id) where start time = _chain_eff_start_sql. The legacy rank
    CASE survives ONLY as a tiebreak for spells sharing a start time (sls br87:
    A2 and B1 both 14:00), so those keep their historical order. For A/B/C
    tenants this replaces the old CASE (A, B -> 99, C -> 5) that sorted every day
    C, A, B.

    Requires aliases in scope: wd = jute_prod_weaving_daily row, sp = its spell_mst
    join. Probe B = latest SAME-DAY predecessor with earlier (start, rank, id);
    probe A = latest prior-day predecessor in the (co_id, machine_id,
    weaving_quality_id) partition; B else A else 0. Each probe COALESCEs
    close_jugar inside so an existing predecessor with NULL close_jugar yields 0
    (LAG + outer COALESCE semantics) and never falls through to an older row.
    Rides idx_wd_jugar_chain.
    """
    rk_sp, rk_ps = _spell_rank_case("sp"), _spell_rank_case("ps")
    eff_wd, eff_p = _chain_eff_start_sql("wd", "sp"), _chain_eff_start_sql("p", "ps")
    return f"""COALESCE(
                                (SELECT COALESCE(p.close_jugar, 0)
                                   FROM jute_prod_weaving_daily p
                                   LEFT JOIN spell_mst ps ON ps.spell_id = p.spell_id
                                  WHERE p.active = 1
                                    AND p.co_id = wd.co_id
                                    AND p.machine_id = wd.machine_id
                                    AND p.tran_date = wd.tran_date
                                    AND ({eff_p}, {rk_ps}, p.weaving_daily_id)
                                        < ({eff_wd}, {rk_sp}, wd.weaving_daily_id)
                                  ORDER BY {eff_p} DESC, {rk_ps} DESC, p.weaving_daily_id DESC
                                  LIMIT 1),
                                (SELECT COALESCE(p.close_jugar, 0)
                                   FROM jute_prod_weaving_daily p
                                   LEFT JOIN spell_mst ps ON ps.spell_id = p.spell_id
                                  WHERE p.active = 1
                                    AND p.co_id = wd.co_id
                                    AND p.machine_id = wd.machine_id
                                    AND p.tran_date < wd.tran_date
                                    AND p.tran_date >= DATE_SUB(wd.tran_date, INTERVAL {WEAVING_CHAIN_MAX_GAP_DAYS} DAY)
                                  ORDER BY p.tran_date DESC, {eff_p} DESC, {rk_ps} DESC, p.weaving_daily_id DESC
                                  LIMIT 1),
                                0)"""


def weaving_day_slice_sql(include_branch_filter: bool = True) -> str:
    """Day-scoped SELECT fragment computing vw_weaving_daily's columns for ONE day.

    EXECUTION PATH for both weaving read queries (entries-by-date grid + planning-grid
    driver). vw_weaving_daily (dbqueries/migrations/alter_weaving_daily_lean_and_view.sql,
    revised total_jugar model 2026-06-30) is demoted to REFERENCE ORACLE — its formula
    chain is ported here byte-for-byte (every COALESCE / guard / ROUND digit), but the
    base scan filters jute_prod_weaving_daily down to the requested (co_id, tran_date)
    FIRST and only then computes; the view re-materializes the LAG/probe chain over FULL
    history on every read. Parity: dbqueries/verify_weaving_dayslice_parity.py.

    Differences in FORM (not semantics) vs the view:

    * open_jugar — STORED (Phase 1b, 2026-07-07). The carry-forward is persisted on
      jute_prod_weaving_daily.open_jugar: resolved at WRITE time by the weaving_entry
      writers (which also repair the single chain successor in the same transaction,
      via _open_jugar_probe_sql / the chain-successor query below) and backfilled
      from the view's LAG ordering by
      dbqueries/migrations/backfill_weaving_open_jugar.sql (rerunnable — re-run after
      any bulk import that bypasses the app writers). The slice reads
      COALESCE(wd.open_jugar, 0) — NO chain probe on the read path. Weekly drift
      check: dbqueries/check_weaving_open_jugar_parity.sql.
    * SET-BASED PROBES (2026-07-07) — the view's remaining correlated probes are
      batched into once-per-request derived tables LEFT JOINed to the day's rows:
      identical values, O(1) probe executions instead of O(rows).

      - tms: machine speed standards (id_type='mcid', param='speed'). ROW_NUMBER()
        OVER (PARTITION BY ref_id, value_role ORDER BY effective_date DESC,
        weaving_target_map_id DESC), rn=1 pivoted to std/act columns — the exact
        last-date + newest-id tiebreak of the view's per-row ORDER BY ... LIMIT 1
        probes. wd.tran_date always equals :tran_date inside the slice, so
        effective_date <= :tran_date is the per-row predicate verbatim.
      - tmq: quality eff standards (id_type='qid', param='eff', roles
        standard/target), same ranking, pivoted to std_eff_raw/target_eff_raw.
      - pks: exact-day pick average — AVG(picks) GROUP BY quality over active
        jute_sqc_weaving_pick rows at entry_date = :tran_date (std_picks, R-08-21
        exact-day-or-zero: no reading that day => NULL => COALESCE 0 upstream).
      - pka: last-date pick average — AVG(picks) at each quality's
        MAX(entry_date) <= :tran_date (act_picks; vw_weaving_pick_act's
        latest-day probe, flattened onto the base table).
      - stp: stoppage SUM(stoppage_hours) GROUP BY machine, spell for the day.
      - att: Σ net weaver attendance hours (working_hours - idle_hours, per-row
        floored at 0) GROUP BY machine, spell — daily_ebmc_attendance marks
        joined to the daily_attendance header, weaver-prefix gated like the
        eb stamp (2026-08-26). working_hours =
        GREATEST(0, LEAST(spell hours, att_hours) - stoppage); no weaver marked
        on the loom => 0 loom hours => std_prod_yds/efficiency 0.

      A missing derived-table row LEFT JOINs to NULL exactly like a no-row
      correlated probe, so every COALESCE / CASE in the layers above r is the
      view's expression, verbatim.
    * an extra raw layer (r) captures each resolved value once so the COALESCE /
      eff_speed / eff_picks CASEs above it stay the view's expressions, verbatim.

    Binds: :co_id, :tran_date (required); :spell_id, :machine_id, :branch_id (optional,
    NULL = no filter; branch is NULL-tolerant like the entries grid). Output columns =
    vw_weaving_daily's outer select list, same aliases, same ROUND precision.

    include_branch_filter=False omits the :branch_id predicate from the base scan —
    used by the plan driver, whose OLD view join was branch-UNfiltered on the view
    side (branch scope is driver-side d.branch_id only). Keeping the slice
    branch-free there preserves exact pre-rewrite semantics when a daily row's
    stored branch_id disagrees with the machine's dept branch (reviewer finding).
    """
    rank_sp = _spell_rank_case("sp")
    branch_pred = (
        "AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)"
        if include_branch_filter
        else ""
    )
    return f"""
        SELECT
            c.weaving_daily_id, c.co_id, c.branch_id, c.tran_date,
            c.spell_id, c.spell_code, c.shift_bucket, c.spell_rank,
            c.machine_id, c.mech_code, c.machine_name, c.line_no,
            c.weaving_quality_id, c.item_id, c.item_code, c.item_name,
            c.weaving_quality_code, c.weaving_quality_name, c.is_composite,
            c.eb_id, c.beam_no,
            c.cuts, c.close_jugar, c.less_production,
            c.finished_length, c.ozs_yds, c.std_ozs_yds, c.no_of_jugar_per_cut,
            c.std_speed, c.act_speed, c.std_picks, c.act_picks, c.std_eff, c.target_eff,
            c.eff_speed, c.eff_picks, c.working_hours,
            c.open_jugar, c.jugar,
            ROUND(c.production_yds, 3)                                            AS production_yds,
            ROUND(c.production_yds * c.ozs_yds * 28.35 / 1000, 3)                 AS production_kg,
            ROUND(c.production_yds * c.ozs_yds * 28.35 / 1000 / 1000, 4)          AS production_mt,
            ROUND(c.std_prod_yds, 3)                                              AS std_prod_yds,
            ROUND(CASE WHEN c.target_eff > 0 THEN c.std_prod_yds * c.target_eff / 100 ELSE 0 END, 3) AS target_prod_yds,
            ROUND(CASE WHEN c.std_prod_yds > 0 THEN c.production_yds * 100 / c.std_prod_yds ELSE 0 END, 2) AS efficiency,
            ROUND(CASE WHEN c.std_ozs_yds IS NOT NULL THEN c.production_yds * c.std_ozs_yds * 28.35 / 1000 ELSE 0 END, 3) AS std_prod_kg,
            ROUND(CASE WHEN c.std_ozs_yds IS NOT NULL AND c.target_eff > 0
                       THEN c.production_yds * c.std_ozs_yds * 28.35 / 1000 * (c.target_eff / 100) ELSE 0 END, 3) AS target_kg
        FROM (
            SELECT
                b.*,
                b.total_jugar AS jugar,
                CASE WHEN b.no_of_jugar_per_cut > 0
                     THEN b.total_jugar * b.finished_length / b.no_of_jugar_per_cut
                     ELSE 0 END AS production_yds,
                CASE WHEN (36 * b.std_picks) > 0
                     THEN (b.eff_speed * b.working_hours * 60) / (36 * b.std_picks)
                     ELSE 0 END AS std_prod_yds
            FROM (
                SELECT
                    a.*,
                    (a.cuts * a.no_of_jugar_per_cut + a.close_jugar - a.open_jugar
                     - COALESCE(a.less_production, 0)) AS total_jugar
                FROM (
                    SELECT
                        r.*,
                        COALESCE(r.std_speed_raw, 0)   AS std_speed,
                        COALESCE(r.act_speed_raw, 0)   AS act_speed,
                        COALESCE(r.std_picks_raw, 0)   AS std_picks,
                        COALESCE(r.act_picks_raw, 0)   AS act_picks,
                        COALESCE(r.std_eff_raw, 0)     AS std_eff,
                        COALESCE(r.target_eff_raw, 0)  AS target_eff,
                        CASE WHEN COALESCE(r.act_speed_raw, 0) > 0 THEN r.act_speed_raw ELSE COALESCE(r.std_speed_raw, 0) END AS eff_speed,
                        CASE WHEN COALESCE(r.act_picks_raw, 0) > 0 THEN r.act_picks_raw ELSE COALESCE(r.std_picks_raw, 0) END AS eff_picks
                    FROM (
                        SELECT
                            wd.weaving_daily_id, wd.co_id, wd.branch_id, wd.tran_date,
                            wd.spell_id, sp.spell_code, LEFT(sp.spell_code, 1) AS shift_bucket,
                            {rank_sp} AS spell_rank,
                            wd.machine_id, m.mech_code, m.machine_name, m.line_no,
                            wd.weaving_quality_id AS weaving_quality_id,
                            q.item_id, im.item_code, im.item_name,
                            q.weaving_quality_code, q.weaving_quality_name, q.is_composite,
                            wd.eb_id, wd.beam_no,
                            wd.cuts,
                            COALESCE(wd.close_jugar, 0)                AS close_jugar,
                            COALESCE(wd.less_production, 0)            AS less_production,
                            COALESCE(q.finished_length, 0)            AS finished_length,
                            COALESCE(q.ozs_yds, 0)                     AS ozs_yds,
                            q.std_ozs_yds,
                            COALESCE(q.no_of_jugar_per_cut, 0)        AS no_of_jugar_per_cut,
                            tms.std_speed_raw,
                            tms.act_speed_raw,
                            -- std_picks = exact-day SQC quality-average picks (R-08-21):
                            -- entry_date = tran_date EXACTLY (no last-date carry, no
                            -- target-map fallback): no SQC reading that day => NULL =>
                            -- std_prod_yds/efficiency 0. Batched in derived table pks.
                            pks.std_picks_raw,
                            pka.act_picks_raw,
                            tmq.std_eff_raw,
                            tmq.target_eff_raw,
                            -- loom hours (2026-08-26): min(spell hours, Σ weaver
                            -- net attendance hours) - stoppage. No weaver marked
                            -- on the loom => att_hours NULL => 0 loom hours =>
                            -- std_prod_yds/efficiency 0 (locked decision).
                            GREATEST(0, LEAST(COALESCE(sp.working_hours, 0),
                                              COALESCE(att.att_hours, 0))
                                        - COALESCE(stp.stoppage_hours, 0)) AS working_hours,
                            -- open_jugar: STORED column (Phase 1b) — write-time resolved
                            -- + successor-repaired by weaving_entry, LAG-backfilled.
                            COALESCE(wd.open_jugar, 0) AS open_jugar
                        FROM jute_prod_weaving_daily wd
                        LEFT JOIN spell_mst sp ON sp.spell_id = wd.spell_id
                        LEFT JOIN machine_mst m ON m.machine_id = wd.machine_id
                        LEFT JOIN jute_prod_weaving_quality q
                               ON q.weaving_quality_id = wd.weaving_quality_id
                        LEFT JOIN item_mst im ON im.item_id = q.item_id
                        LEFT JOIN (
                            SELECT t.ref_id,
                                   MAX(CASE WHEN t.value_role = 'standard' THEN t.value END) AS std_speed_raw,
                                   MAX(CASE WHEN t.value_role = 'actual'   THEN t.value END) AS act_speed_raw
                            FROM (
                                SELECT tm.ref_id, tm.value_role, tm.value,
                                       ROW_NUMBER() OVER (
                                           PARTITION BY tm.ref_id, tm.value_role
                                           ORDER BY tm.effective_date DESC, tm.weaving_target_map_id DESC
                                       ) AS rn
                                FROM jute_prod_weaving_target_map tm
                                WHERE tm.co_id = :co_id AND tm.id_type = 'mcid' AND tm.param = 'speed'
                                  AND tm.value_role IN ('standard', 'actual')
                                  AND tm.active = 1 AND tm.effective_date <= :tran_date
                            ) t
                            WHERE t.rn = 1
                            GROUP BY t.ref_id
                        ) tms ON tms.ref_id = wd.machine_id
                        LEFT JOIN (
                            SELECT t.ref_id,
                                   MAX(CASE WHEN t.value_role = 'standard' THEN t.value END) AS std_eff_raw,
                                   MAX(CASE WHEN t.value_role = 'target'   THEN t.value END) AS target_eff_raw
                            FROM (
                                SELECT tm.ref_id, tm.value_role, tm.value,
                                       ROW_NUMBER() OVER (
                                           PARTITION BY tm.ref_id, tm.value_role
                                           ORDER BY tm.effective_date DESC, tm.weaving_target_map_id DESC
                                       ) AS rn
                                FROM jute_prod_weaving_target_map tm
                                WHERE tm.co_id = :co_id AND tm.id_type = 'qid' AND tm.param = 'eff'
                                  AND tm.value_role IN ('standard', 'target')
                                  AND tm.active = 1 AND tm.effective_date <= :tran_date
                            ) t
                            WHERE t.rn = 1
                            GROUP BY t.ref_id
                        ) tmq ON tmq.ref_id = wd.weaving_quality_id
                        LEFT JOIN (
                            SELECT p.weaving_quality_id, AVG(p.picks) AS std_picks_raw
                            FROM jute_sqc_weaving_pick p
                            WHERE p.active = 1 AND p.co_id = :co_id AND p.entry_date = :tran_date
                            GROUP BY p.weaving_quality_id
                        ) pks ON pks.weaving_quality_id = wd.weaving_quality_id
                        LEFT JOIN (
                            -- picks IS NOT NULL both levels: width-only readings
                            -- (2026-08-24) must neither pick the last date nor
                            -- dilute its AVG
                            SELECT p.weaving_quality_id, AVG(p.picks) AS act_picks_raw
                            FROM jute_sqc_weaving_pick p
                            INNER JOIN (
                                SELECT weaving_quality_id, MAX(entry_date) AS entry_date
                                FROM jute_sqc_weaving_pick
                                WHERE active = 1 AND co_id = :co_id AND entry_date <= :tran_date
                                  AND picks IS NOT NULL
                                GROUP BY weaving_quality_id
                            ) mx ON mx.weaving_quality_id = p.weaving_quality_id
                                AND mx.entry_date = p.entry_date
                            WHERE p.active = 1 AND p.co_id = :co_id AND p.picks IS NOT NULL
                            GROUP BY p.weaving_quality_id
                        ) pka ON pka.weaving_quality_id = wd.weaving_quality_id
                        LEFT JOIN (
                            SELECT st.machine_id, st.spell_id, SUM(st.stoppage_hours) AS stoppage_hours
                            FROM jute_prod_stoppage_hours st
                            WHERE st.active = 1 AND st.co_id = :co_id AND st.tran_date = :tran_date
                            GROUP BY st.machine_id, st.spell_id
                        ) stp ON stp.machine_id = wd.machine_id AND stp.spell_id = wd.spell_id
                        LEFT JOIN (
                            -- att: Σ net weaver attendance hours per (loom, spell)
                            -- for the day. Predicates mirror
                            -- get_weaving_operator_candidates_query (the eb-stamp
                            -- join): header date/spell/is_active, eb_id NOT NULL,
                            -- designation from the HEADER, weaver-prefix gate.
                            -- DISTINCT daily_atten_id first so duplicate machine
                            -- marks of the same attendance row never double-count.
                            -- daily_attendance has no co_id — the machine join
                            -- scopes it; per-row cap at 0 so a data glitch
                            -- (idle > working) can't eat a co-worker's hours.
                            SELECT t.mc_id, t.spell_id,
                                   SUM(GREATEST(0, COALESCE(t.working_hours, 0)
                                                   - COALESCE(t.idle_hours, 0))) AS att_hours
                            FROM (
                                SELECT DISTINCT dea.mc_id, da.daily_atten_id,
                                                da.spell_id, da.working_hours, da.idle_hours
                                FROM daily_attendance da
                                STRAIGHT_JOIN daily_ebmc_attendance dea
                                        ON dea.daily_atten_id = da.daily_atten_id
                                       AND dea.is_active = 1
                                LEFT JOIN designation_mst dm
                                        ON dm.designation_id = da.worked_designation_id
                                WHERE da.is_active = 1
                                  AND da.attendance_date = :tran_date
                                  AND da.eb_id IS NOT NULL
                                  AND LOWER(COALESCE(dm.desig, '')) LIKE 'weaver%'
                            ) t
                            GROUP BY t.mc_id, t.spell_id
                        ) att ON att.mc_id = wd.machine_id AND att.spell_id = wd.spell_id
                        WHERE wd.active = 1
                          AND wd.co_id = :co_id
                          AND wd.tran_date = :tran_date
                          AND (:spell_id IS NULL OR wd.spell_id = :spell_id)
                          AND (:machine_id IS NULL OR wd.machine_id = :machine_id)
                          {branch_pred}
                    ) r
                ) a
            ) b
        ) c
"""


def get_weaving_entries_by_date_query():
    """Weaving-daily entries for the day grid — computed by the day-slice SQL.

    EXECUTION PATH = weaving_day_slice_sql() (day-scoped compute): the daily table
    stores INPUTS ONLY; every derived column (open_jugar, jugar, finished_length/
    ozs_yds/std_ozs_yds/no_of_jugar_per_cut, std/act speed+picks, std/target eff,
    working_hours, production_yds/kg/mt, std_prod_yds, target_prod_yds, efficiency,
    std_prod_kg, target_kg) is computed on read by the day-slice fragment, which
    filters jute_prod_weaving_daily to the requested (co_id, tran_date) FIRST and only
    then runs the formula chain. vw_weaving_daily is now REFERENCE/ORACLE only (parity:
    dbqueries/verify_weaving_dayslice_parity.py) — selecting from it re-materialized
    full history per request. The slice surfaces spell_code, mech_code/machine_name/
    line_no and the item/quality labels, so this just projects + orders. spell_id,
    machine_id, branch_id optional (filters applied inside the slice).
    """
    return text(
        """
        SELECT
            v.weaving_daily_id,
            v.co_id,
            v.branch_id,
            v.tran_date,
            v.spell_id,
            v.spell_code,
            v.machine_id,
            v.mech_code,
            v.machine_name,
            v.line_no,
            v.weaving_quality_id,
            v.item_id,
            v.item_code,
            v.item_name,
            v.weaving_quality_code,
            v.weaving_quality_name,
            v.is_composite,
            v.eb_id,
            v.beam_no,
            v.cuts,
            v.close_jugar,
            v.less_production,
            v.open_jugar,
            v.jugar,
            v.finished_length,
            v.ozs_yds,
            v.std_ozs_yds,
            v.no_of_jugar_per_cut,
            v.std_speed,
            v.act_speed,
            v.std_picks,
            v.act_picks,
            v.std_eff,
            v.target_eff,
            v.eff_speed,
            v.eff_picks,
            v.working_hours,
            v.production_yds,
            v.production_kg,
            v.production_mt,
            v.std_prod_yds,
            v.target_prod_yds,
            v.efficiency,
            v.std_prod_kg,
            v.target_kg
        FROM (
"""
        + weaving_day_slice_sql()
        + """
        ) v
        ORDER BY v.mech_code, v.weaving_daily_id DESC
        """
    )


def get_weaving_entry_inputs_query():
    """Entry-tab day rows STRAIGHT from jute_prod_weaving_daily — inputs + open_jugar + jpc.

    The Production Entry grid needs only the stored inputs (cuts, close_jugar,
    less_production), the STORED open_jugar (Phase 1b — write-time resolved, no
    chain probe here anymore), and the mapped quality's construction attrs
    (no_of_jugar_per_cut AS jpc, finished_length AS fl — the FE preview field
    names) — NONE of the standards / pick-SQC / stoppage joins the full day-slice
    pays; use /entries_by_date when computed production/std/efficiency columns are
    needed. Same binds and filters as the day-slice (branch NULL-tolerant).
    """
    return text(
        """
        SELECT
            wd.weaving_daily_id,
            wd.co_id,
            wd.branch_id,
            wd.tran_date,
            wd.spell_id,
            sp.spell_code,
            wd.machine_id,
            m.mech_code,
            m.machine_name,
            wd.weaving_quality_id,
            q.weaving_quality_code,
            q.weaving_quality_name,
            wd.beam_no,
            wd.cuts,
            COALESCE(wd.close_jugar, 0)         AS close_jugar,
            COALESCE(wd.less_production, 0)     AS less_production,
            COALESCE(q.no_of_jugar_per_cut, 0)  AS jpc,
            COALESCE(q.finished_length, 0)      AS fl,
            COALESCE(q.ozs_yds, 0)              AS ozs_yds,
            COALESCE(wd.open_jugar, 0)          AS open_jugar
        FROM jute_prod_weaving_daily wd
        LEFT JOIN spell_mst sp ON sp.spell_id = wd.spell_id
        LEFT JOIN machine_mst m ON m.machine_id = wd.machine_id
        LEFT JOIN jute_prod_weaving_quality q
               ON q.weaving_quality_id = wd.weaving_quality_id
        WHERE wd.active = 1
          AND wd.co_id = :co_id
          AND wd.tran_date = :tran_date
          AND (:spell_id IS NULL OR wd.spell_id = :spell_id)
          AND (:machine_id IS NULL OR wd.machine_id = :machine_id)
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
        ORDER BY m.mech_code, wd.weaving_daily_id DESC
        """
    )


def get_weaving_plan_driver_query():
    """Driver rows for the planning grid: active jute_prod_weaving_quality_map rows
    LEFT JOIN the day-slice derived table (weaving_day_slice_sql()).

    The grid is DRIVEN by the Loom->Quality map (mapped looms, even with no entry yet) —
    only rows WHERE active AND weaving_quality_id IS NOT NULL participate (an unmapped
    loom has nothing to plan). Each driver row LEFT JOINs the day-slice on
    (spell_id, machine_id, weaving_quality_id); the :co_id/:tran_date predicates live
    INSIDE the derived table (day filter FIRST, then compute) and the outer WHERE pins
    qm to the same binds, so the join grain is unchanged from the old view join on
    (co_id, tran_date, spell_id, machine_id, weaving_quality_id). A saved daily entry
    contributes its INPUTS (cuts, close_jugar, less_production) and EVERY computed
    column (open_jugar, jugar, production_yds/kg/mt, std_prod_yds, target_prod_yds,
    efficiency, std_prod_kg, target_kg, eff_speed/eff_picks, working_hours). A mapped
    loom with no entry yet keeps NULL slice columns (the router coalesces to 0). The map
    still supplies the construction attrs from the quality master so an empty cell can
    show finished_length/ozs_yds/no_of_jugar_per_cut. vw_weaving_daily is now
    REFERENCE/ORACLE only (parity: dbqueries/verify_weaving_dayslice_parity.py) —
    joining it re-materialized full history per request (504). Branch filtering stays
    driver-side (d.branch_id) ONLY — the slice is built with
    include_branch_filter=False so the join side is branch-UNfiltered exactly like
    the old view join (a daily row whose stored branch_id disagrees with the
    machine's dept branch still shows its numbers). spell_id, machine_id optional;
    looms resolved by NAME 'Loom' (:loom_type).
    """
    return text(
        """
        SELECT
            qm.weaving_quality_map_id,
            qm.machine_id,
            qm.spell_id,
            qm.weaving_quality_id,
            q.item_id,
            m.mech_code,
            m.machine_name,
            m.line_no,
            d.branch_id,
            sp.spell_code,
            sp.working_hours AS spell_working_hours,
            im.item_code,
            im.item_name,
            q.weaving_quality_code,
            q.weaving_quality_name,
            q.ends,
            q.finished_length,
            q.ozs_yds,
            q.std_ozs_yds,
            q.no_of_jugar_per_cut,
            q.is_composite,
            v.weaving_daily_id,
            v.eb_id,
            v.beam_no,
            v.cuts,
            v.close_jugar,
            v.less_production,
            v.open_jugar,
            v.jugar,
            v.std_speed,
            v.act_speed,
            v.std_picks,
            v.act_picks,
            v.std_eff,
            v.target_eff,
            v.eff_speed,
            v.eff_picks,
            v.working_hours,
            v.production_yds,
            v.production_kg,
            v.production_mt,
            v.std_prod_yds,
            v.target_prod_yds,
            v.efficiency,
            v.std_prod_kg,
            v.target_kg
        FROM jute_prod_weaving_quality_map qm
        INNER JOIN machine_mst m ON m.machine_id = qm.machine_id
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN (
            SELECT spell_id, spell_code, working_hours
            FROM spell_mst WHERE status = 1
        ) sp ON sp.spell_id = qm.spell_id
        LEFT JOIN jute_prod_weaving_quality q ON q.weaving_quality_id = qm.weaving_quality_id
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        LEFT JOIN ( -- TRIPWIRE: :co_id/:tran_date must stay INSIDE this derived table; moving them to the outer WHERE re-materializes full history (504).
"""
        + weaving_day_slice_sql(include_branch_filter=False)
        + """
        ) v ON v.spell_id = qm.spell_id
           AND v.machine_id = qm.machine_id
           AND v.weaving_quality_id = qm.weaving_quality_id
        WHERE qm.co_id = :co_id
          AND qm.active = 1
          AND qm.tran_date = :tran_date
          AND qm.weaving_quality_id IS NOT NULL
          AND mt.active = 1
          AND mt.machine_type_name = :loom_type
          AND (:spell_id IS NULL OR qm.spell_id = :spell_id)
          AND (:machine_id IS NULL OR qm.machine_id = :machine_id)
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY m.mech_code, qm.spell_id
        """
    )


def resolve_weaving_open_jugar_for_row_query():
    """The correct open_jugar for ONE existing daily row (write-time resolution).

    Runs the shared two-probe predecessor expression (_open_jugar_probe_sql —
    identical semantics to the view's LAG carry-forward) against the row's OWN
    stored identity, so the writers can persist open_jugar right after any
    insert/update. Both probes exclude the row itself (strict < on the chain key),
    so the result is valid whether the row was just inserted or edited in place.
    Returns one row (open_jugar) or none when :row_id does not exist.
    """
    open_jugar = _open_jugar_probe_sql()
    return text(
        f"""
        SELECT {open_jugar} AS open_jugar
        FROM jute_prod_weaving_daily wd
        LEFT JOIN spell_mst sp ON sp.spell_id = wd.spell_id
        WHERE wd.weaving_daily_id = :row_id
        """
    )


def get_weaving_chain_successor_query():
    """The IMMEDIATE successor of one chain position -- the single row whose
    open_jugar depends on the written/deleted row (write-time chain repair).

    Identity is passed as binds (NOT read from the row) so the caller can probe
    the row's OLD position after an identity-changing edit. Chain order mirrors
    _open_jugar_probe_sql forwards: (tran_date, start time, spell_rank,
    weaving_daily_id) with start time = tran_date + starting_time (2026-09-16
    ruling). The position's own start / rank resolve from :spell_id via a LEFT
    JOIN on spell_mst, so a vanished spell row degrades to (23:59:59, 99) exactly
    like the probe. Probe B = earliest SAME-DAY row after the position; probe A =
    earliest row on any LATER day; successor = B else A else NULL. Rides
    idx_wd_jugar_chain. Binds: :co_id, :machine_id, :weaving_quality_id,
    :tran_date, :spell_id, :row_id.
    """
    rk_ps = _spell_rank_case("ps")
    rk_pos = _spell_rank_case("sp2")
    eff_s = _chain_eff_start_sql("s", "ps")
    return text(
        f"""
        SELECT COALESCE(
            (SELECT s.weaving_daily_id
               FROM jute_prod_weaving_daily s
               LEFT JOIN spell_mst ps ON ps.spell_id = s.spell_id
              WHERE s.active = 1
                AND s.co_id = :co_id
                AND s.machine_id = :machine_id
                AND s.tran_date = :tran_date
                AND ({eff_s}, {rk_ps}, s.weaving_daily_id)
                    > (pos.eff_start, pos.rk, :row_id)
              ORDER BY {eff_s} ASC, {rk_ps} ASC, s.weaving_daily_id ASC
              LIMIT 1),
            (SELECT s.weaving_daily_id
               FROM jute_prod_weaving_daily s
               LEFT JOIN spell_mst ps ON ps.spell_id = s.spell_id
              WHERE s.active = 1
                AND s.co_id = :co_id
                AND s.machine_id = :machine_id
                AND s.tran_date > :tran_date
                AND s.tran_date <= DATE_ADD(:tran_date, INTERVAL {WEAVING_CHAIN_MAX_GAP_DAYS} DAY)
              ORDER BY s.tran_date ASC, {eff_s} ASC, {rk_ps} ASC, s.weaving_daily_id ASC
              LIMIT 1)
        ) AS successor_id
        FROM (
            SELECT TIMESTAMP(:tran_date, COALESCE(sp2.starting_time, '23:59:59')) AS eff_start,
                   {rk_pos}                                                     AS rk
              FROM (SELECT 1 AS one) one
              LEFT JOIN spell_mst sp2 ON sp2.spell_id = :spell_id
        ) pos
        """
    )


def update_weaving_daily_open_jugar_query():
    """Set ONLY the stored open_jugar on one daily row (write-time chain repair).

    Deliberately does NOT stamp updated_by/updated_date_time: open_jugar is derived
    data — repairing a successor row must not masquerade as a user edit of it.
    """
    return text(
        """
        UPDATE jute_prod_weaving_daily
        SET open_jugar = :open_jugar
        WHERE weaving_daily_id = :row_id
        """
    )


def get_weaving_daily_active_row_query():
    """Active weaving_daily row id for the entry/plan grain (upsert lookup).

    App-uniqueness: (co_id, tran_date, spell_id, machine_id, weaving_quality_id,
    active=1).
    """
    return text(
        """
        SELECT weaving_daily_id
        FROM jute_prod_weaving_daily
        WHERE co_id = :co_id
          AND tran_date = :tran_date
          AND spell_id = :spell_id
          AND machine_id = :machine_id
          AND weaving_quality_id = :weaving_quality_id
          AND active = 1
        ORDER BY weaving_daily_id DESC
        LIMIT 1
        """
    )


def insert_weaving_daily_query():
    """Insert one per-loom/per-quality/per-spell production entry — INPUTS ONLY.

    STORAGE MODEL = FREEZE NOTHING + day-slice (2026-06-24, revised Phase 1b
    2026-07-07): the table stores identity + operator inputs (cuts, close_jugar,
    less_production); jugar and every resolved-standard / computed output are
    recomputed on read. EXCEPTION: open_jugar is STORED — it is NOT in this INSERT
    (the writer resolves it right after, via resolve_weaving_open_jugar_for_row_query
    + update_weaving_daily_open_jugar_query, and repairs the chain successor in the
    same transaction). close_jugar is the operator's closing-jugar reading
    (0 <= cj <= jc, enforced in the router). No created_* — trigger-based audit.
    """
    return text(
        """
        INSERT INTO jute_prod_weaving_daily
            (co_id, branch_id, tran_date, spell_id, machine_id, weaving_quality_id,
             eb_id, beam_no, cuts, close_jugar, less_production, active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :spell_id, :machine_id, :weaving_quality_id,
             :eb_id, :beam_no, :cuts, :close_jugar, :less_production, 1, :updated_by)
        """
    )


def update_weaving_daily_query():
    """Update one per-loom/per-quality/per-spell production entry by id — INPUTS ONLY.

    Only identity (branch/quality/eb/beam) + the operator inputs (cuts, close_jugar,
    less_production) are updatable; derived columns compute on read (day-slice),
    except the stored open_jugar which the writer re-resolves right after this
    UPDATE (see resolve_weaving_open_jugar_for_row_query).
    """
    return text(
        """
        UPDATE jute_prod_weaving_daily
        SET branch_id = :branch_id,
            weaving_quality_id = :weaving_quality_id,
            eb_id = :eb_id,
            beam_no = :beam_no,
            cuts = :cuts,
            close_jugar = :close_jugar,
            less_production = :less_production,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_daily_id = :id
        """
    )


def update_weaving_daily_less_production_query():
    """Set ONLY less_production (reduce-jugar) on one daily row, by weaving_daily_id.

    Used by the Production Adjustment tab so an adjustment never disturbs the entry inputs
    (cuts/close_jugar/quality stay exactly as the operator saved them); the view re-derives
    production_yds from the new less_production. Stamps who/when.
    """
    return text(
        """
        UPDATE jute_prod_weaving_daily
        SET less_production = :less_production,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_daily_id = :id
        """
    )


def soft_delete_weaving_daily_query():
    """Soft-delete (active=NULL) one weaving-daily row by id.

    NULL, not 0: uq_weaving_daily_unit_machine spans (co_id, tran_date, spell_id,
    machine_id, active) and MySQL unique keys never collide on NULL, so deleted
    rows may repeat while a second active=1 row per unit+loom is rejected.
    """
    return text(
        """
        UPDATE jute_prod_weaving_daily
        SET active = NULL,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_daily_id = :id
        """
    )


# NOTE (FREEZE NOTHING + VIEW, 2026-06-24): get_weaving_idle_hours_query and
# get_weaving_prev_close_jugar_query were DELETED. Net working_hours (gross spell hours
# minus stoppage) is computed on read (day-slice stoppage join). open_jugar came back
# as a STORED column in Phase 1b (2026-07-07): the writers resolve it per written row
# and repair the single chain successor in the same transaction (see
# resolve_weaving_open_jugar_for_row_query / get_weaving_chain_successor_query /
# update_weaving_daily_open_jugar_query above).


# =============================================================================
# PAGE C tab — Loom -> Quality map (jute_prod_weaving_quality_map)
# Clone of spinning_query's frame-map (daily_doff_frames_winding S-rows): one ACTIVE
# row per (tran_date, spell_id, machine_id). Production inherits quality from here.
# =============================================================================


def get_weaving_quality_map_query():
    """All Loom-type machines with today's SAVED Loom->Quality mapping + a carry-forward
    draft (the loom's most-recent saved mapping across ANY spell/date) as prev_quality_*.

    Clone of spinning_query.get_frame_map_query, adapted to the dedicated
    jute_prod_weaving_quality_map table (machine_id, not mc_eb_id). Looms resolved by
    NAME 'Loom' (:loom_type).

    weaving_quality_id/weaving_quality_code/weaving_quality_name
        = today's SAVED mapping for this (spell_id, tran_date) — NULL when nothing saved.
    prev_quality_id/prev_quality_code/prev_quality_name/prev_date
        = the loom's most-recent saved mapping across ANY spell/date, EXCLUDING the
          current (tran_date, spell_id) cell. Read O(1) per loom from the maintained
          weaving_loom_current_quality pointer (a CASE nulls it out when the pointer IS
          today's own cell). Surfaced so the client can prefill the dropdown and flag it
          unsaved until the operator clicks Save Map — lets a never-mapped spell bootstrap
          from the latest prior setup. quality_map_save keeps the pointer in sync.
    """
    return text(
        """
        SELECT
            m.machine_id,
            m.mech_code,
            m.mech_posting_code,
            m.machine_name,
            m.line_no,
            d.branch_id,
            qm.weaving_quality_map_id,
            qm.weaving_quality_id,
            q.weaving_quality_code,
            q.weaving_quality_name,
            q.no_of_jugar_per_cut,
            -- Carry-forward (prev_quality_*): the loom's most-recent PRIOR mapping, read O(1)
            -- from the maintained weaving_loom_current_quality pointer (PK co_id,machine_id)
            -- instead of scanning the loom's ~2.9k-row history. The CASE excludes the pointer
            -- when it IS the cell being viewed (today's own saved mapping) -- exact parity with
            -- the old "EXCLUDING the current (tran_date, spell_id) cell". ~1ms/loom vs ~7s total.
            CASE WHEN cur.machine_id IS NOT NULL
                      AND NOT (cur.tran_date = :tran_date AND cur.spell_id = :spell_id)
                 THEN cur.weaving_quality_id END      AS prev_quality_id,
            CASE WHEN cur.machine_id IS NOT NULL
                      AND NOT (cur.tran_date = :tran_date AND cur.spell_id = :spell_id)
                 THEN curq.weaving_quality_code END   AS prev_quality_code,
            CASE WHEN cur.machine_id IS NOT NULL
                      AND NOT (cur.tran_date = :tran_date AND cur.spell_id = :spell_id)
                 THEN curq.weaving_quality_name END   AS prev_quality_name,
            CASE WHEN cur.machine_id IS NOT NULL
                      AND NOT (cur.tran_date = :tran_date AND cur.spell_id = :spell_id)
                 THEN cur.tran_date END               AS prev_date
        FROM machine_mst m
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN jute_prod_weaving_quality_map qm
               ON qm.machine_id = m.machine_id
              AND qm.co_id = :co_id
              AND qm.tran_date = :tran_date
              AND qm.spell_id = :spell_id
              AND qm.active = 1
        LEFT JOIN jute_prod_weaving_quality q ON q.weaving_quality_id = qm.weaving_quality_id
        LEFT JOIN weaving_loom_current_quality cur
               ON cur.co_id = :co_id AND cur.machine_id = m.machine_id
        LEFT JOIN jute_prod_weaving_quality curq ON curq.weaving_quality_id = cur.weaving_quality_id
        WHERE m.active = 1
          AND mt.active = 1
          AND mt.machine_type_name = :loom_type
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY m.mech_code
        """
    )


def upsert_weaving_current_quality_query():
    """Keep the weaving_loom_current_quality pointer at the loom's latest NON-NULL mapping.

    Called from quality_map_save for every saved entry whose quality is non-null. Guarded by
    tran_date so editing an OLDER date never clobbers a newer current pointer (>= so a re-save
    of the same day wins). A cleared mapping (quality NULL) is NOT synced here — the pointer
    keeps the last known non-null quality, matching the carry-forward's IS NOT NULL semantics.
    """
    return text(
        """
        INSERT INTO weaving_loom_current_quality
            (co_id, branch_id, machine_id, weaving_quality_id, tran_date, spell_id)
        VALUES
            (:co_id, :branch_id, :machine_id, :weaving_quality_id, :tran_date, :spell_id)
        ON DUPLICATE KEY UPDATE
            branch_id          = IF(VALUES(tran_date) >= tran_date, VALUES(branch_id), branch_id),
            weaving_quality_id = IF(VALUES(tran_date) >= tran_date, VALUES(weaving_quality_id), weaving_quality_id),
            spell_id           = IF(VALUES(tran_date) >= tran_date, VALUES(spell_id), spell_id),
            tran_date          = IF(VALUES(tran_date) >= tran_date, VALUES(tran_date), tran_date),
            -- Unconditional: backdated and no-op re-saves must still refresh the
            -- 'Last updated' caption, which now reads MAX(updated_date_time) here.
            -- Without this, an identical-value ODKU leaves the row (and ON UPDATE
            -- timestamp) untouched.
            updated_date_time  = CURRENT_TIMESTAMP
        """
    )


def get_weaving_quality_map_saved_query():
    """Loom->Quality grid WITHOUT the prev_quality_* carry-forward — the cheap read.

    Identical loom set + saved-mapping columns as get_weaving_quality_map_query, but
    drops the four correlated prev_quality_* subqueries (each a per-loom scan of the
    2M+ row map history). On sls that is the difference between ~20s and ~88ms. The
    Production Entry grid (WeavingEntryGrid) only needs the loom list + saved mapping;
    only the mapping EDITOR needs carry-forward, so it keeps get_weaving_quality_map_query.
    """
    return text(
        """
        SELECT
            m.machine_id,
            m.mech_code,
            m.mech_posting_code,
            m.machine_name,
            m.line_no,
            d.branch_id,
            qm.weaving_quality_map_id,
            qm.weaving_quality_id,
            q.weaving_quality_code,
            q.weaving_quality_name,
            q.no_of_jugar_per_cut
        FROM machine_mst m
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN jute_prod_weaving_quality_map qm
               ON qm.machine_id = m.machine_id
              AND qm.co_id = :co_id
              AND qm.tran_date = :tran_date
              AND qm.spell_id = :spell_id
              AND qm.active = 1
        LEFT JOIN jute_prod_weaving_quality q ON q.weaving_quality_id = qm.weaving_quality_id
        WHERE m.active = 1
          AND mt.active = 1
          AND mt.machine_type_name = :loom_type
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY m.mech_code
        """
    )


def get_weaving_quality_map_active_row_query():
    """The active map row id for one loom on a tran_date/spell_id (upsert lookup).

    One ACTIVE row per (co_id, tran_date, spell_id, machine_id). Newest id wins on ties.
    """
    return text(
        """
        SELECT weaving_quality_map_id
        FROM jute_prod_weaving_quality_map
        WHERE co_id = :co_id
          AND tran_date = :tran_date
          AND spell_id = :spell_id
          AND machine_id = :machine_id
          AND active = 1
        ORDER BY weaving_quality_map_id DESC
        LIMIT 1
        """
    )


def update_weaving_quality_map_row_query():
    """Update an existing active map row's quality assignment (stamps who/when)."""
    return text(
        """
        UPDATE jute_prod_weaving_quality_map
        SET weaving_quality_id = :weaving_quality_id,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_quality_map_id = :id
        """
    )


def insert_weaving_quality_map_row_query():
    """Insert a fresh active Loom->Quality map row (stamps who/when)."""
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


def get_weaving_daily_rows_to_restamp_query():
    """Active daily rows in one (co, date, spell, machine) cell whose STORED quality is
    NULL or differs from the map's new quality — quality_map_save re-stamps them.

    Capture BEFORE the map was saved stores weaving_quality_id NULL; a remap leaves the
    old quality frozen on the row. Either way the day-slice (which reads the stored
    column, no map coalesce) computes zero/wrong production. Returns the OLD quality too
    so the caller can repair the old jugar chain's successor."""
    return text(
        """
        SELECT weaving_daily_id, weaving_quality_id
        FROM jute_prod_weaving_daily
        WHERE co_id = :co_id AND tran_date = :tran_date AND spell_id = :spell_id
          AND machine_id = :machine_id AND active = 1
          AND (weaving_quality_id IS NULL
               OR weaving_quality_id <> :weaving_quality_id)
        """
    )


def update_weaving_daily_quality_stamp_query():
    """Re-stamp ONLY weaving_quality_id on one daily row (map-change propagation)."""
    return text(
        """
        UPDATE jute_prod_weaving_daily
        SET weaving_quality_id = :weaving_quality_id,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_daily_id = :id
        """
    )


def get_weaving_quality_map_last_updated_query():
    """The most recent save timestamp across a branch's Loom->Quality mapping.

    Surfaced in the Loom->Quality grid so the operator can see when the branch's mapping
    was last touched. Branch-scoped (NULL branch_id = whole tenant).

    Reads the maintained weaving_loom_current_quality pointer (one row per loom, PK
    co_id+machine_id) instead of MAX-scanning jute_prod_weaving_quality_map — the
    history table has no index ending in updated_date_time, so that MAX walked ~1.1M
    clustered-PK rows and took 8-13s on sls (TKT-000123). Pointer read: ~1ms.
    quality_map_save's upsert bumps the pointer's updated_date_time on every non-null
    save it touches; a save that only CLEARS mappings won't move the caption (the
    pointer deliberately keeps the last non-null quality).
    """
    return text(
        """
        SELECT MAX(updated_date_time) AS last_updated
        FROM weaving_loom_current_quality
        WHERE co_id = :co_id
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        """
    )


def get_weaving_adjustment_grid_query():
    """Production-Adjustment grid: every Loom with its mapped quality + that daily row's
    current less_production (reduce-jugar) for one (tran_date, spell_id).

    Drives the Production Adjustment tab. Looms resolved by NAME 'Loom' (:loom_type,
    case-insensitive) via machine_type_mst, branch-scoped and NULL-tolerant on :branch_id.
    LEFT JOINs the active Loom->Quality map (so an unmapped loom still appears with NULL
    quality) and the active daily row at the mapped quality, surfacing weaving_daily_id +
    COALESCE(less_production, 0) so the FE can patch only that one input. Newest cell wins
    via the upsert grain; machine_mst/machine_type_mst filter active=1.
    """
    return text(
        """
        SELECT
            m.machine_id, m.mech_code, m.mech_posting_code, m.machine_name, m.line_no,
            d.branch_id,
            qm.weaving_quality_id,
            q.weaving_quality_code, q.weaving_quality_name,
            wd.weaving_daily_id,
            COALESCE(wd.less_production, 0) AS less_production
        FROM machine_mst m
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN jute_prod_weaving_quality_map qm
               ON qm.machine_id = m.machine_id AND qm.co_id = :co_id
              AND qm.tran_date = :tran_date AND qm.spell_id = :spell_id AND qm.active = 1
        LEFT JOIN jute_prod_weaving_quality q ON q.weaving_quality_id = qm.weaving_quality_id
        LEFT JOIN jute_prod_weaving_daily wd
               ON wd.co_id = :co_id AND wd.tran_date = :tran_date AND wd.spell_id = :spell_id
              AND wd.machine_id = m.machine_id AND wd.weaving_quality_id = qm.weaving_quality_id
              AND wd.active = 1
        WHERE m.active = 1 AND mt.active = 1 AND mt.machine_type_name = :loom_type
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY m.mech_code
        """
    )


# =============================================================================
# PAGE C tab — Beam -> Loom map (jute_prod_weaving_beam_map)
# Beam change recorded per (tran_date, spell_id, machine_id); production beam_no is
# resolved from the LATEST beam-change for (loom, spell, date) (Q7).
# =============================================================================


def get_weaving_beam_map_query():
    """Latest beam per loom for a (tran_date, spell_id) — the Beam-Change tab grid.

    Returns every Loom-type machine with its most-recent active beam_no for the cell
    (correlated subquery: latest id among active rows for the loom/spell/date), so the
    client can show/edit the mounted beam. Looms resolved by NAME 'Loom' (:loom_type).
    Branch-scoped, NULL-tolerant.
    """
    return text(
        """
        SELECT
            m.machine_id,
            m.mech_code,
            m.machine_name,
            m.line_no,
            d.branch_id,
            (
                SELECT b.weaving_beam_map_id
                FROM jute_prod_weaving_beam_map b
                WHERE b.machine_id = m.machine_id
                  AND b.co_id = :co_id
                  AND b.tran_date = :tran_date
                  AND b.spell_id = :spell_id
                  AND b.active = 1
                ORDER BY b.weaving_beam_map_id DESC
                LIMIT 1
            ) AS weaving_beam_map_id,
            (
                SELECT b.beam_no
                FROM jute_prod_weaving_beam_map b
                WHERE b.machine_id = m.machine_id
                  AND b.co_id = :co_id
                  AND b.tran_date = :tran_date
                  AND b.spell_id = :spell_id
                  AND b.active = 1
                ORDER BY b.weaving_beam_map_id DESC
                LIMIT 1
            ) AS beam_no
        FROM machine_mst m
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE m.active = 1
          AND mt.active = 1
          AND mt.machine_type_name = :loom_type
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY m.mech_code
        """
    )


def get_weaving_latest_beam_no_query():
    """The latest active beam_no for one loom on a (tran_date, spell_id) (resolution).

    Used by the entry/compute layer to stamp jute_prod_weaving_daily.beam_no from the
    §6.7 beam map (beam_no is NOT entered on the production row, Q7). Newest id wins.
    """
    return text(
        """
        SELECT beam_no
        FROM jute_prod_weaving_beam_map
        WHERE co_id = :co_id
          AND tran_date = :tran_date
          AND spell_id = :spell_id
          AND machine_id = :machine_id
          AND active = 1
        ORDER BY weaving_beam_map_id DESC
        LIMIT 1
        """
    )


def get_weaving_beam_map_active_row_query():
    """The active beam-map row id for one loom on a tran_date/spell_id (upsert lookup).

    Beam change is upsert-per-cell (one active row per loom/spell/date). Newest id wins.
    beam_no is returned so the saver can detect an ACTUAL beam change (vs a re-save).
    """
    return text(
        """
        SELECT weaving_beam_map_id, beam_no
        FROM jute_prod_weaving_beam_map
        WHERE co_id = :co_id
          AND tran_date = :tran_date
          AND spell_id = :spell_id
          AND machine_id = :machine_id
          AND active = 1
        ORDER BY weaving_beam_map_id DESC
        LIMIT 1
        """
    )


def update_weaving_beam_map_row_query():
    """Update an existing active beam-map row's beam_no (stamps who/when)."""
    return text(
        """
        UPDATE jute_prod_weaving_beam_map
        SET beam_no = :beam_no,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_beam_map_id = :id
        """
    )


def insert_weaving_beam_map_row_query():
    """Insert a fresh active beam->loom map row (stamps who/when)."""
    return text(
        """
        INSERT INTO jute_prod_weaving_beam_map
            (co_id, branch_id, tran_date, spell_id, machine_id, beam_no,
             active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :spell_id, :machine_id, :beam_no,
             1, :updated_by)
        """
    )


def soft_delete_weaving_beam_map_row_query():
    """Soft-delete (active=0) one beam-map row by id (clear a mounted beam)."""
    return text(
        """
        UPDATE jute_prod_weaving_beam_map
        SET active = 0,
            updated_by = :updated_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_beam_map_id = :id
        """
    )


# =============================================================================
# PAGE C (cont.) — Entry capture (machine-keyed) + Process/Lock (spec 2026-07-07)
# =============================================================================


def get_weaving_daily_active_row_by_machine_query():
    """The single active daily row for (co, date, spell, machine) — quality-agnostic.

    Entry uniqueness dropped weaving_quality_id (one input row per loom/spell); the
    row's quality is whatever the map resolved to (or NULL). Newest id wins."""
    return text(
        """
        SELECT weaving_daily_id, weaving_quality_id
        FROM jute_prod_weaving_daily
        WHERE co_id = :co_id
          AND tran_date = :tran_date
          AND spell_id = :spell_id
          AND machine_id = :machine_id
          AND active = 1
        ORDER BY weaving_daily_id DESC
        LIMIT 1
        """
    )


def get_weaving_unit_daily_ids_query():
    """Active daily rows of the (co, date, spell) unit — id drives the open_jugar
    loop, machine_id scopes the attendance eb stamp to the unit's looms."""
    return text(
        """
        SELECT weaving_daily_id, machine_id
        FROM jute_prod_weaving_daily
        WHERE co_id = :co_id AND tran_date = :tran_date AND spell_id = :spell_id
          AND active = 1
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        """
    )


# A unit carries one jute_prod_weaving_daily row per loom in the shed, produced or
# not (sls co 106: 485 rows/unit, ~200 of them with any production). A loom that
# wove nothing cannot have wrong production, so every per-loom BLOCK and WARN below
# is gated on this predicate — otherwise Process/Sync names ~285 idle looms the
# operator has no reason to act on and buries the real ones (TKT-000111).
# The three columns are the day-slice's only production inputs (open_jugar is a
# resolved carry-forward, NOT production, so an idle loom following a produced spell
# still counts as idle).
WEAVING_PRODUCED_PREDICATE = (
    "(wd.cuts > 0 OR COALESCE(wd.close_jugar, 0) > 0 "
    "OR COALESCE(wd.less_production, 0) > 0)"
)


def get_weaving_unmapped_produced_looms_query():
    """Looms with PRODUCTION in the unit but NO active mapped quality -> Process BLOCK.

    Production-gated (TKT-000111): an idle loom with no mapped quality freezes zero
    either way, so blocking the whole unit for it is a false stop."""
    return text(
        f"""
        SELECT wd.machine_id, m.mech_code, m.machine_name
        FROM jute_prod_weaving_daily wd
        LEFT JOIN machine_mst m ON m.machine_id = wd.machine_id
        LEFT JOIN jute_prod_weaving_quality_map qm
               ON qm.co_id = wd.co_id AND qm.tran_date = wd.tran_date
              AND qm.spell_id = wd.spell_id AND qm.machine_id = wd.machine_id
              AND qm.active = 1 AND qm.weaving_quality_id IS NOT NULL
        WHERE wd.co_id = :co_id AND wd.tran_date = :tran_date AND wd.spell_id = :spell_id
          AND wd.active = 1
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id)
          AND {WEAVING_PRODUCED_PREDICATE}
          AND qm.weaving_quality_map_id IS NULL
        """
    )


def get_weaving_process_quality_mismatch_query():
    """Unit daily rows whose STORED quality is NULL or differs from the active map (BLOCK).

    Belt-and-braces beside get_weaving_unmapped_produced_looms_query: capture BEFORE the
    Loom->Quality map was saved leaves wd.weaving_quality_id NULL (or stale after a remap
    that predates the quality_map_save re-stamp). The day-slice reads the stored column
    with no coalesce to the map, so freezing such a row would compute ZERO production —
    Process blocks and names the looms instead. Production-gated like every other
    per-loom list (TKT-000111): a stale quality on a loom that wove nothing changes
    no number."""
    return text(
        f"""
        SELECT DISTINCT wd.machine_id, m.mech_code, m.machine_name
        FROM jute_prod_weaving_daily wd
        LEFT JOIN machine_mst m ON m.machine_id = wd.machine_id
        JOIN jute_prod_weaving_quality_map qm
              ON qm.co_id = wd.co_id AND qm.tran_date = wd.tran_date
             AND qm.spell_id = wd.spell_id AND qm.machine_id = wd.machine_id
             AND qm.active = 1 AND qm.weaving_quality_id IS NOT NULL
        WHERE wd.co_id = :co_id AND wd.tran_date = :tran_date AND wd.spell_id = :spell_id
          AND wd.active = 1
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id)
          AND {WEAVING_PRODUCED_PREDICATE}
          AND (wd.weaving_quality_id IS NULL
               OR wd.weaving_quality_id <> qm.weaving_quality_id)
        """
    )


def get_weaving_process_no_worker_query():
    """Looms that PRODUCED in the unit with no resolvable attendance worker (WARN).

    Eligibility here MUST equal get_weaving_operator_candidates_query's, or the
    warning disagrees with what actually gets stamped: same spell_id predicate
    (the old query matched ANY spell of the day) and the same weaver-designation
    gate with the same ignore_designation escape.

    The gate lives INSIDE the attendance derived table (pre-join), never in the
    outer WHERE: ineligible attendance rows therefore count as "no worker" (the
    point of the warning) instead of collapsing the LEFT JOIN and dropping the
    loom from the list. One active row per unit+loom (uq_weaving_daily_unit_machine)
    is what lets this be a plain anti-join with no GROUP BY.

    Production-gated (TKT-000111) — an idle loom needs no weaver."""
    return text(
        f"""
        SELECT wd.machine_id, m.mech_code, m.machine_name
        FROM jute_prod_weaving_daily wd
        LEFT JOIN machine_mst m ON m.machine_id = wd.machine_id
        LEFT JOIN (
            -- 2026-09-16: attendance resolved from the DAY first (daily_attendance's
            -- unique index leads with attendance_date), then the machine marks -
            -- the old machine-first LEFT JOIN walked every historical mark of every
            -- loom (260k rows on sls) before the date filter could apply.
            SELECT DISTINCT de.mc_id
            FROM daily_attendance da
            STRAIGHT_JOIN daily_ebmc_attendance de
                    ON de.daily_atten_id = da.daily_atten_id AND de.is_active = 1
            WHERE da.attendance_date = :tran_date AND da.spell_id = :spell_id
              AND da.is_active = 1 AND da.eb_id IS NOT NULL
              AND (:branch_id IS NULL OR da.branch_id = :branch_id)
              AND (:ignore_designation = 1
                   OR EXISTS (SELECT 1 FROM designation_mst dm
                              WHERE dm.designation_id = da.worked_designation_id
                                AND LOWER(COALESCE(dm.desig, '')) LIKE :desig_prefix))
        ) w ON w.mc_id = wd.machine_id
        WHERE wd.co_id = :co_id AND wd.tran_date = :tran_date AND wd.spell_id = :spell_id
          AND wd.active = 1
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id)
          AND {WEAVING_PRODUCED_PREDICATE}
          AND w.mc_id IS NULL
        """
    )


def get_weaving_process_no_standard_query():
    """Mapped looms that PRODUCED whose machine has no speed OR quality no eff (WARN).

    Production-gated (TKT-000111) — a missing standard only misprices real output."""
    return text(
        f"""
        SELECT wd.machine_id, m.mech_code, qm.weaving_quality_id
        FROM jute_prod_weaving_daily wd
        LEFT JOIN machine_mst m ON m.machine_id = wd.machine_id
        JOIN jute_prod_weaving_quality_map qm
              ON qm.co_id = wd.co_id AND qm.tran_date = wd.tran_date
             AND qm.spell_id = wd.spell_id AND qm.machine_id = wd.machine_id
             AND qm.active = 1 AND qm.weaving_quality_id IS NOT NULL
        LEFT JOIN jute_prod_weaving_target_map spd
              ON spd.co_id = :co_id AND spd.id_type = 'mcid' AND spd.param = 'speed'
             AND spd.ref_id = wd.machine_id AND spd.active = 1
             AND spd.effective_date <= :tran_date
        LEFT JOIN jute_prod_weaving_target_map eff
              ON eff.co_id = :co_id AND eff.id_type = 'qid' AND eff.param = 'eff'
             AND eff.ref_id = qm.weaving_quality_id AND eff.active = 1
             AND eff.effective_date <= :tran_date
        WHERE wd.co_id = :co_id AND wd.tran_date = :tran_date AND wd.spell_id = :spell_id
          AND wd.active = 1
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id)
          AND {WEAVING_PRODUCED_PREDICATE}
        GROUP BY wd.machine_id, m.mech_code, qm.weaving_quality_id
        HAVING COUNT(spd.weaving_target_map_id) = 0 OR COUNT(eff.weaving_target_map_id) = 0
        """
    )


def get_weaving_process_no_picks_query():
    """Mapped qualities that PRODUCED with no SQC pick reading for the day (WARN).

    Production-gated (TKT-000111) — picks only matter where cloth was woven."""
    return text(
        f"""
        SELECT qm.weaving_quality_id, wd.machine_id, m.mech_code
        FROM jute_prod_weaving_daily wd
        LEFT JOIN machine_mst m ON m.machine_id = wd.machine_id
        JOIN jute_prod_weaving_quality_map qm
              ON qm.co_id = wd.co_id AND qm.tran_date = wd.tran_date
             AND qm.spell_id = wd.spell_id AND qm.machine_id = wd.machine_id
             AND qm.active = 1 AND qm.weaving_quality_id IS NOT NULL
        LEFT JOIN jute_sqc_weaving_pick p
              ON p.co_id = :co_id AND p.weaving_quality_id = qm.weaving_quality_id
             AND p.entry_date = :tran_date AND p.active = 1
        WHERE wd.co_id = :co_id AND wd.tran_date = :tran_date AND wd.spell_id = :spell_id
          AND wd.active = 1
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id)
          AND {WEAVING_PRODUCED_PREDICATE}
        GROUP BY qm.weaving_quality_id, wd.machine_id, m.mech_code
        HAVING COUNT(p.weaving_sqc_pick_id) = 0
        """
    )


def get_weaving_process_negative_jugar_query():
    """Unit rows whose straight-count total_jugar is negative (WARN — never block/clamp).

    total_jugar = cuts * no_of_jugar_per_cut + close_jugar - open_jugar -
    less_production (the day-slice expression, on the STORED open_jugar). A negative
    is representable BY DESIGN (e.g. mid-spell beam change resets the count), so
    Process only surfaces it for review — same row shape as the other warn lists."""
    return text(
        """
        SELECT wd.machine_id, m.mech_code, m.machine_name
        FROM jute_prod_weaving_daily wd
        LEFT JOIN machine_mst m ON m.machine_id = wd.machine_id
        LEFT JOIN jute_prod_weaving_quality q
               ON q.weaving_quality_id = wd.weaving_quality_id
        WHERE wd.co_id = :co_id AND wd.tran_date = :tran_date AND wd.spell_id = :spell_id
          AND wd.active = 1
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id)
          AND (COALESCE(wd.cuts, 0) * COALESCE(q.no_of_jugar_per_cut, 0)
               + COALESCE(wd.close_jugar, 0)
               - COALESCE(wd.open_jugar, 0)
               - COALESCE(wd.less_production, 0)) < 0
        """
    )


def soft_delete_weaving_log_for_unit_query():
    """Soft-delete existing active log rows for the unit (reprocess idempotency)."""
    return text(
        """
        UPDATE jute_prod_weaving_log
        SET active = 0, updated_by = :updated_by, updated_date_time = CURRENT_TIMESTAMP
        WHERE co_id = :co_id AND tran_date = :tran_date AND spell_id = :spell_id
          AND active = 1
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        """
    )


def insert_weaving_log_from_slice_query():
    """Freeze the unit's computed rows into jute_prod_weaving_log in ONE statement.

    SELECT source = weaving_day_slice_sql() (parity oracle) filtered to (co, date,
    spell). LEFT JOIN a per-quality SQC fingerprint (AVG(picks), MAX(entry_date) for
    entry_date = :tran_date). Columns named explicitly (slice emits spell_rank).

    :branch_id optional — the outer WHERE is STRICT (v.branch_id = :branch_id, no
    NULL-branch pass-through), matching soft_delete_weaving_log_for_unit_query so a
    branch-scoped reprocess deletes exactly what it re-inserts; NULL-branch daily
    rows are only frozen by a co-wide (branch_id IS NULL) Process."""
    slice_sql = weaving_day_slice_sql()
    return text(
        f"""
        INSERT INTO jute_prod_weaving_log (
            weaving_daily_id, co_id, branch_id, tran_date, spell_id, spell_code,
            shift_bucket, spell_rank, machine_id, mech_code, machine_name, line_no,
            weaving_quality_id, item_id, item_code, item_name, weaving_quality_code,
            weaving_quality_name, is_composite, eb_id, beam_no, cuts, close_jugar,
            less_production, open_jugar, jugar, finished_length, ozs_yds, std_ozs_yds,
            no_of_jugar_per_cut, std_speed, act_speed, std_picks, act_picks, std_eff,
            target_eff, eff_speed, eff_picks, working_hours, production_yds,
            production_kg, production_mt, std_prod_yds, target_prod_yds, efficiency,
            std_prod_kg, target_kg, sqc_pick_avg, sqc_pick_maxdate, active, updated_by
        )
        SELECT
            v.weaving_daily_id, v.co_id, v.branch_id, v.tran_date, v.spell_id, v.spell_code,
            v.shift_bucket, v.spell_rank, v.machine_id, v.mech_code, v.machine_name, v.line_no,
            v.weaving_quality_id, v.item_id, v.item_code, v.item_name, v.weaving_quality_code,
            v.weaving_quality_name, v.is_composite, v.eb_id, v.beam_no, v.cuts, v.close_jugar,
            v.less_production, v.open_jugar, v.jugar, v.finished_length, v.ozs_yds, v.std_ozs_yds,
            v.no_of_jugar_per_cut, v.std_speed, v.act_speed, v.std_picks, v.act_picks, v.std_eff,
            v.target_eff, v.eff_speed, v.eff_picks, v.working_hours, v.production_yds,
            v.production_kg, v.production_mt, v.std_prod_yds, v.target_prod_yds, v.efficiency,
            v.std_prod_kg, v.target_kg, fp.pick_avg, fp.pick_maxdate, 1, :updated_by
        FROM (
        {slice_sql}
        ) v
        LEFT JOIN (
            SELECT weaving_quality_id, AVG(picks) AS pick_avg, MAX(entry_date) AS pick_maxdate
            FROM jute_sqc_weaving_pick
            WHERE co_id = :co_id AND entry_date = :tran_date AND active = 1
            GROUP BY weaving_quality_id
        ) fp ON fp.weaving_quality_id = v.weaving_quality_id
        WHERE (:branch_id IS NULL OR v.branch_id = :branch_id)
        """
    )


# The loom's worker is the WEAVER on the machine. PREFIX match, never substring
# or '=': sls carries 'WEAVER' alongside 'WEAVER(S4)', and the weaver family is
# 99.9% of loom attendance rows (probed 2026-08-24) — a prefix takes both without
# raking in the jobbers/oilers/babus who are also marked against a loom.
WEAVER_DESIG_PREFIX = "weaver%"


def get_weaving_operator_candidates_query():
    """Worker candidates per loom for one (date, spell_id, branch) unit.

    Mirrors spinning's get_operator_candidates_query — same lessons, same shape:

    * date/spell/branch predicates live on the daily_attendance HEADER; branch via
      da.branch_id NEVER dea.branch_id (mobile marking leaves the line-level
      branch NULL).
    * spell joins on da.spell_id, not the spell CODE: spell_code repeats once per
      shift generation, so a name join rakes in another generation's attendance.
    * designation gate reads da.worked_designation_id, NOT dea.designation_id —
      the line-level designation is sparsely filled on sls.
    * eb_id IS NOT NULL: the column is nullable, and a NULL-eb mark is not a
      worker — it would also disagree with no_worker's COUNT(da.eb_id).
    * the gate is the WEAVER_DESIG_PREFIX LIKE, with the ignore_designation
      escape hatch. It replaces the old designation_mst.on_machine = 'Yes' gate,
      which matched NOTHING: that column holds 'Y'/'N'/''/'No'/NULL on sls and
      never the literal 'Yes' (probed 2026-08-24).

    Cascade + ambiguity reporting happen in Python (see
    weaving_process._resolve_operators)."""
    return text(
        """
        SELECT DISTINCT dea.mc_id, da.eb_id, da.daily_atten_id
        FROM daily_attendance da
        STRAIGHT_JOIN daily_ebmc_attendance dea
                ON dea.daily_atten_id = da.daily_atten_id
               AND dea.is_active = 1
        LEFT JOIN designation_mst dm
                ON dm.designation_id = da.worked_designation_id
        WHERE da.is_active = 1
          AND da.attendance_date = :tran_date
          AND da.spell_id = :spell_id
          AND da.eb_id IS NOT NULL
          AND (:branch_id IS NULL OR da.branch_id = :branch_id)
          AND (:ignore_designation = 1
               OR LOWER(COALESCE(dm.desig, '')) LIKE :desig_prefix)
        ORDER BY dea.mc_id, da.daily_atten_id
        """
    )


def clear_weaving_daily_eb_query():
    """Blank the unit's daily eb stamps before a re-resolve.

    eb_id is SYNC-OWNED on weaving: there is no manual eb entry on the screen and
    no eb_source column, so a full clear is safe and makes a re-sync after an
    attendance edit deterministic (a worker removed from a loom must not keep his
    stale stamp).
    # ponytail: no eb_source column, so the clear is unconditional — if manual eb
    # entry is ever added to the weaving screen, add eb_source and exempt
    # 'manual' rows here, exactly like spinning's stamp_operator_query does."""
    return text(
        """
        UPDATE jute_prod_weaving_daily
        SET eb_id = NULL
        WHERE co_id = :co_id AND tran_date = :tran_date AND spell_id = :spell_id
          AND active = 1
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        """
    )


def stamp_weaving_daily_eb_query():
    """Stamp one resolved eb_id onto one loom's daily rows in the unit.

    Runs BEFORE the log freeze, so the frozen jute_prod_weaving_log inherits the
    eb through the day-slice (v.eb_id) instead of being patched afterwards."""
    return text(
        """
        UPDATE jute_prod_weaving_daily
        SET eb_id = :eb_id
        WHERE co_id = :co_id AND tran_date = :tran_date AND spell_id = :spell_id
          AND machine_id = :mc_id AND active = 1
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        """
    )


def stamp_weaving_daily_eb_bulk_query(n: int):
    """Stamp EVERY resolved loom of the unit in ONE statement (TKT-000134: the
    per-loom stamp_weaving_daily_eb_query was ~200 round trips per Sync, minutes
    over a WAN). Binds: the unit binds + :m0..:m{n-1} / :e0..:e{n-1} from
    bulk_case_binds("m", "e", [(machine_id, eb_id), ...]). n must be >= 1."""
    whens = " ".join(f"WHEN :m{i} THEN :e{i}" for i in range(n))
    ins = ", ".join(f":m{i}" for i in range(n))
    return text(
        f"""
        UPDATE jute_prod_weaving_daily
        SET eb_id = CASE machine_id {whens} END
        WHERE co_id = :co_id AND tran_date = :tran_date AND spell_id = :spell_id
          AND machine_id IN ({ins}) AND active = 1
          AND (:branch_id IS NULL OR branch_id = :branch_id)
        """
    )


def get_weaving_unit_open_jugar_query():
    """Resolved open_jugar for EVERY active row of the unit in ONE statement (Sync).

    2026-09-16: a single bounded WINDOW instead of two correlated probes per row.
    Loads the unit's looms' rows for the last WEAVING_CHAIN_MAX_GAP_DAYS days, runs
    LAG over the chain order (tran_date, start time, spell_rank, id) per
    (co, machine, quality) - byte-for-byte the _open_jugar_probe_sql rule incl. the
    gap cap (predecessor older than the cap -> 0) - then keeps only the unit's rows.
    sls br29 unit of 141 looms: ~3 s of 282 probes -> one ~0.2 s scan.
    NULL-quality rows (capture-before-map looms) open at 0 and never chain: the
    per-row probe's = on a NULL quality finds nothing and the entry writers skip
    chain sync for them, so the window must not let PARTITION BY (which groups
    NULLs) chain them together. Same rule in the view, backfill, parity, repair.
    Returns (weaving_daily_id, stored_value, open_jugar, cuts, close_jugar); Process/Sync
    write back only the rows whose stored value differs (update_weaving_daily_open_jugar_bulk_query)
    and carry open -> close on idle looms (cuts + close_jugar = 0).
    (Alias is stored_value, not stored - STORED is a MySQL 8 reserved word.)"""
    eff_w = _chain_eff_start_sql("w", "sp")
    rk_w = _spell_rank_case("sp")
    return text(
        f"""
        SELECT /*+ NO_DERIVED_CONDITION_PUSHDOWN(x) */
               x.weaving_daily_id, x.stored_value, x.open_jugar, x.cuts, x.close_jugar
        FROM (
            -- The outer unit filter (tran_date / spell / branch) must NEVER reach this
            -- derived table: the window has to see the looms' full 45-day history or
            -- every row resolves to 0. MySQL only pushes predicates on PARTITION BY
            -- columns (none of these are), the hint pins that for future optimizers.
            SELECT w.weaving_daily_id, w.spell_id, w.branch_id, w.tran_date,
                   w.cuts, w.close_jugar,
                   w.open_jugar AS stored_value,
                   CASE WHEN w.weaving_quality_id IS NULL THEN 0
                        WHEN LAG(w.tran_date) OVER chain IS NULL
                          OR DATEDIFF(w.tran_date, LAG(w.tran_date) OVER chain) > {WEAVING_CHAIN_MAX_GAP_DAYS}
                        THEN 0
                        ELSE COALESCE(LAG(w.close_jugar) OVER chain, 0) END AS open_jugar
            FROM jute_prod_weaving_daily w
            LEFT JOIN spell_mst sp ON sp.spell_id = w.spell_id
            WHERE w.active = 1
              AND w.co_id = :co_id
              AND w.tran_date BETWEEN DATE_SUB(:tran_date, INTERVAL {WEAVING_CHAIN_MAX_GAP_DAYS} DAY) AND :tran_date
              AND w.machine_id IN (
                    SELECT u.machine_id FROM jute_prod_weaving_daily u
                     WHERE u.co_id = :co_id AND u.tran_date = :tran_date AND u.spell_id = :spell_id
                       AND u.active = 1
                       AND (:branch_id IS NULL OR u.branch_id = :branch_id))
            WINDOW chain AS (PARTITION BY w.co_id, w.machine_id, w.weaving_quality_id
                             ORDER BY w.tran_date, {eff_w}, {rk_w}, w.weaving_daily_id)
        ) x
        WHERE x.tran_date = :tran_date AND x.spell_id = :spell_id
          AND (:branch_id IS NULL OR x.branch_id = :branch_id)
        """
    )


def get_weaving_units_in_range_query():
    """(tran_date, spell_id) units with active daily rows in a date range, chain
    order - drives /sync_range. Binds: :co_id, :branch_id, :from_date, :to_date."""
    return text(
        """
        SELECT wd.tran_date, wd.spell_id
        FROM jute_prod_weaving_daily wd
        LEFT JOIN spell_mst sp ON sp.spell_id = wd.spell_id
        WHERE wd.active = 1 AND wd.co_id = :co_id
          AND wd.tran_date BETWEEN :from_date AND :to_date
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id)
        GROUP BY wd.tran_date, wd.spell_id, sp.starting_time
        ORDER BY wd.tran_date, sp.starting_time, wd.spell_id
        """
    )


def update_weaving_daily_open_jugar_bulk_query(n: int, column: str = "open_jugar"):
    """Write back n resolved open_jugar values in ONE statement (CASE on the PK —
    a single correlated UPDATE over the same table trips MySQL 1093). Binds
    :i0..:i{n-1} / :v0..:v{n-1} from bulk_case_binds("i", "v", pairs). n >= 1.
    Like update_weaving_daily_open_jugar_query, deliberately no updated_by stamp.
    column="close_jugar" is the idle-loom carry (open -> close) of Process/Sync."""
    if column not in ("open_jugar", "close_jugar"):
        raise ValueError(column)
    whens = " ".join(f"WHEN :i{i} THEN :v{i}" for i in range(n))
    ins = ", ".join(f":i{i}" for i in range(n))
    return text(
        f"""
        UPDATE jute_prod_weaving_daily
        SET {column} = CASE weaving_daily_id {whens} END
        WHERE weaving_daily_id IN ({ins})
        """
    )


def bulk_case_binds(key_prefix: str, val_prefix: str, pairs) -> dict:
    """{key_prefix}{i} / {val_prefix}{i} binds for the *_bulk_query builders."""
    binds = {}
    for i, (k, v) in enumerate(pairs):
        binds[f"{key_prefix}{i}"] = k
        binds[f"{val_prefix}{i}"] = v
    return binds


def get_weaving_process_lock_row_query():
    """Active header id for the unit at the EXACT run scope (upsert probe).

    Scope-STRICT on branch (co-wide run matches only the co-wide header, a
    branch-scoped run only its own branch's header) — a branch-scoped sync
    refreshes only its branch's log rows, so clearing the stale flag on a
    co-wide header it merely overlaps would fresh-stamp rows it never touched
    (silent-stale reports). A mismatched scope simply inserts its own header;
    read-side freshness requires ALL headers fresh, so partial refreshes stay
    visibly stale until a matching-scope run clears them."""
    return text(
        """
        SELECT weaving_process_lock_id FROM jute_prod_weaving_process_lock
        WHERE co_id = :co_id AND tran_date = :tran_date AND spell_id = :spell_id
          AND ((:branch_id IS NULL AND branch_id IS NULL) OR branch_id = :branch_id)
          AND active = 1
        ORDER BY weaving_process_lock_id DESC LIMIT 1
        """
    )


def insert_weaving_process_lock_query():
    """Insert a fresh locked header for the unit (Process implies a sync stamp)."""
    return text(
        """
        INSERT INTO jute_prod_weaving_process_lock
            (co_id, branch_id, tran_date, spell_id, is_locked, reprocess_needed,
             processed_by, processed_date_time, synced_by, synced_date_time,
             active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :spell_id, 1, 0,
             :processed_by, CURRENT_TIMESTAMP, :processed_by, CURRENT_TIMESTAMP,
             1, :processed_by)
        """
    )


def update_weaving_process_lock_query():
    """Re-lock + clear stale on an existing header (reprocess run; stamps sync too)."""
    return text(
        """
        UPDATE jute_prod_weaving_process_lock
        SET is_locked = 1, reprocess_needed = 0, processed_by = :processed_by,
            processed_date_time = CURRENT_TIMESTAMP, synced_by = :processed_by,
            synced_date_time = CURRENT_TIMESTAMP, updated_by = :processed_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_process_lock_id = :id
        """
    )


def insert_weaving_sync_header_query():
    """Insert a fresh SYNC-ONLY header (is_locked = 0) for the unit.

    Sync (2026-08-21) stores the same jute_prod_weaving_log snapshot as Process but
    never locks — the header records that stored rows exist (readers serve the log)
    and when they were last recomputed."""
    return text(
        """
        INSERT INTO jute_prod_weaving_process_lock
            (co_id, branch_id, tran_date, spell_id, is_locked, reprocess_needed,
             synced_by, synced_date_time, active, updated_by)
        VALUES
            (:co_id, :branch_id, :tran_date, :spell_id, 0, 0,
             :synced_by, CURRENT_TIMESTAMP, 1, :synced_by)
        """
    )


def update_weaving_header_synced_query():
    """Stamp a successful sync on an existing header: clear stale, keep lock state
    (a locked unit re-synced under the Edit gate stays locked)."""
    return text(
        """
        UPDATE jute_prod_weaving_process_lock
        SET reprocess_needed = 0, synced_by = :synced_by,
            synced_date_time = CURRENT_TIMESTAMP, updated_by = :synced_by,
            updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_process_lock_id = :id
        """
    )


def update_weaving_process_lock_reprocess_query():
    """Raise reprocess_needed on a lock header (drift detected on read)."""
    return text(
        """
        UPDATE jute_prod_weaving_process_lock
        SET reprocess_needed = 1, updated_date_time = CURRENT_TIMESTAMP
        WHERE weaving_process_lock_id = :id
        """
    )


def flag_weaving_unit_reprocess_query():
    """Raise the stale flag (reprocess_needed) on ANY active header for a
    (co, date, spell) unit — locked (frozen snapshot invalidated, spec §7) or
    sync-only (stored cache invalidated, 2026-08-21). No-op when the unit has no
    header (nothing stored, nothing to stale)."""
    return text(
        """
        UPDATE jute_prod_weaving_process_lock
        SET reprocess_needed = 1, updated_date_time = CURRENT_TIMESTAMP
        WHERE co_id = :co_id AND tran_date = :tran_date AND spell_id = :spell_id
          AND active = 1
        """
    )


def get_weaving_drift_query():
    """One row if the frozen log disagrees with a fresh recompute of either drift
    source: SQC pick AVG (per quality) or net working_hours (per machine/spell).

    working_hours recompute MUST mirror weaving_day_slice_sql's formula
    (LEAST(spell, att attendance hours) - stoppage, 2026-08-26) — a fork here
    would flap stored units or miss attendance edits entirely."""
    return text(
        """
        SELECT wl.weaving_log_id
        FROM jute_prod_weaving_log wl
        LEFT JOIN spell_mst sp ON sp.spell_id = wl.spell_id
        LEFT JOIN (
            SELECT weaving_quality_id, AVG(picks) AS pick_avg
            FROM jute_sqc_weaving_pick
            WHERE co_id = :co_id AND entry_date = :tran_date AND active = 1
            GROUP BY weaving_quality_id
        ) fp ON fp.weaving_quality_id = wl.weaving_quality_id
        LEFT JOIN (
            SELECT machine_id, spell_id, SUM(stoppage_hours) AS stoppage_hours
            FROM jute_prod_stoppage_hours
            WHERE co_id = :co_id AND tran_date = :tran_date AND active = 1
            GROUP BY machine_id, spell_id
        ) st ON st.machine_id = wl.machine_id AND st.spell_id = wl.spell_id
        LEFT JOIN (
            SELECT t.mc_id, t.spell_id,
                   SUM(GREATEST(0, COALESCE(t.working_hours, 0)
                                   - COALESCE(t.idle_hours, 0))) AS att_hours
            FROM (
                SELECT DISTINCT dea.mc_id, da.daily_atten_id,
                                da.spell_id, da.working_hours, da.idle_hours
                FROM daily_attendance da
                STRAIGHT_JOIN daily_ebmc_attendance dea
                        ON dea.daily_atten_id = da.daily_atten_id
                       AND dea.is_active = 1
                LEFT JOIN designation_mst dm
                        ON dm.designation_id = da.worked_designation_id
                WHERE da.is_active = 1
                  AND da.attendance_date = :tran_date
                  AND da.eb_id IS NOT NULL
                  AND LOWER(COALESCE(dm.desig, '')) LIKE 'weaver%'
            ) t
            GROUP BY t.mc_id, t.spell_id
        ) att ON att.mc_id = wl.machine_id AND att.spell_id = wl.spell_id
        WHERE wl.co_id = :co_id AND wl.tran_date = :tran_date AND wl.spell_id = :spell_id
          AND wl.active = 1
          AND (
            -- sqc_pick_avg is frozen at DECIMAL(10,3); round the fresh full-precision
            -- AVG(picks) to the same scale so an unchanged reading never trips drift.
            ROUND(COALESCE(wl.sqc_pick_avg, 0), 3) <> ROUND(COALESCE(fp.pick_avg, 0), 3)
            OR COALESCE(wl.working_hours, 0)
               <> GREATEST(0, LEAST(COALESCE(sp.working_hours, 0),
                                    COALESCE(att.att_hours, 0))
                           - COALESCE(st.stoppage_hours, 0))
          )
        LIMIT 1
        """
    )


def get_weaving_log_rows_query():
    """Frozen log rows for a unit, projected with the SAME aliases as the day-slice
    entries projection so reads are source-agnostic (Task 11)."""
    return text(
        """
        SELECT weaving_daily_id, co_id, branch_id, tran_date, spell_id, spell_code,
               machine_id, mech_code, machine_name, line_no, weaving_quality_id,
               item_id, item_code, item_name, weaving_quality_code, weaving_quality_name,
               is_composite, eb_id, beam_no, cuts, close_jugar, less_production,
               open_jugar, jugar, finished_length, ozs_yds, std_ozs_yds,
               no_of_jugar_per_cut, std_speed, act_speed, std_picks, act_picks,
               std_eff, target_eff, eff_speed, eff_picks, working_hours,
               production_yds, production_kg, production_mt, std_prod_yds,
               target_prod_yds, efficiency, std_prod_kg, target_kg
        FROM jute_prod_weaving_log
        WHERE co_id = :co_id AND tran_date = :tran_date AND spell_id = :spell_id
          AND (:machine_id IS NULL OR machine_id = :machine_id)
          AND (:branch_id IS NULL OR branch_id = :branch_id OR branch_id IS NULL)
          AND active = 1
        ORDER BY mech_code, weaving_log_id DESC
        """
    )


def get_weaving_quality_map_pairs_query():
    """(spell_id, machine_id) pairs with an ACTIVE, quality-carrying §6.6 map row
    for a (co, date). Loom-checklist blank-reason attribution: a daily row with
    weaving_quality_id NULL on a never-synced unit is 'run Sync' when its pair is
    here, 'map the quality first' when it is not."""
    return text(
        """
        SELECT DISTINCT spell_id, machine_id
        FROM jute_prod_weaving_quality_map
        WHERE co_id = :co_id AND tran_date = :tran_date AND active = 1
          AND weaving_quality_id IS NOT NULL
        """
    )


def get_weaving_day_pick_count_query():
    """Count of ACTIVE SQC weaving pick readings that actually carry picks for a
    (co, date) — width-only readings (picks NULL) don't feed std_picks and don't
    count. Zero => efficiency computes 0 mill-wide that day (R-08-21 exact-day)."""
    return text(
        """
        SELECT COUNT(*)
        FROM jute_sqc_weaving_pick
        WHERE co_id = :co_id AND entry_date = :tran_date AND active = 1
          AND picks IS NOT NULL
        """
    )


def get_weaving_daily_spells_query():
    """Distinct spells with active daily entries for a (co, date) — the loop spine
    for per-spell stored-vs-live row fetching in day-level reports."""
    return text(
        """
        SELECT DISTINCT wd.spell_id
        FROM jute_prod_weaving_daily wd
        WHERE wd.co_id = :co_id AND wd.tran_date = :tran_date AND wd.active = 1
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
        ORDER BY wd.spell_id
        """
    )


def get_weaving_map_spells_query():
    """Distinct spells with active quality-map rows for a (co, date) — the loop
    spine for per-spell stored-vs-live driver selection (planning grid)."""
    return text(
        """
        SELECT DISTINCT qm.spell_id
        FROM jute_prod_weaving_quality_map qm
        INNER JOIN machine_mst m ON m.machine_id = qm.machine_id
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE qm.co_id = :co_id AND qm.tran_date = :tran_date AND qm.active = 1
          AND qm.weaving_quality_id IS NOT NULL
          AND mt.active = 1 AND mt.machine_type_name = :loom_type
          AND (:spell_id IS NULL OR qm.spell_id = :spell_id)
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY qm.spell_id
        """
    )


def get_weaving_day_headers_query():
    """ALL active sync/lock headers for a (co, date) — a unit can carry one header
    per branch scope (branch-scoped syncs insert per-branch headers), so consumers
    must consider every row, never just the newest: a spell serves the STORED log
    only when a header COVERS the request scope (branch_id IS NULL, or the request
    is branch-scoped — the query already excludes other branches then), and it is
    FRESH only when EVERY header agrees (reprocess_needed = 0). Drives the
    per-spell stored-vs-live switch (planning grid, day reports) + sync_status."""
    return text(
        """
        SELECT weaving_process_lock_id, spell_id, branch_id, is_locked,
               reprocess_needed, processed_date_time, synced_date_time
        FROM jute_prod_weaving_process_lock
        WHERE co_id = :co_id AND tran_date = :tran_date AND active = 1
          AND (:spell_id IS NULL OR spell_id = :spell_id)
          AND (:branch_id IS NULL OR branch_id = :branch_id OR branch_id IS NULL)
        ORDER BY spell_id, weaving_process_lock_id DESC
        """
    )


def get_weaving_plan_driver_log_query():
    """Planning-grid driver served from the STORED snapshot (jute_prod_weaving_log)
    instead of the live day-slice — same driver spine (active quality-map rows, so
    mapped looms with no entry still appear) and the SAME output aliases as
    get_weaving_plan_driver_query, so the router's row shaping is source-agnostic.

    Used per-spell when the unit has an active sync/lock header: one indexed read
    (idx_wlog_unit) replaces the full slice recompute. The log row is matched on
    (co, date, spell, machine, quality) exactly like the live join grain."""
    return text(
        """
        SELECT
            qm.weaving_quality_map_id,
            qm.machine_id,
            qm.spell_id,
            qm.weaving_quality_id,
            q.item_id,
            m.mech_code,
            m.machine_name,
            m.line_no,
            d.branch_id,
            sp.spell_code,
            sp.working_hours AS spell_working_hours,
            im.item_code,
            im.item_name,
            q.weaving_quality_code,
            q.weaving_quality_name,
            q.ends,
            q.finished_length,
            q.ozs_yds,
            q.std_ozs_yds,
            q.no_of_jugar_per_cut,
            q.is_composite,
            v.weaving_daily_id,
            v.eb_id,
            v.beam_no,
            v.cuts,
            v.close_jugar,
            v.less_production,
            v.open_jugar,
            v.jugar,
            v.std_speed,
            v.act_speed,
            v.std_picks,
            v.act_picks,
            v.std_eff,
            v.target_eff,
            v.eff_speed,
            v.eff_picks,
            v.working_hours,
            v.production_yds,
            v.production_kg,
            v.production_mt,
            v.std_prod_yds,
            v.target_prod_yds,
            v.efficiency,
            v.std_prod_kg,
            v.target_kg
        FROM jute_prod_weaving_quality_map qm
        INNER JOIN machine_mst m ON m.machine_id = qm.machine_id
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN (
            SELECT spell_id, spell_code, working_hours
            FROM spell_mst WHERE status = 1
        ) sp ON sp.spell_id = qm.spell_id
        LEFT JOIN jute_prod_weaving_quality q ON q.weaving_quality_id = qm.weaving_quality_id
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        LEFT JOIN jute_prod_weaving_log v
               ON v.co_id = qm.co_id AND v.tran_date = qm.tran_date
              AND v.spell_id = qm.spell_id AND v.machine_id = qm.machine_id
              AND v.weaving_quality_id = qm.weaving_quality_id
              AND v.active = 1
        WHERE qm.co_id = :co_id
          AND qm.active = 1
          AND qm.tran_date = :tran_date
          AND qm.weaving_quality_id IS NOT NULL
          AND mt.active = 1
          AND mt.machine_type_name = :loom_type
          AND (:spell_id IS NULL OR qm.spell_id = :spell_id)
          AND (:machine_id IS NULL OR qm.machine_id = :machine_id)
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY m.mech_code, qm.weaving_quality_map_id
        """
    )


def get_weaving_null_branch_rows_query():
    """Unit daily rows with branch_id NULL (WARN on a BRANCH-SCOPED sync/process):
    the freeze is branch-strict so these rows are skipped by a branch-scoped run,
    yet the NULL-tolerant live slice shows them — once the unit serves the stored
    log they would silently vanish from reads. A co-wide run freezes them."""
    return text(
        """
        SELECT wd.machine_id, m.mech_code, m.machine_name
        FROM jute_prod_weaving_daily wd
        LEFT JOIN machine_mst m ON m.machine_id = wd.machine_id
        WHERE wd.co_id = :co_id AND wd.tran_date = :tran_date AND wd.spell_id = :spell_id
          AND wd.active = 1 AND wd.branch_id IS NULL
        """
    )


def flag_weaving_stale_for_pick_query():
    """Stale-flag the stored units an SQC pick reading (save/delete) invalidates.

    A reading at :entry_date for :weaving_quality_id changes std_picks on that exact
    day AND act_picks (last-date-<= resolution) for every later day UP TO the next
    reading date for the quality — days at/after the next reading resolve to that
    newer reading and are untouched. Only headers whose stored log rows actually
    contain the quality are flagged (EXISTS on idx_wlog_unit)."""
    return text(
        """
        UPDATE jute_prod_weaving_process_lock l
        SET l.reprocess_needed = 1, l.updated_date_time = CURRENT_TIMESTAMP
        WHERE l.co_id = :co_id AND l.active = 1
          AND l.tran_date >= :entry_date
          AND l.tran_date < COALESCE(
                (SELECT MIN(p.entry_date) FROM jute_sqc_weaving_pick p
                 WHERE p.co_id = :co_id
                   AND p.weaving_quality_id = :weaving_quality_id
                   AND p.entry_date > :entry_date AND p.active = 1),
                '9999-12-31')
          AND EXISTS (
                SELECT 1 FROM jute_prod_weaving_log wl
                WHERE wl.co_id = l.co_id AND wl.tran_date = l.tran_date
                  AND wl.spell_id = l.spell_id
                  AND wl.weaving_quality_id = :weaving_quality_id
                  AND wl.active = 1)
        """
    )


def flag_weaving_stale_for_target_query():
    """Stale-flag stored units a weaving target-map write invalidates.

    Standards are last-date resolved, so a row effective :effective_date changes
    every stored day AT/AFTER it (until superseded — over-flagging past a newer
    standard is accepted: a re-sync simply restores identical values). Scoped to
    headers whose log rows involve the edited ref (machine for id_type 'mcid',
    quality for 'qid'). NOTE: the on-read drift probe deliberately ignores
    standards; this write-time flag is the only staleness signal for them."""
    return text(
        """
        UPDATE jute_prod_weaving_process_lock l
        SET l.reprocess_needed = 1, l.updated_date_time = CURRENT_TIMESTAMP
        WHERE l.co_id = :co_id AND l.active = 1
          AND l.tran_date >= :effective_date
          AND EXISTS (
                SELECT 1 FROM jute_prod_weaving_log wl
                WHERE wl.co_id = l.co_id AND wl.tran_date = l.tran_date
                  AND wl.spell_id = l.spell_id AND wl.active = 1
                  AND ((:id_type = 'mcid' AND wl.machine_id = :ref_id)
                    OR (:id_type = 'qid' AND wl.weaving_quality_id = :ref_id)))
        """
    )


def flag_weaving_stale_for_stoppage_query():
    """Stale-flag the stored unit a stoppage-hours write invalidates (working_hours
    feeds std_prod_yds/efficiency). Same shape as flag_weaving_unit_reprocess_query
    but restricted to headers whose log rows contain the machine."""
    return text(
        """
        UPDATE jute_prod_weaving_process_lock l
        SET l.reprocess_needed = 1, l.updated_date_time = CURRENT_TIMESTAMP
        WHERE l.co_id = :co_id AND l.tran_date = :tran_date AND l.spell_id = :spell_id
          AND l.active = 1
          AND EXISTS (
                SELECT 1 FROM jute_prod_weaving_log wl
                WHERE wl.co_id = l.co_id AND wl.tran_date = l.tran_date
                  AND wl.spell_id = l.spell_id AND wl.machine_id = :machine_id
                  AND wl.active = 1)
        """
    )

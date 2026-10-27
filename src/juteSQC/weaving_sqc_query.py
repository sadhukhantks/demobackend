"""Raw SQL builders for the Weaving Pick-SQC capture feature (jute SQC module).

One capture surface feeds the weaving planning grid:
  * jute_sqc_weaving_pick - multi-observation pick (picks-per-inch) readings per
    (date, loom, weaving quality); Act Picks is the AVG(picks) per
    (co_id, weaving_quality_id, entry_date), exposed via vw_weaving_pick_act and
    consumed downstream by last-date resolution. Each save is a NEW reading (no
    upsert); a reading is removed by soft-delete.

Mirrors spinning_sqc_query.py conventions: named binds, the ``:x IS NULL OR ...``
optional-filter idiom, and active = 1 soft-delete (active IS NULL legacy rows are
not expected on these freshly-created tables). The weaving-quality and loom lookups
are reused directly from weaving_query.py by the router
(get_weaving_entry_qualities_query / get_weaving_entry_machines_query) and are NOT
re-declared here.
"""

from sqlalchemy import text


# =============================================================================
# jute_sqc_weaving_pick (multi-observation pick) builders
# =============================================================================


def get_weaving_pick_readings_by_date_query():
    """Active pick reading rows for a co/entry_date (branch optional), with labels.

    Each row is a single observation (no upsert); the planning grid averages these
    via vw_weaving_pick_act. LEFT JOINs the weaving-quality master (+ item_mst) for
    the quality/item labels and machine_mst for the loom labels.
    """
    return text(
        """
        SELECT
            p.weaving_sqc_pick_id,
            p.co_id,
            p.branch_id,
            p.entry_date,
            p.weaving_quality_id,
            q.weaving_quality_code,
            q.weaving_quality_name,
            im.item_code,
            im.item_name,
            p.machine_id,
            m.mech_code,
            m.machine_name,
            m.line_no,
            p.width,
            p.picks,
            p.picks_dm
        FROM jute_sqc_weaving_pick p
        LEFT JOIN jute_prod_weaving_quality q ON q.weaving_quality_id = p.weaving_quality_id
        LEFT JOIN item_mst im ON im.item_id = q.item_id
        LEFT JOIN machine_mst m ON m.machine_id = p.machine_id
        WHERE p.co_id = :co_id
          AND p.entry_date = :entry_date
          AND p.active = 1
          AND (:branch_id IS NULL OR p.branch_id = :branch_id OR p.branch_id IS NULL)
        ORDER BY q.weaving_quality_name, m.mech_code, p.weaving_sqc_pick_id
        """
    )


def get_weaving_pick_summary_query():
    """Per-quality pick summary for a co/entry_date from vw_weaving_pick_act.

    One row per (co_id, weaving_quality_id, entry_date) over active=1 readings.
    LEFT JOINs jute_prod_weaving_quality (+ item_mst) for the quality labels.
    Drives the Act Picks column of the weaving planning grid.
    """
    return text(
        """
        SELECT
            v.weaving_quality_id,
            q.weaving_quality_code,
            q.weaving_quality_name,
            v.avg_picks,
            v.std_picks,
            v.min_picks,
            v.max_picks,
            v.avg_width,
            v.min_width,
            v.max_width,
            v.n_obs
        FROM vw_weaving_pick_act v
        LEFT JOIN jute_prod_weaving_quality q ON q.weaving_quality_id = v.weaving_quality_id
        WHERE v.co_id = :co_id
          AND v.entry_date = :entry_date
        ORDER BY q.weaving_quality_name
        """
    )


def insert_weaving_pick_query():
    """Insert one pick observation (a fresh active row per reading).

    spell_id is OPTIONAL (Phase 1c join-key hygiene, add_weaving_pick_spell_id.sql):
    NULL when the client sends no spell - readings stay quality/date-grained for the
    Act Picks average, the spell only adds wage-side traceability.

    picks_dm (2026-08-21) is the as-entered picks-per-decimetre reading; ``picks``
    (PPI = picks_dm * 0.254) is computed by the router so every downstream AVG
    stays PPI-based. picks_dm is NULL when the client sent a raw PPI value.
    """
    return text(
        """
        INSERT INTO jute_sqc_weaving_pick
            (co_id, branch_id, entry_date, weaving_quality_id, machine_id,
             spell_id, width, picks, picks_dm, active, updated_by)
        VALUES
            (:co_id, :branch_id, :entry_date, :weaving_quality_id, :machine_id,
             :spell_id, :width, :picks, :picks_dm, 1, :updated_by)
        """
    )


def get_weaving_pick_active_row_query():
    """The active pick row (soft-delete guard) — identity columns included so the
    router can flag the stored weaving units the deleted reading fed."""
    return text(
        """
        SELECT weaving_sqc_pick_id, co_id, entry_date, weaving_quality_id
        FROM jute_sqc_weaving_pick
        WHERE weaving_sqc_pick_id = :id
          AND active = 1
        """
    )


def soft_delete_weaving_pick_query():
    """Soft-delete a pick reading (active = 0)."""
    return text(
        """
        UPDATE jute_sqc_weaving_pick
        SET active = 0,
            updated_by = :updated_by
        WHERE weaving_sqc_pick_id = :id
          AND active = 1
        """
    )


# =============================================================================
# ITM-013 — standard-picks hint + measured-vs-standard coverage builders
# =============================================================================


def get_weaving_pick_coverage_groups_query():
    """Pick readings grouped by (entry_date, weaving_quality_id, ROUND(picks, 2))
    for a co/date range (branch optional) — the input of the Picks Coverage
    report (weaving_pick_coverage).

    Base table only (never vw_weaving_pick_act): drives idx_jswp_co_date
    (co_id, entry_date, active). Width-only readings (picks IS NULL) are NOT
    readings here, so they are excluded. Grouping by the 2-dp PPI value keeps the
    result tiny (a handful of distinct values per quality/day) and lets the router
    test equality against the DATE-RESOLVED standard in Python (the standard is
    effective-dated, so it cannot be joined as a constant per quality). Branch
    rule mirrors get_weaving_pick_readings_by_date_query; the standard itself is
    company-wide.
    """
    return text(
        """
        SELECT
            p.entry_date,
            p.weaving_quality_id,
            q.weaving_quality_code,
            q.weaving_quality_name,
            ROUND(p.picks, 2) AS picks_2dp,
            COUNT(*) AS n
        FROM jute_sqc_weaving_pick p
        LEFT JOIN jute_prod_weaving_quality q ON q.weaving_quality_id = p.weaving_quality_id
        WHERE p.co_id = :co_id
          AND p.entry_date BETWEEN :date_from AND :date_to
          AND p.active = 1
          AND p.picks IS NOT NULL
          AND (:branch_id IS NULL OR p.branch_id = :branch_id OR p.branch_id IS NULL)
        GROUP BY p.entry_date, p.weaving_quality_id, q.weaving_quality_code,
                 q.weaving_quality_name, ROUND(p.picks, 2)
        ORDER BY p.entry_date, q.weaving_quality_name, p.weaving_quality_id, picks_2dp
        """
    )


def get_weaving_pick_std_history_query():
    """Full standard-picks history (<= :on_date) per weaving quality from
    jute_prod_weaving_target_map — binds id_type='qid', value_role='standard',
    param='picks' (the stored value is PPI).

    NO branch filter and the SAME ranking (effective_date DESC,
    weaving_target_map_id DESC) as resolve_weaving_grid_cells_batch_query /
    services.weaving_standards.resolve_param, so the router's last-date pick per
    quality (std_history_by_quality + resolve_std_picks_on) matches production
    resolution. Fetching the whole history up to :on_date lets ONE execute serve
    every date of a coverage range (and the single-date standards hint).
    """
    return text(
        """
        SELECT tm.ref_id, tm.value, tm.effective_date, tm.weaving_target_map_id
        FROM jute_prod_weaving_target_map tm
        WHERE tm.co_id = :co_id
          AND tm.id_type = :id_type
          AND tm.value_role = :value_role
          AND tm.param = :param
          AND tm.active = 1
          AND tm.effective_date <= :on_date
        ORDER BY tm.ref_id, tm.effective_date DESC, tm.weaving_target_map_id DESC
        """
    )

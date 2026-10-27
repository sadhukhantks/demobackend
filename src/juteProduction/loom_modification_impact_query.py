"""Raw SQL builders for the Loom Modification Impact report (Jute Production).

Spec: docs/superpowers/specs/2026-09-26-loom-modification-impact-design.md §4.5, §7.3,
§8, §9. Every builder is arg-less (or takes only a SQL-shape switch) and returns a
sqlalchemy.text() statement; the router (loom_modification_impact.py) supplies the
named binds. List binds go through ``bindparam(..., expanding=True)``; the ``:x IS NULL
OR ...`` idiom keeps optional filters NULL-tolerant.

THE RANGE READ — ``weaving_range_loomday_sql()`` (§9)
=====================================================
``weaving_day_slice_sql`` (weaving_query.py:737) computes ONE day and joins every
standard to every spell row. The impact report needs up to 120 looms x 200 days, so
this module ports the slice's formula chain to a DATE RANGE as a staged aggregation:

  stage 1 (SQL, spell grain, spell-keyed joins only)
      jute_prod_weaving_daily rows of :machines in [:date_from, :date_to] joined to
      spell_mst, the quality master, ``stp`` (stoppage, GROUP BY date/machine/spell
      over the range) and ``att`` (weaver net attendance hours, GROUP BY
      date/machine/spell, STRAIGHT_JOIN + weaver-prefix gate exactly as the slice,
      restricted to dea.mc_id IN :machines and the range). Per row the slice's
      working_hours = GREATEST(0, LEAST(spell hours, att hours) - stoppage) and
      total_jugar = cuts x no_of_jugar_per_cut + close_jugar - open_jugar -
      less_production, then GROUP BY machine_id, tran_date, weaving_quality_id.
      One row per (machine, date, spell) is guaranteed by
      uq_weaving_daily_unit_machine, so Σ spell hours / Σ stoppage never double count.

  stage 2 (PYTHON, loom-quality-day grain) — the DATE-KEYED standards are resolved
      in Python, not by LEAD() intervals in SQL. Chosen because (a) the ~500 active
      mcid/speed rows are fetched anyway for the baseline speed, the S flag and the
      day-wise markers, (b) the exact-day SQC pick rows of the span are a few
      thousand rows, (c) the slice's tie-break (last effective_date, newest
      weaving_target_map_id; actual role wins when > 0) is then a pure function with
      its own unit tests (same-day duplicate rows, actual-role precedence), and (d)
      the SQL stays a plain aggregation with no window functions. Both eff_speed
      (machine, date) and std_picks (quality, date) are constant inside a
      loom-quality-day group, so
          std_prod_yds  = eff_speed x Σ working_hours x 60 / (36 x std_picks)
          cap_per_rpm   = Σ working_hours x 60 / (36 x std_picks)
          picks_delivered = production_yds x 36 x std_picks
      are exact re-associations of the slice's per-row sums (rows with std_picks = 0
      contribute 0 exactly as the slice's CASE WHEN (36 * std_picks) > 0 guard).
      production_yds = total_jugar x finished_length / no_of_jugar_per_cut and
      production_kg = yds x ozs_yds x 28.35 / 1000 use the quality's constants.
      ROUND(…, 3) now applies per aggregate instead of per spell row, hence the
      parity tolerance (yds / kg 0.01, efficiency 0.05 pts) in
      dbqueries/verify_weaving_range_parity.py.

  stage 3 (PYTHON, loom-day grain) — build_loom_days() in loom_modification_impact.py
      rolls the quality rows up to the §7.3 columns per (machine_id, tran_date) and
      carries ``is_control`` (also emitted by the SQL from :control_machines).

INDEX HINT (§4.5): unhinted, MySQL ranges on uq_weaving_daily_unit_machine /
idx_wd_co_branch_date (co_id + tran_date leading — every loom of the company for
the span) or, on dev3, scans the table (EXPLAIN 2026-09-28: type=ALL unhinted,
type=range on idx_wd_jugar_chain hinted). ``FORCE INDEX (idx_wd_jugar_chain)`` walks
each study loom's qualities and filters on date (0.6 s vs 3.5 s on sls, §9). Once
the migration 20260928_weaving_daily_machine_date_index.sql has added
idx_wd_co_machine_date the hint would pin the WORSE index, so the router asks
``weaving_range_index_hint()`` (SHOW INDEX) per request and passes the result to
``weaving_range_loomday_sql(force_index=...)``.

Not carried from the slice: tmq (quality eff standards, std_eff / target_eff) and
pka (last-date act_picks) — the report uses neither.

Looms are resolved by machine_type NAME inside SQL (case-insensitive collation:
dev3 'LOOM', sls 'Loom'), never by id. line_no is INT on dev3 and VARCHAR on sls —
always CAST to CHAR.
"""

from sqlalchemy import bindparam, text


# =============================================================================
# Range read (§9)
# =============================================================================


def weaving_range_index_hint():
    """Which index the range read should force, decided from SHOW INDEX.

    Returns the SHOW INDEX statement; the router maps the Key_name set to
    ``None`` when idx_wd_co_machine_date exists, else 'idx_wd_jugar_chain'.
    """
    return text(
        """
        SHOW INDEX FROM jute_prod_weaving_daily
        WHERE Key_name IN ('idx_wd_co_machine_date', 'idx_wd_jugar_chain')
        """
    )


def pick_range_index_hint(key_names) -> "str | None":
    """Pure mapper for weaving_range_index_hint(): no hint once the §4.5 index exists."""
    names = {str(k) for k in key_names}
    if "idx_wd_co_machine_date" in names:
        return None
    if "idx_wd_jugar_chain" in names:
        return "idx_wd_jugar_chain"
    return None


def weaving_range_loomday_sql(force_index: "str | None" = "idx_wd_jugar_chain"):
    """Stage 1 of the range read: loom-quality-day rows over a date range.

    Binds: :co_id, :branch_id (NULL-tolerant exactly as the slice:
    ``:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL``),
    :machines (expanding — study + control ids in ONE statement, §8 cap keeps it
    under 120), :control_machines (expanding, may be empty), :date_from, :date_to.

    Output per (machine_id, tran_date, weaving_quality_id):
      cuts, total_jugar, total_jugar_manned (rows with working_hours > 0 only — the
      efficiency numerators), working_hours (Σ slice working_hours), available_hours
      (Σ spell_mst.working_hours of the entered spell rows), stoppage_hours,
      rows_n, rows_with_hours, eb_ids (GROUP_CONCAT DISTINCT), the quality's
      finished_length / ozs_yds / no_of_jugar_per_cut / code / name, is_control.

    The slice's per-row COALESCE guards are kept verbatim: close_jugar,
    less_production, open_jugar, finished_length, ozs_yds, no_of_jugar_per_cut,
    spell working_hours, att hours, stoppage all COALESCE to 0.
    """
    hint = f"FORCE INDEX ({force_index})" if force_index else ""
    return text(
        f"""
        SELECT
            r.machine_id,
            r.tran_date,
            r.weaving_quality_id,
            MAX(r.weaving_quality_code)                         AS weaving_quality_code,
            MAX(r.weaving_quality_name)                         AS weaving_quality_name,
            MAX(r.finished_length)                              AS finished_length,
            MAX(r.ozs_yds)                                      AS ozs_yds,
            MAX(r.no_of_jugar_per_cut)                          AS no_of_jugar_per_cut,
            SUM(r.cuts)                                         AS cuts,
            SUM(r.cuts * r.no_of_jugar_per_cut + r.close_jugar
                - r.open_jugar - r.less_production)             AS total_jugar,
            -- jugar of the MANNED spell rows only (working_hours > 0): the slice's
            -- efficiency numerator counts a spell row's yds only when that row has
            -- std_prod_yds > 0, which needs working_hours > 0 — output of an
            -- unmanned spell on a day another spell was manned must not count.
            SUM(CASE WHEN r.working_hours > 0
                     THEN r.cuts * r.no_of_jugar_per_cut + r.close_jugar
                          - r.open_jugar - r.less_production
                     ELSE 0 END)                                AS total_jugar_manned,
            SUM(r.working_hours)                                AS working_hours,
            SUM(r.spell_hours)                                  AS available_hours,
            SUM(r.stoppage_hours)                               AS stoppage_hours,
            COUNT(*)                                            AS rows_n,
            SUM(CASE WHEN r.working_hours > 0 THEN 1 ELSE 0 END) AS rows_with_hours,
            GROUP_CONCAT(DISTINCT r.eb_id)                      AS eb_ids,
            MAX(r.is_control)                                   AS is_control
        FROM (
            SELECT
                wd.machine_id,
                wd.tran_date,
                wd.weaving_quality_id,
                wd.eb_id,
                wd.cuts,
                COALESCE(wd.close_jugar, 0)                     AS close_jugar,
                COALESCE(wd.less_production, 0)                 AS less_production,
                COALESCE(wd.open_jugar, 0)                      AS open_jugar,
                q.weaving_quality_code,
                q.weaving_quality_name,
                COALESCE(q.finished_length, 0)                  AS finished_length,
                COALESCE(q.ozs_yds, 0)                          AS ozs_yds,
                COALESCE(q.no_of_jugar_per_cut, 0)              AS no_of_jugar_per_cut,
                COALESCE(sp.working_hours, 0)                   AS spell_hours,
                COALESCE(stp.stoppage_hours, 0)                 AS stoppage_hours,
                -- loom hours, the slice's expression verbatim (weaving_query.py:885-888)
                GREATEST(0, LEAST(COALESCE(sp.working_hours, 0),
                                  COALESCE(att.att_hours, 0))
                            - COALESCE(stp.stoppage_hours, 0))  AS working_hours,
                CASE WHEN wd.machine_id IN :control_machines THEN 1 ELSE 0 END AS is_control
            FROM jute_prod_weaving_daily wd {hint}
            LEFT JOIN spell_mst sp ON sp.spell_id = wd.spell_id
            LEFT JOIN jute_prod_weaving_quality q
                   ON q.weaving_quality_id = wd.weaving_quality_id
            LEFT JOIN (
                SELECT st.tran_date, st.machine_id, st.spell_id,
                       SUM(st.stoppage_hours) AS stoppage_hours
                FROM jute_prod_stoppage_hours st
                WHERE st.active = 1 AND st.co_id = :co_id
                  AND st.machine_id IN :machines
                  AND st.tran_date BETWEEN :date_from AND :date_to
                GROUP BY st.tran_date, st.machine_id, st.spell_id
            ) stp ON stp.tran_date = wd.tran_date
                 AND stp.machine_id = wd.machine_id
                 AND stp.spell_id = wd.spell_id
            LEFT JOIN (
                -- att: Σ net weaver attendance hours per (date, loom, spell) over
                -- the range — the slice's derived table (weaving_query.py:972-990)
                -- with the date added to the DISTINCT / GROUP BY and the loom list
                -- pushed into the marks table. Header first (STRAIGHT_JOIN) so the
                -- date index on daily_attendance drives the scan until the §4.5
                -- (mc_id, daily_atten_id, is_active) index exists.
                SELECT t.attendance_date, t.mc_id, t.spell_id,
                       SUM(GREATEST(0, COALESCE(t.working_hours, 0)
                                       - COALESCE(t.idle_hours, 0))) AS att_hours
                FROM (
                    SELECT DISTINCT da.attendance_date, dea.mc_id, da.daily_atten_id,
                                    da.spell_id, da.working_hours, da.idle_hours
                    FROM daily_attendance da
                    STRAIGHT_JOIN daily_ebmc_attendance dea
                            ON dea.daily_atten_id = da.daily_atten_id
                           AND dea.is_active = 1
                           AND dea.mc_id IN :machines
                    LEFT JOIN designation_mst dm
                            ON dm.designation_id = da.worked_designation_id
                    WHERE da.is_active = 1
                      AND da.attendance_date BETWEEN :date_from AND :date_to
                      AND da.eb_id IS NOT NULL
                      AND LOWER(COALESCE(dm.desig, '')) LIKE 'weaver%'
                ) t
                GROUP BY t.attendance_date, t.mc_id, t.spell_id
            ) att ON att.attendance_date = wd.tran_date
                 AND att.mc_id = wd.machine_id
                 AND att.spell_id = wd.spell_id
            WHERE wd.active = 1
              AND wd.co_id = :co_id
              AND wd.machine_id IN :machines
              AND wd.tran_date BETWEEN :date_from AND :date_to
              AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
        ) r
        GROUP BY r.machine_id, r.tran_date, r.weaving_quality_id
        ORDER BY r.machine_id, r.tran_date, r.weaving_quality_id
        """
    ).bindparams(
        bindparam("machines", expanding=True),
        bindparam("control_machines", expanding=True),
    )


def get_target_map_speed_rows_query():
    """Every active mcid/speed row of BOTH roles (standard, actual) of the company.

    Fetched once per report: baseline speed per loom (resolved in Python by the
    slice's rule), the S flag, the day-wise markers and the control speed bands.
    ~500 rows on sls. Ordered so Python can bisect per (ref_id, role).
    """
    return text(
        """
        SELECT tm.weaving_target_map_id, tm.ref_id AS machine_id, tm.value_role,
               tm.value, tm.effective_date
        FROM jute_prod_weaving_target_map tm
        WHERE tm.co_id = :co_id
          AND tm.id_type = 'mcid'
          AND tm.param = 'speed'
          AND tm.value_role IN ('standard', 'actual')
          AND tm.active = 1
        ORDER BY tm.ref_id, tm.value_role, tm.effective_date, tm.weaving_target_map_id
        """
    )


def get_pick_rows_query():
    """Exact-day SQC pick readings of the span (R-08-21): one row per reading.

    Python averages per (weaving_quality_id, entry_date) — the slice's
    ``AVG(p.picks) GROUP BY weaving_quality_id`` at entry_date = :tran_date. AVG
    ignores NULL picks, so NULL readings (width-only, 2026-08-24) are skipped here.
    """
    return text(
        """
        SELECT p.weaving_quality_id, p.entry_date, p.picks
        FROM jute_sqc_weaving_pick p
        WHERE p.active = 1
          AND p.co_id = :co_id
          AND p.picks IS NOT NULL
          AND p.entry_date BETWEEN :date_from AND :date_to
        """
    )


# =============================================================================
# Chain / snapshot reads
# =============================================================================


def get_modification_root_query():
    """root_modification_id of any active stage id (a follow-up's or reversal's id
    resolves to its chain)."""
    return text(
        """
        SELECT lm.modification_id, lm.root_modification_id, lm.branch_id
        FROM jute_prod_loom_modification lm
        WHERE lm.modification_id = :modification_id
          AND lm.co_id = :co_id
          AND lm.active = 1
        """
    )


def get_chain_events_query():
    """Every active event of a chain in date order with its group label."""
    return text(
        """
        SELECT
            lm.modification_id, lm.co_id, lm.branch_id, lm.loom_group_id,
            g.group_code, g.group_name,
            lm.mod_date, lm.mod_to_date, lm.settle_days, lm.mod_kind,
            lm.parent_modification_id, lm.root_modification_id,
            lm.mod_category, lm.title, lm.done_by
        FROM jute_prod_loom_modification lm
        LEFT JOIN jute_prod_loom_group g ON g.loom_group_id = lm.loom_group_id
        WHERE lm.root_modification_id = :root_modification_id
          AND lm.co_id = :co_id
          AND lm.active = 1
        ORDER BY lm.mod_date, lm.modification_id
        """
    )


def get_chain_snapshot_query():
    """Active snapshot rows of every active event of a chain with the resolved
    per-loom D_m / E_m (§5) and the loom's identity."""
    return text(
        """
        SELECT
            mc.modification_id,
            mc.machine_id,
            m.machine_name,
            m.mech_code,
            CAST(m.line_no AS CHAR) AS line_no,
            m.dept_id,
            m.active AS machine_active,
            mc.done_date,
            mc.done_to_date,
            COALESCE(mc.done_date, lm.mod_date) AS d_m,
            COALESCE(mc.done_to_date, mc.done_date, lm.mod_to_date, lm.mod_date) AS e_m
        FROM jute_prod_loom_modification_mc mc
        INNER JOIN jute_prod_loom_modification lm ON lm.modification_id = mc.modification_id
        INNER JOIN machine_mst m ON m.machine_id = mc.machine_id
        WHERE lm.root_modification_id = :root_modification_id
          AND lm.co_id = :co_id
          AND lm.active = 1
          AND mc.active = 1
        ORDER BY lm.mod_date, lm.modification_id, m.machine_name
        """
    )


def get_all_loom_spans_query():
    """Every active per-loom span [D_m, E_m] of the given looms across ALL chains
    (next-event cut, other-chain exclusions, control ineligibility). Carries the
    stage's settle_days so the other chain's excluded span [D_m, E_m + S] can be
    rebuilt in Python."""
    return text(
        """
        SELECT
            mc.machine_id,
            lm.modification_id,
            lm.root_modification_id,
            lm.mod_kind,
            lm.title,
            lm.settle_days,
            COALESCE(mc.done_date, lm.mod_date) AS d_m,
            COALESCE(mc.done_to_date, mc.done_date, lm.mod_to_date, lm.mod_date) AS e_m
        FROM jute_prod_loom_modification_mc mc
        INNER JOIN jute_prod_loom_modification lm ON lm.modification_id = mc.modification_id
        WHERE mc.active = 1
          AND lm.active = 1
          AND lm.co_id = :co_id
          AND mc.machine_id IN :machine_ids
        ORDER BY mc.machine_id, d_m
        """
    ).bindparams(bindparam("machine_ids", expanding=True))


# =============================================================================
# Looms / controls
# =============================================================================


def get_loom_info_query():
    """Identity of a list of machines (name, line as CHAR, dept, branch, active)."""
    return text(
        """
        SELECT
            m.machine_id,
            m.machine_name,
            m.mech_code,
            CAST(m.line_no AS CHAR) AS line_no,
            m.dept_id,
            d.dept_desc AS dept_name,
            d.branch_id,
            m.active
        FROM machine_mst m
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE m.machine_id IN :machine_ids
        """
    ).bindparams(bindparam("machine_ids", expanding=True))


def get_eligible_control_looms_query():
    """Active looms (by machine_type NAME) of the study looms' departments that are
    NOT in the study. Ineligibility by own modification spans / standard changes is
    decided in Python from get_all_loom_spans_query / the target-map rows."""
    return text(
        """
        SELECT
            m.machine_id,
            m.machine_name,
            m.mech_code,
            CAST(m.line_no AS CHAR) AS line_no,
            m.dept_id,
            d.branch_id
        FROM machine_mst m
        INNER JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        INNER JOIN branch_mst b ON b.branch_id = d.branch_id
        WHERE m.active = 1
          AND mt.active = 1
          AND mt.machine_type_name = :loom_type
          AND b.co_id = :co_id
          AND m.dept_id IN :dept_ids
          AND m.machine_id NOT IN :exclude_ids
        ORDER BY m.machine_name
        """
    ).bindparams(
        bindparam("dept_ids", expanding=True),
        bindparam("exclude_ids", expanding=True),
    )


def get_control_group_query():
    """One active CONTROL / STUDY group header of the company (control override)."""
    return text(
        """
        SELECT g.loom_group_id, g.group_code, g.group_name, g.group_purpose, g.branch_id
        FROM jute_prod_loom_group g
        WHERE g.loom_group_id = :loom_group_id
          AND g.co_id = :co_id
          AND g.active = 1
        """
    )


def get_control_group_members_query():
    """Active members of a group with identity (control-group override)."""
    return text(
        """
        SELECT
            gd.machine_id,
            m.machine_name,
            m.mech_code,
            CAST(m.line_no AS CHAR) AS line_no,
            m.dept_id,
            m.active AS machine_active
        FROM jute_prod_loom_group_dtl gd
        INNER JOIN machine_mst m ON m.machine_id = gd.machine_id
        WHERE gd.loom_group_id = :loom_group_id
          AND gd.active = 1
        ORDER BY m.machine_name
        """
    )


# =============================================================================
# Branch data coverage (last data date, data holes, producing-day planning)
# =============================================================================


def get_last_data_date_query():
    """Last date with an active production row for the branch (synced spells for
    today exist, so this is never forced to yesterday). NULL-tolerant branch."""
    return text(
        """
        SELECT MAX(wd.tran_date) AS last_data_date
        FROM jute_prod_weaving_daily wd
        WHERE wd.active = 1
          AND wd.co_id = :co_id
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
        """
    )


def get_branch_row_dates_query():
    """Distinct dates with ANY active weaving row for the branch in the range —
    the data-hole detector's input (>= IMPACT_DATA_HOLE_DAYS consecutive dates
    absent from this list = a hole)."""
    return text(
        """
        SELECT DISTINCT wd.tran_date
        FROM jute_prod_weaving_daily wd
        WHERE wd.active = 1
          AND wd.co_id = :co_id
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
          AND wd.tran_date BETWEEN :date_from AND :date_to
        ORDER BY wd.tran_date
        """
    )


def get_producing_loomdays_query():
    """Producing loom-days of the given looms per date (planning read for the
    producing-day rule, §7.3): a loom-day produces when its Σ total_jugar > 0 with
    a positive finished_length / ozs_yds / no_of_jugar_per_cut, i.e. production_kg
    > 0 by the slice's formula. Base table + quality master only — no attendance,
    no stoppage — so the 180-day look-back costs one index range per loom."""
    return text(
        """
        SELECT x.machine_id, x.tran_date
        FROM (
            SELECT wd.machine_id, wd.tran_date,
                   SUM(CASE WHEN COALESCE(q.finished_length, 0) > 0
                             AND COALESCE(q.ozs_yds, 0) > 0
                             AND COALESCE(q.no_of_jugar_per_cut, 0) > 0
                            THEN (wd.cuts * COALESCE(q.no_of_jugar_per_cut, 0)
                                  + COALESCE(wd.close_jugar, 0) - COALESCE(wd.open_jugar, 0)
                                  - COALESCE(wd.less_production, 0))
                                 * COALESCE(q.finished_length, 0) / COALESCE(q.no_of_jugar_per_cut, 0)
                                 * COALESCE(q.ozs_yds, 0)
                            ELSE 0 END) AS kg_units
            FROM jute_prod_weaving_daily wd
            LEFT JOIN jute_prod_weaving_quality q ON q.weaving_quality_id = wd.weaving_quality_id
            WHERE wd.active = 1
              AND wd.co_id = :co_id
              AND wd.machine_id IN :machine_ids
              AND wd.tran_date BETWEEN :date_from AND :date_to
              AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
            GROUP BY wd.machine_id, wd.tran_date
        ) x
        WHERE x.kg_units > 0
        ORDER BY x.machine_id, x.tran_date
        """
    ).bindparams(bindparam("machine_ids", expanding=True))


# =============================================================================
# Mechanical context legs (§7.2 F, trimmed 2026-09-29 to breakdown / mechanical
# only) — per (machine, date) so Python splits before / after per study loom;
# restricted to machine_id IN :machine_ids and the span
# =============================================================================


def get_mechanical_stoppage_query():
    """jute_prod_stoppage_hours, reason_code 'mechanical' only (STOPPAGE_REASONS in
    constants.py): hours per (machine, date)."""
    return text(
        """
        SELECT st.machine_id, st.tran_date,
               SUM(COALESCE(st.stoppage_hours, 0)) AS stoppage_hours
        FROM jute_prod_stoppage_hours st
        WHERE st.active = 1
          AND st.co_id = :co_id
          AND st.reason_code = 'mechanical'
          AND st.machine_id IN :machine_ids
          AND st.tran_date BETWEEN :date_from AND :date_to
        GROUP BY st.machine_id, st.tran_date
        """
    ).bindparams(bindparam("machine_ids", expanding=True))


def get_breakdowns_query():
    """jute_mech_breakdown, failures only (downtime_type = 'BREAKDOWN'): count and
    minutes per (machine, date)."""
    return text(
        """
        SELECT bd.machine_id, bd.tran_date,
               COUNT(*) AS breakdown_count,
               SUM(COALESCE(bd.duration_min, 0)) AS breakdown_minutes
        FROM jute_mech_breakdown bd
        WHERE bd.active = 1
          AND bd.co_id = :co_id
          AND bd.downtime_type = 'BREAKDOWN'
          AND bd.machine_id IN :machine_ids
          AND bd.tran_date BETWEEN :date_from AND :date_to
        GROUP BY bd.machine_id, bd.tran_date
        """
    ).bindparams(bindparam("machine_ids", expanding=True))


# =============================================================================
# Options endpoint
# =============================================================================


def get_root_modifications_query():
    """Root (INITIAL, parent NULL) active events of a company / branch for the
    picker with group label, loom count and stage count."""
    return text(
        """
        SELECT
            lm.modification_id,
            lm.mod_date,
            lm.title,
            lm.mod_kind,
            g.group_name AS loom_group_name,
            (SELECT COUNT(*) FROM jute_prod_loom_modification_mc mc
              WHERE mc.modification_id = lm.modification_id AND mc.active = 1) AS loom_count,
            (SELECT COUNT(*) FROM jute_prod_loom_modification s
              WHERE s.root_modification_id = lm.modification_id AND s.active = 1) AS stage_count
        FROM jute_prod_loom_modification lm
        LEFT JOIN jute_prod_loom_group g ON g.loom_group_id = lm.loom_group_id
        WHERE lm.active = 1
          AND lm.co_id = :co_id
          AND (:branch_id IS NULL OR lm.branch_id = :branch_id)
          AND lm.parent_modification_id IS NULL
        ORDER BY lm.mod_date DESC, lm.modification_id DESC
        """
    )


def get_control_groups_query():
    """Active CONTROL-purpose groups of the company / branch."""
    return text(
        """
        SELECT g.loom_group_id, g.group_code, g.group_name
        FROM jute_prod_loom_group g
        WHERE g.active = 1
          AND g.co_id = :co_id
          AND g.group_purpose = 'CONTROL'
          AND (:branch_id IS NULL OR g.branch_id IS NULL OR g.branch_id = :branch_id)
        ORDER BY g.group_name
        """
    )

"""Raw SQL for the Spinning Production Trend report (day-wise / quality-wise
production, kg per frame-shift and the loss analysis over a date range).

Source is ``daily_doff_tbl`` spinning rows only. The scope predicate, the frame
label and the quality label are imported from ``doff_analysis_query`` rather
than restated, so the trend can never disagree with the Doff Analysis figures
for the same day: the same rows, the same company spine (daily_doff_tbl has no
co column, so scope rides mc_id -> machine_mst.dept_id -> dept_mst.branch_id ->
branch_mst.co_id), the same ``weight_type IN ('SPG1', 'Auto')`` discriminator.

One aggregating scan at the (date, shift letter, frame, yarn, spinner) grain
serves every cut of the report — day totals, day x quality, day x shift,
quality totals, the frame ranking, the spinner and regular-vs-contract cuts —
and the previous-period comparison is the same statement run over the wider
window and split by date in Python.

The page's global filters (shift letter, frame, yarn) are binds on this one
statement, so every chart, table and the previous-period figures are cut from
the same slice.
"""

from sqlalchemy import text

from src.juteProduction.doff_analysis_query import (  # noqa: F401 (re-exported)
    _FRAME_EXPR,
    _QUALITY_EXPR,
    _SCOPE_WHERE,
    get_branch_scope_check_query,
)
from src.juteProduction.reportQueries import _emp_code_expr

# Shift letter of a doff: first character of the spell code. The caller buckets
# anything outside A/B/C as 'G'; the :shift filter therefore matches 'G' as
# "not A, B or C".
_SHIFT_EXPR = "LEFT(COALESCE(sp.spell_code, ''), 1)"


def get_spinning_trend_rows_query():
    """Doffs aggregated to (date, shift letter, frame, yarn item, spinner).

    ``shift`` is the first character of ``spell_code`` so A1 / A2 fold into A
    (the DPR's frame-run convention). Spell joins on spell_id (the doff's
    ``spell`` column holds the id), never on spell_code — the code repeats once
    per shift generation. ``num_doffs`` feeds the average doff weight and the
    short-frame-shift test. The spinner label (``emp_code``) is resolved
    OUTSIDE the GROUP BY by a scalar subquery — joining
    hrms_ed_official_details in would fan the SUMs out for any eb with a
    branch-transfer history.

    Binds: :co_id :branch_id :date_from :date_to :item_id (nullable)
    :shift (nullable letter: 'A' | 'B' | 'C' | 'G') :frame (nullable frame label).
    """
    return text(
        f"""
        SELECT a.*,
               {_emp_code_expr('a')} AS emp_code
        FROM (
            SELECT
                dd.doff_date,
                {_SHIFT_EXPR}   AS shift,
                dd.mc_id        AS machine_id,
                {_FRAME_EXPR}   AS frame,
                dd.item_id,
                {_QUALITY_EXPR} AS quality_name,
                dd.eb_id,
                SUM(COALESCE(dd.net_weight, 0)) AS net_wt,
                COUNT(*)                        AS num_doffs
            FROM daily_doff_tbl dd
            LEFT JOIN machine_mst m ON m.machine_id = dd.mc_id
            LEFT JOIN spell_mst sp ON sp.spell_id = dd.spell AND sp.status = 1
            LEFT JOIN item_mst im ON im.item_id = dd.item_id
            WHERE dd.doff_date BETWEEN :date_from AND :date_to
              AND (:item_id IS NULL OR dd.item_id = :item_id)
              AND (:frame IS NULL OR {_FRAME_EXPR} = :frame)
              AND (
                    :shift IS NULL
                 OR (:shift IN ('A', 'B', 'C') AND {_SHIFT_EXPR} = :shift)
                 OR (:shift = 'G' AND {_SHIFT_EXPR} NOT IN ('A', 'B', 'C'))
              )
              AND {_SCOPE_WHERE}
            GROUP BY dd.doff_date, shift, dd.mc_id, frame, dd.item_id, quality_name, dd.eb_id
        ) a
        ORDER BY a.doff_date, a.frame, a.shift
        """
    )


def get_installed_frames_query():
    """Active spinning frames of the branch — the capacity denominator of the
    loss bridge. Same spine as the doff scope (machine -> dept -> branch).

    Binds: :branch_id :spinning_type (machine_type_mst.machine_type_name).
    """
    return text(
        """
        SELECT COUNT(*) AS installed
        FROM machine_mst m
        INNER JOIN machine_type_mst mt
                ON mt.machine_type_id = m.machine_type_id
               AND mt.active = 1
               AND mt.machine_type_name = :spinning_type
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE m.active = 1
          AND d.branch_id = :branch_id
        """
    )


def get_trend_target_inputs_query():
    """Standard AND target inputs for every (date, spell, frame, yarn) unit that
    doffed in the window — one row per unit, nothing computed.

    Same shape and rules as ``doff_analysis_query.get_doff_target_inputs_query``
    (last-date resolution of the Spinning Standards map, the day's SQC count,
    frozen planning-grid figures win when non-zero), plus the TARGET role:
    the yarn's ``target`` efficiency and the frozen ``target_prod``. The
    planning grid defines target production as ``p100prod x target_eff / 100``
    (spinning_query.py) — the same 100 %-production as the standard, at the
    target efficiency instead of the standard one — so a target exists only
    when a target efficiency has been entered for the yarn ("include targets
    if the parameter is entered").

    Also carries the yarn's STANDARD count (``jute_yarn_mst.jute_yarn_count``)
    and the frozen row's own ``act_count`` / ``std_count``, so the builder can
    restate the standard at the standard count instead of the day's SQC count
    (``unit_stdc``).

    Binds: :co_id :branch_id :date_from :date_to :spell_id (nullable)
    :item_id (nullable) :frame (nullable).
    """
    return text(
        f"""
        WITH unit AS (
            SELECT dd.doff_date,
                   dd.spell AS spell_id,
                   dd.mc_id,
                   dd.item_id,
                   SUM(COALESCE(dd.net_weight, 0)) AS item_net,
                   SUM(SUM(COALESCE(dd.net_weight, 0))) OVER (
                       PARTITION BY dd.doff_date, dd.spell, dd.mc_id
                   ) AS frame_net
            FROM daily_doff_tbl dd
            LEFT JOIN machine_mst m ON m.machine_id = dd.mc_id
            WHERE dd.doff_date BETWEEN :date_from AND :date_to
              AND dd.item_id IS NOT NULL
              AND (:frame IS NULL OR {_FRAME_EXPR} = :frame)
              AND {_SCOPE_WHERE}
            GROUP BY dd.doff_date, dd.spell, dd.mc_id, dd.item_id
        ),
        dts AS (
            SELECT DISTINCT doff_date AS d FROM unit
        ),
        tm AS (
            SELECT t.d, t.id_type, t.ref_id,
                   MAX(CASE WHEN t.value_role = 'standard' AND t.param = 'speed'
                            THEN t.value END) AS std_speed,
                   MAX(CASE WHEN t.value_role = 'standard' AND t.param = 'spindles'
                            THEN t.value END) AS spindles,
                   MAX(CASE WHEN t.value_role = 'standard' AND t.param = 'tpi'
                            THEN t.value END) AS std_tpi,
                   MAX(CASE WHEN t.value_role = 'standard' AND t.param = 'eff'
                            THEN t.value END) AS std_eff,
                   MAX(CASE WHEN t.value_role = 'target' AND t.param = 'eff'
                            THEN t.value END) AS target_eff
            FROM (
                SELECT dts.d, t2.id_type, t2.ref_id, t2.value_role, t2.param, t2.value,
                       ROW_NUMBER() OVER (
                           PARTITION BY dts.d, t2.id_type, t2.ref_id, t2.value_role, t2.param
                           ORDER BY t2.effective_date DESC, t2.spng_target_map_id DESC
                       ) AS rn
                FROM dts
                INNER JOIN jute_prod_spng_target_map t2
                        ON t2.effective_date <= dts.d
                WHERE t2.co_id = :co_id
                  AND t2.active = 1
                  AND (
                      (t2.id_type = 'mcid' AND t2.value_role = 'standard'
                       AND t2.param IN ('speed', 'spindles'))
                   OR (t2.id_type = 'qid' AND t2.value_role = 'standard'
                       AND t2.param IN ('tpi', 'eff'))
                   OR (t2.id_type = 'qid' AND t2.value_role = 'target'
                       AND t2.param = 'eff')
                  )
            ) t
            WHERE t.rn = 1
            GROUP BY t.d, t.id_type, t.ref_id
        ),
        cnt AS (
            SELECT entry_date, item_id, AVG(observed_count) AS act_count
            FROM jute_sqc_spinning_count
            WHERE co_id = :co_id
              AND entry_date BETWEEN :date_from AND :date_to
              AND active = 1
            GROUP BY entry_date, item_id
        ),
        lg AS (
            SELECT sd.tran_date, sd.spell_id, sd.machine_id, sd.item_id,
                   MAX(sd.std_prod) AS std_prod,
                   MAX(sd.target_prod) AS target_prod,
                   MAX(sd.act_count) AS act_count,
                   MAX(sd.std_count) AS std_count
            FROM jute_prod_spinning_daily sd
            WHERE sd.co_id = :co_id
              AND sd.active = 1
              AND sd.tran_date BETWEEN :date_from AND :date_to
              AND (sd.branch_id = :branch_id OR sd.branch_id IS NULL)
            GROUP BY sd.tran_date, sd.spell_id, sd.machine_id, sd.item_id
        )
        SELECT u.doff_date,
               u.spell_id,
               sp.spell_code,
               sp.working_hours,
               u.mc_id AS machine_id,
               {_FRAME_EXPR} AS frame,
               u.item_id,
               u.item_net,
               u.frame_net,
               tmm.std_speed,
               tmm.spindles,
               tmq.std_tpi,
               tmq.std_eff,
               tmq.target_eff,
               cnt.act_count,
               ym.jute_yarn_count AS std_count,
               lg.std_prod AS frozen_target,
               lg.target_prod AS frozen_tgt_prod,
               lg.act_count AS frozen_act_count,
               lg.std_count AS frozen_std_count
        FROM unit u
        LEFT JOIN machine_mst m ON m.machine_id = u.mc_id
        LEFT JOIN jute_yarn_mst ym ON ym.item_id = u.item_id
        LEFT JOIN spell_mst sp ON sp.spell_id = u.spell_id AND sp.status = 1
        LEFT JOIN tm tmm ON tmm.d = u.doff_date AND tmm.id_type = 'mcid'
                        AND tmm.ref_id = u.mc_id
        LEFT JOIN tm tmq ON tmq.d = u.doff_date AND tmq.id_type = 'qid'
                        AND tmq.ref_id = u.item_id
        LEFT JOIN cnt ON cnt.entry_date = u.doff_date AND cnt.item_id = u.item_id
        LEFT JOIN lg ON lg.tran_date = u.doff_date
                    AND lg.spell_id = u.spell_id
                    AND lg.machine_id = u.mc_id
                    AND lg.item_id = u.item_id
        WHERE (:spell_id IS NULL OR u.spell_id = :spell_id)
          AND (:item_id IS NULL OR u.item_id = :item_id)
        ORDER BY u.doff_date, u.spell_id, u.mc_id, u.item_id
        """
    )

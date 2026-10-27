"""Raw SQL for the Doff Analysis report (port of the legacy MIS doff10 pages).

Source is ``daily_doff_tbl`` — spinning weight types ONLY (SPINNING_WEIGHT_TYPES_SQL
is imported from spinning_query so the discriminator has exactly one definition:
'Auto' rows are weighing-scale doffs and are just as real as 'SPG1').

daily_doff_tbl carries no co column, so company scope rides the machine spine
(mc_id -> machine_mst.dept_id -> dept_mst.branch_id -> branch_mst.co_id), the same
EXISTS fragment the spinning sync uses. The branch predicate sits on the SPINE
(dept_mst.branch_id) rather than on the doff row, because the mobile app leaves
daily_doff_tbl.branch_id NULL on some rows — a frame belongs to exactly one
branch through its department, so the spine is the reliable key and the row's own
branch_id is only used as a guard against cross-branch rows.
"""

from sqlalchemy import text

from src.juteProduction.reportQueries import _emp_code_expr
from src.juteProduction.spinning_query import SPINNING_WEIGHT_TYPES_SQL


# Frame identity = the machine's mech_code (what the mill calls "frame no"),
# falling back to machine_name when a frame carries no code.
_FRAME_EXPR = "COALESCE(NULLIF(TRIM(m.mech_code), ''), m.machine_name)"

# Display quality, legacy-style 'CODE - NAME'. CONCAT_WS drops the missing half
# instead of leaving a dangling separator; a doff with no yarn stamped yields ''
# and the router labels it 'Unmapped'. The CASE is not cosmetic-only: dev3 yarn
# items carry item_code = item_name, which would render every quality cell as
# '14-SKWP - 14-SKWP'.
_QUALITY_EXPR = """
                CASE
                    WHEN NULLIF(TRIM(im.item_code), '') = NULLIF(TRIM(im.item_name), '')
                        THEN TRIM(im.item_name)
                    ELSE CONCAT_WS(' - ', NULLIF(TRIM(im.item_code), ''),
                                          NULLIF(TRIM(im.item_name), ''))
                END"""

_SCOPE_WHERE = f"""
          (dd.active = 1 OR dd.active IS NULL)
          AND dd.weight_type IN {SPINNING_WEIGHT_TYPES_SQL}
          AND (dd.branch_id IS NULL OR dd.branch_id = :branch_id)
          AND EXISTS (
              SELECT 1
              FROM machine_mst mm
              INNER JOIN dept_mst dmm ON dmm.dept_id = mm.dept_id
              INNER JOIN branch_mst bmm ON bmm.branch_id = dmm.branch_id
              WHERE mm.machine_id = dd.mc_id
                AND bmm.co_id = :co_id
                AND dmm.branch_id = :branch_id
          )
"""


def get_spinning_doff_weight_query():
    """Total spinning doff weight for a (co, branch) over a date range, in KG.

    This is the source the owner asked for on the winding/weaving difference
    report's "Spinning Production" row: "From doff wt", i.e. the actual weighed
    doffs (daily_doff_tbl.net_weight keyed on doff_date). It is deliberately NOT
    jute_prod_spinning_daily.act_prod_doff, the spinning planning snapshot: on
    sls branch 29 that snapshot was empty or partial on 16 of the 18 days of
    September 2026 (0.0 kg on 12 days while the doffs totalled ~20 t/day), and on
    the days it IS filled it matches the doffs exactly -- so it is a data-entry
    artefact, not a second opinion. Do not swap this back.

    The scope predicates are _SCOPE_WHERE, shared verbatim with the Doff Analysis
    report rather than restated here, so a future change to what counts as
    spinning production cannot leave this report behind. The one that is easiest
    to miss is `weight_type IN SPINNING_WEIGHT_TYPES_SQL` ('SPG1' + 'Auto'): a
    plain SUM(net_weight) silently absorbs non-spinning weighings. Company scope
    rides the machine spine because daily_doff_tbl has no co column.

    One function serves both grains, like the jute-issue figure beside it: the
    day passes :date_from = :date_to = report_date, MTD passes the month window.

    Binds: :co_id, :branch_id, :date_from, :date_to.
    """
    return text(
        f"""
        SELECT COALESCE(SUM(dd.net_weight), 0) AS production_kg,
               COUNT(*) AS doff_count
        FROM daily_doff_tbl dd
        WHERE dd.doff_date BETWEEN :date_from AND :date_to
          AND {_SCOPE_WHERE}
        """
    )


def get_branch_scope_check_query():
    """(co_id, branch_id) pair check — 400 when the branch is not the company's."""
    return text(
        "SELECT 1 FROM branch_mst WHERE branch_id = :branch_id AND co_id = :co_id"
    )


def get_doff_detail_rows_query():
    """mode=details: one row per active spinning doff on ``report_date``.

    Deliberately NOT pre-aggregated: the response needs the raw grid anyway, and
    the per-frame summary, the spell totals and the weight bands are all cuts of
    the same few hundred rows (one day = frames x spells x doffs). Two scans of
    daily_doff_tbl to save a Python groupby would be the more expensive trade.

    Spell joins on spell_id (the doff's ``spell`` column holds the id), never on
    spell_code — the code repeats once per shift generation.
    """
    return text(
        f"""
        SELECT
            dd.daily_doff_tbl_id AS doff_id,
            dd.mc_id             AS machine_id,
            {_FRAME_EXPR}        AS frame,
            dd.spell             AS spell_id,
            sp.spell_code,
            dd.item_id,
            {_QUALITY_EXPR}      AS quality_name,
            dd.eb_id,
            {_emp_code_expr('dd')} AS emp_code,
            COALESCE(dd.net_weight, 0) AS net_wt
        FROM daily_doff_tbl dd
        LEFT JOIN machine_mst m ON m.machine_id = dd.mc_id
        LEFT JOIN spell_mst sp ON sp.spell_id = dd.spell AND sp.status = 1
        LEFT JOIN item_mst im ON im.item_id = dd.item_id
        WHERE dd.doff_date = :report_date
          AND (:spell_id IS NULL OR dd.spell = :spell_id)
          AND (:item_id IS NULL OR dd.item_id = :item_id)
          AND {_SCOPE_WHERE}
        ORDER BY sp.spell_code, dd.daily_doff_tbl_id
        """
    )


def get_doff_frame_range_query():
    """mode=frames: doffs aggregated to (date, shift, frame, quality, worker).

    That grain is the coarsest one every frames-mode cut can be rebuilt from —
    the quality x shift average and the frame pivot roll the worker away, the
    per-frame drill keeps it — so the range costs ONE aggregating scan instead of
    three raw ones (31 days x ~24 frames x 4 shifts is a few thousand rows).

    ``shift`` is the first character of spell_code; the A/B/C-else-G rule is
    applied by the caller so two raw codes that both fall outside A/B/C collapse
    into one 'G' bucket there. The worker label is resolved OUTSIDE the GROUP BY
    (scalar subquery on the derived table) — joining hrms_ed_official_details in
    would fan the aggregate out for any eb with a branch-transfer history and
    silently double SUM/COUNT.
    """
    return text(
        f"""
        SELECT a.*,
               {_emp_code_expr('a')} AS emp_code
        FROM (
            SELECT
                dd.doff_date,
                dd.mc_id      AS machine_id,
                {_FRAME_EXPR} AS frame,
                dd.item_id,
                {_QUALITY_EXPR} AS quality_name,
                LEFT(COALESCE(sp.spell_code, ''), 1) AS shift,
                dd.eb_id,
                SUM(COALESCE(dd.net_weight, 0)) AS net_wt,
                COUNT(*)                        AS num_doffs,
                MAX(COALESCE(dd.net_weight, 0)) AS max_wt,
                MIN(COALESCE(dd.net_weight, 0)) AS min_wt
            FROM daily_doff_tbl dd
            LEFT JOIN machine_mst m ON m.machine_id = dd.mc_id
            LEFT JOIN spell_mst sp ON sp.spell_id = dd.spell AND sp.status = 1
            LEFT JOIN item_mst im ON im.item_id = dd.item_id
            WHERE dd.doff_date BETWEEN :date_from AND :date_to
              AND (:item_id IS NULL OR dd.item_id = :item_id)
              AND {_SCOPE_WHERE}
            GROUP BY dd.doff_date, dd.mc_id, frame, dd.item_id, quality_name,
                     shift, dd.eb_id
        ) a
        ORDER BY a.doff_date, a.frame, a.shift
        """
    )


def get_doff_target_inputs_query():
    """Inputs of the production TARGET for every (date, spell, frame, yarn) unit
    that doffed in the window — one row per unit, target NOT yet computed.

    "Target" on this report is the figure the Spinning Production planning grid
    calls *Std Prod*: ``p100prod * std_eff / 100`` where p100prod is the frame's
    100%-efficiency production for the spell. (Owner decision on TKT-000168: the
    mill's target is the STANDARD efficiency, not the grid's separate Target
    Eff % / *Tgt Prod*.) Every input comes from
    masters the mill already maintains — nothing new is stored:

    * Spinning Standards (``jute_prod_spng_target_map``, last-date resolution
      ``effective_date <= doff_date``): machine ``standard`` speed + spindles,
      quality ``standard`` tpi and quality ``standard`` eff;
    * the day's SQC spinning count (``jute_sqc_spinning_count``, AVG per yarn);
    * ``spell_mst.working_hours`` for the spell minutes.

    The arithmetic is done by the caller with ``spinning_rules.p100_prod_spell``
    so the formula keeps ONE definition in Python; this statement only gathers
    the inputs. A missing input comes back NULL (never 0) so the caller can tell
    "not entered" from a real zero and leave the target blank.

    ``frozen_target`` is ``jute_prod_spinning_daily.std_prod`` (the Std-Eff
    figure — NOT ``target_prod``, which is built with Target Eff) for a unit that
    has been Processed. A non-zero frozen value wins (the report then shows the
    same figure as the locked planning grid); a frozen 0 does NOT win, because on
    a day processed before the standards were entered it only means "was not
    computable then".

    The date-ranked target-map CTEs follow get_spinning_lowprod_base_query (one
    query for the whole window instead of one per day). ``frame_net`` is the
    frame+spell total over ALL yarns, so the item/spell filters are applied in
    the outer SELECT only — filtering inside ``unit`` would hand a mid-spell
    yarn change the full spell minutes.

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
                            THEN t.value END) AS std_eff
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
                   MAX(sd.std_prod) AS std_prod
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
               cnt.act_count,
               lg.std_prod AS frozen_target
        FROM unit u
        LEFT JOIN machine_mst m ON m.machine_id = u.mc_id
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

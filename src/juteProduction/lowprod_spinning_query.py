"""Raw SQL for the Spinning Low Producer report (MIS port, spec section 2).

One statement, one round trip for the whole date range. The grain is the
WORKER row — (doff_date, spell, frame, yarn item, eb) — which is finer than the
planning slice's (frame, spell, item), so the slice SQL cannot be reused as-is;
what IS reused verbatim is everything that decides the standard:

* the machine spine co-scope (``_SYNC_SCOPE_WHERE`` in spinning_query.py) —
  daily_doff_tbl has no co_id, so company scope rides
  mc_id -> machine_mst.dept_id -> dept_mst.branch_id -> branch_mst.co_id;
* the spinning weight-type discriminator (``SPINNING_WEIGHT_TYPES_SQL``);
* the spinner designation gate (``SPINNER_DESIG_PREFIX``, prefix match, never
  substring — see its comment in spinning_query.py);
* the p100prod formula and its minute attribution, copied from
  ``spinning_day_slice_sql`` so a live-computed day matches a Processed one;
* the frozen ``jute_prod_spinning_daily`` p100prod, which WINS over the
  recompute for units that have been Processed when it is non-zero (same
  log-vs-live rule the planning grid reads with). A frozen 0 does NOT win: on
  a day Processed before the standards (TPI / speed / SQC count) were entered
  it only means "was not computable then", so the live recompute is used —
  the same rule doff_analysis_query applies (TKT-000179).

Date-effective standards over a RANGE: the slice resolves the target map with
``effective_date <= :tran_date``; here the distinct doff dates are cross-joined
to the map and ranked per date, so one query covers every day in the window
instead of one query per day.
"""

from sqlalchemy import text

from src.juteProduction.spinning_query import SPINNING_WEIGHT_TYPES_SQL


# Contract-row hours per spell. Also the denominator that turns the frame's
# whole-spell p100prod into the worker's share, so a contract row (no
# attendance) always lands on ratio 1. Must share its basis with _MINUTES_SQL
# (spell_mst.working_hours), which feeds p100prod — a fixed map here while
# p100prod uses an 8h spell deflates eff by hours_map/8 for every full-shift
# worker. Legacy MIS values (C 7.5, A1 5, ...) survive only as the fallback
# for spells with no configured hours.
_SPELL_HOURS_SQL = """
            COALESCE(NULLIF(sp.working_hours, 0),
            CASE sp.spell_code
                WHEN 'C'  THEN 7.5
                WHEN 'A1' THEN 5
                WHEN 'A2' THEN 3
                WHEN 'B1' THEN 3
                WHEN 'B2' THEN 5
                ELSE 8
            END)
"""

# Shift letter for grouping: first char of spell_code, anything not A/B/C -> G.
_SHIFT_SQL = """
            CASE WHEN LEFT(sp.spell_code, 1) IN ('A', 'B', 'C')
                 THEN LEFT(sp.spell_code, 1) ELSE 'G' END
"""

# emp_code for an eb — scalar subquery, NEVER a join: hrms_ed_official_details
# holds one row per branch transfer and a join would fan every SUM out.
_EMP_CODE_SQL = """
            (SELECT NULLIF(TRIM(o.emp_code), '')
             FROM hrms_ed_official_details o
             WHERE o.eb_id = df.eb_id AND o.active = 1
             ORDER BY o.tbl_hrms_ed_official_detail_id DESC
             LIMIT 1)
"""

# Minutes exactly as spinning_day_slice_sql computes them (single-item frame
# keeps the full spell, a mid-shift item change splits by weight share), so a
# recomputed day equals the frozen one.
_MINUTES_SQL = """
            CAST(ROUND(
                (CASE
                    WHEN sp.working_hours IS NOT NULL THEN sp.working_hours * 60
                    WHEN sp.spell_code = 'A1' THEN 300
                    WHEN sp.spell_code = 'A2' THEN 180
                    ELSE 0
                END)
                * COALESCE(u.item_net / NULLIF(u.frame_net, 0), 1)
            ) AS SIGNED)
"""


def get_spinning_lowprod_base_query():
    """Per (date, spell, frame, yarn, worker) production with its standard.

    Binds: ``co_id`` ``branch_id`` (nullable) ``d_from`` ``d_to``
    ``desig_prefix`` ``shift`` (nullable) ``item_id`` (nullable)
    ``frame_like`` (nullable, ``%x%``) ``eb_no_like`` (nullable, ``%x%``).

    eff_pct = net_wt / (p100prod * whrs / spell_hours) * 100. Rows whose yarn is
    unstamped (item_id NULL) or whose frame/yarn has no standard land on
    std_prod 0 -> eff 0; the router's averages drop zeros ("shed average").
    A Processed unit whose frozen p100prod is 0 falls through to the live
    standard (NULLIF) instead of pinning std_prod to 0 (TKT-000179).
    """
    return text(
        f"""
        WITH doff AS (
            SELECT d.doff_date,
                   d.spell AS spell_id,
                   d.mc_id,
                   d.item_id,
                   d.eb_id,
                   SUM(COALESCE(d.net_weight, 0)) AS net_wt,
                   COUNT(*) AS doff_count
            FROM daily_doff_tbl d
            WHERE d.doff_date BETWEEN :d_from AND :d_to
              AND (d.active = 1 OR d.active IS NULL)
              AND d.weight_type IN {SPINNING_WEIGHT_TYPES_SQL}
              AND (:branch_id IS NULL OR d.branch_id = :branch_id)
              AND EXISTS (
                  SELECT 1
                  FROM machine_mst mm
                  INNER JOIN dept_mst dmm ON dmm.dept_id = mm.dept_id
                  INNER JOIN branch_mst bmm ON bmm.branch_id = dmm.branch_id
                  WHERE mm.machine_id = d.mc_id AND bmm.co_id = :co_id
              )
            GROUP BY d.doff_date, d.spell, d.mc_id, d.item_id, d.eb_id
        ),
        dts AS (
            SELECT DISTINCT doff_date AS d FROM doff
        ),
        unit AS (
            SELECT df.doff_date, df.spell_id, df.mc_id, df.item_id,
                   SUM(df.net_wt) AS item_net,
                   SUM(SUM(df.net_wt)) OVER (
                       PARTITION BY df.doff_date, df.spell_id, df.mc_id
                   ) AS frame_net
            FROM doff df
            WHERE df.item_id IS NOT NULL
            GROUP BY df.doff_date, df.spell_id, df.mc_id, df.item_id
        ),
        tmm AS (
            SELECT t.d, t.ref_id,
                   MAX(CASE WHEN t.param = 'speed' THEN t.value END) AS std_speed,
                   MAX(CASE WHEN t.param = 'spindles' THEN t.value END) AS spindles
            FROM (
                SELECT dts.d, t2.ref_id, t2.param, t2.value,
                       ROW_NUMBER() OVER (
                           PARTITION BY dts.d, t2.ref_id, t2.param
                           ORDER BY t2.effective_date DESC, t2.spng_target_map_id DESC
                       ) AS rn
                FROM dts
                INNER JOIN jute_prod_spng_target_map t2
                        ON t2.effective_date <= dts.d
                WHERE t2.co_id = :co_id
                  AND t2.id_type = 'mcid'
                  AND t2.value_role = 'standard'
                  AND t2.param IN ('speed', 'spindles')
                  AND t2.active = 1
            ) t
            WHERE t.rn = 1
            GROUP BY t.d, t.ref_id
        ),
        tmq AS (
            SELECT t.d, t.ref_id,
                   MAX(CASE WHEN t.param = 'tpi' THEN t.value END) AS std_tpi
            FROM (
                SELECT dts.d, t2.ref_id, t2.param, t2.value,
                       ROW_NUMBER() OVER (
                           PARTITION BY dts.d, t2.ref_id, t2.param
                           ORDER BY t2.effective_date DESC, t2.spng_target_map_id DESC
                       ) AS rn
                FROM dts
                INNER JOIN jute_prod_spng_target_map t2
                        ON t2.effective_date <= dts.d
                WHERE t2.co_id = :co_id
                  AND t2.id_type = 'qid'
                  AND t2.value_role = 'standard'
                  AND t2.param = 'tpi'
                  AND t2.active = 1
            ) t
            WHERE t.rn = 1
            GROUP BY t.d, t.ref_id
        ),
        cnt AS (
            SELECT entry_date, item_id, AVG(observed_count) AS act_count
            FROM jute_sqc_spinning_count
            WHERE co_id = :co_id
              AND entry_date BETWEEN :d_from AND :d_to
              AND active = 1
            GROUP BY entry_date, item_id
        ),
        lg AS (
            SELECT sd.tran_date, sd.spell_id, sd.machine_id, sd.item_id,
                   MAX(sd.p100prod) AS p100prod
            FROM jute_prod_spinning_daily sd
            WHERE sd.co_id = :co_id
              AND sd.active = 1
              AND sd.tran_date BETWEEN :d_from AND :d_to
              AND (:branch_id IS NULL OR sd.branch_id = :branch_id
                   OR sd.branch_id IS NULL)
            GROUP BY sd.tran_date, sd.spell_id, sd.machine_id, sd.item_id
        ),
        att AS (
            SELECT da.attendance_date, da.spell_id, da.eb_id,
                   SUM(COALESCE(da.working_hours, 0)
                       - COALESCE(da.idle_hours, 0)) AS net_hours,
                   MAX(CASE WHEN CAST(da.status_id AS UNSIGNED) = 3
                            THEN 1 ELSE 0 END) AS approved
            FROM daily_attendance da
            INNER JOIN designation_mst dm
                    ON dm.designation_id = da.worked_designation_id
                   AND LOWER(COALESCE(dm.desig, '')) LIKE :desig_prefix
            WHERE da.attendance_date BETWEEN :d_from AND :d_to
              AND da.is_active = 1
              AND da.eb_id IS NOT NULL
              AND (da.status_id IS NULL OR CAST(da.status_id AS UNSIGNED) <> 4)
              AND (:branch_id IS NULL OR da.branch_id = :branch_id)
              AND da.branch_id IN (
                  SELECT b.branch_id FROM branch_mst b WHERE b.co_id = :co_id
              )
            GROUP BY da.attendance_date, da.spell_id, da.eb_id
        ),
        std AS (
            SELECT u.doff_date, u.spell_id, u.mc_id, u.item_id,
                   COALESCE(
                       NULLIF(lg.p100prod, 0),
                       COALESCE(ROUND(
                           (COALESCE(tmm.std_speed, 0) * {_MINUTES_SQL}
                            * COALESCE(cnt.act_count, 0)
                            * COALESCE(tmm.spindles, 0))
                           / (36 * 14400 * 2.2046
                              * NULLIF(COALESCE(tmq.std_tpi, 0), 0)),
                           0
                       ), 0)
                   ) AS p100prod
            FROM unit u
            LEFT JOIN (
                SELECT spell_id, spell_code, working_hours
                FROM spell_mst WHERE status = 1
            ) sp ON sp.spell_id = u.spell_id
            LEFT JOIN tmm ON tmm.d = u.doff_date AND tmm.ref_id = u.mc_id
            LEFT JOIN tmq ON tmq.d = u.doff_date AND tmq.ref_id = u.item_id
            LEFT JOIN cnt ON cnt.entry_date = u.doff_date
                         AND cnt.item_id = u.item_id
            LEFT JOIN lg ON lg.tran_date = u.doff_date
                        AND lg.spell_id = u.spell_id
                        AND lg.machine_id = u.mc_id
                        AND lg.item_id = u.item_id
        )
        SELECT b.*,
               ROUND(b.p100prod * b.whrs / NULLIF(b.spell_hours, 0), 3) AS std_prod,
               ROUND(b.whrs / NULLIF(b.spell_hours, 0), 3) AS no_of_frame,
               COALESCE(ROUND(
                   b.net_wt
                   / NULLIF(b.p100prod * b.whrs / NULLIF(b.spell_hours, 0), 0)
                   * 100, 2), 0) AS eff_pct
        FROM (
            SELECT df.doff_date,
                   sp.spell_code,
                   {_SHIFT_SQL} AS shift,
                   df.mc_id AS machine_id,
                   COALESCE(NULLIF(TRIM(m.mech_code), ''), m.machine_name) AS frame,
                   df.item_id,
                   im.item_name AS quality_name,
                   df.eb_id,
                   {_EMP_CODE_SQL} AS eb_no,
                   NULLIF(TRIM(CONCAT_WS(' ', pd.first_name, pd.middle_name,
                                         pd.last_name)), '') AS worker_name,
                   df.net_wt,
                   df.doff_count,
                   CASE WHEN at.eb_id IS NULL THEN 'Contract' ELSE 'R' END
                       AS attendance_type,
                   COALESCE(at.approved, 0) AS att_approved,
                   COALESCE(at.net_hours, {_SPELL_HOURS_SQL}) AS whrs,
                   {_SPELL_HOURS_SQL} AS spell_hours,
                   COALESCE(st.p100prod, 0) AS p100prod
            FROM doff df
            LEFT JOIN (
                SELECT spell_id, spell_code, working_hours
                FROM spell_mst WHERE status = 1
            ) sp ON sp.spell_id = df.spell_id
            LEFT JOIN machine_mst m ON m.machine_id = df.mc_id
            LEFT JOIN item_mst im ON im.item_id = df.item_id
            LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = df.eb_id
            LEFT JOIN att at ON at.attendance_date = df.doff_date
                            AND at.spell_id = df.spell_id
                            AND at.eb_id = df.eb_id
            LEFT JOIN std st ON st.doff_date = df.doff_date
                            AND st.spell_id = df.spell_id
                            AND st.mc_id = df.mc_id
                            AND st.item_id = df.item_id
        ) b
        WHERE (:shift IS NULL OR b.shift = :shift)
          AND (:item_id IS NULL OR b.item_id = :item_id)
          AND (:frame_like IS NULL OR b.frame LIKE :frame_like)
          AND (:eb_no_like IS NULL OR b.eb_no LIKE :eb_no_like)
        ORDER BY b.doff_date, b.spell_code, b.frame
        """
    )

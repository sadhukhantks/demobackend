"""Query builders for the Daily Production Report (DPR).

The DPR is a single-page mill summary (spinning, winding, weaving, finishing,
yarn heavy-light SQC, jute stock, worker hands, machine-run analysis) for one
report date plus month-to-date context.

Day-level spinning and weaving figures reuse the proven day-slice builders
(`spinning_query.spinning_day_slice_sql`, `weaving_query.weaving_day_slice_sql`)
so the DPR can never drift from the planning grids. The builders in this file
cover only what the slices cannot: cheap MTD aggregates over the raw tables and
the sections that have no slice (finishing, SQC counts, attendance hands,
installed machine counts).

MTD windows are always `[:date_from, :report_date]` where date_from is the
first of the report month — computed by the router, not the SQL.
"""

from sqlalchemy import text

from src.juteProcurement.constants import (
    YARN_DOC_STATUS_APPROVED,
    YARN_ISSUE_TYPE_RETURN,
)


def get_dpr_spinning_mtd_query():
    """MTD spinning production + frame runs per shift bucket from the raw doff log.

    A "frame run" is one frame producing in one shift on one day
    (COUNT(DISTINCT doff_date, mc_id, shift)); A1/A2 collapse into shift A so a
    frame doffing in both half-spells counts once, matching the day slice's
    shift_bucket convention. Rows whose spell doesn't resolve in spell_mst
    bucket as '?' (still counted). `item_id IS NOT NULL` mirrors the day
    slice's driver filter so MTD == sum of day slices over the window.

    daily_doff_tbl has no co_id — company scope rides the machine → dept →
    branch spine, same as the day slice.

    Binds: :co_id, :branch_id (nullable), :date_from, :report_date,
    :spinning_type (machine_type_mst.machine_type_name).
    """
    return text(
        """
        SELECT
            COALESCE(LEFT(sp.spell_code, 1), '?') AS shift_bucket,
            ROUND(SUM(dd.net_weight), 3) AS production_kg,
            COUNT(DISTINCT dd.doff_date, dd.mc_id,
                  COALESCE(LEFT(sp.spell_code, 1), '?')) AS frame_runs
        FROM daily_doff_tbl dd
        INNER JOIN machine_mst m ON m.machine_id = dd.mc_id
        INNER JOIN machine_type_mst mt
                ON mt.machine_type_id = m.machine_type_id
               AND mt.active = 1
               AND mt.machine_type_name = :spinning_type
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        INNER JOIN branch_mst bm ON bm.branch_id = d.branch_id
        LEFT JOIN spell_mst sp ON sp.spell_id = dd.spell
        WHERE dd.doff_date BETWEEN :date_from AND :report_date
          AND (dd.active = 1 OR dd.active IS NULL)
          AND dd.item_id IS NOT NULL
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        GROUP BY COALESCE(LEFT(sp.spell_code, 1), '?')
        """
    )


def get_dpr_spinning_daily_production_query():
    """Per-day spinning production + shifts worked over a short look-back window.

    TKT-000172 — the jute "days of cover" divides closing stock by the report
    date's spinning production, or by the 7-producing-day average when that date
    was a holiday / part day. This returns one row per doff date so the router
    can decide (see pick_cover_basis): the day's kg and how many shift buckets
    spinning ran in, which is what tells a full day from a one-shift Sunday.

    Scope predicate is get_dpr_spinning_mtd_query()'s verbatim (machine -> dept
    -> branch spine, `item_id IS NOT NULL`, A1/A2 collapse to A), so the report
    date's row equals the Spinning section's O/A production printed above it.
    net_weight is kg.

    Binds: :co_id, :branch_id (nullable), :window_from, :report_date,
    :spinning_type.
    """
    return text(
        """
        SELECT
            dd.doff_date AS tran_date,
            ROUND(SUM(dd.net_weight), 3) AS production_kg,
            COUNT(DISTINCT COALESCE(LEFT(sp.spell_code, 1), '?')) AS shifts
        FROM daily_doff_tbl dd
        INNER JOIN machine_mst m ON m.machine_id = dd.mc_id
        INNER JOIN machine_type_mst mt
                ON mt.machine_type_id = m.machine_type_id
               AND mt.active = 1
               AND mt.machine_type_name = :spinning_type
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        INNER JOIN branch_mst bm ON bm.branch_id = d.branch_id
        LEFT JOIN spell_mst sp ON sp.spell_id = dd.spell
        WHERE dd.doff_date BETWEEN :window_from AND :report_date
          AND (dd.active = 1 OR dd.active IS NULL)
          AND dd.item_id IS NOT NULL
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        GROUP BY dd.doff_date
        ORDER BY dd.doff_date
        """
    )


def get_dpr_winding_mtd_query():
    """MTD reconciled winding production per yarn quality (item).

    Same reconciliation model as get_winding_reconciliation_query() — jugar
    open/close adjustments applied once per (branch, date, spell, winder),
    yarn quality falling back to the person→quality map
    (jute_prod_winding_daily_qlty) when the doff row carries no item —
    extended over a date range and rolled up by item. winder_shifts counts
    person-shifts (one winder in two spells = 2), so mtd_kg_per_winder_shift is
    a per-shift figure; the day table's winders/kg_per_winder are per distinct
    PERSON. Different grains by design — the FE labels them differently.

    Binds: :branch_id (required — winding is branch-keyed), :date_from,
    :report_date.
    """
    return text(
        """
        SELECT
            wdr.item_id,
            im.item_code,
            im.item_name,
            ROUND(SUM(wdr.reconciled_qty), 3) AS production_kg,
            COUNT(DISTINCT wdr.tran_date, wdr.spell_id, wdr.eb_id) AS winder_shifts
        FROM (
            SELECT wd.tran_date, wd.branch_id, wd.spell_id, wd.eb_id,
                   COALESCE(MAX(wd.item_id), MAX(dq.item_id)) AS item_id,
                   SUM(wd.production_qty)
                   - COALESCE(MAX(jo.open_w), 0)
                   + COALESCE(MAX(jc.close_w), 0) AS reconciled_qty
            FROM jute_prod_winding_doff wd
            LEFT JOIN (
                SELECT branch_id, tran_date, spell_id, eb_id, MAX(weight) AS open_w
                FROM jute_prod_winding_jugar
                WHERE tran_date BETWEEN :date_from AND :report_date
                  AND open_close = 'O'
                  AND (active = 1 OR active IS NULL)
                  AND branch_id = :branch_id
                GROUP BY branch_id, tran_date, spell_id, eb_id
            ) jo ON jo.branch_id = wd.branch_id
                AND jo.tran_date = wd.tran_date
                AND jo.spell_id = wd.spell_id
                AND jo.eb_id = wd.eb_id
            LEFT JOIN (
                SELECT branch_id, tran_date, spell_id, eb_id, MAX(weight) AS close_w
                FROM jute_prod_winding_jugar
                WHERE tran_date BETWEEN :date_from AND :report_date
                  AND open_close = 'C'
                  AND (active = 1 OR active IS NULL)
                  AND branch_id = :branch_id
                GROUP BY branch_id, tran_date, spell_id, eb_id
            ) jc ON jc.branch_id = wd.branch_id
                AND jc.tran_date = wd.tran_date
                AND jc.spell_id = wd.spell_id
                AND jc.eb_id = wd.eb_id
            LEFT JOIN (
                SELECT branch_id, tran_date, spell_id, eb_id, MAX(item_id) AS item_id
                FROM jute_prod_winding_daily_qlty
                WHERE tran_date BETWEEN :date_from AND :report_date
                  AND branch_id = :branch_id
                  AND (active = 1 OR active IS NULL)
                GROUP BY branch_id, tran_date, spell_id, eb_id
            ) dq ON dq.branch_id = wd.branch_id
                AND dq.tran_date = wd.tran_date
                AND dq.spell_id = wd.spell_id
                AND dq.eb_id = wd.eb_id
            WHERE wd.branch_id = :branch_id
              AND wd.tran_date BETWEEN :date_from AND :report_date
              AND (wd.active = 1 OR wd.active IS NULL)
            GROUP BY wd.tran_date, wd.branch_id, wd.spell_id, wd.eb_id
        ) wdr
        LEFT JOIN item_mst im ON im.item_id = wdr.item_id
        GROUP BY wdr.item_id, im.item_code, im.item_name
        ORDER BY im.item_code
        """
    )


# The weaving stored-jugar formula as ONE SQL expression, in MT. Aliases: wd =
# jute_prod_weaving_daily, q = jute_prod_weaving_quality. total_jugar = cuts *
# njpc + close_jugar - open_jugar - less_production; yds = total_jugar *
# finished_length / njpc; kg = yds * ozs_yds * 28.35 / 1000; MT = kg / 1000.
# Shared by get_dpr_weaving_mtd_query (per quality), the DPR Power line's
# keyed-day cloth tonnes (get_dpr_power_query) and the Power & DG report's
# per-day cloth feed (utility_query.get_power_report_cloth_daily_query), so
# every kWh/t cloth figure divides by one and the same definition.
WEAVING_STORED_JUGAR_MT_SQL = """ROUND(SUM(
                CASE WHEN COALESCE(q.no_of_jugar_per_cut, 0) > 0
                     THEN (wd.cuts * q.no_of_jugar_per_cut
                           + COALESCE(wd.close_jugar, 0)
                           - COALESCE(wd.open_jugar, 0)
                           - COALESCE(wd.less_production, 0))
                          * COALESCE(q.finished_length, 0) / q.no_of_jugar_per_cut
                     ELSE 0 END
                * COALESCE(q.ozs_yds, 0) * 28.35 / 1000 / 1000
            ), 3)"""


def get_dpr_weaving_mtd_query():
    """MTD weaving production per quality from stored columns.

    Uses the exact stored-jugar formula of the weaving day slice
    (total_jugar = cuts * njpc + close_jugar - open_jugar - less_production;
    production_yds = total_jugar * finished_length / njpc;
    kg = yds * ozs_yds * 28.35 / 1000). open_jugar is a stored column
    (Phase 1b), so this stays exact without re-running the slice per day.

    Binds: :co_id, :branch_id (nullable), :date_from, :report_date.
    """
    return text(
        f"""
        SELECT
            q.weaving_quality_id,
            q.weaving_quality_code,
            q.weaving_quality_name,
            {WEAVING_STORED_JUGAR_MT_SQL} AS production_mt
        FROM jute_prod_weaving_daily wd
        INNER JOIN jute_prod_weaving_quality q
                ON q.weaving_quality_id = wd.weaving_quality_id
        WHERE wd.active = 1
          AND wd.co_id = :co_id
          AND wd.tran_date BETWEEN :date_from AND :report_date
          -- branch NULL-tolerant by design (parity with weaving_day_slice_sql)
          AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
        GROUP BY q.weaving_quality_id, q.weaving_quality_code, q.weaving_quality_name
        ORDER BY q.weaving_quality_code
        """
    )


def get_dpr_finishing_query():
    """Finishing production per process (day + MTD in one pass).

    Bale press output = process 'balepress' rows (prod_uom 'bale'); the
    frontend surfaces that row as press production.

    Binds: :co_id, :branch_id (nullable), :date_from, :report_date.
    """
    return text(
        """
        SELECT
            fd.process,
            fd.prod_uom,
            ROUND(SUM(CASE WHEN fd.tran_date = :report_date THEN fd.prod_qty ELSE 0 END), 2) AS day_qty,
            ROUND(SUM(CASE WHEN fd.tran_date = :report_date THEN COALESCE(fd.prod_wt_kg, 0) ELSE 0 END), 2) AS day_wt_kg,
            ROUND(SUM(fd.prod_qty), 2) AS mtd_qty,
            ROUND(SUM(COALESCE(fd.prod_wt_kg, 0)), 2) AS mtd_wt_kg
        FROM jute_prod_finishing_daily fd
        WHERE fd.active = 1
          AND fd.co_id = :co_id
          AND fd.tran_date BETWEEN :date_from AND :report_date
          -- branch NULL-tolerant by design (branch_id is nullable/derived here)
          AND (:branch_id IS NULL OR fd.branch_id = :branch_id OR fd.branch_id IS NULL)
        GROUP BY fd.process, fd.prod_uom
        ORDER BY fd.process
        """
    )


def get_dpr_yarn_heavy_light_query():
    """Yarn heavy-light per quality from R-08-16 SQC counts.

    std_count comes from jute_yarn_mst.jute_yarn_count (the same source the
    spinning day slice uses), deduped to one row per item (the table's item_id
    is non-unique, and a raw join would fan one item into a row per distinct
    count value); obs/corr are AVG over the day's tests, MTD over the month.
    The heavy/light verdict ((corr - std) / std * 100, ±0.2 lb dead band) is
    computed in the router — the `$$`/`$` flags were never persisted (R-08-16
    doc §36).

    Binds: :co_id, :branch_id (nullable), :date_from, :report_date.
    """
    return text(
        """
        SELECT
            sc.item_id,
            im.item_code,
            im.item_name,
            ym.jute_yarn_count AS std_count,
            ROUND(AVG(CASE WHEN sc.entry_date = :report_date THEN sc.observed_count END), 2) AS obs_count,
            ROUND(AVG(CASE WHEN sc.entry_date = :report_date THEN sc.corrected_count END), 2) AS corr_count,
            COUNT(CASE WHEN sc.entry_date = :report_date THEN 1 END) AS day_tests,
            ROUND(AVG(sc.observed_count), 2) AS mtd_obs_count,
            ROUND(AVG(sc.corrected_count), 2) AS mtd_corr_count
        FROM jute_sqc_spinning_count sc
        LEFT JOIN item_mst im ON im.item_id = sc.item_id
        LEFT JOIN (
            SELECT item_id, MAX(jute_yarn_count) AS jute_yarn_count
            FROM jute_yarn_mst
            GROUP BY item_id
        ) ym ON ym.item_id = sc.item_id
        WHERE sc.active = 1
          AND sc.co_id = :co_id
          AND sc.entry_date BETWEEN :date_from AND :report_date
          -- branch NULL-tolerant by design (branch_id is nullable on SQC rows)
          AND (:branch_id IS NULL OR sc.branch_id = :branch_id OR sc.branch_id IS NULL)
        GROUP BY sc.item_id, im.item_code, im.item_name, ym.jute_yarn_count
        ORDER BY im.item_code
        """
    )


def get_dpr_hands_query():
    """Worker hands per shift bucket (day + MTD in one pass).

    Hands = worked hours / 8 — every shift is an 8-hour shift (same flat /8
    the HRMS attendance summary uses; the old printed report's 7.5h C-shift
    norm over-counted C-shift workers, whose rows carry working_hours = 8).
    Approved attendance only (status_id = 3), same as the HRMS reports.

    Binds: :co_id, :branch_id (nullable), :date_from, :report_date.
    """
    return text(
        """
        SELECT
            LEFT(COALESCE(sp.spell_code, da.spell), 1) AS shift_bucket,
            ROUND(SUM(CASE WHEN da.attendance_date = :report_date
                           THEN da.working_hours - COALESCE(da.idle_hours, 0)
                           ELSE 0 END), 2) AS day_hours,
            ROUND(SUM(CASE WHEN da.attendance_date = :report_date
                           THEN (da.working_hours - COALESCE(da.idle_hours, 0)) / 8
                           ELSE 0 END), 2) AS day_hands,
            ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)), 2) AS mtd_hours,
            ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)) / 8, 2) AS mtd_hands
        FROM daily_attendance da
        INNER JOIN branch_mst bm ON bm.branch_id = da.branch_id
        LEFT JOIN spell_mst sp ON sp.spell_id = da.spell_id
        WHERE da.is_active = 1
          AND da.status_id = 3
          AND da.attendance_date BETWEEN :date_from AND :report_date
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR da.branch_id = :branch_id)
        GROUP BY LEFT(COALESCE(sp.spell_code, da.spell), 1)
        """
    )


def get_dpr_hands_by_dept_query():
    """The hands of get_dpr_hands_query() again, per DEPARTMENT (day + MTD).

    TKT-000185 — the stage-wise hands/MT needs to know which hands made yarn
    and which made cloth. Department = dept_mst via the worked designation
    (designation_mst.dept_id), the route the Hands Report takes;
    daily_attendance.worked_department_id is a legacy id space that does not
    join to dept_mst and is ignored. Same scope, same flat /8, same
    approved-only cut as the shift query, so the two totals agree to the hand.
    Rows without a resolvable department come back with dept_desc NULL and
    land in "common" (dpr_hands_stages.classify_dept).

    Binds: :co_id, :branch_id (nullable), :date_from, :report_date.
    """
    return text(
        """
        SELECT
            dg.dept_id,
            dp.dept_desc,
            ROUND(SUM(CASE WHEN da.attendance_date = :report_date
                           THEN (da.working_hours - COALESCE(da.idle_hours, 0)) / 8
                           ELSE 0 END), 2) AS day_hands,
            ROUND(SUM((da.working_hours - COALESCE(da.idle_hours, 0)) / 8), 2) AS mtd_hands
        FROM daily_attendance da
        INNER JOIN branch_mst bm ON bm.branch_id = da.branch_id
        LEFT JOIN designation_mst dg ON dg.designation_id = da.worked_designation_id
        LEFT JOIN dept_mst dp ON dp.dept_id = dg.dept_id
        WHERE da.is_active = 1
          AND da.status_id = 3
          AND da.attendance_date BETWEEN :date_from AND :report_date
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR da.branch_id = :branch_id)
        GROUP BY dg.dept_id, dp.dept_desc
        ORDER BY dp.dept_desc
        """
    )


def get_dpr_purchased_yarn_issue_query():
    """Purchased yarn issued to the floor — approved Yarn Issues net of returns
    (kg), the report day and MTD in one pass (TKT-000185).

    Bought yarn goes godown -> beaming through jute_yarn_issue, never through
    winding, so this is the tonnage weaving used that no spinning hand made.
    Approved documents only (status 3), the same cut the Purchased Yarn Ledger
    takes; a RETURN line counts negative. The tables exist only on tenants that
    ran 20260925_yarn_purchase_issue_tables.sql — the caller degrades to
    "not tracked" on 1146 (see dpr_reports.fetch_purchased_yarn_issue).

    Binds: :co_id, :branch_id (nullable), :date_from, :report_date.
    """
    return text(
        f"""
        SELECT
            ROUND(SUM(CASE WHEN i.tran_date = :report_date
                           THEN CASE WHEN i.issue_type = '{YARN_ISSUE_TYPE_RETURN}'
                                     THEN -li.weight_kg ELSE li.weight_kg END
                           ELSE 0 END), 3) AS day_kg,
            ROUND(SUM(CASE WHEN i.issue_type = '{YARN_ISSUE_TYPE_RETURN}'
                           THEN -li.weight_kg ELSE li.weight_kg END), 3) AS mtd_kg
        FROM jute_yarn_issue i
        INNER JOIN jute_yarn_issue_li li
                ON li.yarn_issue_id = i.yarn_issue_id
               AND li.active = 1
        WHERE i.active = 1
          AND i.status_id = {YARN_DOC_STATUS_APPROVED}
          AND i.co_id = :co_id
          AND (:branch_id IS NULL OR i.branch_id = :branch_id)
          AND i.tran_date BETWEEN :date_from AND :report_date
        """
    )


def get_dpr_shifts_worked_query():
    """Distinct (section, date, shift bucket) the mill actually produced in.

    TKT-000152 — the DPR carried hands and output but never said how many
    shifts the mill ran, so "today vs to date" per-shift averages could not be
    checked against the printed sheet. A shift counts as WORKED when that
    section has at least one active production entry in it; attendance is not
    the basis (staff attendance is logged on shifts no machine ran, and it is
    only visible once HR approves it, which lags the production day).

    Each leg carries the exact scope predicate of that section's own DPR query,
    so the counts cannot disagree with the tables above them: spinning rides
    the machine -> dept -> branch spine with `item_id IS NOT NULL`; weaving and
    finishing stay branch-NULL-tolerant; winding is co+branch keyed. A1/A2
    collapse to A (LEFT(spell_code, 1)), the DPR's shift_bucket convention;
    a spell that does not resolve buckets as '?' and still counts.

    Rows (<= sections x days x buckets) are rolled up in the router so the mill
    total is the UNION across sections, not the sum of them.

    Binds: :co_id, :branch_id (nullable), :date_from, :report_date,
    :spinning_type.
    """
    return text(
        """
        SELECT DISTINCT x.section, x.tran_date, x.shift_bucket
        FROM (
            SELECT 'Spinning' AS section, dd.doff_date AS tran_date,
                   COALESCE(LEFT(sp.spell_code, 1), '?') AS shift_bucket
            FROM daily_doff_tbl dd
            INNER JOIN machine_mst m ON m.machine_id = dd.mc_id
            INNER JOIN machine_type_mst mt
                    ON mt.machine_type_id = m.machine_type_id
                   AND mt.active = 1
                   AND mt.machine_type_name = :spinning_type
            INNER JOIN dept_mst d ON d.dept_id = m.dept_id
            INNER JOIN branch_mst bm ON bm.branch_id = d.branch_id
            LEFT JOIN spell_mst sp ON sp.spell_id = dd.spell
            WHERE dd.doff_date BETWEEN :date_from AND :report_date
              AND (dd.active = 1 OR dd.active IS NULL)
              AND dd.item_id IS NOT NULL
              AND bm.co_id = :co_id
              AND (:branch_id IS NULL OR d.branch_id = :branch_id)

            UNION

            SELECT 'Winding', wd.tran_date,
                   COALESCE(LEFT(sp.spell_code, 1), '?')
            FROM jute_prod_winding_doff wd
            LEFT JOIN spell_mst sp ON sp.spell_id = wd.spell_id
            WHERE wd.tran_date BETWEEN :date_from AND :report_date
              AND (wd.active = 1 OR wd.active IS NULL)
              AND wd.co_id = :co_id
              AND (:branch_id IS NULL OR wd.branch_id = :branch_id)

            UNION

            SELECT 'Weaving', wv.tran_date,
                   COALESCE(LEFT(sp.spell_code, 1), '?')
            FROM jute_prod_weaving_daily wv
            LEFT JOIN spell_mst sp ON sp.spell_id = wv.spell_id
            WHERE wv.active = 1
              AND wv.co_id = :co_id
              AND wv.tran_date BETWEEN :date_from AND :report_date
              AND (:branch_id IS NULL OR wv.branch_id = :branch_id OR wv.branch_id IS NULL)

            UNION

            SELECT 'Finishing', fd.tran_date,
                   COALESCE(LEFT(sp.spell_code, 1), '?')
            FROM jute_prod_finishing_daily fd
            LEFT JOIN spell_mst sp ON sp.spell_id = fd.spell_id
            WHERE fd.active = 1
              AND fd.co_id = :co_id
              AND fd.tran_date BETWEEN :date_from AND :report_date
              AND (:branch_id IS NULL OR fd.branch_id = :branch_id OR fd.branch_id IS NULL)
        ) x
        ORDER BY x.section, x.tran_date, x.shift_bucket
        """
    )


def get_dpr_emulsion_query():
    """Batching-oil (JBO / Ecofin) usage — the report day plus the MTD window.

    Source is the R-08-02 emulsion log (`jute_sqc_emulsion`), a daily recipe
    sheet, not a sampled QC test — so the day figure is a SUM over that date's
    rows (one per mixing machine when mc_id is filled) and the percentage an
    AVG. `oil_pct_in_emulsion` is the MEASURED strength; the derived
    oil_used/tank_capacity ratio ships beside it because the two disagree in
    practice and the mill reads the measured one.

    Rows carry the std band per entry (std_oil_pct_low/high), so the band comes
    back with the data rather than from a constant that could drift from what
    the operator was working to.

    Binds: :co_id, :branch_id (nullable), :date_from, :report_date.
    """
    return text(
        """
        SELECT
            SUM(CASE WHEN e.entry_date = :report_date THEN COALESCE(e.jbo_ltr, 0) END) AS day_jbo_ltr,
            SUM(CASE WHEN e.entry_date = :report_date THEN COALESCE(e.eco_fin_used_ltr, 0) END) AS day_eco_fin_ltr,
            SUM(CASE WHEN e.entry_date = :report_date THEN COALESCE(e.oil_used_ltr, 0) END) AS day_oil_used_ltr,
            SUM(CASE WHEN e.entry_date = :report_date THEN COALESCE(e.tank_capacity_ltr, 0) END) AS day_tank_capacity_ltr,
            AVG(CASE WHEN e.entry_date = :report_date THEN e.oil_pct_in_emulsion END) AS day_oil_pct,
            AVG(CASE WHEN e.entry_date = :report_date THEN e.std_oil_pct_low END) AS day_std_low,
            AVG(CASE WHEN e.entry_date = :report_date THEN e.std_oil_pct_high END) AS day_std_high,
            COUNT(CASE WHEN e.entry_date = :report_date THEN 1 END) AS day_rows,
            SUM(COALESCE(e.jbo_ltr, 0)) AS mtd_jbo_ltr,
            SUM(COALESCE(e.eco_fin_used_ltr, 0)) AS mtd_eco_fin_ltr,
            AVG(e.oil_pct_in_emulsion) AS mtd_avg_oil_pct,
            COUNT(DISTINCT e.entry_date) AS mtd_days
        FROM jute_sqc_emulsion e
        WHERE e.active = 1
          AND e.co_id = :co_id
          AND e.entry_date BETWEEN :date_from AND :report_date
          -- branch NULL-tolerant by design (parity with the other DPR sections)
          AND (:branch_id IS NULL OR e.branch_id = :branch_id OR e.branch_id IS NULL)
        """
    )


def get_dpr_machine_count_query():
    """Installed (active) machine count per machine type — the utilization
    denominators (total spinning frames, total looms).

    Binds: :co_id, :branch_id (nullable), :spinning_type, :weaving_type.
    """
    return text(
        """
        SELECT mt.machine_type_name, COUNT(*) AS total_machines
        FROM machine_mst m
        INNER JOIN machine_type_mst mt
                ON mt.machine_type_id = m.machine_type_id AND mt.active = 1
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        INNER JOIN branch_mst bm ON bm.branch_id = d.branch_id
        WHERE m.active = 1
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
          AND mt.machine_type_name IN (:spinning_type, :weaving_type)
        GROUP BY mt.machine_type_name
        """
    )


def get_dpr_power_query():
    """Power readings for the DPR's Power line (ITM-004): the report day's and
    the MTD kWh, DG kWh, DG hours and DG litres in ONE row.

    jute_power_reading rows are per meter per day (optionally per spell); DG
    kWh is the share on DG-type meters (jute_power_meter_mst.meter_type = 'DG').
    day_rows = 0 means nothing was keyed for the report date ("not keyed");
    mtd_days counts the distinct dates keyed in the month so far.

    mtd_keyed_cloth_mt / mtd_keyed_yarn_kg are the weaving MT and spinning kg
    produced on THOSE keyed dates only (two scalar subqueries over the same
    keyed-date set), so the MTD kWh/t divides kWh by the tonnes of the days
    the kWh covers -- the same rule as /power_report's MTD block. Dividing by
    the full month's tonnes would understate kWh/t by keyed/total days. The
    cloth expression is WEAVING_STORED_JUGAR_MT_SQL (one definition); the
    spinning scope is get_dpr_spinning_daily_production_query's verbatim
    (machine -> dept -> branch spine, item_id IS NOT NULL).

    The table exists only on tenants that ran 20261018_jute_power_reading.sql —
    the caller (dpr_reports.fetch_power_readings) swallows the missing-table
    error and the sheet prints "not keyed".

    Binds: :co_id, :branch_id (nullable), :date_from, :report_date,
    :spinning_type (machine_type_mst.machine_type_name).
    """
    return text(
        f"""
        SELECT
            ROUND(SUM(CASE WHEN r.tran_date = :report_date THEN r.kwh ELSE 0 END), 2) AS day_kwh,
            ROUND(SUM(CASE WHEN r.tran_date = :report_date AND m.meter_type = 'DG' THEN r.kwh ELSE 0 END), 2) AS day_dg_kwh,
            ROUND(SUM(CASE WHEN r.tran_date = :report_date THEN COALESCE(r.dg_hours, 0) ELSE 0 END), 2) AS day_dg_hours,
            ROUND(SUM(CASE WHEN r.tran_date = :report_date THEN COALESCE(r.dg_litres, 0) ELSE 0 END), 2) AS day_dg_litres,
            SUM(CASE WHEN r.tran_date = :report_date THEN 1 ELSE 0 END) AS day_rows,
            ROUND(SUM(r.kwh), 2) AS mtd_kwh,
            ROUND(SUM(CASE WHEN m.meter_type = 'DG' THEN r.kwh ELSE 0 END), 2) AS mtd_dg_kwh,
            ROUND(SUM(COALESCE(r.dg_hours, 0)), 2) AS mtd_dg_hours,
            ROUND(SUM(COALESCE(r.dg_litres, 0)), 2) AS mtd_dg_litres,
            COUNT(DISTINCT r.tran_date) AS mtd_days,
            (
                SELECT {WEAVING_STORED_JUGAR_MT_SQL}
                FROM jute_prod_weaving_daily wd
                INNER JOIN jute_prod_weaving_quality q
                        ON q.weaving_quality_id = wd.weaving_quality_id
                WHERE wd.active = 1
                  AND wd.co_id = :co_id
                  AND wd.tran_date BETWEEN :date_from AND :report_date
                  -- branch NULL-tolerant by design (parity with weaving_day_slice_sql)
                  AND (:branch_id IS NULL OR wd.branch_id = :branch_id OR wd.branch_id IS NULL)
                  AND wd.tran_date IN (
                      SELECT k.tran_date FROM jute_power_reading k
                      WHERE k.active = 1
                        AND k.co_id = :co_id
                        AND k.tran_date BETWEEN :date_from AND :report_date
                        AND (:branch_id IS NULL OR k.branch_id = :branch_id)
                  )
            ) AS mtd_keyed_cloth_mt,
            (
                SELECT ROUND(SUM(dd.net_weight), 3)
                FROM daily_doff_tbl dd
                INNER JOIN machine_mst m2 ON m2.machine_id = dd.mc_id
                INNER JOIN machine_type_mst mt
                        ON mt.machine_type_id = m2.machine_type_id
                       AND mt.active = 1
                       AND mt.machine_type_name = :spinning_type
                INNER JOIN dept_mst d ON d.dept_id = m2.dept_id
                INNER JOIN branch_mst bm ON bm.branch_id = d.branch_id
                WHERE dd.doff_date BETWEEN :date_from AND :report_date
                  AND (dd.active = 1 OR dd.active IS NULL)
                  AND dd.item_id IS NOT NULL
                  AND bm.co_id = :co_id
                  AND (:branch_id IS NULL OR d.branch_id = :branch_id)
                  AND dd.doff_date IN (
                      SELECT k.tran_date FROM jute_power_reading k
                      WHERE k.active = 1
                        AND k.co_id = :co_id
                        AND k.tran_date BETWEEN :date_from AND :report_date
                        AND (:branch_id IS NULL OR k.branch_id = :branch_id)
                  )
            ) AS mtd_keyed_yarn_kg
        FROM jute_power_reading r
        LEFT JOIN jute_power_meter_mst m
               ON m.co_id = r.co_id
              AND m.branch_id = r.branch_id
              AND m.meter_code = r.meter_code
        WHERE r.active = 1
          AND r.co_id = :co_id
          AND r.tran_date BETWEEN :date_from AND :report_date
          AND (:branch_id IS NULL OR r.branch_id = :branch_id)
        """
    )

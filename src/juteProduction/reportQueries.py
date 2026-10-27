"""Raw SQL builders for Jute Production reports.

The ``*_checklist`` builders below back the wage-reconciliation checklists
(ports of the old PHP ones). They are plain exception grids: production is
matched against attendance both ways and the router turns the mismatches into
remarks. Everything is aggregated in SQL — ``daily_attendance`` is ~1.2M rows on
sls, so every builder filters on the date column first (its leading index
column) and never hands raw rows to Python for grouping.

``daily_attendance`` / ``daily_ebmc_attendance`` carry ``branch_id`` but NO
``co_id``, so the company scope is applied as ``branch_id IN (branches of
:co_id)``.
"""

from sqlalchemy import bindparam, text

from src.juteProduction.query import quality_label_sql


def get_maturity_time_report_query():
    """Per-issue × per-production-entry maturity vs target maturity, for a given issue date."""
    return text(
        f"""
        SELECT
            r.spreader_roll_issue_id,
            r.entry_id_grp,
            r.issue_date,
            r.issue_time,
            r.spell AS issue_spell,
            r.no_of_rolls AS issue_rolls,
            r.wt_per_roll,
            r.issue_dt,
            p.bin_id,
            b.bin_code,
            p.item_id,
            i.item_name,
            p.item_id_2,
            p.ratio_1,
            p.ratio_2,
            i2.item_name AS item_name_2,
            {quality_label_sql('p', 'i', 'i2')} AS quality_label,
            p.entry_date AS prod_entry_date,
            p.entry_time AS prod_entry_time,
            p.entry_dt AS prod_entry_dt,
            p.no_of_rolls AS prod_rolls,
            TIMESTAMPDIFF(HOUR, p.entry_dt, r.issue_dt) AS maturity_hrs,
            ROUND(CASE WHEN p.item_id_2 IS NULL THEN COALESCE(mm.maturity_hours, 48)
                 ELSE (COALESCE(mm.maturity_hours, mm2.maturity_hours, 48) * p.ratio_1
                     + COALESCE(mm2.maturity_hours, mm.maturity_hours, 48) * p.ratio_2)
                     / (p.ratio_1 + p.ratio_2) END) AS target_maturity_hrs
        FROM spreader_roll_issue r
        LEFT JOIN spreader_prod_entry p
               ON p.entry_id_grp = r.entry_id_grp
              AND p.wt_per_roll = r.wt_per_roll
              AND p.co_id = r.co_id
              AND p.active = 1
        LEFT JOIN spreader_bin_mst b ON b.bin_id = p.bin_id
        LEFT JOIN item_mst i ON i.item_id = p.item_id
        LEFT JOIN item_maturity_mst mm
               ON mm.item_id = p.item_id AND mm.co_id = r.co_id AND mm.active = 1
        LEFT JOIN item_mst i2 ON i2.item_id = p.item_id_2
        LEFT JOIN item_maturity_mst mm2
               ON mm2.item_id = p.item_id_2 AND mm2.co_id = r.co_id AND mm2.active = 1
        WHERE r.co_id = :co_id
          AND r.active = 1
          AND r.issue_date = :d
        ORDER BY r.issue_time, r.entry_id_grp, p.entry_dt
        """
    )


def get_spreader_production_report_query():
    """Production rows for a date window covering report_date + early hours of next day (C overflow)."""
    return text(
        f"""
        SELECT
            p.spreader_prod_entry_id,
            p.entry_date,
            p.entry_time,
            p.entry_dt,
            p.spell,
            p.machine_id,
            m.machine_name,
            m.mech_code,
            p.item_id,
            i.item_name,
            p.item_id_2,
            p.ratio_1,
            p.ratio_2,
            i2.item_name AS item_name_2,
            {quality_label_sql('p', 'i', 'i2')} AS quality_label,
            p.no_of_rolls,
            p.wt_per_roll,
            (p.no_of_rolls * p.wt_per_roll) AS weight_kg
        FROM spreader_prod_entry p
        LEFT JOIN machine_mst m ON m.machine_id = p.machine_id
        LEFT JOIN item_mst i ON i.item_id = p.item_id
        LEFT JOIN item_mst i2 ON i2.item_id = p.item_id_2
        WHERE p.co_id = :co_id
          AND p.active = 1
          AND (p.entry_date = :d OR (p.entry_date = DATE_ADD(:d, INTERVAL 1 DAY) AND p.entry_time < 6))
        ORDER BY p.entry_date, p.entry_time
        """
    )


def get_spreader_datewise_production_query():
    """Rolls + kg per calendar date × machine × blend. ``early`` = 1 marks the
    00-05 hours, which the router books to the previous production day (C shift),
    so the window runs one calendar day past ``d_to``."""
    return text(
        f"""
        SELECT
            p.entry_date AS cal_date,
            (p.entry_time < 6) AS early,
            p.machine_id,
            MIN(COALESCE(m.mech_code, m.machine_name)) AS machine_name,
            p.item_id,
            p.item_id_2,
            p.ratio_1,
            p.ratio_2,
            MIN({quality_label_sql('p', 'i', 'i2')}) AS quality_label,
            SUM(p.no_of_rolls) AS rolls,
            SUM(p.no_of_rolls * p.wt_per_roll) AS kg
        FROM spreader_prod_entry p
        LEFT JOIN machine_mst m ON m.machine_id = p.machine_id
        LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN item_mst i ON i.item_id = p.item_id
        LEFT JOIN item_mst i2 ON i2.item_id = p.item_id_2
        WHERE p.co_id = :co_id
          AND p.active = 1
          AND p.entry_date BETWEEN :d_from AND DATE_ADD(:d_to, INTERVAL 1 DAY)
          AND (:branch_id IS NULL OR COALESCE(p.branch_id, d.branch_id) = :branch_id)
          AND (:machine_id IS NULL OR p.machine_id = :machine_id)
        GROUP BY p.entry_date, (p.entry_time < 6), p.machine_id,
                 p.item_id, p.item_id_2, p.ratio_1, p.ratio_2
        """
    )


def get_spreader_datewise_issue_query():
    """Rolls + kg issued per calendar date × blend. An issue carries no spreader
    machine — only the bin group it drew from — so the blend comes from that
    group's production rows (same join as ``get_issues_by_date_query``)."""
    return text(
        f"""
        SELECT
            r.issue_date AS cal_date,
            (r.issue_time < 6) AS early,
            pg.item_id,
            pg.item_id_2,
            pg.ratio_1,
            pg.ratio_2,
            MIN(pg.quality_label) AS quality_label,
            SUM(r.no_of_rolls) AS rolls,
            SUM(r.no_of_rolls * r.wt_per_roll) AS kg
        FROM spreader_roll_issue r
        LEFT JOIN (
            SELECT
                p.entry_id_grp,
                MIN(p.item_id) AS item_id,
                MIN(p.item_id_2) AS item_id_2,
                MIN(p.ratio_1) AS ratio_1,
                MIN(p.ratio_2) AS ratio_2,
                MIN({quality_label_sql('p', 'i', 'i2')}) AS quality_label,
                MIN(COALESCE(p.branch_id, d.branch_id)) AS branch_id
            FROM spreader_prod_entry p
            LEFT JOIN machine_mst m ON m.machine_id = p.machine_id
            LEFT JOIN dept_mst d ON d.dept_id = m.dept_id
            LEFT JOIN item_mst i ON i.item_id = p.item_id
            LEFT JOIN item_mst i2 ON i2.item_id = p.item_id_2
            WHERE p.co_id = :co_id AND p.active = 1
            GROUP BY p.entry_id_grp
        ) pg ON pg.entry_id_grp = r.entry_id_grp
        WHERE r.co_id = :co_id
          AND r.active = 1
          AND r.issue_date BETWEEN :d_from AND DATE_ADD(:d_to, INTERVAL 1 DAY)
          AND (:branch_id IS NULL OR COALESCE(r.branch_id, pg.branch_id) = :branch_id)
        GROUP BY r.issue_date, (r.issue_time < 6),
                 pg.item_id, pg.item_id_2, pg.ratio_1, pg.ratio_2
        """
    )


# =============================================================================
# Wage-reconciliation checklists
# =============================================================================


def _emp_code_expr(alias: str) -> str:
    """emp_code for ``{alias}.eb_id`` — scalar subquery, never a join:
    hrms_ed_official_details holds several rows per eb after a branch transfer
    and a join would fan the grid (and every SUM in it) out."""
    return (
        "(SELECT NULLIF(TRIM(o.emp_code), '') FROM hrms_ed_official_details o "
        f"WHERE o.eb_id = {alias}.eb_id AND o.active = 1 "
        "ORDER BY o.tbl_hrms_ed_official_detail_id DESC LIMIT 1)"
    )


def _eb_no_expr(alias: str) -> str:
    """Attendance rows carry an ``eb_no`` column that live data leaves empty
    (sls: 100% NULL for Aug-2026), so fall back to the HRMS emp_code."""
    return f"COALESCE(NULLIF(TRIM({alias}.eb_no), ''), {_emp_code_expr(alias)})"


# Worker display name, from the LEFT-JOINed hrms_ed_personal_details alias `pd`
# (eb_id is unique there — verified on dev3 + sls — so it cannot fan out).
_WORKER_NAME = "TRIM(CONCAT_WS(' ', pd.first_name, pd.middle_name, pd.last_name))"

# Company scope for the attendance tables, which have no co_id of their own.
_CO_BRANCHES = "(SELECT b.branch_id FROM branch_mst b WHERE b.co_id = :co_id)"

def get_checklist_map_query():
    """Configured ref_ids for one checklist x ref_type, branch-scoped.

    ``jute_prod_checklist_map`` is DB-only config (no UI): which designations
    the finishing/winding checklists count, which machine types beaming counts.
    Same scope idiom as the grids — the requested branch, or every branch of
    :co_id when branch_id is NULL."""
    return text(
        f"""
        SELECT DISTINCT cm.ref_id
        FROM jute_prod_checklist_map cm
        WHERE cm.checklist = :checklist
          AND cm.ref_type = :ref_type
          AND cm.active = 1
          AND (:branch_id IS NULL OR cm.branch_id = :branch_id)
          AND cm.branch_id IN {_CO_BRANCHES}
        """
    )


def _expanding(sql: str, *names: str):
    """text() with the given binds marked expanding (``IN :ids`` lists)."""
    return text(sql).bindparams(*(bindparam(n, expanding=True) for n in names))


def _desig_filter(alias: str, use_map: bool) -> str:
    """Which designations a checklist counts: the config map when the scope has
    rows, else the legacy name pattern."""
    return (
        f"{alias}.designation_id IN :desig_ids"
        if use_map
        else f"LOWER({alias}.desig) LIKE :desig_pattern"
    )


# Beaming machines, by machine TYPE — configured type ids when the scope has
# map rows, else by TYPE NAME (ids differ per tenant: 5 on sls, 12 on dev3, so
# they are never hardcoded).
def _beaming_machines(use_map: bool) -> str:
    return (
        "(SELECT bm.machine_id FROM machine_mst bm "
        " JOIN machine_type_mst bt ON bt.machine_type_id = bm.machine_type_id "
        f" WHERE {_beaming_type_filter('bt', use_map)})"
    )


def _beaming_type_filter(alias: str, use_map: bool) -> str:
    return (
        f"{alias}.machine_type_id IN :mt_ids"
        if use_map
        else f"LOWER({alias}.machine_type_name) = :beaming_type"
    )

_WINDING_DOFF_AGG = """
            SELECT wd.tran_date, wd.spell_id, wd.eb_id,
                   COUNT(*) AS doff_count,
                   SUM(COALESCE(wd.production_qty, 0)) AS production_qty,
                   GROUP_CONCAT(DISTINCT CONCAT_WS(' - ', im.item_name, im.item_code)
                                SEPARATOR ', ') AS quality
            FROM jute_prod_winding_doff wd
            LEFT JOIN item_mst im ON im.item_id = wd.item_id
            WHERE wd.active = 1
              AND wd.co_id = :co_id
              AND wd.tran_date BETWEEN :d_from AND :d_to
              AND (:branch_id IS NULL OR wd.branch_id = :branch_id)
            GROUP BY wd.tran_date, wd.spell_id, wd.eb_id
"""

# Attendance of ANY designation, collapsed to one row per (date, spell, eb).
# Live data has a handful of duplicate keys (sls: 10 in 8050); MIN() on the
# designation just picks one deterministically instead of fanning the grid.
_ATTENDANCE_AGG = f"""
            SELECT da.attendance_date, da.spell_id, da.eb_id,
                   COUNT(*) AS atten_rows,
                   SUM(COALESCE(da.working_hours, 0) - COALESCE(da.idle_hours, 0))
                       AS net_hours,
                   MIN(da.worked_designation_id) AS worked_designation_id
            FROM daily_attendance da
            WHERE da.attendance_date BETWEEN :d_from AND :d_to
              AND da.is_active = 1
              AND (:branch_id IS NULL OR da.branch_id = :branch_id)
              AND da.branch_id IN {_CO_BRANCHES}
            GROUP BY da.attendance_date, da.spell_id, da.eb_id
"""


def get_winding_attendance_checklist_query(use_map: bool = False):
    """Winder-designation attendance rows, each with its winding doff totals.

    Attendance-first direction: "who was marked present as a winder, and did
    they produce?". One row per attendance row (daily_atten_id keeps the rare
    duplicate-key rows distinct).

    ``use_map`` picks the configured designation ids over the name pattern."""
    sql = f"""
        SELECT da.daily_atten_id,
               da.attendance_date AS tran_date,
               da.spell_id,
               sp.spell_code,
               da.eb_id,
               {_eb_no_expr('da')} AS eb_no,
               {_WORKER_NAME} AS worker_name,
               dm.desig AS designation,
               (COALESCE(da.working_hours, 0) - COALESCE(da.idle_hours, 0)) AS net_hours,
               w.quality,
               COALESCE(w.doff_count, 0) AS doff_count,
               COALESCE(w.production_qty, 0) AS production_qty
        FROM daily_attendance da
        JOIN designation_mst dm
          ON dm.designation_id = da.worked_designation_id
         AND {_desig_filter('dm', use_map)}
        LEFT JOIN spell_mst sp ON sp.spell_id = da.spell_id
        LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = da.eb_id
        LEFT JOIN ({_WINDING_DOFF_AGG}) w
               ON w.tran_date = da.attendance_date
              AND w.spell_id = da.spell_id
              AND w.eb_id = da.eb_id
        WHERE da.attendance_date BETWEEN :d_from AND :d_to
          AND da.is_active = 1
          AND (:branch_id IS NULL OR da.branch_id = :branch_id)
          AND da.branch_id IN {_CO_BRANCHES}
        """
    return _expanding(sql, "desig_ids") if use_map else text(sql)


def get_winding_production_checklist_query():
    """Winding doff totals per (eb, date, spell), each with its attendance —
    of ANY designation, so the router can tell "absent" from "present under a
    non-winding designation"."""
    return text(
        f"""
        SELECT w.tran_date,
               w.spell_id,
               sp.spell_code,
               w.eb_id,
               {_emp_code_expr('w')} AS eb_no,
               {_WORKER_NAME} AS worker_name,
               dm.desig AS designation,
               a.worked_designation_id,
               COALESCE(a.atten_rows, 0) AS atten_rows,
               COALESCE(a.net_hours, 0) AS net_hours,
               w.quality,
               w.doff_count,
               w.production_qty
        FROM ({_WINDING_DOFF_AGG}) w
        LEFT JOIN ({_ATTENDANCE_AGG}) a
               ON a.attendance_date = w.tran_date
              AND a.spell_id = w.spell_id
              AND a.eb_id = w.eb_id
        LEFT JOIN designation_mst dm ON dm.designation_id = a.worked_designation_id
        LEFT JOIN spell_mst sp ON sp.spell_id = w.spell_id
        LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = w.eb_id
        """
    )


def get_beaming_checklist_query(use_map: bool = False):
    """One row per beaming machine x date x spell — booked worker hours (from
    the machine-attendance detail) against cuts produced.

    FULL OUTER JOIN emulated with a UNION of the keys from both sides, so a
    machine that only ran and a machine that only booked hours both appear.

    ``use_map`` picks the configured machine-type ids over the type name."""
    beaming_machines = _beaming_machines(use_map)
    hours_agg = f"""
            SELECT x.tran_date, x.spell_id, x.machine_id,
                   SUM(x.net_hours) AS net_hours,
                   GROUP_CONCAT(DISTINCT x.eb_label SEPARATOR ', ') AS workers
            FROM (
                SELECT e.attendace_date AS tran_date, e.spell_id,
                       e.mc_id AS machine_id,
                       {_eb_no_expr('e')} AS eb_label,
                       (COALESCE(da.working_hours, 0) - COALESCE(da.idle_hours, 0))
                           AS net_hours
                FROM daily_ebmc_attendance e
                LEFT JOIN daily_attendance da
                       ON da.daily_atten_id = e.daily_atten_id AND da.is_active = 1
                WHERE e.attendace_date BETWEEN :d_from AND :d_to
                  AND e.is_active = 1
                  AND (:branch_id IS NULL OR e.branch_id = :branch_id)
                  AND e.branch_id IN {_CO_BRANCHES}
                  AND e.mc_id IN {beaming_machines}
            ) x
            GROUP BY x.tran_date, x.spell_id, x.machine_id
"""
    # Inner level is one row per quality, so the code and name lists of a
    # multi-quality machine-spell stay aligned (both ordered by code).
    cuts_agg = """
            SELECT y.tran_date, y.spell_id, y.machine_id,
                   SUM(y.cuts) AS cuts,
                   SUM(y.beams) AS beams,
                   GROUP_CONCAT(y.bm_quality_code ORDER BY y.bm_quality_code
                                SEPARATOR ', ') AS quality_code,
                   GROUP_CONCAT(y.bm_quality_name ORDER BY y.bm_quality_code
                                SEPARATOR ', ') AS quality
            FROM (
                SELECT bd.tran_date, bd.spell_id, bd.machine_id,
                       q.bm_quality_code, q.bm_quality_name,
                       SUM(COALESCE(bd.act_cuts, 0)) AS cuts,
                       SUM(COALESCE(bd.no_of_beam, 0)) AS beams
                FROM jute_prod_beaming_daily bd
                LEFT JOIN jute_prod_bm_quality q ON q.bm_quality_id = bd.bm_quality_id
                WHERE bd.active = 1
                  AND bd.co_id = :co_id
                  AND bd.tran_date BETWEEN :d_from AND :d_to
                  AND (:branch_id IS NULL OR bd.branch_id = :branch_id)
                GROUP BY bd.tran_date, bd.spell_id, bd.machine_id,
                         q.bm_quality_code, q.bm_quality_name
            ) y
            GROUP BY y.tran_date, y.spell_id, y.machine_id
"""
    sql = f"""
        SELECT k.tran_date,
               k.spell_id,
               sp.spell_code,
               k.machine_id,
               m.mech_code,
               m.machine_name,
               h.workers,
               c.quality_code,
               c.quality,
               COALESCE(h.net_hours, 0) AS net_hours,
               COALESCE(c.cuts, 0) AS cuts,
               COALESCE(c.beams, 0) AS beams
        FROM (
            SELECT e.attendace_date AS tran_date, e.spell_id, e.mc_id AS machine_id
            FROM daily_ebmc_attendance e
            WHERE e.attendace_date BETWEEN :d_from AND :d_to
              AND e.is_active = 1
              AND (:branch_id IS NULL OR e.branch_id = :branch_id)
              AND e.branch_id IN {_CO_BRANCHES}
              AND e.mc_id IN {beaming_machines}
            UNION
            SELECT bd.tran_date, bd.spell_id, bd.machine_id
            FROM jute_prod_beaming_daily bd
            WHERE bd.active = 1
              AND bd.co_id = :co_id
              AND bd.tran_date BETWEEN :d_from AND :d_to
              AND (:branch_id IS NULL OR bd.branch_id = :branch_id)
        ) k
        JOIN machine_mst m ON m.machine_id = k.machine_id
        JOIN machine_type_mst mt
          ON mt.machine_type_id = m.machine_type_id
         AND {_beaming_type_filter('mt', use_map)}
        LEFT JOIN spell_mst sp ON sp.spell_id = k.spell_id
        LEFT JOIN ({hours_agg}) h
               ON h.tran_date = k.tran_date
              AND h.spell_id = k.spell_id
              AND h.machine_id = k.machine_id
        LEFT JOIN ({cuts_agg}) c
               ON c.tran_date = k.tran_date
              AND c.spell_id = k.spell_id
              AND c.machine_id = k.machine_id
        """
    return _expanding(sql, "mt_ids") if use_map else text(sql)


def get_finishing_checklist_query():
    """Two-way match between finishing-designation attendance and finishing
    production, in one result set: the key set is the UNION of both sides, so
    production-without-attendance and attendance-without-production come back
    as rows of the same grid.

    ``<=>`` on the production join keeps production rows whose eb_id is NULL
    (the router flags those separately — they can never match attendance).

    The finishing designations come from ``jute_prod_checklist_map`` only —
    there is no name-pattern fallback. The old '%finish%' pattern was 100%
    false positives on sls (carding "FINISHER FEEDER", finishing mechanics)
    and matched none of the real hemming/herackle/sewing staff."""
    finishing_agg = """
            SELECT fd.tran_date, fd.spell_id, fd.eb_id,
                   COUNT(*) AS prod_rows,
                   GROUP_CONCAT(DISTINCT CONCAT_WS(' - ', im.item_name, im.item_code)
                                SEPARATOR ', ') AS quality,
                   SUM(COALESCE(fd.prod_qty, 0)) AS prod_qty,
                   SUM(COALESCE(fd.prod_wt_kg, 0)) AS prod_wt_kg
            FROM jute_prod_finishing_daily fd
            LEFT JOIN jute_prod_finishing_quality fq
                   ON fq.finishing_quality_id = fd.finishing_quality_id
            LEFT JOIN item_mst im ON im.item_id = fq.item_id
            WHERE fd.active = 1
              AND fd.co_id = :co_id
              AND fd.tran_date BETWEEN :d_from AND :d_to
              AND (:branch_id IS NULL OR fd.branch_id = :branch_id)
            GROUP BY fd.tran_date, fd.spell_id, fd.eb_id
"""
    sql = f"""
        SELECT k.tran_date,
               k.spell_id,
               sp.spell_code,
               k.eb_id,
               {_emp_code_expr('k')} AS eb_no,
               {_WORKER_NAME} AS worker_name,
               dm.desig AS designation,
               a.worked_designation_id,
               COALESCE(a.atten_rows, 0) AS atten_rows,
               COALESCE(a.net_hours, 0) AS net_hours,
               f.quality,
               COALESCE(f.prod_rows, 0) AS prod_rows,
               COALESCE(f.prod_qty, 0) AS prod_qty,
               COALESCE(f.prod_wt_kg, 0) AS prod_wt_kg
        FROM (
            SELECT da.attendance_date AS tran_date, da.spell_id, da.eb_id
            FROM daily_attendance da
            WHERE da.worked_designation_id IN :desig_ids
              AND da.attendance_date BETWEEN :d_from AND :d_to
              AND da.is_active = 1
              AND (:branch_id IS NULL OR da.branch_id = :branch_id)
              AND da.branch_id IN {_CO_BRANCHES}
            UNION
            SELECT fd.tran_date, fd.spell_id, fd.eb_id
            FROM jute_prod_finishing_daily fd
            WHERE fd.active = 1
              AND fd.co_id = :co_id
              AND fd.tran_date BETWEEN :d_from AND :d_to
              AND (:branch_id IS NULL OR fd.branch_id = :branch_id)
        ) k
        LEFT JOIN ({_ATTENDANCE_AGG}) a
               ON a.attendance_date = k.tran_date
              AND a.spell_id = k.spell_id
              AND a.eb_id = k.eb_id
        LEFT JOIN ({finishing_agg}) f
               ON f.tran_date = k.tran_date
              AND f.spell_id = k.spell_id
              AND f.eb_id <=> k.eb_id
        LEFT JOIN designation_mst dm ON dm.designation_id = a.worked_designation_id
        LEFT JOIN spell_mst sp ON sp.spell_id = k.spell_id
        LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = k.eb_id
        """
    return _expanding(sql, "desig_ids")


def get_deactivate_rejected_attendance_query():
    """Run by the Attendance Checklist (only) before it selects: rejected
    attendance (status_id 4) in scope is switched to is_active = 0, so the
    checklist and the wages build that follows it agree.

    daily_attendance_unique includes is_active, so a rejected row whose
    inactive twin already exists cannot be flipped; IGNORE skips those rows
    instead of failing the whole search."""
    return text(
        f"""
        UPDATE IGNORE daily_attendance
        SET is_active = 0
        WHERE attendance_date BETWEEN :d_from AND :d_to
          AND is_active = 1
          AND status_id = 4
          AND (:branch_id IS NULL OR branch_id = :branch_id)
          AND branch_id IN {_CO_BRANCHES}
        """
    )


def get_attendance_checklist_query():
    """Plain listing of every active attendance row in the range, with the
    machines the worker was booked on. ``daily_ebmc_attendance.daily_atten_id``
    is fully populated (verified dev3 + sls), so it is the only link used.

    The machine list is a range-scoped derived table, NOT a correlated
    subquery: MySQL ignores the daily_atten_id index for the correlated form
    and scans all ~1.2M ebmc rows per attendance row (measured on sls: >5 min
    vs 0.2s)."""
    machines_agg = """
            SELECT e.daily_atten_id,
                   GROUP_CONCAT(DISTINCT m.machine_name
                                ORDER BY m.machine_name SEPARATOR '/') AS machines
            FROM daily_ebmc_attendance e
            JOIN machine_mst m ON m.machine_id = e.mc_id
            WHERE e.attendace_date BETWEEN :d_from AND :d_to
              AND e.is_active = 1
            GROUP BY e.daily_atten_id
"""
    return text(
        f"""
        SELECT da.daily_atten_id,
               da.attendance_date AS tran_date,
               da.spell_id,
               sp.spell_code,
               da.eb_id,
               {_eb_no_expr('da')} AS eb_no,
               {_WORKER_NAME} AS worker_name,
               COALESCE(dp.dept_desc, sdp.dept_desc) AS department,
               dm.desig AS designation,
               da.attendance_type,
               (COALESCE(da.working_hours, 0) - COALESCE(da.idle_hours, 0)) AS net_hours,
               mc.machines
        FROM daily_attendance da
        LEFT JOIN spell_mst sp ON sp.spell_id = da.spell_id
        LEFT JOIN designation_mst dm ON dm.designation_id = da.worked_designation_id
        LEFT JOIN dept_mst dp ON dp.dept_id = da.worked_department_id
        -- Some branches store a SUB-department id in worked_department_id
        -- (sls br 87: 6074 of 6128 Aug rows), so fall back through
        -- sub_dept_mst to its parent department instead of showing nothing.
        LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = da.worked_department_id
        LEFT JOIN dept_mst sdp ON sdp.dept_id = sd.dept_id
        LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = da.eb_id
        LEFT JOIN ({machines_agg}) mc ON mc.daily_atten_id = da.daily_atten_id
        WHERE da.attendance_date BETWEEN :d_from AND :d_to
          AND da.is_active = 1
          AND (:branch_id IS NULL OR da.branch_id = :branch_id)
          AND da.branch_id IN {_CO_BRANCHES}
        """
    )

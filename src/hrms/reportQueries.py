"""
HRMS / labour report queries (surfaced under Jute Production > Production Reports).
Built on dev3's daily_attendance + employee/department/designation masters.
"""

from sqlalchemy import bindparam, text

from src.hrms.constants import RESIGN_DETAIL_STATUSES

# Lifecycle statuses that mean the employee has left (resigned / terminated /
# retired / blacklisted). IN NOTICE (40) is deliberately not here — they are
# still on the roll and can still be absent.
_SEPARATED_STATUS_SQL = ", ".join(str(s) for s in sorted(RESIGN_DETAIL_STATUSES))


def get_attendance_summary_query():
    """
    Attendance summary: per department + designation, total worked hours
    (working_hours - idle_hours) and "Hands" (worked hours / 8 = man-days).

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
        :spell (str or NULL) - optional spell filter
    """
    sql = """
    SELECT
        sd.sub_dept_desc AS department,
        dsg.desig AS designation,
        ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)), 2) AS work_hours,
        ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)) / 8, 2) AS hands
    FROM daily_attendance AS da
    INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = da.worked_department_id
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = da.worked_designation_id
    LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
    WHERE da.attendance_date >= :date_from
        AND da.attendance_date <= :date_to
        AND da.is_active = 1
        AND da.status_id = 3
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR da.branch_id = :branch_id)
        AND (:spell IS NULL OR da.spell = :spell OR sp.spell_name = :spell)
    GROUP BY da.worked_department_id, sd.sub_dept_desc,
             da.worked_designation_id, dsg.desig
    ORDER BY sd.sub_dept_desc, dsg.desig;
    """
    return text(sql)


def get_worker_master_query():
    """
    Worker / employee master listing with last working day, sourced from the
    dev3 HRMS employee tables (hrms_ed_*). Department resolves via the employee's
    sub-department. Last working day = latest active attendance date.

    Parameters:
        :co_id (int) - required (scoped via the employee's branch)
        :branch_id (int or NULL) - optional
        :search_like (str or NULL) - emp_code / name LIKE
    """
    sql = """
    SELECT
        pd.eb_id,
        od.emp_code,
        CONCAT(
            pd.first_name, ' ',
            IFNULL(pd.middle_name, ''), ' ',
            IFNULL(pd.last_name, '')
        ) AS emp_name,
        pd.gender,
        dm.dept_desc,
        sd.sub_dept_desc,
        dsg.desig AS designation,
        cm.cata_desc AS category,
        pd.date_of_birth,
        od.date_of_join,
        cnt.contractor_name,
        esi.esi_no,
        pf.pf_no,
        pf.pf_uan_no,
        bd.bank_acc_no,
        bd.bank_name,
        ps.NAME AS pay_scheme,
        sm.status_name,
        CASE WHEN pd.active = 1 THEN 'Active' ELSE 'InActive' END AS is_active,
        da.last_working_day
    FROM hrms_ed_personal_details AS pd
    INNER JOIN branch_mst AS bm ON bm.branch_id = pd.branch_id
    LEFT JOIN hrms_ed_official_details AS od
        ON od.eb_id = pd.eb_id AND od.active = 1
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = od.sub_dept_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = sd.dept_id
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = od.designation_id
    LEFT JOIN category_mst AS cm ON cm.cata_id = od.catagory_id
    LEFT JOIN contractor_mst AS cnt ON cnt.cont_id = od.contractor_id
    LEFT JOIN hrms_ed_esi AS esi ON esi.eb_id = pd.eb_id AND esi.active = 1
    LEFT JOIN hrms_ed_pf AS pf ON pf.eb_id = pd.eb_id AND pf.active = 1
    LEFT JOIN hrms_ed_bank_details AS bd ON bd.eb_id = pd.eb_id AND bd.active = 1
    LEFT JOIN pay_employee_payscheme AS pep
        ON pep.EMPLOYEEID = pd.eb_id AND pep.STATUS = 1
    LEFT JOIN pay_scheme AS ps ON ps.ID = pep.PAY_SCHEME_ID
    LEFT JOIN status_mst AS sm ON sm.status_id = pd.status_id
    LEFT JOIN (
        SELECT eb_id, MAX(attendance_date) AS last_working_day
        FROM daily_attendance WHERE is_active = 1 GROUP BY eb_id
    ) AS da ON da.eb_id = pd.eb_id
    WHERE pd.active = 1
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR pd.branch_id = :branch_id)
        AND (
            :search_like IS NULL
            OR od.emp_code LIKE :search_like
            OR CONCAT(pd.first_name, ' ', IFNULL(pd.last_name, '')) LIKE :search_like
        )
    ORDER BY od.emp_code, pd.eb_id;
    """
    return text(sql)


def get_man_machine_query():
    """
    Man-machine deployment (dev3-native): per machine, designation and spell,
    the number of workers deployed (hands) and total machine stoppage hours,
    from daily_ebmc_attendance.

    NOTE: the legacy report 684 pivoted precomputed shift-hands vs targets from a
    separate EMPMILL12 database that dev3 does not have; this is the equivalent
    built from dev3's own machine-attendance data.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = """
    SELECT
        mm.machine_name,
        dm.dept_desc AS department,
        dsg.desig AS designation,
        COALESCE(sp.spell_name, dea.spell) AS spell,
        COUNT(DISTINCT dea.eb_id) AS hands,
        ROUND(SUM(COALESCE(dea.mc_stoppage_hours, 0)), 2) AS stoppage_hours
    FROM daily_ebmc_attendance AS dea
    INNER JOIN branch_mst AS bm ON bm.branch_id = dea.branch_id
    LEFT JOIN machine_mst AS mm ON mm.machine_id = dea.mc_id
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = dea.designation_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = dsg.dept_id
    LEFT JOIN spell_mst AS sp ON sp.spell_id = dea.spell_id
    WHERE dea.attendace_date >= :date_from
        AND dea.attendace_date <= :date_to
        AND dea.is_active = 1
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR dea.branch_id = :branch_id)
    GROUP BY dea.mc_id, mm.machine_name, dm.dept_desc,
             dea.designation_id, dsg.desig, COALESCE(sp.spell_name, dea.spell)
    ORDER BY mm.machine_name, dsg.desig, spell;
    """
    return text(sql)


def get_attendance_register_query():
    """
    Attendance register: flat daily_attendance detail rows (one per punch/spell)
    with employee name, department, designation, mark (P/A/HD/L), spell and hours.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = """
    SELECT
        da.daily_atten_id AS tran_no,
        COALESCE(od.emp_code, da.eb_no, da.eb_code) AS eb_no,
        CONCAT(
            IFNULL(pd.first_name, ''), ' ',
            IFNULL(pd.middle_name, ''), ' ',
            IFNULL(pd.last_name, '')
        ) AS emp_name,
        da.attendance_date,
        sd.sub_dept_desc AS department,
        dsg.desig AS designation,
        da.attendance_mark AS mark,
        COALESCE(sp.spell_name, da.spell) AS spell,
        da.idle_hours,
        da.spell_hours,
        da.working_hours,
        sm.status_name
    FROM daily_attendance AS da
    INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
    LEFT JOIN hrms_ed_personal_details AS pd ON pd.eb_id = da.eb_id
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = da.eb_id AND od.active = 1
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = da.worked_department_id
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = da.worked_designation_id
    LEFT JOIN status_mst AS sm ON sm.status_id = da.status_id
    LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
    WHERE da.attendance_date >= :date_from
        AND da.attendance_date <= :date_to
        AND da.is_active = 1
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR da.branch_id = :branch_id)
    ORDER BY da.attendance_date, da.eb_no;
    """
    return text(sql)


def get_employee_working_query():
    """
    Employee working details (report 650): per employee per month, work days
    (worked hours / 7.5 for spell 'C', / 8 otherwise), leave days (approved
    leaves), and total. Holiday days are 0 — the tenant has no holiday table.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = """
    SELECT
        w.yearmn,
        od.emp_code,
        CONCAT(
            IFNULL(pd.first_name, ''), ' ',
            IFNULL(pd.middle_name, ''), ' ',
            IFNULL(pd.last_name, '')
        ) AS emp_name,
        dm.dept_desc AS department,
        cm.cata_desc AS category,
        sm.status_name,
        w.wdays,
        w.lvdays,
        w.hldays,
        (w.wdays + w.lvdays + w.hldays) AS total_days
    FROM (
        SELECT yearmn, eb_id,
               SUM(wdays) AS wdays, SUM(lvdays) AS lvdays, SUM(hldays) AS hldays
        FROM (
            SELECT
                CONCAT(YEAR(da.attendance_date), '-',
                       LPAD(MONTH(da.attendance_date), 2, '0')) AS yearmn,
                da.eb_id,
                CEIL(SUM(da.working_hours - COALESCE(da.idle_hours, 0)) / 7.5) AS wdays,
                0 AS lvdays, 0 AS hldays
            FROM daily_attendance AS da
            INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
            LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
            WHERE da.is_active = 1 AND bm.co_id = :co_id
                AND (:branch_id IS NULL OR da.branch_id = :branch_id)
                AND da.attendance_date BETWEEN :date_from AND :date_to
                AND (da.spell = 'C' OR sp.spell_code = 'C')
                AND da.attendance_type = 'R'
            GROUP BY yearmn, da.eb_id
            UNION ALL
            SELECT
                CONCAT(YEAR(da.attendance_date), '-',
                       LPAD(MONTH(da.attendance_date), 2, '0')) AS yearmn,
                da.eb_id,
                CEIL(SUM(da.working_hours - COALESCE(da.idle_hours, 0)) / 8) AS wdays,
                0, 0
            FROM daily_attendance AS da
            INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
            LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
            WHERE da.is_active = 1 AND bm.co_id = :co_id
                AND (:branch_id IS NULL OR da.branch_id = :branch_id)
                AND da.attendance_date BETWEEN :date_from AND :date_to
                AND COALESCE(da.spell, '') <> 'C' AND COALESCE(sp.spell_code, '') <> 'C'
                AND da.attendance_type = 'R'
            GROUP BY yearmn, da.eb_id
            UNION ALL
            SELECT
                CONCAT(YEAR(ltd.leave_date), '-',
                       LPAD(MONTH(ltd.leave_date), 2, '0')) AS yearmn,
                lt.eb_id, 0, COUNT(*) AS lvdays, 0
            FROM leave_tran_details AS ltd
            INNER JOIN leave_transactions AS lt
                ON lt.leave_transaction_id = ltd.ltran_id
            INNER JOIN branch_mst AS bm ON bm.branch_id = lt.branch_id
            WHERE ltd.is_active = 1 AND lt.status = 3 AND bm.co_id = :co_id
                AND (:branch_id IS NULL OR lt.branch_id = :branch_id)
                AND ltd.leave_date BETWEEN :date_from AND :date_to
            GROUP BY yearmn, lt.eb_id
        ) AS g
        GROUP BY yearmn, eb_id
    ) AS w
    LEFT JOIN hrms_ed_personal_details AS pd ON pd.eb_id = w.eb_id
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = w.eb_id AND od.active = 1
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = od.sub_dept_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = sd.dept_id
    LEFT JOIN category_mst AS cm ON cm.cata_id = od.catagory_id
    LEFT JOIN status_mst AS sm ON sm.status_id = pd.status_id
    ORDER BY w.yearmn, od.emp_code, w.eb_id;
    """
    return text(sql)


def get_employee_headcount_query():
    """
    Employee headcount at the finest grain (department, sub-department,
    designation, category) for active employees. Feeds the legacy summary
    reports 505/506/508/509/517 — each frontend page aggregates this grain
    differently (by dept, by category, by designation, dept x category pivot).

    Parameters:
        :co_id (int) - required (scoped via the employee's branch)
        :branch_id (int or NULL) - optional
    """
    sql = """
    SELECT
        dm.dept_desc AS department,
        sd.sub_dept_desc AS sub_department,
        dsg.desig AS designation,
        cm.cata_desc AS category,
        COUNT(*) AS emp_count
    FROM hrms_ed_personal_details AS pd
    INNER JOIN branch_mst AS bm ON bm.branch_id = pd.branch_id
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = pd.eb_id AND od.active = 1
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = od.sub_dept_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = sd.dept_id
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = od.designation_id
    LEFT JOIN category_mst AS cm ON cm.cata_id = od.catagory_id
    WHERE pd.active = 1
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR pd.branch_id = :branch_id)
    GROUP BY dm.dept_desc, sd.sub_dept_desc, dsg.desig, cm.cata_desc
    ORDER BY dm.dept_desc, sd.sub_dept_desc, dsg.desig, cm.cata_desc;
    """
    return text(sql)


def get_spell_wise_query():
    """
    Spell-wise hands (legacy report 559): per worked department + designation
    and spell, hands = worked hours / 8. Long format — the frontend pivots
    spells into columns (spell names are tenant data, not fixed).

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = """
    SELECT
        sd.sub_dept_desc AS department,
        dsg.desig AS designation,
        COALESCE(sp.spell_name, da.spell) AS spell,
        ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)) / 8, 2) AS hands
    FROM daily_attendance AS da
    INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = da.worked_department_id
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = da.worked_designation_id
    LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
    WHERE da.attendance_date BETWEEN :date_from AND :date_to
        AND da.is_active = 1
        AND da.status_id = 3
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR da.branch_id = :branch_id)
    GROUP BY sd.sub_dept_desc, dsg.desig, COALESCE(sp.spell_name, da.spell)
    ORDER BY sd.sub_dept_desc, dsg.desig, spell;
    """
    return text(sql)


def get_shift_wise_query():
    """
    Shift-wise hands: the spell-wise report rolled up to the shift — the first
    letter of the spell name (A1/A2 -> A, B1/B2 -> B, C -> C), the same
    derivation the half-day and hands-complement reports use. Long format —
    the frontend pivots shifts into columns and keeps this row order, so the
    ORDER BY (sub_dept_code, NULL codes last = legacy production order) is
    the display order.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = """
    SELECT
        sd.sub_dept_desc AS department,
        dsg.desig AS designation,
        SUBSTRING(COALESCE(sp.spell_name, da.spell), 1, 1) AS shift,
        ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)) / 8, 2) AS hands
    FROM daily_attendance AS da
    INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = da.worked_department_id
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = da.worked_designation_id
    LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
    WHERE da.attendance_date BETWEEN :date_from AND :date_to
        AND da.is_active = 1
        AND da.status_id = 3
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR da.branch_id = :branch_id)
    GROUP BY sd.sub_dept_code, sd.sub_dept_desc, dsg.desig,
             SUBSTRING(COALESCE(sp.spell_name, da.spell), 1, 1)
    ORDER BY (sd.sub_dept_code IS NULL), sd.sub_dept_code,
             sd.sub_dept_desc, dsg.desig, shift;
    """
    return text(sql)


def get_hands_complement_query():
    """
    Hands Complement (second sheet of the legacy Attendance Check List export,
    model Attendance_checklist_Model::directsummReport): per attendance date,
    master department and designation, hands (worked hours / 8) split two ways —
    by shift (A = spells A1/A2, B = B1/B2, C = C) and by employee category.

    Only the seven worker categories the legacy report counted are included.
    Filtering by id is deliberate: cata_code is NOT unique — 'C' is both PF
    BADLI (5) and CONTRACT (14), 'A' is both PERMANENTS (3) and APPRENTICE (21)
    — so the id filter is what makes the cata_code buckets below unambiguous.

    category_mst is BRANCH-scoped, so each branch has its own id set for the
    same seven codes. Ids listed per branch (code: b4 / b87):
        C 14/84, PER 15/85, BUD 16/86, NB 17/87, RTD 20/68, A 21/90, O 22/91.
    ponytail: explicit id list, one branch at a time — resolve the ids from
    (cata_code, branch_id) instead if a third branch needs this report.

    Note the spell comes from spell_mst: daily_attendance.spell is NULL on the
    migrated tenants, with the real spell carried by spell_id.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = """
    SELECT
        h.attendance_date,
        dm.dept_desc AS department,
        dsg.desig    AS designation,
        ROUND(SUM(CASE WHEN h.spell_name IN ('A1','A2') THEN h.hands ELSE 0 END), 4) AS shift_a,
        ROUND(SUM(CASE WHEN h.spell_name IN ('B1','B2') THEN h.hands ELSE 0 END), 4) AS shift_b,
        ROUND(SUM(CASE WHEN h.spell_name = 'C'          THEN h.hands ELSE 0 END), 4) AS shift_c,
        ROUND(SUM(h.hands), 4) AS shift_total,
        ROUND(SUM(CASE WHEN cm.cata_code = 'PER' THEN h.hands ELSE 0 END), 4) AS permanent,
        ROUND(SUM(CASE WHEN cm.cata_code = 'BUD' THEN h.hands ELSE 0 END), 4) AS budli,
        ROUND(SUM(CASE WHEN cm.cata_code = 'RTD' THEN h.hands ELSE 0 END), 4) AS retired,
        ROUND(SUM(CASE WHEN cm.cata_code = 'NB'  THEN h.hands ELSE 0 END), 4) AS new_budli,
        ROUND(SUM(CASE WHEN cm.cata_code = 'C'   THEN h.hands ELSE 0 END), 4) AS contract,
        ROUND(SUM(CASE WHEN cm.cata_code = 'O'   THEN h.hands ELSE 0 END), 4) AS outsider,
        ROUND(SUM(CASE WHEN cm.cata_code = 'A'   THEN h.hands ELSE 0 END), 4) AS apprentice,
        ROUND(SUM(h.hands), 4) AS total_hands
    FROM (
        SELECT da.attendance_date, da.eb_id,
               da.worked_department_id, da.worked_designation_id,
               COALESCE(sp.spell_name, da.spell) AS spell_name,
               SUM(da.working_hours - COALESCE(da.idle_hours, 0)) / 8 AS hands
        FROM daily_attendance AS da
        INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
        LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
        WHERE da.attendance_date BETWEEN :date_from AND :date_to
            AND da.is_active = 1
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR da.branch_id = :branch_id)
        GROUP BY da.attendance_date, da.eb_id, da.worked_department_id,
                 da.worked_designation_id, COALESCE(sp.spell_name, da.spell)
    ) AS h
    INNER JOIN hrms_ed_official_details AS od ON od.eb_id = h.eb_id AND od.active = 1
    INNER JOIN sub_dept_mst AS sd ON sd.sub_dept_id = h.worked_department_id
    INNER JOIN dept_mst AS dm ON dm.dept_id = sd.dept_id
    INNER JOIN designation_mst AS dsg ON dsg.designation_id = h.worked_designation_id
    INNER JOIN category_mst AS cm ON cm.cata_id = od.catagory_id
    WHERE od.catagory_id IN (14, 15, 16, 17, 20, 21, 22,
                             68, 84, 85, 86, 87, 90, 91)
    GROUP BY h.attendance_date, dm.order_id, dm.dept_desc, dsg.desig
    ORDER BY h.attendance_date, dm.order_id, dm.dept_desc, dsg.desig;
    """
    return text(sql)


def get_bank_statement_query():
    """
    Employee bank statement (legacy report 534): per employee net pay for pay
    periods inside the date range, with bank name / account no / IFSC from
    hrms_ed_bank_details. Net comes straight from pay_employee_payperiod.NET
    (the legacy component-21 filter is unnecessary in the new schema). Only
    approved/processed periods (status 3 or 28) — rejected reruns are skipped.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range containing the pay periods
    """
    sql = """
    SELECT
        od.emp_code,
        CONCAT(IFNULL(pd.first_name, ''), ' ', IFNULL(pd.middle_name, ''), ' ',
               IFNULL(pd.last_name, '')) AS emp_name,
        bd.bank_name,
        bd.bank_acc_no,
        bd.ifsc_code,
        pp.FROM_DATE AS from_date,
        pp.TO_DATE AS to_date,
        pep.NET AS net_pay
    FROM pay_period AS pp
    INNER JOIN pay_employee_payperiod AS pep ON pep.PAY_PERIOD_ID = pp.ID
    INNER JOIN hrms_ed_personal_details AS pd ON pd.eb_id = pep.EMPLOYEEID
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = pd.eb_id AND od.active = 1
    LEFT JOIN hrms_ed_bank_details AS bd ON bd.eb_id = pd.eb_id AND bd.active = 1
    WHERE pp.COMPANY_ID = :co_id
        AND (:branch_id IS NULL OR pp.branch_id = :branch_id)
        AND pp.FROM_DATE >= :date_from
        AND pp.TO_DATE <= :date_to
        AND pp.STATUS IN (3, 28)
    ORDER BY od.emp_code;
    """
    return text(sql)


def get_full_attendance_query():
    """
    Full attendance (legacy report 603): flat daily_attendance detail rows with
    decoded attendance source (Manual/Facial/Logs) and type (Regular/OT/Cash).

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = """
    SELECT
        da.daily_atten_id AS tran_no,
        COALESCE(od.emp_code, da.eb_no, da.eb_code) AS eb_no,
        CONCAT(IFNULL(pd.first_name, ''), ' ', IFNULL(pd.middle_name, ''), ' ',
               IFNULL(pd.last_name, '')) AS emp_name,
        da.attendance_date,
        sd.sub_dept_desc AS department,
        dsg.desig AS designation,
        da.attendance_mark AS mark,
        COALESCE(sp.spell_name, da.spell) AS spell,
        da.idle_hours,
        da.spell_hours,
        da.working_hours,
        CASE WHEN da.attendance_source IN ('A', 'M', 'Manual') THEN 'Manual'
             WHEN da.attendance_source IN ('F', 'Face') THEN 'Facial'
             WHEN da.attendance_source = 'P' THEN 'Logs'
             ELSE COALESCE(da.attendance_source, '') END AS source,
        CASE da.attendance_type WHEN 'R' THEN 'Regular' WHEN 'O' THEN 'OT'
             WHEN 'C' THEN 'Cash' ELSE COALESCE(da.attendance_type, '') END AS att_type,
        sm.status_name,
        da.remarks
    FROM daily_attendance AS da
    INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
    LEFT JOIN hrms_ed_personal_details AS pd ON pd.eb_id = da.eb_id
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = da.eb_id AND od.active = 1
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = da.worked_department_id
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = da.worked_designation_id
    LEFT JOIN status_mst AS sm ON sm.status_id = da.status_id
    LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
    WHERE da.attendance_date BETWEEN :date_from AND :date_to
        AND da.is_active = 1
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR da.branch_id = :branch_id)
    ORDER BY da.attendance_date, eb_no;
    """
    return text(sql)


def get_absenteeism_query():
    """
    Absenteeism (legacy report 673): per employee still on the roll, the last
    date they were seen (attendance or approved leave day) up to :as_on and the
    days absent since. The gap is bounded on BOTH sides:

      * :min_days — the shortest gap worth reporting (>= 1).
      * :max_days — the longest. Past it the employee has left, not "gone
        absent". pd.active = 1 is not maintained as a roll: sls br 29 carries
        ~9,000 active employee rows against ~600 who actually clock in, so the
        unbounded report returned 1,307 rows for a 7-day query — 1,228 of them
        90+ days, including ACCOUNTS/CASHIER staff whose branch stopped feeding
        attendance in 2024 (TKT-000151). NULL = no cap, for the deliberate
        "who left without being marked" query.

    Employees carrying a separation lifecycle status are excluded outright.

    Holidays are NOT counted as a "seen" day (the legacy query counted them):
    hrms_holiday_transactions rows are per-employee eligibility, so an absent
    worker gets one too and it would silently reset their last-seen date.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional (employee's branch)
        :as_on (str) - required as-on date
        :min_days (int) - required minimum days absent (>= 1)
        :max_days (int or NULL) - optional maximum days absent
    """
    sql = f"""
    SELECT
        od.emp_code,
        CONCAT(IFNULL(pd.first_name, ''), ' ', IFNULL(pd.middle_name, ''), ' ',
               IFNULL(pd.last_name, '')) AS emp_name,
        dm.dept_desc AS department,
        sd.sub_dept_desc AS sub_department,
        cm.cata_desc AS category,
        seen.last_seen,
        DATEDIFF(:as_on, seen.last_seen) AS absent_for
    FROM hrms_ed_personal_details AS pd
    INNER JOIN branch_mst AS bm ON bm.branch_id = pd.branch_id
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = pd.eb_id AND od.active = 1
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = od.sub_dept_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = sd.dept_id
    LEFT JOIN category_mst AS cm ON cm.cata_id = od.catagory_id
    INNER JOIN (
        SELECT eb_id, MAX(seen_date) AS last_seen
        FROM (
            SELECT eb_id, attendance_date AS seen_date
            FROM daily_attendance
            WHERE is_active = 1 AND attendance_date <= :as_on
            UNION ALL
            SELECT lt.eb_id, ltd.leave_date
            FROM leave_tran_details AS ltd
            INNER JOIN leave_transactions AS lt
                ON lt.leave_transaction_id = ltd.ltran_id
            WHERE ltd.is_active = 1 AND lt.status = 3 AND ltd.leave_date <= :as_on
        ) AS g
        GROUP BY eb_id
    ) AS seen ON seen.eb_id = pd.eb_id
    WHERE pd.active = 1
        AND pd.status_id NOT IN ({_SEPARATED_STATUS_SQL})
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR pd.branch_id = :branch_id)
        AND DATEDIFF(:as_on, seen.last_seen) >= :min_days
        AND (:max_days IS NULL OR DATEDIFF(:as_on, seen.last_seen) <= :max_days)
    ORDER BY dm.dept_desc, od.emp_code;
    """
    return text(sql)


def get_half_day_query():
    """
    Half-day absenteeism (legacy report 687): days where an employee worked only
    the first spell of a shift (A1 without A2, B1 without B2) and the day's total
    working hours are not a full 8 — the legacy half-day detection rule, minus
    the external EMPMILL12.half_day_data feed the tenant does not have. Window
    counts give per-month and range totals per employee.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = """
    SELECT
        od.emp_code,
        CONCAT(IFNULL(pd.first_name, ''), ' ', IFNULL(pd.middle_name, ''), ' ',
               IFNULL(pd.last_name, '')) AS emp_name,
        dm.dept_desc AS department,
        hd.attendance_date,
        hd.shift,
        hd.day_hours,
        COUNT(*) OVER (PARTITION BY hd.eb_id, YEAR(hd.attendance_date),
                       MONTH(hd.attendance_date)) AS halfdays_month,
        COUNT(*) OVER (PARTITION BY hd.eb_id) AS halfdays_total
    FROM (
        SELECT spl.eb_id, spl.attendance_date, spl.shift, tot.day_hours
        FROM (
            SELECT da.eb_id, da.attendance_date,
                   SUBSTRING(da.spell, 1, 1) AS shift,
                   SUM(CASE WHEN da.spell IN ('A1', 'B1')
                            THEN da.working_hours - COALESCE(da.idle_hours, 0)
                            ELSE 0 END) AS spell1_hours,
                   SUM(CASE WHEN da.spell IN ('A2', 'B2')
                            THEN da.working_hours - COALESCE(da.idle_hours, 0)
                            ELSE 0 END) AS spell2_hours
            FROM daily_attendance AS da
            INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
            WHERE da.attendance_type = 'R' AND da.is_active = 1
                AND da.spell IN ('A1', 'A2', 'B1', 'B2')
                AND da.attendance_date BETWEEN :date_from AND :date_to
                AND bm.co_id = :co_id
                AND (:branch_id IS NULL OR da.branch_id = :branch_id)
            GROUP BY da.eb_id, da.attendance_date, shift
        ) AS spl
        INNER JOIN (
            SELECT da.eb_id, da.attendance_date, SUM(da.working_hours) AS day_hours
            FROM daily_attendance AS da
            WHERE da.attendance_type = 'R' AND da.is_active = 1
                AND da.attendance_date BETWEEN :date_from AND :date_to
            GROUP BY da.eb_id, da.attendance_date
        ) AS tot ON tot.eb_id = spl.eb_id AND tot.attendance_date = spl.attendance_date
        WHERE spl.spell1_hours > 0 AND spl.spell2_hours = 0 AND tot.day_hours <> 8
    ) AS hd
    LEFT JOIN hrms_ed_personal_details AS pd ON pd.eb_id = hd.eb_id
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = hd.eb_id AND od.active = 1
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = od.sub_dept_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = sd.dept_id
    ORDER BY od.emp_code, hd.attendance_date;
    """
    return text(sql)


def get_overstay_query():
    """
    Overstay after leave (legacy report 686): approved leaves ending in the date
    range where the employee did not resume the next day — overstay days =
    days between leave end and first attendance after it (or up to today when
    the employee has not rejoined at all). Only overstays of 1+ days.

    ponytail: rejoin lookup is a grouped scan of daily_attendance (no
    (eb_id, attendance_date) index on the big tenants) — ~10s on sls's 1.1M-row
    quarter; add that index if this becomes a hot report.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range (leave end dates)
    """
    sql = """
    SELECT * FROM (
        SELECT
            od.emp_code,
            CONCAT(IFNULL(pd.first_name, ''), ' ', IFNULL(pd.middle_name, ''), ' ',
                   IFNULL(pd.last_name, '')) AS emp_name,
            dm.dept_desc AS department,
            cm.cata_desc AS category,
            ltm.leave_type_description AS leave_type,
            l.leave_from_date,
            l.leave_to_date,
            rj.rejoin_date,
            CASE WHEN rj.rejoin_date IS NULL
                 THEN DATEDIFF(CURDATE(), l.leave_to_date) - 1
                 ELSE DATEDIFF(rj.rejoin_date, l.leave_to_date) - 1 END AS overstay_days
        FROM (
            SELECT lt.leave_transaction_id, lt.eb_id, lt.leave_type_id,
                   lt.leave_from_date, lt.leave_to_date
            FROM leave_transactions AS lt
            INNER JOIN branch_mst AS bm ON bm.branch_id = lt.branch_id
            WHERE lt.status = 3
                AND lt.leave_to_date BETWEEN :date_from AND :date_to
                AND bm.co_id = :co_id
                AND (:branch_id IS NULL OR lt.branch_id = :branch_id)
        ) AS l
        LEFT JOIN (
            SELECT l2.leave_transaction_id, MIN(da.attendance_date) AS rejoin_date
            FROM (
                SELECT lt.leave_transaction_id, lt.eb_id, lt.leave_to_date
                FROM leave_transactions AS lt
                INNER JOIN branch_mst AS bm ON bm.branch_id = lt.branch_id
                WHERE lt.status = 3
                    AND lt.leave_to_date BETWEEN :date_from AND :date_to
                    AND bm.co_id = :co_id
                    AND (:branch_id IS NULL OR lt.branch_id = :branch_id)
            ) AS l2
            INNER JOIN daily_attendance AS da
                ON da.eb_id = l2.eb_id
                AND da.attendance_date > l2.leave_to_date
                AND da.is_active = 1
            GROUP BY l2.leave_transaction_id
        ) AS rj ON rj.leave_transaction_id = l.leave_transaction_id
        INNER JOIN hrms_ed_personal_details AS pd ON pd.eb_id = l.eb_id AND pd.active = 1
        LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = l.eb_id AND od.active = 1
        LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = od.sub_dept_id
        LEFT JOIN dept_mst AS dm ON dm.dept_id = sd.dept_id
        LEFT JOIN category_mst AS cm ON cm.cata_id = od.catagory_id
        LEFT JOIN hrms_leave_types_mst AS ltm ON ltm.leave_type_id = l.leave_type_id
    ) AS r
    WHERE r.overstay_days >= 1
    ORDER BY r.leave_to_date, r.emp_code;
    """
    return text(sql)


def get_occupation_deviation_query():
    """
    Occupation deviation (legacy report 601): approved attendance rows where the
    worked department/designation differs from the employee's advised (official)
    department/designation. Rows with no official record are excluded, matching
    the legacy NULL-comparison behaviour.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = """
    SELECT
        da.daily_atten_id AS tran_no,
        COALESCE(od.emp_code, da.eb_no, da.eb_code) AS eb_no,
        CONCAT(IFNULL(pd.first_name, ''), ' ', IFNULL(pd.middle_name, ''), ' ',
               IFNULL(pd.last_name, '')) AS emp_name,
        da.attendance_date,
        sd_w.sub_dept_desc AS actual_dept,
        dsg_w.desig AS actual_desig,
        sd.sub_dept_desc AS advised_dept,
        dsg_a.desig AS advised_desig,
        COALESCE(sp.spell_name, da.spell) AS spell,
        ROUND(da.working_hours - COALESCE(da.idle_hours, 0), 2) AS work_hours
    FROM daily_attendance AS da
    INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
    LEFT JOIN hrms_ed_personal_details AS pd ON pd.eb_id = da.eb_id
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = da.eb_id AND od.active = 1
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = od.sub_dept_id
    LEFT JOIN designation_mst AS dsg_a ON dsg_a.designation_id = od.designation_id
    LEFT JOIN sub_dept_mst AS sd_w ON sd_w.sub_dept_id = da.worked_department_id
    LEFT JOIN designation_mst AS dsg_w ON dsg_w.designation_id = da.worked_designation_id
    LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
    WHERE da.attendance_date BETWEEN :date_from AND :date_to
        AND da.is_active = 1
        AND da.status_id = 3
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR da.branch_id = :branch_id)
        -- Both sides are sub-department ids: worked_department_id holds a
        -- sub_dept_id, so comparing it to the master dept_id flagged every row.
        AND (od.sub_dept_id <> da.worked_department_id
             OR od.designation_id <> da.worked_designation_id)
    ORDER BY da.attendance_date, eb_no;
    """
    return text(sql)


def get_cash_attendance_query():
    """
    Cash attendance (legacy report 610 "Cash in Hands"): cash-type attendance
    (attendance_type = 'C') hours per employee, date and spell. The legacy
    report priced hours at worker_master.cash_rate — the tenant schema has no
    cash rate column, so this lists hours only.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = """
    SELECT
        COALESCE(od.emp_code, da.eb_no, da.eb_code) AS eb_no,
        CONCAT(IFNULL(pd.first_name, ''), ' ', IFNULL(pd.middle_name, ''), ' ',
               IFNULL(pd.last_name, '')) AS emp_name,
        sd.sub_dept_desc AS department,
        dsg.desig AS designation,
        da.attendance_date,
        COALESCE(sp.spell_name, da.spell) AS spell,
        ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)), 2) AS hours
    FROM daily_attendance AS da
    INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
    LEFT JOIN hrms_ed_personal_details AS pd ON pd.eb_id = da.eb_id
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = da.eb_id AND od.active = 1
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = da.worked_department_id
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = da.worked_designation_id
    LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
    WHERE da.attendance_type = 'C'
        AND da.is_active = 1
        AND da.attendance_date BETWEEN :date_from AND :date_to
        AND bm.co_id = :co_id
        AND (:branch_id IS NULL OR da.branch_id = :branch_id)
    GROUP BY da.eb_id, COALESCE(od.emp_code, da.eb_no, da.eb_code),
             CONCAT(IFNULL(pd.first_name, ''), ' ', IFNULL(pd.middle_name, ''), ' ',
                    IFNULL(pd.last_name, '')),
             sd.sub_dept_desc, dsg.desig, da.attendance_date,
             COALESCE(sp.spell_name, da.spell)
    ORDER BY da.attendance_date, sd.sub_dept_desc, eb_no;
    """
    return text(sql)


def get_pay_periods_query():
    """
    Pay periods for one payscheme (non-cancelled), newest first — feeds the
    period picker on the Pay Register / Payslip print pages.

    Parameters:
        :payscheme_id (int) - required
        :branch_id (int or NULL) - optional; periods with NULL branch always
            pass (legacy-migrated periods may be branchless on purpose)
    """
    sql = """
    SELECT pp.ID AS payperiod_id, pp.FROM_DATE AS from_date,
           pp.TO_DATE AS to_date, pp.STATUS AS status
    FROM pay_period AS pp
    WHERE pp.PAYSCHEME_ID = :payscheme_id
        AND pp.STATUS <> 4
        AND (:branch_id IS NULL OR pp.branch_id IS NULL OR pp.branch_id = :branch_id)
    ORDER BY pp.TO_DATE DESC, pp.ID DESC;
    """
    return text(sql)


def get_pay_register_print_query():
    """
    Pay Register / Payslip print rows (legacy Data_entry payregisterprint /
    payslipprint, holget=1 "Main Pay Roll"): one row per employee for one pay
    period, pivoting pay_employee_payroll component amounts into the legacy
    register columns. Component ids match vowsls.tbl_pay_components (verified
    identical to this tenant's pay_components).

    Parameters:
        :payperiod_id (int) - required (period picker already excludes
            cancelled periods; a period belongs to exactly one payscheme)
    """
    sql = """
    SELECT
        od.emp_code AS eb_no,
        CONCAT(TRIM(IFNULL(pd.first_name, '')), ' ', TRIM(IFNULL(pd.middle_name, '')),
               ' ', TRIM(IFNULL(pd.last_name, ''))) AS wname,
        IFNULL(dm.dept_code, '') AS dept_code,
        IFNULL(dm.dept_desc, '') AS dept_desc,
        IFNULL(dsg.desig, '') AS desig,
        IFNULL(dsg.time_piece, '') AS time_piece,
        IFNULL(esi.esi_no, '') AS esi_no,
        IFNULL(pf.pf_no, '') AS pf_no,
        IFNULL(pf.pf_uan_no, '') AS pf_uan_no,
        CASE MAX(CASE WHEN k.COMPONENT_ID = 175 THEN k.AMOUNT ELSE 0 END)
            WHEN 1 THEN 'A' WHEN 2 THEN 'B' WHEN 3 THEN 'C' ELSE 'G' END AS shift,
        MAX(CASE WHEN k.COMPONENT_ID = 238 THEN ROUND(k.AMOUNT, 2) ELSE 0 END) AS basic_rate,
        MAX(CASE WHEN k.COMPONENT_ID = 178 THEN k.AMOUNT ELSE 0 END) AS working_hours,
        MAX(CASE WHEN k.COMPONENT_ID = 179 THEN k.AMOUNT ELSE 0 END) AS ns_hrs,
        MAX(CASE WHEN k.COMPONENT_ID = 180 THEN k.AMOUNT ELSE 0 END) AS hl_hrs,
        MAX(CASE WHEN k.COMPONENT_ID = 182 THEN k.AMOUNT ELSE 0 END) AS ls_hrs,
        MAX(CASE WHEN k.COMPONENT_ID = 183 THEN k.AMOUNT ELSE 0 END) AS stl_d,
        MAX(CASE WHEN k.COMPONENT_ID = 198 THEN k.AMOUNT ELSE 0 END) AS wrk_days,
        MAX(CASE WHEN k.COMPONENT_ID = 206 THEN k.AMOUNT ELSE 0 END) AS c_work_day,
        MAX(CASE WHEN k.COMPONENT_ID = 251 THEN ROUND(k.AMOUNT, 2) ELSE 0 END) AS prod_basic,
        MAX(CASE WHEN k.COMPONENT_ID = 189 THEN ROUND(k.AMOUNT, 2) ELSE 0 END) AS time_rated_basic,
        MAX(CASE WHEN k.COMPONENT_ID = 216 THEN k.AMOUNT ELSE 0 END) AS fix_basic,
        MAX(CASE WHEN k.COMPONENT_ID = 207 THEN k.AMOUNT ELSE 0 END) AS c_bon_ern,
        MAX(CASE WHEN k.COMPONENT_ID = 134 THEN k.AMOUNT ELSE 0 END) AS pf_gross,
        MAX(CASE WHEN k.COMPONENT_ID = 212 THEN k.AMOUNT ELSE 0 END) AS da,
        MAX(CASE WHEN k.COMPONENT_ID = 171 THEN k.AMOUNT ELSE 0 END) AS ns_amount,
        MAX(CASE WHEN k.COMPONENT_ID = 109 THEN k.AMOUNT ELSE 0 END) AS hol_amt,
        MAX(CASE WHEN k.COMPONENT_ID = 217 THEN k.AMOUNT ELSE 0 END) AS increment_amt,
        MAX(CASE WHEN k.COMPONENT_ID = 221 THEN k.AMOUNT ELSE 0 END) AS layoff_wgs,
        MAX(CASE WHEN k.COMPONENT_ID = 219 THEN k.AMOUNT ELSE 0 END) AS incentive_amount,
        MAX(CASE WHEN k.COMPONENT_ID = 184 THEN k.AMOUNT ELSE 0 END) AS misc_earn,
        MAX(CASE WHEN k.COMPONENT_ID = 112 THEN k.AMOUNT ELSE 0 END) AS stl_wgs,
        MAX(CASE WHEN k.COMPONENT_ID = 20 THEN k.AMOUNT ELSE 0 END) AS gross_pay,
        MAX(CASE WHEN k.COMPONENT_ID = 8 THEN k.AMOUNT ELSE 0 END) AS hra,
        MAX(CASE WHEN k.COMPONENT_ID = 223 THEN k.AMOUNT ELSE 0 END) AS b_f,
        MAX(CASE WHEN k.COMPONENT_ID = 224 THEN k.AMOUNT ELSE 0 END) AS total_earn,
        MAX(CASE WHEN k.COMPONENT_ID = 18 THEN k.AMOUNT ELSE 0 END) AS epf,
        MAX(CASE WHEN k.COMPONENT_ID = 231 THEN k.AMOUNT ELSE 0 END) AS c_pf_cont,
        MAX(CASE WHEN k.COMPONENT_ID = 232 THEN k.AMOUNT ELSE 0 END) AS c_fpf_cont,
        MAX(CASE WHEN k.COMPONENT_ID = 134 THEN ROUND(k.AMOUNT * 8.33 / 100, 0) ELSE 0 END) AS epf_833,
        MAX(CASE WHEN k.COMPONENT_ID = 134 THEN ROUND(k.AMOUNT * 1.67 / 100, 0) ELSE 0 END) AS epf_167,
        MAX(CASE WHEN k.COMPONENT_ID = 233 THEN k.AMOUNT ELSE 0 END) AS co_cont,
        MAX(CASE WHEN k.COMPONENT_ID = 149 THEN k.AMOUNT ELSE 0 END) AS esi_gross,
        MAX(CASE WHEN k.COMPONENT_ID = 19 THEN k.AMOUNT ELSE 0 END) AS esic,
        MAX(CASE WHEN k.COMPONENT_ID = 16 THEN k.AMOUNT ELSE 0 END) AS p_tax,
        MAX(CASE WHEN k.COMPONENT_ID = 235 THEN k.AMOUNT ELSE 0 END) AS lwf,
        MAX(CASE WHEN k.COMPONENT_ID = 185 THEN k.AMOUNT ELSE 0 END) AS co_loan,
        MAX(CASE WHEN k.COMPONENT_ID = 222 THEN k.AMOUNT ELSE 0 END) AS co_loan_bal,
        MAX(CASE WHEN k.COMPONENT_ID = 186 THEN k.AMOUNT ELSE 0 END) AS puja_advance,
        MAX(CASE WHEN k.COMPONENT_ID = 187 THEN k.AMOUNT ELSE 0 END) AS stl_advance,
        MAX(CASE WHEN k.COMPONENT_ID = 166 THEN k.AMOUNT ELSE 0 END) AS exadvance,
        MAX(CASE WHEN k.COMPONENT_ID = 268 THEN k.AMOUNT ELSE 0 END) AS arrear_plus,
        MAX(CASE WHEN k.COMPONENT_ID = 269 THEN k.AMOUNT ELSE 0 END) AS arrear_minus,
        MAX(CASE WHEN k.COMPONENT_ID = 25 THEN k.AMOUNT ELSE 0 END) AS total_deduction,
        MAX(CASE WHEN k.COMPONENT_ID = 236 THEN k.AMOUNT ELSE 0 END) AS c_f,
        MAX(CASE WHEN k.COMPONENT_ID = 21 THEN ROUND(k.AMOUNT, 2) ELSE 0 END) AS net_pay
    FROM pay_employee_payroll AS k
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = k.EMPLOYEEID AND od.active = 1
    LEFT JOIN hrms_ed_personal_details AS pd ON pd.eb_id = k.EMPLOYEEID
    LEFT JOIN sub_dept_mst AS sdm ON sdm.sub_dept_id = od.sub_dept_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = sdm.dept_id
    LEFT JOIN designation_mst AS dsg ON dsg.designation_id = od.designation_id
    LEFT JOIN hrms_ed_esi AS esi ON esi.eb_id = k.EMPLOYEEID AND esi.active = 1
    LEFT JOIN hrms_ed_pf AS pf ON pf.eb_id = k.EMPLOYEEID AND pf.active = 1
    WHERE k.PAYPERIOD_ID = :payperiod_id
        AND k.STATUS = 1
    GROUP BY k.EMPLOYEEID, od.emp_code, wname, dm.dept_code, dm.dept_desc,
             dsg.desig, dsg.time_piece, esi.esi_no, pf.pf_no, pf.pf_uan_no
    HAVING total_earn > 0
    ORDER BY dept_code, eb_no;
    """
    return text(sql)


def get_attendance_payscheme_query():
    """
    "Attendance With PaySchemes" checklist (NJM Wages Data Preparation).

    Ports Njmallwagesprocess::njmattwithpayschmexceldata — one row per employee
    with worked hours (net of idle) over the wage period, plus the identity
    columns the operator checks before the wages run: department, category,
    status, pay scheme, DOJ, PF UAN and ESI no.

    The attendance filter deliberately matches what the wages build actually
    consumes (src/hrms/ejmWagesData._build_from_attendance_query): active rows,
    types R and O, one branch. Rejected attendance (status_id 4) is dropped;
    NULL status (legacy / bio-imported rows) is kept -- those are still real.

    Scheme names come from pay_scheme_master, not pay_scheme: the FE dropdown
    and pay_employee_payscheme.PAY_SCHEME_ID both key on payscheme_id, and
    pay_scheme is empty in migrated tenants.

    An employee on two active schemes yields one row per scheme, each carrying
    the full period hours -- same as legacy. That is a data fault worth seeing,
    not a total to sum blindly.

    Department is dept_mst.dept_desc, reached through the employee's sub-dept:
    that is legacy's single-level department_master column, and it is the only
    one that is unambiguous -- sub_dept_desc repeats across mills ('SPG MAIN A'
    exists under both SPINNING(LC) and SPINNING(NJM)). sub_department rides
    along because the screen's Department filter picks a sub-dept.

    Parameters:
        :branch_id (int) - required
        :date_from, :date_to (str) - required range
        :pay_scheme_id (int) - required
        :sub_dept_id (int or NULL) - optional department filter
    """
    sql = """
    SELECT
        od.emp_code,
        CONCAT(TRIM(IFNULL(pd.first_name, '')), ' ', TRIM(IFNULL(pd.middle_name, '')),
               ' ', TRIM(IFNULL(pd.last_name, ''))) AS emp_name,
        dm.dept_desc AS department,
        sd.sub_dept_desc AS sub_department,
        cm.cata_desc AS category,
        sm.status_name,
        psm.payscheme_name AS pay_scheme,
        ROUND(SUM(da.working_hours - COALESCE(da.idle_hours, 0)), 2) AS work_hours,
        od.date_of_join,
        pf.pf_uan_no,
        esi.esi_no
    FROM daily_attendance AS da
    LEFT JOIN pay_employee_payscheme AS pep
        ON pep.EMPLOYEEID = da.eb_id AND pep.STATUS = 1
    LEFT JOIN pay_scheme_master AS psm ON psm.payscheme_id = pep.PAY_SCHEME_ID
    LEFT JOIN hrms_ed_personal_details AS pd ON pd.eb_id = da.eb_id
    LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = da.eb_id AND od.active = 1
    LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = od.sub_dept_id
    LEFT JOIN dept_mst AS dm ON dm.dept_id = sd.dept_id
    LEFT JOIN category_mst AS cm ON cm.cata_id = od.catagory_id
    LEFT JOIN status_mst AS sm ON sm.status_id = pd.status_id
    LEFT JOIN hrms_ed_pf AS pf ON pf.eb_id = da.eb_id AND pf.active = 1
    LEFT JOIN hrms_ed_esi AS esi ON esi.eb_id = da.eb_id AND esi.active = 1
    WHERE da.attendance_date BETWEEN :date_from AND :date_to
        AND da.is_active = 1
        AND da.branch_id = :branch_id
        AND da.attendance_type IN ('R', 'O')
        AND COALESCE(da.status_id, 0) <> 4
        AND (:sub_dept_id IS NULL OR od.sub_dept_id = :sub_dept_id)
        -- The selected scheme, PLUS anyone with no active scheme at all. Legacy
        -- LEFT JOINed and never applied att_payschm, and those unmapped rows
        -- are the whole point of the checklist: attendance the wages run will
        -- not pay. Employees on some OTHER scheme are still filtered out.
        AND (pep.PAY_SCHEME_ID = :pay_scheme_id OR pep.ID IS NULL)
    GROUP BY da.eb_id, od.emp_code, emp_name, dm.dept_desc, sd.sub_dept_desc,
             cm.cata_desc, sm.status_name, psm.payscheme_name, od.date_of_join,
             pf.pf_uan_no, esi.esi_no
    ORDER BY cm.cata_desc, od.emp_code;
    """
    return text(sql)


def get_deactivate_rejected_attendance_query():
    """
    Run by the "Attendance With PaySchemes" checklist before it selects:
    rejected attendance (status_id 4) in the branch and period is switched to
    is_active = 0 so neither the checklist nor the wages build ever sees it.

    daily_attendance_unique includes is_active, so a rejected row whose
    inactive twin already exists cannot be flipped; IGNORE skips those rows
    instead of failing the whole checklist.

    Parameters:
        :branch_id (int) - required
        :date_from, :date_to (str) - required range
    """
    return text("""
    UPDATE IGNORE daily_attendance
    SET is_active = 0
    WHERE branch_id = :branch_id
        AND attendance_date BETWEEN :date_from AND :date_to
        AND status_id = 4
        AND is_active = 1;
    """)


def get_daily_hands_complement_query():
    """
    Daily Hands Complement (legacy Data-Portal "Other Entry/Reports" option 2,
    EMPMILL12.dailyhandcomp): per attendance date, sub-department and worked
    occupation, hands (worked hours / 8) split by shift for regular attendance
    and, alongside it, for overtime.

    The legacy source was a view over the old attendance table, not stored
    data, so the report reads daily_attendance directly. "OT" keeps the legacy
    grouping: attendance_type 'O' *and* 'C' (the legacy OAHND + CAHND columns).

    Two legacy quirks are deliberately not carried over, matching the other
    reports in this module: worked hours are net of idle hours, and the C-shift
    7.5 -> 8 night bump is not applied.

    The legacy Occu Code column is dropped -- it came from the external
    OCCUPATION_MASTER.HOCCU_CODE, which has no counterpart in the tenant schema
    (designation_mst.cost_code is empty on half the rows).

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
    """
    sql = """
    SELECT
        h.attendance_date,
        h.dept_code,
        h.department,
        h.occupation,
        ROUND(SUM(CASE WHEN h.atype = 'R' AND h.shift = 'A' THEN h.hrs ELSE 0 END) / 8, 2) AS shift_a,
        ROUND(SUM(CASE WHEN h.atype = 'R' AND h.shift = 'B' THEN h.hrs ELSE 0 END) / 8, 2) AS shift_b,
        ROUND(SUM(CASE WHEN h.atype = 'R' AND h.shift = 'C' THEN h.hrs ELSE 0 END) / 8, 2) AS shift_c,
        ROUND(SUM(CASE WHEN h.atype = 'R' THEN h.hrs ELSE 0 END) / 8, 2) AS shift_total,
        ROUND(SUM(CASE WHEN h.atype <> 'R' AND h.shift = 'A' THEN h.hrs ELSE 0 END) / 8, 2) AS ot_a,
        ROUND(SUM(CASE WHEN h.atype <> 'R' AND h.shift = 'B' THEN h.hrs ELSE 0 END) / 8, 2) AS ot_b,
        ROUND(SUM(CASE WHEN h.atype <> 'R' AND h.shift = 'C' THEN h.hrs ELSE 0 END) / 8, 2) AS ot_c,
        ROUND(SUM(CASE WHEN h.atype <> 'R' THEN h.hrs ELSE 0 END) / 8, 2) AS ot_total
    FROM (
        SELECT
            da.attendance_date,
            sd.sub_dept_code AS dept_code,
            sd.sub_dept_desc AS department,
            dsg.desig        AS occupation,
            COALESCE(da.attendance_type, 'R') AS atype,
            SUBSTRING(COALESCE(sp.spell_name, da.spell), 1, 1) AS shift,
            da.working_hours - COALESCE(da.idle_hours, 0) AS hrs
        FROM daily_attendance AS da
        INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
        LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
        LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = da.worked_department_id
        LEFT JOIN designation_mst AS dsg ON dsg.designation_id = da.worked_designation_id
        WHERE da.attendance_date BETWEEN :date_from AND :date_to
            AND da.is_active = 1
            AND da.status_id = 3
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR da.branch_id = :branch_id)
    ) AS h
    GROUP BY h.attendance_date, h.dept_code, h.department, h.occupation
    ORDER BY h.attendance_date, (h.dept_code IS NULL), h.dept_code,
             h.department, h.occupation;
    """
    return text(sql)


def get_extra_advance_payment_query():
    """
    Daily Extra Advance Payment (legacy Data-Portal "Other Entry/Reports"
    option 8): overtime attendance for the period, priced per employee.

    The legacy report hardcoded the payout slabs and the two S4 weaving
    designations; here they come from extra_advance_rate, so a branch with no
    rate rows returns nothing rather than silently paying zero. Most specific
    rate wins: designation-specific over the catch-all, Sunday/weekday-specific
    over any-day, then the newest eff_date. The rate is a full-shift (8 hour)
    figure when pro_rata = 1, a flat amount otherwise.

    The slab floor replaces the legacy `HAVING work_hours >= 3`: hours below
    the lowest slab match no rate and drop out.

    Legacy parity: the employee-code prefix exclusion (C / T / L = contract,
    temporary and leave-reserve numbering) is kept. The code itself comes from
    hrms_ed_official_details.emp_code -- daily_attendance.eb_no / eb_code are
    NULL on the migrated tenants.

    Parameters:
        :co_id (int) - required
        :branch_id (int or NULL) - optional
        :date_from, :date_to (str) - required range
        :shift (str or NULL) - optional single shift letter (A / B / C)
    """
    sql = """
    SELECT
        g.attendance_date,
        g.eb_no,
        g.worker_name,
        g.shift,
        g.department,
        g.occupation,
        ROUND(g.work_hours, 2) AS work_hours,
        (
            SELECT ROUND(CASE WHEN ra.pro_rata = 1
                              THEN ra.rate / 8 * g.work_hours
                              ELSE ra.rate END, 0)
            FROM extra_advance_rate AS ra
            WHERE ra.is_active = 1
                AND ra.branch_id = g.branch_id
                AND ra.eff_date <= g.attendance_date
                AND (ra.desig_id IS NULL OR ra.desig_id = g.worked_designation_id)
                AND (ra.is_sunday IS NULL
                     OR ra.is_sunday = (DAYOFWEEK(g.attendance_date) = 1))
                AND g.work_hours BETWEEN ra.min_hours AND ra.max_hours
            ORDER BY (ra.desig_id IS NOT NULL) DESC,
                     (ra.is_sunday IS NOT NULL) DESC,
                     ra.eff_date DESC,
                     ra.extra_adv_rate_id DESC
            LIMIT 1
        ) AS advance_amount
    FROM (
        SELECT
            da.attendance_date,
            da.branch_id,
            da.eb_id,
            da.worked_designation_id,
            od.emp_code AS eb_no,
            CONCAT_WS(' ', pd.first_name, pd.middle_name, pd.last_name) AS worker_name,
            SUBSTRING(COALESCE(sp.spell_name, da.spell), 1, 1) AS shift,
            sd.sub_dept_code AS dept_code,
            sd.sub_dept_desc AS department,
            dsg.desig        AS occupation,
            SUM(da.working_hours - COALESCE(da.idle_hours, 0)) AS work_hours
        FROM daily_attendance AS da
        INNER JOIN branch_mst AS bm ON bm.branch_id = da.branch_id
        LEFT JOIN hrms_ed_personal_details AS pd ON pd.eb_id = da.eb_id
        LEFT JOIN hrms_ed_official_details AS od ON od.eb_id = da.eb_id AND od.active = 1
        LEFT JOIN spell_mst AS sp ON sp.spell_id = da.spell_id
        LEFT JOIN sub_dept_mst AS sd ON sd.sub_dept_id = da.worked_department_id
        LEFT JOIN designation_mst AS dsg ON dsg.designation_id = da.worked_designation_id
        WHERE da.attendance_date BETWEEN :date_from AND :date_to
            AND da.is_active = 1
            AND da.status_id = 3
            AND da.attendance_type = 'O'
            AND bm.co_id = :co_id
            AND (:branch_id IS NULL OR da.branch_id = :branch_id)
            AND (:shift IS NULL
                 OR SUBSTRING(COALESCE(sp.spell_name, da.spell), 1, 1) = :shift)
            AND COALESCE(SUBSTRING(od.emp_code, 1, 1), '') NOT IN ('C', 'T', 'L')
        -- Grouped on the expressions, not their aliases: ONLY_FULL_GROUP_BY
        -- does not treat an aliased COALESCE as functionally dependent.
        GROUP BY da.attendance_date, da.branch_id, da.eb_id,
                 da.worked_designation_id,
                 od.emp_code,
                 pd.first_name, pd.middle_name, pd.last_name,
                 SUBSTRING(COALESCE(sp.spell_name, da.spell), 1, 1),
                 sd.sub_dept_code, sd.sub_dept_desc, dsg.desig
    ) AS g
    HAVING advance_amount IS NOT NULL
    ORDER BY g.attendance_date, (g.dept_code IS NULL), g.dept_code,
             g.department, g.shift, g.eb_no;
    """
    return text(sql)

"""EJM wages data collection — the "Wages Data Process" report on the
EJM Wages Process screen.

Everything the EJM wage-preparation actions write lands in
tbl_ejm_wages_data_collection, keyed by (date_from, date_to, eb_id,
pay_scheme_id). This router lets the operator see that collected data for a
wage period and correct individual rows before the payroll run consumes them
(the legacy "Attendance Preparation & Updation" / "Advance & Other Entries"
modals in Data-Portal-Php did the same against EMPMILL12).
"""
import re
from datetime import date, timedelta
from io import BytesIO

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook, load_workbook
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db
from src.hrms.cashHands import EJM_CASH_BRANCH_ID
from src.hrms.constants import (
    ATT_LIVE_SQL, ATTENDANCE_APPROVED, EMPLOYEE_LIFECYCLE_STATUS, SHIFT_SQL, SPELL_JOIN,
)
from src.hrms.ejmPayRegister import _resolve_period
from src.hrms.payRoll import XLSX_MEDIA_TYPE
from src.hrms.query import get_pay_roll_custom_components, get_payscheme_mapped_employees
from src.juteProduction.weaving_lock import fetch_weaving_day_rows

router = APIRouter()

# The fields an operator may correct from the grid — the hour/advance columns
# the legacy modals exposed. Identity/key columns (dates, eb_id, pay scheme)
# are set by the processes and stay read-only.
EDITABLE_FIELDS = {
    "dept_code", "occu_code", "shift", "t_p", "mc_nos",
    "working_hours", "ot_hours", "ns_hours", "ot_ns_hours", "fest_hours",
    "stl_days", "production", "puja_advance", "ot_advance", "stl_advance",
    "installment_advance", "co_loan", "atten_incentive", "misc_earn", "misc_ded",
    "misc_ot_earn", "misc_ot_ded",
    # Hand-set on the Wages Manual Entries page; "Apply Efficiency" overwrites them.
    "working_hours_eff", "ot_hours_eff", "ns_hrs_eff",
}


# dept_mst ids whose attendance is collected machine-wise (20 BEAMING, 22 FINISHING).
MC_WISE_ATTENDANCE_DEPT_IDS = (20, 22)


def _build_from_attendance_query():
    """Base wages rows for a period, aggregated from daily_attendance.

    Ports Ejmallprocessdata::processAttPrep. The legacy version staged into
    tbl_ejm_wages_att_summary and let the per-department MainWagesProcess* arms
    copy it forward; those arms need EMPMILL12 production/efficiency masters
    (tbl_ejm_wages_occu_mast, tbl_all_trn_eff, daily_drawing_transaction) that
    tenant DBs do not have, so this writes the attendance base straight into
    tbl_ejm_wages_data_collection and leaves the efficiency columns at 0.

    Night hours are deliberately NOT posted here — legacy's copy-forward arms
    drop att_summary.ns_hours, and data_collection gets it from the NSH arm
    below instead. Posting it in both places would double-count.

    Every arm stamps the scheme into both pay_scheme_id (legacy's key) and
    payschme_id (the sls-era column nothing else fills), from the one bind.

    BEAMING (dept 20) and FINISHING (dept 22) attendance is collected machine-wise
    (user, 2026-10-03): one row per machine worked (daily_ebmc_attendance) with
    mc_id and the machine's code in mc_nos; an attendance on n machines puts
    hours / n on each, so the employee's totals are unchanged. An attendance with
    no machine keeps a single row (mc_id NULL), as every other department does.
    """
    mc_depts = ", ".join(str(d) for d in MC_WISE_ATTENDANCE_DEPT_IDS)
    return text(f"""
        INSERT INTO tbl_ejm_wages_data_collection
            (date_from, date_to, eb_id, spell_id, dept_id, desig_id, dept_code, occu_code, shift, t_p,
             mc_id, mc_nos, working_hours, ot_hours, pay_scheme_id, payschme_id, update_for)
        SELECT
            :date_from, :date_to, x.eb_id, x.spell_id, x.dept_id, x.desig_id,
            x.dept_code, x.occu_code, x.shift, x.t_p,
            x.mc_id, LEFT(m.mech_code, 30),
            SUM(CASE WHEN x.attendance_type = 'R' THEN x.whrs ELSE 0 END),
            SUM(CASE WHEN x.attendance_type = 'O' THEN x.whrs ELSE 0 END),
            :pay_scheme_id, :pay_scheme_id, 'ATT'
        FROM (
            SELECT
                da.eb_id,
                da.spell_id,
                da.worked_department_id                     AS dept_id,
                da.worked_designation_id                    AS desig_id,
                {SHIFT_SQL}                                 AS shift,
                da.attendance_type,
                sd.sub_dept_code                            AS dept_code,
                dg.occu_code                                AS occu_code,  -- designation_mst.occu_code (user, 2026-10-04)
                dg.time_piece                               AS t_p,
                e.mc_id,
                SUM((da.working_hours - COALESCE(da.idle_hours, 0))
                    / CASE WHEN e.daily_atten_id IS NULL THEN 1 ELSE (
                          SELECT COUNT(*) FROM daily_ebmc_attendance e2
                           WHERE e2.daily_atten_id = da.daily_atten_id AND e2.is_active = 1)
                      END) AS whrs
            FROM daily_attendance da
            JOIN pay_employee_payscheme t
              ON t.EMPLOYEEID = da.eb_id
             AND t.PAY_SCHEME_ID = :pay_scheme_id
             AND t.STATUS = 1
            {SPELL_JOIN}
            -- worked_department_id holds a sub_dept_mst id (EJM's dept codes live there)
            LEFT JOIN sub_dept_mst sd    ON sd.sub_dept_id = da.worked_department_id
            LEFT JOIN designation_mst dg ON dg.designation_id = da.worked_designation_id
            LEFT JOIN daily_ebmc_attendance e
                   ON e.daily_atten_id = da.daily_atten_id AND e.is_active = 1
                  AND sd.dept_id IN ({mc_depts})
            WHERE da.attendance_date BETWEEN :date_from AND :date_to
              AND {ATT_LIVE_SQL}
              AND da.branch_id = :branch_id
            GROUP BY da.eb_id, da.attendance_date, da.spell_id, sp.spell_code, da.spell,
                     da.attendance_type, da.worked_department_id, da.worked_designation_id,
                     sd.sub_dept_code, dg.occu_code, dg.time_piece, e.mc_id
        ) x
        LEFT JOIN machine_mst m ON m.machine_id = x.mc_id
        GROUP BY x.eb_id, x.spell_id, x.dept_id, x.desig_id, x.dept_code, x.occu_code,
                 x.shift, x.t_p, x.mc_id, m.mech_code
    """)


def _night_half_hours_query(attendance_type: str, column: str):
    """Night-shift half-hours (legacy MainWagesProcessns, 'NSH' arm).

    Each day whose 'C' spell hours of this attendance type (working less idle,
    summed per day) are exactly 7.5 earns 0.5 in `column`. Run once per type:
    'R' -> ns_hours (night allowance), 'O' -> ot_hours (7.5 + 0.5 = 8 OT hours).
    ot_ns_hours is left for manual entry.
    """
    return text(f"""
        INSERT INTO tbl_ejm_wages_data_collection
            (date_from, date_to, eb_id, {column}, pay_scheme_id, payschme_id, update_for)
        SELECT :date_from, :date_to, d.eb_id, 0.5 * COUNT(*),
               :pay_scheme_id, :pay_scheme_id, 'NSH'
        FROM (
            SELECT da.eb_id, da.attendance_date,
                   SUM(da.working_hours - COALESCE(da.idle_hours, 0)) AS whrs
            FROM daily_attendance da
            JOIN pay_employee_payscheme t
              ON t.EMPLOYEEID = da.eb_id
             AND t.PAY_SCHEME_ID = :pay_scheme_id
             AND t.STATUS = 1
            {SPELL_JOIN}
            WHERE da.attendance_date BETWEEN :date_from AND :date_to
              AND {SHIFT_SQL} = 'C'
              AND {ATT_LIVE_SQL}
              AND da.branch_id = :branch_id
              AND da.attendance_type = '{attendance_type}'
            GROUP BY da.eb_id, da.attendance_date
            HAVING whrs = 7.5
        ) d
        GROUP BY d.eb_id
    """)


def _build_night_hours_query():
    return _night_half_hours_query("R", "ns_hours")


def _build_night_ot_hours_query():
    return _night_half_hours_query("O", "ot_hours")


def _build_festival_hours_query():
    """Festival hours from approved holidays (legacy MainWagesProcessns, 'FES').

    Reads what the Holiday Data process credited into
    hrms_holiday_transactions, for every holiday falling in the wage period.
    """
    return text("""
        INSERT INTO tbl_ejm_wages_data_collection
            (date_from, date_to, eb_id, fest_hours, pay_scheme_id, payschme_id, update_for)
        SELECT :date_from, :date_to, h.eb_id,
               SUM(h.holiday_hours), :pay_scheme_id, :pay_scheme_id, 'FES'
        FROM hrms_holiday_transactions h
        JOIN holiday_master hm ON hm.holiday_id = h.holiday_id
        JOIN pay_employee_payscheme t
          ON t.EMPLOYEEID = h.eb_id
         AND t.PAY_SCHEME_ID = :pay_scheme_id
         AND t.STATUS = 1
        WHERE h.is_active = 1
          AND hm.holiday_date BETWEEN :date_from AND :date_to
          AND hm.branch_id = :branch_id
        GROUP BY h.eb_id
    """)


def _build_stl_days_query():
    """STL days from approved STL leave (legacy MainWagesProcessns, 'STL').

    Leave type 24 is STL and status 3 is Approved, both as legacy hardcoded.
    Dates on the branch's non-working days (tbl_non_working_days) aren't counted.
    """
    return text("""
        INSERT INTO tbl_ejm_wages_data_collection
            (date_from, date_to, eb_id, stl_days, pay_scheme_id, payschme_id, update_for)
            SELECT  
             :date_from, :date_to, lt.eb_id,
               COUNT(*), :pay_scheme_id, :pay_scheme_id, 'STL'
        FROM vw_leave_dates  ltd
        JOIN leave_transactions lt ON lt.leave_transaction_id = ltd.leave_transaction_id
        JOIN pay_employee_payscheme t
          ON t.EMPLOYEEID = lt.eb_id
         AND t.PAY_SCHEME_ID = :pay_scheme_id
         AND t.STATUS = 1
        join hrms_leave_types_mst hltm on lt.leave_type_id =hltm.leave_type_id 
        WHERE ltd.leave_date BETWEEN :date_from AND :date_to
--          AND ltd.is_active = 1
          AND ltd.non_working_day = 'N'
          AND lt.status = 3
          AND hltm.payable  ='Y'
          AND lt.branch_id = :branch_id
         GROUP BY lt.eb_id
    """)


# Legacy hardcoded both: leave type 24 is STL, status 3 is Approved.
STL_LEAVE_TYPE = 24
LEAVE_APPROVED = 3
LEAVE_REJECTED = 4
LEAVE_CANCELLED = 6

# --- Main Payroll FN Attendance Incentive (legacy holget = 2) ---------------
# Legacy Loan_adv_model::getFNattincentiveData, all three hardcoded there.
INCENTIVE_FULL_DAY_HOURS = 8    # $elhrs — a date counts as worked at >= 8 hrs
INCENTIVE_MIN_DAYS = 12         # $attincd — days needed to qualify at all


def _attendance_incentive_sql(monthly: bool = False, scheme_bound: bool = True) -> str:
    """Fortnightly attendance incentive per employee (legacy holget = 2).

    The same query also serves the dot-matrix prints for the other legacy
    incentive codes: ``monthly`` switches to mn_att_inc_rate and the
    :min_paid_days floor (holget 4/8), ``scheme_bound=False`` drops the pay
    scheme join for the trainee variants (holget 7/8).

    Per-day eligibility (2026-09-15): for each employee and date,
      regular hours (attendance_type 'R', working less idle; a 'C' spell of
      7.5+ counts as a full 8) + holiday hours credited for that date
      + approved PAID leave hours (hrms_leave_types_mst.payable = 'Y',
      Leave_hours, NULL = 8)
    must reach :full_day_hours (8). An eligible date earns the incentive only
    when the employee has 'R' attendance on it — overtime ('O') never counts.
    An employee still qualifies at :min_days eligible dates, and is paid for
    the eligible 'R' dates (incdays) at their fn_att_inc_rate; lvdays are the
    eligible dates carried by holiday/leave alone.
    """
    rate = "mn_att_inc_rate" if monthly else "fn_att_inc_rate"
    scheme_join = ("""JOIN pay_employee_payscheme t
          ON t.EMPLOYEEID = k.eb_id
         AND t.PAY_SCHEME_ID = :pay_scheme_id
         AND t.STATUS = 1""" if scheme_bound else "")
    min_paid = "WHERE ROUND(k.incdays, 0) >= :min_paid_days" if monthly else ""
    return f"""
        SELECT
            k.eb_id,
            COALESCE(o.emp_code, '')                        AS emp_code,
            d.dept_desc,
            d.dept_code,
            TRIM(CONCAT(
                COALESCE(p.first_name, ''), ' ',
                COALESCE(p.middle_name, ''), ' ',
                COALESCE(p.last_name, '')))                 AS empname,
            ROUND(k.incdays, 0)                             AS incdays,
            k.elegday - k.incdays                           AS lvdays,
            k.elegday,
            e.{rate}                                        AS fn_att_inc_rate,
            ROUND(k.incdays * e.{rate}, 0)                  AS incamt
        FROM (
            SELECT eb_id, SUM(has_r) incdays, COUNT(*) - SUM(has_r) lvdays,
                   COUNT(*) elegday
            FROM (
                -- one row per employee-date whose hours reach a full day
                SELECT eb_id, dt, MAX(is_r) has_r
                FROM (
                    SELECT da.eb_id, da.attendance_date AS dt,
                           CASE WHEN {SHIFT_SQL} = 'C'
                                 AND (da.working_hours - COALESCE(da.idle_hours, 0)) >= 7.5
                                THEN 8
                                ELSE da.working_hours - COALESCE(da.idle_hours, 0)
                           END AS hrs,
                           1 AS is_r
                    FROM daily_attendance da
                    {SPELL_JOIN}
                    WHERE {ATT_LIVE_SQL}
                      AND da.attendance_date BETWEEN :date_from AND :date_to
                      AND da.branch_id = :branch_id
                      AND da.attendance_type = 'R'

                    UNION ALL

                    SELECT lt.eb_id, ltd.leave_date, COALESCE(ltm.Leave_hours, 8), 0
                    FROM leave_transactions lt
                    JOIN leave_tran_details ltd
                      ON lt.leave_transaction_id = ltd.ltran_id
                    JOIN hrms_leave_types_mst ltm
                      ON ltm.leave_type_id = lt.leave_type_id
                     AND ltm.payable = 'Y'
                    WHERE ltd.leave_date BETWEEN :date_from AND :date_to
                      AND lt.status = :leave_approved
                      AND lt.branch_id = :branch_id
                      AND ltd.is_active = 1

                    UNION ALL

                    SELECT h.eb_id, hm.holiday_date, COALESCE(h.holiday_hours, 0), 0
                    FROM hrms_holiday_transactions h
                    JOIN holiday_master hm ON hm.holiday_id = h.holiday_id
                    WHERE hm.branch_id = :branch_id
                      AND h.is_active = 1
                      AND hm.holiday_date BETWEEN :date_from AND :date_to
                ) d
                GROUP BY eb_id, dt
                HAVING SUM(hrs) >= :full_day_hours
            ) e
            GROUP BY eb_id
            HAVING COUNT(*) >= :min_days
        ) k
        JOIN tbl_holiday_att_inc_eligibility e
          ON e.eb_id = k.eb_id
         AND e.att_incn_eligibility = 'Y'
         AND e.is_active = 1
        {scheme_join}
        LEFT JOIN hrms_ed_official_details o ON o.eb_id = k.eb_id AND o.active = 1
        LEFT JOIN hrms_ed_personal_details p ON p.eb_id = k.eb_id
        LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = o.sub_dept_id
        LEFT JOIN dept_mst d      ON d.dept_id = sd.dept_id
        {min_paid}
        ORDER BY d.dept_code, o.emp_code
    """


def _attendance_incentive_query(monthly: bool = False, scheme_bound: bool = True):
    return text(_attendance_incentive_sql(monthly, scheme_bound))


def _build_incentive_query():
    """Main Payroll FN Attendance Incentive (legacy holget = 2) staged into
    atten_incentive ('INC' arm).

    Legacy only reported/exported this figure and let the payroll engine
    recompute it as component 248; the Wages Data Process pipeline posts it
    with the other arms instead so the collected period is complete in one
    place. It gets its own column so a manual misc_earn entry is never mixed
    with it.
    """
    return text(f"""
        INSERT INTO tbl_ejm_wages_data_collection
            (date_from, date_to, eb_id, atten_incentive, pay_scheme_id, payschme_id, update_for)
        SELECT :date_from, :date_to, i.eb_id, i.incamt, :pay_scheme_id, :pay_scheme_id, 'INC'
        FROM ({_attendance_incentive_sql()}) i
        WHERE i.incamt > 0
    """)


def _year_starts(date_from: str) -> tuple[str, str]:
    """(calendar-year start, financial-year start) for a period's first day.
    The financial year runs April to March."""
    d = date.fromisoformat(str(date_from)[:10])
    fy = d.year if d.month >= 4 else d.year - 1
    return f"{d.year}-01-01", f"{fy}-04-01"


def _build_prev_cumulative_query():
    """Previous cumulatives ('CUM' arm): what each hand has already run up
    this year before the period, one row per hand, summed from
    cumulative_data_table (one row per hand per past period, posted by
    ejm_cumulative_transfer). The payroll engine adds the current period on
    top — legacy fed these in as the *_PREV_* input components.

      prev_cuml_w_days            calendar year   (working + night hours) / 8
      prev_cuml_pf_gross          financial year  pf_gross
      prev_cuml_pf_contribution   financial year  pf_contibution
      prev_cuml_bonus_gross       financial year  bonus_gross      (C_BON_EARN)
      prev_cuml_fpf_contribution  financial year  fpf_contribution (C_FPF_CONT)

    Only periods starting before this one count, so a year's first period
    starts from zero.
    """
    def fy(col):
        return f"COALESCE(SUM(CASE WHEN c.date_from >= :fy_start THEN c.{col} END), 0)"

    return text(f"""
        INSERT INTO tbl_ejm_wages_data_collection
            (date_from, date_to, eb_id, prev_cuml_w_days, prev_cuml_pf_gross,
             prev_cuml_pf_contribution, prev_cuml_bonus_gross, prev_cuml_fpf_contribution,
             pay_scheme_id, payschme_id, update_for)
        SELECT :date_from, :date_to, c.eb_id,
               ROUND(COALESCE(SUM(CASE WHEN c.date_from >= :cy_start
                     THEN (c.working_hours + c.night_shift_hours) / 8 END), 0), 3),
               {fy("pf_gross")}, {fy("pf_contibution")},
               {fy("bonus_gross")}, {fy("fpf_contribution")},
               :pay_scheme_id, :pay_scheme_id, 'CUM'
        FROM cumulative_data_table c
        JOIN pay_employee_payscheme t
          ON t.EMPLOYEEID = c.eb_id
         AND t.PAY_SCHEME_ID = :pay_scheme_id
         AND t.STATUS = 1
        WHERE c.date_from >= LEAST(:cy_start, :fy_start)
          AND c.date_from < :date_from
        GROUP BY c.eb_id
    """)

# The arms this process owns, in pipeline order. A rebuild replaces exactly
# the arms it runs; anything else in the period (manual entries, advances) is
# left alone — legacy cleared everything except 'M', which would have
# destroyed advance rows.
BUILD_ARMS = (
    ("ATT", "attendance", _build_from_attendance_query),
    ("NSH", "night hours", _build_night_hours_query),
    ("NSH", "night OT hours", _build_night_ot_hours_query),
    ("STL", "STL days", _build_stl_days_query),
    ("FES", "festival hours", _build_festival_hours_query),
    ("INC", "attendance incentive", _build_incentive_query),
    ("CUM", "previous cumulative", _build_prev_cumulative_query),
)
BUILD_ARM_CODES = tuple(code for code, _, _ in BUILD_ARMS)


@router.post("/ejm_att_incentive_report")
def ejm_att_incentive_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Main Payroll FN Attendance Incentive — read-only, nothing is posted."""
    body = parse_json_body(request)
    date_from = body.get("date_from")
    date_to = body.get("date_to")
    pay_scheme_id = body.get("pay_scheme_id")
    branch_id = body.get("branch_id")
    if not date_from or not date_to or not pay_scheme_id:
        raise HTTPException(status_code=400, detail="date_from, date_to and pay_scheme_id are required")
    if not branch_id:
        raise HTTPException(status_code=400, detail="Select a branch in the sidebar before running this report")

    rows = db.execute(_attendance_incentive_query(), {
        "date_from": date_from,
        "date_to": date_to,
        "pay_scheme_id": int(pay_scheme_id),
        "branch_id": int(branch_id),
        "full_day_hours": INCENTIVE_FULL_DAY_HOURS,
        "min_days": INCENTIVE_MIN_DAYS,
        "leave_approved": LEAVE_APPROVED,
    }).fetchall()

    return {"data": [dict(r._mapping) for r in rows]}


def _fortnight_id(db: Session, date_from: str, date_to: str) -> int:
    """From/To must be exactly one forthnight_period_mst fortnight — the same
    period master ejm_wages_fne_qual_target and the other ejm_fne_* tables use."""
    row = db.execute(text("""
        SELECT forthnight_id FROM forthnight_period_mst
        WHERE start_date = :date_from AND end_date = :date_to
        ORDER BY forthnight_id DESC LIMIT 1
    """), {"date_from": date_from, "date_to": date_to}).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=(
            f"{date_from} to {date_to} is not a fortnight in the fortnight period master — "
            "pick the fortnight's exact start and end dates"))
    return int(row.forthnight_id)


@router.post("/ejm_wages_data_build")
def ejm_wages_data_build(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Rebuild the derived wages rows for one period + pay scheme.

    Body may name the arms to run (``arms: ["ATT", "NSH"]``); the Wages Data
    Process pipeline calls this once per step. Default is every arm.
    Re-runnable: only the rows of the arms being run are cleared first.
    Advance/other entries keyed to the same period come from elsewhere and
    must survive a re-run.
    """
    body = parse_json_body(request)
    date_from = body.get("date_from")
    date_to = body.get("date_to")
    pay_scheme_id = body.get("pay_scheme_id")
    branch_id = body.get("branch_id")
    if not date_from or not date_to or not pay_scheme_id:
        raise HTTPException(status_code=400, detail="date_from, date_to and pay_scheme_id are required")
    if not branch_id:
        raise HTTPException(status_code=400, detail="Select a branch in the sidebar before processing")
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="To date cannot be earlier than From date")
    try:
        cy_start, fy_start = _year_starts(date_from)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")
    wanted = body.get("arms") or list(BUILD_ARM_CODES)
    unknown = [a for a in wanted if a not in BUILD_ARM_CODES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown build arm(s): {', '.join(map(str, unknown))}")
    arms = [arm for arm in BUILD_ARMS if arm[0] in wanted]
    # From/To must be an existing pay period of the scheme (404 with the reason otherwise).
    _resolve_period(db, date_from, date_to, int(pay_scheme_id), int(branch_id))
    period_id = _fortnight_id(db, date_from, date_to)

    params = {
        "date_from": date_from,
        "date_to": date_to,
        "pay_scheme_id": int(pay_scheme_id),
        "branch_id": int(branch_id),
        "stl_leave_type": STL_LEAVE_TYPE,
        "leave_approved": LEAVE_APPROVED,
        "full_day_hours": INCENTIVE_FULL_DAY_HOURS,
        "min_days": INCENTIVE_MIN_DAYS,
        "cy_start": cy_start,
        "fy_start": fy_start,
    }
    print("ejm build params:", params, "arms:", [a[0] for a in arms], flush=True)  # debug, remove before commit
    # Unapproved attendance would silently drop out of every arm — refuse to
    # build until the scheme's attendance and leave for the period are approved.
    pending = db.execute(text("""
        SELECT COUNT(*) AS n, MIN(da.attendance_date) AS first_date, MAX(da.attendance_date) AS last_date
        FROM daily_attendance da
        JOIN pay_employee_payscheme t
          ON t.EMPLOYEEID = da.eb_id
         AND t.PAY_SCHEME_ID = :pay_scheme_id
         AND t.STATUS = 1
        WHERE da.attendance_date BETWEEN :date_from AND :date_to
          AND da.branch_id = :branch_id
          AND da.is_active = 1
          AND (da.status_id IS NULL OR da.status_id <> :approved)
    """), {**params, "approved": ATTENDANCE_APPROVED}).mappings().one()
    if pending["n"]:
        raise HTTPException(status_code=400, detail=(
            f"{pending['n']} attendance record(s) between {pending['first_date']} and "
            f"{pending['last_date']} are not approved. Approve them before processing wages."))

    # Rejected / cancelled leave is final, not pending, so it does not block.
    pending = db.execute(text("""
        SELECT COUNT(DISTINCT lt.leave_transaction_id) AS n,
               MIN(ltd.leave_date) AS first_date, MAX(ltd.leave_date) AS last_date
        FROM leave_transactions lt
        JOIN leave_tran_details ltd ON ltd.ltran_id = lt.leave_transaction_id
        JOIN pay_employee_payscheme t
          ON t.EMPLOYEEID = lt.eb_id
         AND t.PAY_SCHEME_ID = :pay_scheme_id
         AND t.STATUS = 1
        WHERE ltd.leave_date BETWEEN :date_from AND :date_to
          AND lt.branch_id = :branch_id
          AND ltd.is_active = 1
          AND (lt.status IS NULL OR lt.status NOT IN (:approved, :rejected, :cancelled))
    """), {**params, "approved": str(LEAVE_APPROVED),
           "rejected": str(LEAVE_REJECTED), "cancelled": str(LEAVE_CANCELLED)}).mappings().one()
    if pending["n"]:
        raise HTTPException(status_code=400, detail=(
            f"{pending['n']} leave request(s) between {pending['first_date']} and "
            f"{pending['last_date']} are not approved. Approve them before processing wages."))

    key ={k: params[k] for k in ("date_from", "date_to", "pay_scheme_id")}
    try:
        # Each step first deletes its own update_for rows for the WHOLE fortnight
        # (user, 2026-10-04): rows stamped with this period, or any date range
        # inside it (e.g. a stale 16–22 run inside 16–30) — same pay scheme only.
        cleared = db.execute(text("""
            DELETE FROM tbl_ejm_wages_data_collection
            WHERE pay_scheme_id = :pay_scheme_id
              AND update_for IN :codes
              AND (period_id = :period_id
                   OR (date_from >= :date_from AND date_to <= :date_to))
        """).bindparams(codes=tuple(code for code, _, _ in arms)),
            {**key, "period_id": period_id}).rowcount

        # Each arm posts its own rows, exactly as legacy did — an employee with
        # festival hours but no attendance still gets credited.
        counts = {
            label: db.execute(build_query(), params).rowcount
            for _, label, build_query in arms
        }
        # The arms' INSERTs don't carry the fortnight; stamp it on the period's rows.
        db.execute(text("""
            UPDATE tbl_ejm_wages_data_collection SET period_id = :period_id
            WHERE date_from = :date_from AND date_to = :date_to
              AND pay_scheme_id = :pay_scheme_id
              AND (period_id IS NULL OR period_id <> :period_id)
        """), {**key, "period_id": period_id})
        db.commit()

        inserted = sum(counts.values())
        if inserted == 0:
            what = " / ".join(label for _, label, _ in arms)
            return {
                "message": f"No {what} found for that period, pay scheme and branch",
                "inserted": 0, "cleared": cleared, "counts": counts,
            }
        detail = ", ".join(f"{n} {label}" for label, n in counts.items() if n)
        return {
            "message": f"{inserted} wages record(s) built ({detail})"
                       + (f"; {cleared} previous record(s) replaced" if cleared else ""),
            "inserted": inserted, "cleared": cleared, "counts": counts,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ejm_wages_data_list")
def ejm_wages_data_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Collected wages data for one wage period + pay scheme."""
    body = parse_json_body(request)
    date_from = body.get("date_from")
    date_to = body.get("date_to")
    pay_scheme_id = body.get("pay_scheme_id")
    if not date_from or not date_to or not pay_scheme_id:
        raise HTTPException(status_code=400, detail="date_from, date_to and pay_scheme_id are required")

    rows = db.execute(text("""
        SELECT
            a.data_collection_id,
            a.eb_id,
            COALESCE(o.emp_code, '')                          AS eb_no,
            TRIM(CONCAT(
                COALESCE(p.first_name, ''), ' ',
                COALESCE(p.middle_name, ''), ' ',
                COALESCE(p.last_name, '')))                   AS emp_name,
            a.spell_id, spl.spell_code AS spell_name,
            a.dept_id, sd.sub_dept_desc AS sub_dept_name,
            a.desig_id, dg.desig AS desig_name,
            COALESCE(NULLIF(a.dept_code, ''), sd.sub_dept_code)              AS dept_code,
            COALESCE(NULLIF(a.occu_code, ''), dg.occu_code)                  AS occu_code,
            a.shift, a.t_p, a.mc_nos, a.mc_id,
            a.working_hours, a.ot_hours, a.ns_hours, a.ot_ns_hours,
            a.act_eff, a.working_hours_eff, a.ot_hours_eff, a.ns_hrs_eff, a.prod_basic, a.time_basic,
            a.fest_hours, a.stl_days, a.production,
            a.puja_advance, a.ot_advance, a.stl_advance, a.installment_advance,
            a.co_loan, a.atten_incentive, a.misc_earn, a.misc_ded, a.misc_ot_earn,
            a.misc_ot_ded, a.prev_cuml_w_days, a.prev_cuml_pf_gross,
            a.prev_cuml_pf_contribution, a.prev_cuml_bonus_gross,
            a.prev_cuml_fpf_contribution, a.update_for
        FROM tbl_ejm_wages_data_collection a
        LEFT JOIN spell_mst spl      ON spl.spell_id = a.spell_id
        LEFT JOIN sub_dept_mst sd    ON sd.sub_dept_id = a.dept_id
        LEFT JOIN designation_mst dg ON dg.designation_id = a.desig_id
        LEFT JOIN hrms_ed_official_details o ON o.eb_id = a.eb_id AND o.active = 1
        LEFT JOIN hrms_ed_personal_details p ON p.eb_id = a.eb_id
        WHERE a.date_from = :date_from
          AND a.date_to = :date_to
          AND a.pay_scheme_id = :pay_scheme_id
          AND a.is_active = 1
          AND (:update_for IS NULL OR a.update_for = :update_for)
        ORDER BY o.emp_code, a.data_collection_id
    """), {
        "date_from": date_from,
        "date_to": date_to,
        "pay_scheme_id": int(pay_scheme_id),
        "update_for": body.get("update_for") or None,
    }).fetchall()

    return {"data": [dict(r._mapping) for r in rows]}


@router.post("/ejm_wages_eff_data_list")
def ejm_wages_eff_data_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The fortnight's efficiency rows (ejm_fne_wages_eff_data) — the second
    sheet of the Wages Data Process excel. Not a fortnight → no rows."""
    body = parse_json_body(request)
    date_from = body.get("date_from")
    date_to = body.get("date_to")
    if not date_from or not date_to:
        raise HTTPException(status_code=400, detail="date_from and date_to are required")
    period = db.execute(text("""
        SELECT forthnight_id FROM forthnight_period_mst
        WHERE start_date = :date_from AND end_date = :date_to
        ORDER BY forthnight_id DESC LIMIT 1
    """), {"date_from": date_from, "date_to": date_to}).fetchone()
    if not period:
        return {"data": []}

    rows = db.execute(text("""
        SELECT e.details, e.detcode, e.deptcode, e.occucode,
               COALESCE(o.emp_code, '') AS eb_no, e.name, s.shift_name AS shift,
               e.mcno, e.quality_id, e.quantity, e.target_qty, e.eff, e.tareff, e.acteff,
               e.effcode, e.prod_amount
        FROM ejm_fne_wages_eff_data e
        LEFT JOIN shift_mst s ON s.shift_id = e.shift_id
        LEFT JOIN hrms_ed_official_details o ON o.eb_id = e.eb_id AND o.active = 1
        WHERE e.period_id = :period_id
        ORDER BY e.detcode, e.deptcode, e.occucode, o.emp_code, s.shift_name
    """), {"period_id": int(period.forthnight_id)}).fetchall()

    return {"data": [dict(r._mapping) for r in rows]}


# ─── Wages Manual Entries (wages/ejm/wagesManualEntry) ──────────────────────
#
# Hand-keyed rows, stamped update_for = 'Manual'. The Wages Data Process only
# clears its own codes (ATT/NSH/FES/...), so these survive a re-run, and the
# Pay Roll excel sums them with everything else.

MANUAL_UPDATE_FOR = "Manual"
MANUAL_FIELDS = (
    "working_hours", "ot_hours", "ns_hours", "fest_hours", "stl_days",
    "working_hours_eff", "ot_hours_eff", "ot_ns_hours", "ns_hrs_eff",
    "puja_advance", "ot_advance", "stl_advance", "co_loan",
    "misc_earn", "misc_ded", "misc_ot_earn", "misc_ot_ded",
)


@router.post("/ejm_wages_scheme_employees")
def ejm_wages_scheme_employees(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Employees on a pay scheme, for the EB No picker. Branch-scoped: emp_code is
    unique only per branch on sls, so an unscoped list shows twins."""
    body = parse_json_body(request)
    if not body.get("pay_scheme_id"):
        raise HTTPException(status_code=400, detail="pay_scheme_id is required")
    rows = db.execute(get_payscheme_mapped_employees(), {
        "pay_scheme_id": int(body["pay_scheme_id"]),
        "branch_id": int(body["branch_id"]) if body.get("branch_id") else None,
    }).mappings().all()
    return {"data": [{
        "eb_id": r["eb_id"],
        "eb_no": r["emp_code"] or "",
        "emp_name": " ".join(filter(None, (r["first_name"], r["middle_name"], r["last_name"]))),
    } for r in sorted(rows, key=lambda r: r["emp_code"] or "")]}


@router.post("/ejm_wages_manual_create")
def ejm_wages_manual_create(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Add one manual row for an employee of the selected pay scheme."""
    body = parse_json_body(request)
    for k in ("date_from", "date_to", "pay_scheme_id", "eb_id"):
        if not body.get(k):
            raise HTTPException(status_code=400, detail=f"{k} is required")
    params = {
        "date_from": body["date_from"], "date_to": body["date_to"],
        "pay_scheme_id": int(body["pay_scheme_id"]), "eb_id": int(body["eb_id"]),
        "update_for": MANUAL_UPDATE_FOR,
        **{f: float(body.get(f) or 0) for f in MANUAL_FIELDS},
    }
    on_scheme = db.execute(text("""
        SELECT 1 FROM pay_employee_payscheme
        WHERE EMPLOYEEID = :eb_id AND PAY_SCHEME_ID = :pay_scheme_id AND STATUS = 1 LIMIT 1
    """), params).fetchone()
    if not on_scheme:
        raise HTTPException(status_code=400, detail="Employee is not on the selected pay scheme")
    try:
        result = db.execute(text(f"""
            INSERT INTO tbl_ejm_wages_data_collection
                (date_from, date_to, eb_id, pay_scheme_id, payschme_id, update_for,
                 {", ".join(MANUAL_FIELDS)})
            VALUES (:date_from, :date_to, :eb_id, :pay_scheme_id, :pay_scheme_id, :update_for,
                 {", ".join(f":{f}" for f in MANUAL_FIELDS)})
        """), params)
        db.commit()
        return {"message": "Manual entry added", "data_collection_id": result.lastrowid}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/ejm_wages_manual_delete/{data_collection_id}")
def ejm_wages_manual_delete(
    data_collection_id: int,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft-delete a manual row. Process-built rows are refused."""
    result = db.execute(text("""
        UPDATE tbl_ejm_wages_data_collection SET is_active = 0
        WHERE data_collection_id = :id AND update_for = :update_for AND is_active = 1
    """), {"id": data_collection_id, "update_for": MANUAL_UPDATE_FOR})
    if result.rowcount == 0:
        db.rollback()
        raise HTTPException(status_code=404, detail="Manual entry not found")
    db.commit()
    return {"message": "Manual entry deleted"}


# Upload headers -> MANUAL_FIELDS. Headers are normalised (upper case, non
# alphanumerics -> '_'), so the legacy WAGES ADJUSTMENT sheet (WK_HRS, MIS_OT-ER,
# ...) and the field names themselves both work. Columns not in the file stay 0.
MANUAL_UPLOAD_HEADERS = {
    "WK_HRS": "working_hours", "OT_HRS": "ot_hours", "NS_HRS": "ns_hours",
    "F_HRS": "fest_hours", "STL_DAYS": "stl_days", "WRK_HRS_EFF": "working_hours_eff",
    "OT_HRS_EFF": "ot_hours_eff", "OT_NS_HRS": "ot_ns_hours", "NS_HRS_EFF": "ns_hrs_eff",
    "PUJA_AD": "puja_advance", "OT_AD": "ot_advance", "STL_AD": "stl_advance",
    "CO_LOAN": "co_loan", "MISC_ER": "misc_earn", "MIS_DED": "misc_ded",
    "MIS_OT_ER": "misc_ot_earn", "MIS_OT_DE": "misc_ot_ded",
    **{f.upper(): f for f in MANUAL_FIELDS},
}


def _norm_header(h) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(h or "").strip().upper()).strip("_")


def _parse_manual_rows(rows):
    """[(eb_no, {field: value})] + error list. First row is the header; needs an
    EB_NO column. Rows without an EB No and with no values are skipped."""
    if not rows:
        return [], ["The file is empty"]
    header = [_norm_header(h) for h in rows[0]]
    eb_col = next((i for i, h in enumerate(header) if h in ("EB_NO", "EBNO")), None)
    if eb_col is None:
        return [], ["No EB_NO column in the header row"]
    cols = {i: MANUAL_UPLOAD_HEADERS[h] for i, h in enumerate(header) if h in MANUAL_UPLOAD_HEADERS}
    if not cols:
        return [], ["No wages columns recognised in the header row"]
    parsed, errors, seen = [], [], set()
    for n, r in enumerate(rows[1:], start=2):
        eb_no = str(r[eb_col]).strip() if eb_col < len(r) and r[eb_col] is not None else ""
        if eb_no.endswith(".0"):  # Excel stores numeric EB Nos as floats
            eb_no = eb_no[:-2]
        values, bad = {}, False
        for i, f in cols.items():
            raw = r[i] if i < len(r) else None
            try:
                values[f] = float(raw) if raw not in (None, "") else 0.0
            except (TypeError, ValueError):
                errors.append(f"Row {n}: {rows[0][i]} '{raw}' is not a number")
                bad = True
        if not eb_no:
            if any(values.values()):
                errors.append(f"Row {n}: EB No missing")
            continue
        if bad:
            continue
        if eb_no in seen:
            errors.append(f"Row {n}: EB No {eb_no} repeated")
            continue
        seen.add(eb_no)
        parsed.append((eb_no, values))
    return parsed, errors


# Each EB No's own active pay scheme(s), branch-scoped (emp_code twins on sls).
_MANUAL_UPLOAD_HANDS = text("""
    SELECT o.emp_code, o.eb_id,
           (SELECT GROUP_CONCAT(DISTINCT t.PAY_SCHEME_ID) FROM pay_employee_payscheme t
             WHERE t.EMPLOYEEID = o.eb_id AND t.STATUS = 1) AS schemes
    FROM hrms_ed_official_details o
    WHERE o.active = 1 AND (:branch_id IS NULL OR o.branch_id = :branch_id)
      AND o.emp_code IN :codes
""").bindparams(bindparam("codes", expanding=True))


@router.post("/ejm_wages_manual_upload")
def ejm_wages_manual_upload(
    file: UploadFile = File(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    branch_id: int | None = Form(None),
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Manual rows from Excel for the selected period. Every row is uploaded
    under the employee's own active pay scheme; EB Nos that are unknown, have no
    pay scheme or several are skipped and listed in the message. A bad number or
    repeated EB No rejects the whole file. An uploaded employee's earlier manual
    row for the period is replaced, so re-uploading does not double up."""
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="File must be an Excel .xlsx")
    try:
        ws = load_workbook(BytesIO(file.file.read()), data_only=True, read_only=True).active
        rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read Excel file: {e}")

    parsed, errors = _parse_manual_rows(rows)
    if errors:
        more = f" … and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise HTTPException(status_code=400, detail="Nothing uploaded. " + "; ".join(errors[:20]) + more)
    if not parsed:
        raise HTTPException(status_code=400, detail="No rows in the file")

    hands = {}
    for r in db.execute(_MANUAL_UPLOAD_HANDS, {"branch_id": branch_id,
                                               "codes": [c for c, _ in parsed]}).mappings():
        hands.setdefault(r["emp_code"], []).append(r)
    inserts, no_scheme, skipped = [], [], []
    for eb_no, values in parsed:
        found = hands.get(eb_no, [])
        if not found:
            skipped.append(f"{eb_no} (not found)")
        elif len(found) > 1:
            skipped.append(f"{eb_no} (matches {len(found)} employees)")
        elif not found[0]["schemes"]:
            no_scheme.append(eb_no)
        elif "," in str(found[0]["schemes"]):
            skipped.append(f"{eb_no} (several pay schemes {found[0]['schemes']})")
        else:
            inserts.append({"eb_id": found[0]["eb_id"], "pay_scheme_id": int(found[0]["schemes"]),
                            **{f: values.get(f, 0.0) for f in MANUAL_FIELDS}})

    replaced = 0
    if inserts:
        params = {"date_from": date_from, "date_to": date_to, "update_for": MANUAL_UPDATE_FOR}
        try:
            replaced = db.execute(text("""
                UPDATE tbl_ejm_wages_data_collection SET is_active = 0
                WHERE update_for = :update_for AND is_active = 1
                  AND date_from = :date_from AND date_to = :date_to AND eb_id IN :eb_ids
            """).bindparams(bindparam("eb_ids", expanding=True)),
                {**params, "eb_ids": [r["eb_id"] for r in inserts]}).rowcount
            db.execute(text(f"""
                INSERT INTO tbl_ejm_wages_data_collection
                    (date_from, date_to, eb_id, pay_scheme_id, payschme_id, update_for,
                     {", ".join(MANUAL_FIELDS)})
                VALUES (:date_from, :date_to, :eb_id, :pay_scheme_id, :pay_scheme_id, :update_for,
                     {", ".join(f":{f}" for f in MANUAL_FIELDS)})
            """), [{**params, **r} for r in inserts])
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=str(e))

    notes = []
    if no_scheme:
        notes.append(f"No pay scheme for EB No {', '.join(no_scheme)}")
    if skipped:
        notes.append(f"Skipped EB No {', '.join(skipped)}")
    return {
        "message": f"{len(inserts)} manual record(s) uploaded"
                   + (f"; {replaced} previous record(s) replaced" if replaced else "")
                   + "".join(f". {n}" for n in notes),
        "inserted": len(inserts), "replaced": replaced,
        "no_scheme": no_scheme, "skipped": skipped,
    }


@router.put("/ejm_wages_data_update/{data_collection_id}")
def ejm_wages_data_update(
    data_collection_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Correct one collected row. Only the fields sent are touched."""
    body = parse_json_body(request)
    updates = {k: v for k, v in body.items() if k in EDITABLE_FIELDS}
    if not updates:
        raise HTTPException(status_code=400, detail="No editable fields in payload")

    try:
        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        result = db.execute(
            text(f"""
                UPDATE tbl_ejm_wages_data_collection
                SET {set_clause}
                WHERE data_collection_id = :data_collection_id AND is_active = 1
            """),
            {**updates, "data_collection_id": data_collection_id},
        )
        if result.rowcount == 0:
            db.rollback()
            raise HTTPException(status_code=404, detail="Record not found")
        db.commit()
        return {"message": "Record updated", "data_collection_id": data_collection_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── Excel for Pay Roll ─────────────────────────────────────────────────────
#
# The Pay Roll screen's upload template (payRoll.pay_roll_download_template:
# Employee Code / Employee Name / PayScheme Name, then one column per input
# component CODE of the scheme) filled from the period's collected wages, so it
# uploads through Pay Roll unchanged. Every hand on the scheme is listed, zeros
# where nothing was collected, so one upload covers the whole scheme.

# wages column -> component CODE (the header the upload resolves). A wages
# column with no input component on the scheme stays out: ot_ns_hours,
# production, installment_advance, misc_ded, misc_ot_ded, prev_cuml_pf_gross.
# Every other template column (C_SHIFT_ included) is written as 0.
PAYROLL_EXCEL_MAP = {
    "prod_basic": "C_PROD_BASIC_", "time_basic": "C_TIME_RATED_BASIC_",
    "working_hours": "C_WORKING_HR_", "ot_hours": "c_ot_hrs_", "ns_hours": "C_NSHR_",
    "fest_hours": "C_HLHRS_", "stl_days": "C_STLD_",
    "working_hours_eff": "C_WRK_HR_EFF_", "ot_hours_eff": "C_OT_HR_EFF_",
    "puja_advance": "C_PUJA_ADV_", "ot_advance": "C_OTADV_", "stl_advance": "C_STL_ADV_",
    "co_loan": "C_CO_LOAN_", "atten_incentive": "C_ATTINCENTIVE_",
    "misc_earn": "C_MISC_EARN_", "misc_ot_earn": "C_miscotearn_",
    "prev_cuml_w_days": "C_PRV_C_WORKDAY_", "prev_cuml_bonus_gross": "C_PREV_C_BON_ERN_",
    "prev_cuml_pf_contribution": "C_PF_PREV_CONT_",
    "prev_cuml_fpf_contribution": "C_FPF_PREV_CONT_",
}
_PAYROLL_SUMS = text(f"""
    SELECT eb_id,
           {", ".join(f"SUM(COALESCE({c}, 0)) AS {c}" for c in PAYROLL_EXCEL_MAP)}
    FROM tbl_ejm_wages_data_collection
    WHERE date_from = :date_from AND date_to = :date_to
      AND pay_scheme_id = :pay_scheme_id AND is_active = 1
    GROUP BY eb_id
""")

# Only Joined employees (status 35) go into the Pay Roll upload.
_PAYROLL_HANDS = text(f"""
    SELECT DISTINCT o.eb_id, o.emp_code,
           TRIM(CONCAT(COALESCE(p.first_name, ''), ' ', COALESCE(p.middle_name, ''), ' ',
                       COALESCE(p.last_name, ''))) AS emp_name
    FROM pay_employee_payscheme t
    JOIN hrms_ed_official_details o ON o.eb_id = t.EMPLOYEEID AND o.active = 1
    JOIN hrms_ed_personal_details p
      ON p.eb_id = o.eb_id AND p.status_id = {EMPLOYEE_LIFECYCLE_STATUS["JOINED"]}
    WHERE t.PAY_SCHEME_ID = :pay_scheme_id AND t.STATUS = 1
    ORDER BY o.emp_code
""")


def _payroll_excel_rows(codes, scheme_name, hands, sums):
    """Header + one row per hand, in the template's column order. Pure, so the
    layout is checkable without a workbook or a database."""
    by_code = {code: col for col, code in PAYROLL_EXCEL_MAP.items()}
    header = ["Employee Code", "Employee Name", "PayScheme Name", *codes]
    rows = []
    for eb_id, emp_code, emp_name in hands:
        s = sums.get(eb_id, {})
        vals = [float(s.get(by_code.get(code, ""), 0) or 0) for code in codes]
        rows.append([emp_code, (emp_name or "").strip(), scheme_name, *vals])
    return header, rows


@router.get("/ejm_wages_payroll_excel")
def ejm_wages_payroll_excel(
    request: Request,
    date_from: str,
    date_to: str,
    pay_scheme_id: int,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Excel for Pay Roll: the scheme's Pay Roll upload template, filled from
    the period's collected wages for every hand on the scheme."""
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="To date cannot be earlier than From date")
    codes = [r._mapping["code"] for r in db.execute(
        get_pay_roll_custom_components(), {"pay_scheme_id": pay_scheme_id}).fetchall()]
    if not codes:
        raise HTTPException(status_code=400, detail=(
            "This pay scheme has no Excel-downloadable input component, so there is nothing to export"))
    scheme_name = db.execute(
        text("SELECT payscheme_name FROM pay_scheme_master WHERE payscheme_id = :id"),
        {"id": pay_scheme_id}).scalar() or ""
    hands = [tuple(r) for r in db.execute(_PAYROLL_HANDS, {"pay_scheme_id": pay_scheme_id}).fetchall()]
    if not hands:
        raise HTTPException(status_code=400, detail="No employee is on this pay scheme")
    sums = {r._mapping["eb_id"]: dict(r._mapping) for r in db.execute(_PAYROLL_SUMS, {
        "date_from": date_from, "date_to": date_to, "pay_scheme_id": pay_scheme_id,
    }).fetchall()}

    header, rows = _payroll_excel_rows(codes, scheme_name, hands, sums)
    wb = Workbook()
    ws = wb.active
    ws.title = "PayRoll"
    ws.append(header)
    for row in rows:
        ws.append(row)
    bio = BytesIO()
    wb.save(bio)
    return StreamingResponse(
        iter([bio.getvalue()]),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="PayRoll_{pay_scheme_id}_{date_from}_to_{date_to}.xlsx"'},
    )


# ─── EJM-Cumulative Data Transfer (legacy hol_get = 20) ─────────────────────
#
# Legacy (Data_entry::exportdbfdata + Loan_adv_model::getejmattdata) dumped one
# CSV line per employee with earnings in the pay period, which was then imported
# into cumulative_data_table. This posts the same columns straight into that
# table. Component ids are the legacy tbl_pay_components ids (see ejmPayRegister).
# ponytail: bf/cf/puja-adv/co-loan balances are written as 0 exactly as the
# legacy CSV did (components 223 / 236 / 186 / 222 if they are ever wanted).
# bonus_gross / fpf_contribution are not in the legacy file; the 'CUM' arm's
# financial-year sums need them (rows loaded from the DBF carry 0 there until
# this transfer is re-run for their periods).
CUMULATIVE_COLUMNS = [
    ("working_hours", 178), ("night_shift_hours", 179), ("fest_hours", 180),
    ("stl_days", 183), ("pf_gross", 134), ("pf_contibution", 18),
    ("fest_amount", 109), ("total_earnings", 224),
    ("bonus_gross", 207), ("fpf_contribution", 232),
]

_CUMULATIVE_DELETE = text("""
    DELETE FROM cumulative_data_table
    WHERE date_from = :date_from AND date_to = :date_to
      AND eb_id IN (SELECT DISTINCT k.EMPLOYEEID FROM pay_employee_payroll k
                    WHERE k.PAYPERIOD_ID = :payperiod_id)
""")

_CUMULATIVE_INSERT = text(f"""
    INSERT INTO cumulative_data_table
        (date_from, date_to, eb_id, {", ".join(f for f, _ in CUMULATIVE_COLUMNS)},
         bf_amount, cf_amount, puja_adv_bal, co_loan_bal)
    SELECT :date_from, :date_to, g.EMPLOYEEID,
           {", ".join(f"g.{f}" for f, _ in CUMULATIVE_COLUMNS)}, 0, 0, 0, 0
    FROM (
        SELECT k.EMPLOYEEID,
               {", ".join(f"MAX(CASE WHEN k.COMPONENT_ID = {cid} THEN k.AMOUNT ELSE 0 END) AS {f}"
                          for f, cid in CUMULATIVE_COLUMNS)}
        FROM pay_employee_payroll k
        WHERE k.PAYPERIOD_ID = :payperiod_id AND k.STATUS = 1
          AND EXISTS (SELECT 1 FROM hrms_ed_official_details od
                      WHERE od.eb_id = k.EMPLOYEEID AND od.active = 1)
        GROUP BY k.EMPLOYEEID
    ) g
    WHERE g.total_earnings > 0
""")


@router.post("/ejm_cumulative_transfer")
def ejm_cumulative_transfer(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Post the pay period's per-employee hours / PF / earnings into
    cumulative_data_table. Re-runnable: the period's rows for this pay
    scheme's employees are replaced, other schemes' rows are left alone."""
    body = parse_json_body(request)
    date_from = body.get("date_from")
    date_to = body.get("date_to")
    pay_scheme_id = body.get("pay_scheme_id")
    branch_id = body.get("branch_id")
    if not date_from or not date_to or not pay_scheme_id:
        raise HTTPException(status_code=400, detail="date_from, date_to and pay_scheme_id are required")
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="To date cannot be earlier than From date")

    payperiod_id = _resolve_period(db, date_from, date_to, int(pay_scheme_id),
                                   int(branch_id) if branch_id else None)
    params = {"date_from": date_from, "date_to": date_to, "payperiod_id": payperiod_id}
    try:
        replaced = db.execute(_CUMULATIVE_DELETE, params).rowcount
        inserted = db.execute(_CUMULATIVE_INSERT, params).rowcount
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    if inserted == 0:
        return {"message": "No employee with earnings in that pay period", "inserted": 0, "replaced": replaced}
    return {
        "message": f"{inserted} cumulative record(s) transferred"
                   + (f"; {replaced} previous record(s) replaced" if replaced else ""),
        "inserted": inserted, "replaced": replaced,
    }


# ─── EJM Cash/Outsider Daily Payment preparation (legacy oattprdprocess) ────
#
# Rebuilds tbl_daily_cash_outsider_payment_production for every day of the
# period: the daily cash rate each T-numbered outsider earned, decided by
# whether the production of what they ran that day met its standard.
# Designation ids are used as-is: EJM FACTORY (branch 29) carried legacy's ids
# over unchanged (verified 51/196, 52/197, 53/198, 199-202, 330, 11/12/604/605).
#
# Legacy sources → this tenant:
#   EMPMILL12.tbl_doffdata_all_calc            → daily_doff_tbl (spinning doffs)
#   EMPMILL12.tbl_winding_daily_spell_update   → jute_prod_winding_doff
#   weaving_quality_master.yarn_count          → jute_yarn_mst.jute_yarn_count
#       (per item_id — the yarn master this tenant keeps counts in)
#   mechine_master/frame_no                    → machine ids straight from
#       daily_ebmc_attendance (no frame_no hop needed)
# ponytail: legacy weighted standards by per-frame logged hours, which this
# tenant does not capture; the standard here is the yarn band's 8-hour figure
# pro-rated by spell hours (production-weighted when a frame ran two yarns in
# one spell). Upgrade path: capture frame hours on daily_doff_tbl.

# Who the EJM cash/outsider process covers: T-numbered outsiders on regular or
# overtime attendance only (legacy's 'C' rows are not paid here).
_CASH_WHO_SQL = ("SUBSTRING(TRIM(od.emp_code), 1, 1) = 'T' "
                 "AND da.attendance_type IN ('R', 'O')")

# Standard production per 8 hours by yarn-count band: <=12.99 / 13-15.99 / >=16
# (legacy $spg9/$spg13/$spg16 and $wnd9/$wnd13/$wnd16).
CASH_SPG_STD = (261, 363, 394)
CASH_WND_STD = (277, 330, 362)

# Which designations each arm covers, for which contractor, at what rates, all
# come from tbl_outsider_occupation_rate: prod_calc names the arm,
# rate_on_production is the met-standard rate, rate the missed one, contac_id
# the contractor (NULL = any contractor without its own row for that designation).
CASH_ARM_FLAT = (1, 2, 3)              # time-rated: always rate_on_production
CASH_ARM_HELPER = (4,)                 # production of the frames worked
CASH_ARM_SPINNER = (5,)                # own eb-stamped doffs
CASH_ARM_WINDER = (6,)                 # winding doff production
# ponytail: legacy's one hardcoded hand stays in code — no designation to key a row on.
CASH_FLAT_EMP_CODES = ("T2151",)
CASH_T2151_CONTRACTOR_ID = 10
CASH_T2151_RATE = 520

# Shared SELECT head: every arm feeds a `t` row set with eb_id, dept_id,
# desig_id, shift, whrs, prod, std, ratetype, rate_hi, rate_lo; amount resolves here.
# nhrs follows legacy: a C-shift total of exactly 7.5 hours is paid as 8.
_CASH_INSERT_HEAD = """
    INSERT INTO tbl_daily_cash_outsider_payment_production
        (branch_id, eb_id, dept_id, desig_id, production, is_active, rate_type,
         prod_date, prod_shift, targt_production, prod_amount, prod_rate, updated_by)
    SELECT :branch_id, t.eb_id, t.dept_id, t.desig_id, ROUND(t.prod, 3), 1, t.ratetype,
           :d, t.shift, ROUND(t.std, 3),
           ROUND((CASE WHEN t.ratetype = 1 THEN t.rate_hi ELSE t.rate_lo END) / 8
                 * (t.whrs + CASE WHEN t.shift = 'C' AND t.whrs = 7.5
                             THEN 0.5 ELSE 0 END), 2),
           CASE WHEN t.ratetype = 1 THEN t.rate_hi ELSE t.rate_lo END,
           :user_id
    FROM (
"""

# T-numbered outsiders, one row per attendance row. With `arm_param` (the bind
# name holding the arm's prod_calc codes) only workers whose worked designation
# has a rate row for that arm are kept — their own contractor's row first, else
# the contac_id NULL row — and its rates come out as rate_hi / rate_lo.
# `rate_cols` supplies those columns instead when there is no arm.
def _cash_att_sql(extra_where: str = "", arm_param: str | None = None,
                  rate_cols: str = "") -> str:
    rate_join = ""
    if arm_param:
        rate_cols = "r.rate_on_production AS rate_hi, r.rate AS rate_lo"
        rate_join = f"""
        JOIN tbl_outsider_occupation_rate r
          ON r.occupation_rate_id = (
             SELECT r2.occupation_rate_id FROM tbl_outsider_occupation_rate r2
             WHERE r2.is_active = 1
               AND r2.desig_id = da.worked_designation_id
               AND r2.prod_calc IN :{arm_param}
               AND (r2.contac_id = od.contractor_id OR r2.contac_id IS NULL)
             ORDER BY (r2.contac_id IS NULL), r2.occupation_rate_id DESC
             LIMIT 1)"""
    return f"""
        SELECT da.daily_atten_id, da.eb_id,
               da.worked_department_id AS dept_id,
               da.worked_designation_id AS desig_id,
               da.spell_id,
               LEFT(sp.spell_code, 1) AS shift,
               da.working_hours - COALESCE(da.idle_hours, 0) AS whrs
               {", " + rate_cols if rate_cols else ""}
        FROM daily_attendance da
        JOIN spell_mst sp ON sp.spell_id = da.spell_id
        JOIN hrms_ed_official_details od
          ON od.eb_id = da.eb_id AND od.active = 1
        {rate_join}
        WHERE da.attendance_date = :d AND {ATT_LIVE_SQL}
          AND {_CASH_WHO_SQL}
          AND da.branch_id = :branch_id
          {extra_where}
    """

# The yarn item's count comes from jute_yarn_mst (jute_yarn_count per item_id);
# MAX() collapses any duplicate master row so the join can never fan the
# production sums out. An item with no yarn master row -> NULL, which drops it
# from the standard, as legacy's missing yarn_count did.
def _cash_yarn_join(col: str) -> str:
    return (f"LEFT JOIN (SELECT item_id, MAX(jute_yarn_count) AS jute_yarn_count "
            f"FROM jute_yarn_mst GROUP BY item_id) jy ON jy.item_id = {col}")


# The yarn band's 8-hour standard, weighted by production where a unit ran two
# yarns in one spell. `{net}` is the production column.
def _cash_band_std_sql(net: str, alias: str = "jy") -> str:
    return f"""
        SUM({net} * CASE WHEN {alias}.jute_yarn_count <= 12.99 THEN :std_low
                         WHEN {alias}.jute_yarn_count <= 15.99 THEN :std_mid
                         WHEN {alias}.jute_yarn_count IS NOT NULL THEN :std_high
                         ELSE NULL END)
          / NULLIF(SUM({net}), 0)
    """

# 1) Spinning helpers: production of the frames each helper was assigned to
#    (daily_ebmc_attendance) vs those frames' spell standard.
_CASH_HELPERS_SQL = _CASH_INSERT_HEAD + f"""
        SELECT att.eb_id, att.dept_id, att.desig_id, att.shift,
               SUM(att.whrs) AS whrs,
               SUM(mpa.prod) AS prod, SUM(mpa.std) AS std,
               CASE WHEN SUM(mpa.prod) >= SUM(mpa.std) THEN 1 ELSE 0 END AS ratetype,
               MAX(att.rate_hi) AS rate_hi, MAX(att.rate_lo) AS rate_lo
        FROM ({_cash_att_sql(arm_param="helper_codes")}) att
        LEFT JOIN (
            SELECT dea.daily_atten_id, SUM(mp.prod) AS prod, SUM(mp.std) AS std
            FROM daily_ebmc_attendance dea
            JOIN daily_attendance da2 ON da2.daily_atten_id = dea.daily_atten_id
            JOIN (
                SELECT d.mc_id, d.spell AS spell_id,
                       SUM(d.net_weight) AS prod,
                       {_cash_band_std_sql("d.net_weight")}
                         * COALESCE(sp2.working_hours, 8) / 8 AS std
                FROM daily_doff_tbl d
                {_cash_yarn_join("d.item_id")}
                LEFT JOIN spell_mst sp2 ON sp2.spell_id = d.spell
                WHERE d.doff_date = :d AND (d.active = 1 OR d.active IS NULL)
                  AND d.branch_id = :branch_id
                GROUP BY d.mc_id, d.spell
            ) mp ON mp.mc_id = dea.mc_id AND mp.spell_id = da2.spell_id
            WHERE dea.is_active = 1
            GROUP BY dea.daily_atten_id
        ) mpa ON mpa.daily_atten_id = att.daily_atten_id
        GROUP BY att.eb_id, att.dept_id, att.desig_id, att.shift
    ) t
"""

# 2) Spinners: their own eb-stamped doffs vs the per-frame spell standard;
#    hours and dept/desig come from that spell's attendance row (LEFT, as legacy).
#    Doffs carry no designation, so the spinner rate is per contractor: the
#    prod_calc-5 rows of the worker's contractor, else the contac_id NULL ones.
_CASH_SPINNER_RATE = """
    (SELECT contac_id, MAX(rate_on_production) AS rate_hi, MAX(rate) AS rate_lo
     FROM tbl_outsider_occupation_rate
     WHERE is_active = 1 AND prod_calc IN :spinner_codes
     GROUP BY contac_id)
"""
_CASH_SPINNER_SQL = _CASH_INSERT_HEAD + f"""
        SELECT sd.eb_id,
               MIN(att.dept_id) AS dept_id, MIN(att.desig_id) AS desig_id,
               COALESCE(MIN(att.shift), 'G') AS shift,
               COALESCE(SUM(att.whrs), 0) AS whrs,
               SUM(sd.prod) AS prod, SUM(sd.std) AS std,
               CASE WHEN SUM(sd.prod) >= SUM(sd.std) THEN 1 ELSE 0 END AS ratetype,
               MAX(sd.rate_hi) AS rate_hi, MAX(sd.rate_lo) AS rate_lo
        FROM (
            SELECT d.eb_id, d.spell AS spell_id, d.mc_id,
                   SUM(d.net_weight) AS prod,
                   {_cash_band_std_sql("d.net_weight")}
                     * COALESCE(sp2.working_hours, 8) / 8 AS std,
                   MAX(COALESCE(sr.rate_hi, sn.rate_hi)) AS rate_hi,
                   MAX(COALESCE(sr.rate_lo, sn.rate_lo)) AS rate_lo
            FROM daily_doff_tbl d
            JOIN hrms_ed_official_details od
              ON od.eb_id = d.eb_id AND od.active = 1
             AND SUBSTRING(TRIM(od.emp_code), 1, 1) = 'T'
            LEFT JOIN {_CASH_SPINNER_RATE} sr ON sr.contac_id = od.contractor_id
            LEFT JOIN {_CASH_SPINNER_RATE} sn ON sn.contac_id IS NULL
            {_cash_yarn_join("d.item_id")}
            LEFT JOIN spell_mst sp2 ON sp2.spell_id = d.spell
            WHERE d.doff_date = :d AND (d.active = 1 OR d.active IS NULL)
              AND d.branch_id = :branch_id
              AND COALESCE(sr.rate_hi, sn.rate_hi) IS NOT NULL
            GROUP BY d.eb_id, d.spell, d.mc_id
        ) sd
        LEFT JOIN ({_cash_att_sql()}) att
               ON att.eb_id = sd.eb_id AND att.spell_id = sd.spell_id
        GROUP BY sd.eb_id
    ) t
"""

# 3) Winders: their winding doff production vs the yarn band's standard
#    pro-rated by attended hours (legacy tgprod = wnd_rate/8 × atthrs).
_CASH_WINDING_SQL = _CASH_INSERT_HEAD + f"""
        SELECT att.eb_id, att.dept_id, att.desig_id, att.shift,
               SUM(att.whrs) AS whrs,
               SUM(wp.prod) AS prod,
               SUM(wp.band8 * att.whrs / 8) AS std,
               CASE WHEN SUM(wp.prod) >= SUM(wp.band8 * att.whrs / 8)
                    THEN 1 ELSE 0 END AS ratetype,
               MAX(att.rate_hi) AS rate_hi, MAX(att.rate_lo) AS rate_lo
        FROM ({_cash_att_sql(arm_param="winder_codes")}) att
        LEFT JOIN (
            SELECT w.eb_id, w.spell_id,
                   SUM(w.production_qty) AS prod,
                   {_cash_band_std_sql("w.production_qty")} AS band8
            FROM jute_prod_winding_doff w
            LEFT JOIN (SELECT item_id, MAX(jute_yarn_count) AS jute_yarn_count
                       FROM jute_yarn_mst GROUP BY item_id) jy
                   ON jy.item_id = w.item_id
            WHERE w.tran_date = :d AND w.active = 1 AND w.branch_id = :branch_id
            GROUP BY w.eb_id, w.spell_id
        ) wp ON wp.eb_id = att.eb_id AND wp.spell_id = att.spell_id
        GROUP BY att.eb_id, att.dept_id, att.desig_id, att.shift
    ) t
"""

# 4) Flat-rated arms: attended hours at the flat rate, no production test.
_CASH_FLAT_SQL = _CASH_INSERT_HEAD + """
        SELECT att.eb_id, att.dept_id, att.desig_id, att.shift,
               SUM(att.whrs) AS whrs, 0 AS prod, 0 AS std, 1 AS ratetype,
               MAX(att.rate_hi) AS rate_hi, MAX(att.rate_lo) AS rate_lo
        FROM ({att_sql}) att
        GROUP BY att.eb_id, att.dept_id, att.desig_id, att.shift
    ) t
"""

# (name, sql, per-arm bind params); list binds are expanded at execute time.
_CASH_ARMS = [
    ("helpers", _CASH_HELPERS_SQL, {"helper_codes": list(CASH_ARM_HELPER)}),
    ("spinner", _CASH_SPINNER_SQL, {"spinner_codes": list(CASH_ARM_SPINNER)}),
    ("winding", _CASH_WINDING_SQL,
     {"winder_codes": list(CASH_ARM_WINDER), "std_low": CASH_WND_STD[0],
      "std_mid": CASH_WND_STD[1], "std_high": CASH_WND_STD[2]}),
    ("timerate",
     _CASH_FLAT_SQL.format(att_sql=_cash_att_sql(arm_param="flat_codes")),
     {"flat_codes": list(CASH_ARM_FLAT)}),
    ("t2151",
     _CASH_FLAT_SQL.format(att_sql=_cash_att_sql(
         "AND TRIM(od.emp_code) IN :t2151_codes AND od.contractor_id = :t2151_cont",
         rate_cols=":t2151_rate AS rate_hi, :t2151_rate AS rate_lo")),
     {"t2151_codes": list(CASH_FLAT_EMP_CODES),
      "t2151_cont": CASH_T2151_CONTRACTOR_ID, "t2151_rate": CASH_T2151_RATE}),
]

# ─── Payment build (legacy Loan_adv_model.php 318-454, holget==14) ──────────
#
# After the production prep, the day's actual payment rows land in
# daily_cash_outsider_payment (the table the Daily Cash Outsider Payment
# report prints). One INSERT replaces legacy's update chain (later legacy
# UPDATEs win, so they rank first here); rate priority:
#   1. day-exact approved rate (outsider_rate_approve.app_date = the day) —
#      legacy's final override, the manual correction screen
#   2. the production prep's prod_rate (eb + shift + designation match)
#   3. tbl_outsider_occupation_rate (legacy EMPMILL12.tbl_occupation_rate)
#      for the designation — contractor-wise: a
#      prod_calc NULL/0 row applies to every contractor, otherwise only when
#      contac_id is the worker's contractor and rate > 0
#   4. standing approved rate (latest app_date <= the day) — stands in for
#      legacy worker_master.cash_rate, a column this tenant does not have.
#      So a designation change changes the rate, as legacy: a Daily Rate Entry
#      rate beats the designation rate only on its exact date (1).
# Only workers with tbl_worker_sublocation_table.daily_others_pay_duration
# = 'T' are paid, as legacy. The masters came from EMPMILL12 via
# dbqueries/migrations/run_migrate_empmill12_cash_outsider_masters_to_sls.py.

_CASH_PAYMENT_DELETE = """
    DELETE FROM daily_cash_outsider_payment
    WHERE pay_date = :d AND branch_id = :branch_id
"""

# Every T-numbered outsider's attendance for the day, one payment row per
# eb / dept / desig / shift, as legacy grouped. The C-shift 7.5-paid-as-8 rule
# applies per attendance row before summing, matching the production prep.
_CASH_PAYMENT_INSERT = f"""
    INSERT INTO daily_cash_outsider_payment
        (eb_id, pay_date, shift, dept_id, desig_id, subloca_id, working_hours,
         rate, oth_rate, amount, branch_id, cont_id, is_active)
    SELECT h.eb_id, :d, h.shift, h.dept_id, h.desig_id, h.subloca_id, ROUND(h.whrs, 3),
           h.rate, 0, ROUND(h.rate / 8 * h.whrs, 0), :branch_id, h.cont_id, 1
    FROM (
        SELECT g.*,
               COALESCE(g.day_rate, g.prod_rate, g.occ_rate, g.std_rate, 0) AS rate
        FROM (
            SELECT att.eb_id, att.dept_id, att.desig_id, att.shift, att.cont_id,
                   att.subloca_id,
                   SUM(att.whrs) AS whrs,
                   (SELECT tor.rate FROM tbl_outsider_occupation_rate tor
                    WHERE tor.is_active = 1 AND tor.desig_id = att.desig_id
                      AND (IFNULL(tor.prod_calc, 0) = 0
                           OR (tor.rate > 0 AND tor.contac_id = att.cont_id))
                    ORDER BY tor.occupation_rate_id DESC
                    LIMIT 1) AS occ_rate,
                   (SELECT ra.rate_approve FROM outsider_rate_approve ra
                    WHERE ra.is_active = 1 AND ra.eb_id = att.eb_id
                      AND ra.app_date = :d
                      AND (ra.app_shift IS NULL OR ra.app_shift = ''
                           OR ra.app_shift = att.shift)
                    ORDER BY (ra.app_shift = att.shift) DESC,
                             ra.outsider_rate_app_id DESC
                    LIMIT 1) AS day_rate,
                   (SELECT pp.prod_rate
                    FROM tbl_daily_cash_outsider_payment_production pp
                    WHERE pp.is_active = 1 AND pp.eb_id = att.eb_id
                      AND pp.prod_date = :d AND pp.branch_id = :branch_id
                      AND pp.prod_shift = att.shift
                      AND pp.desig_id = att.desig_id
                    ORDER BY pp.cash_outsider_prod_id DESC
                    LIMIT 1) AS prod_rate,
                   (SELECT ra.rate_approve FROM outsider_rate_approve ra
                    WHERE ra.is_active = 1 AND ra.eb_id = att.eb_id
                      AND ra.app_date <= :d
                      AND (ra.app_shift IS NULL OR ra.app_shift = ''
                           OR ra.app_shift = att.shift)
                    ORDER BY ra.app_date DESC,
                             (ra.app_shift = att.shift) DESC,
                             ra.outsider_rate_app_id DESC
                    LIMIT 1) AS std_rate
            FROM (
                SELECT da.eb_id,
                       da.worked_department_id AS dept_id,
                       da.worked_designation_id AS desig_id,
                       LEFT(sp.spell_code, 1) AS shift,
                       od.contractor_id AS cont_id,
                       ws.subloca_id,
                       da.working_hours - COALESCE(da.idle_hours, 0)
                         + CASE WHEN LEFT(sp.spell_code, 1) = 'C'
                                 AND da.working_hours
                                     - COALESCE(da.idle_hours, 0) = 7.5
                                THEN 0.5 ELSE 0 END AS whrs
                FROM daily_attendance da
                JOIN spell_mst sp ON sp.spell_id = da.spell_id
                JOIN hrms_ed_official_details od
                  ON od.eb_id = da.eb_id AND od.active = 1
                JOIN tbl_worker_sublocation_table ws
                  ON ws.eb_id = da.eb_id AND ws.daily_others_pay_duration = 'T'
                WHERE da.attendance_date = :d AND {ATT_LIVE_SQL}
                  AND {_CASH_WHO_SQL}
                  AND da.branch_id = :branch_id
            ) att
            GROUP BY att.eb_id, att.dept_id, att.desig_id, att.shift,
                     att.cont_id, att.subloca_id
        ) g
    ) h
"""

# Travel allowance in oth_rate, per 8 hours. Legacy hardcoded contractor 10
# at 30; here the ta_rate comes from the tbl_outsider_occupation_rate row for
# the payment's designation AND contractor (exact match on both, latest row
# wins). Rows with no contractor (contac_id NULL) give no TA.
_CASH_PAYMENT_TA = """
    UPDATE daily_cash_outsider_payment p
    JOIN tbl_outsider_occupation_rate r
      ON r.occupation_rate_id = (
         SELECT r2.occupation_rate_id FROM tbl_outsider_occupation_rate r2
         WHERE r2.is_active = 1
           AND r2.desig_id = p.desig_id
           AND r2.contac_id = p.cont_id
           AND r2.ta_rate > 0
         ORDER BY r2.occupation_rate_id DESC
         LIMIT 1)
    SET p.oth_rate = ROUND(p.working_hours / 8 * r.ta_rate, 0),
        -- amount includes TA; the expression is repeated because multi-table
        -- UPDATE does not guarantee left-to-right assignment. Safe on rerun:
        -- the day's rows are rebuilt (amount = rate part) before this runs.
        p.amount = p.amount + ROUND(p.working_hours / 8 * r.ta_rate, 0)
    WHERE p.pay_date = :d AND p.branch_id = :branch_id
"""


@router.post("/ejm_cash_outsider_process")
def ejm_cash_outsider_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """EJM Cash/Outsider Daily Payment preparation.

    Per day: first the production prep (legacy oattprdprocess) rebuilds
    tbl_daily_cash_outsider_payment_production. Arms (legacy's), with their
    designations, contractors and met/missed rates read from
    tbl_outsider_occupation_rate by prod_calc:
      1/2/3 flat      attended hours at rate_on_production (+ T2151, 520)
      4     helpers   production of the frames worked
      5     spinner   own eb-stamped spinning doffs (rate per contractor)
      6     winding   winding doff production
    Then the payment build (legacy Loan_adv_model.php holget==14) rebuilds
    daily_cash_outsider_payment from the day's attendance of 'T'-duration
    workers (tbl_worker_sublocation_table), priced by day-exact approved rate
    > production rate > tbl_outsider_occupation_rate (designation, contractor-wise)
    > standing approved rate (outsider_rate_approve), plus TA
    (tbl_outsider_occupation_rate.ta_rate by desig_id + contac_id) in oth_rate.

    Everything is T-numbered outsiders only. A day is DELETEd and rebuilt, so
    rerunning is safe. Note: the generic Cash Hands process also writes
    daily_cash_outsider_payment — for EJM (branch 29) THIS action is the
    authoritative builder, as only it applies the production rates.
    Body: { branch_id, date_from, date_to? (default from) }
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to") or date_from
        if not branch_id or not date_from:
            raise HTTPException(status_code=400, detail="branch_id and date_from are required")
        # EJM FACTORY only; the T-number filter lives in _CASH_WHO_SQL.
        if int(branch_id) != EJM_CASH_BRANCH_ID:
            raise HTTPException(
                status_code=400,
                detail=f"EJM Cash/Outsider Daily Payment runs only for branch {EJM_CASH_BRANCH_ID} (EJM FACTORY)")
        try:
            d_from = date.fromisoformat(str(date_from)[:10])
            d_to = date.fromisoformat(str(date_to)[:10])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date")
        if d_to < d_from:
            raise HTTPException(status_code=400, detail="To date is before From date")
        if (d_to - d_from).days >= 31:
            raise HTTPException(status_code=400, detail="At most 31 days per run")

        user_id = token_data.get("user_id", 0)
        base = {
            "branch_id": int(branch_id), "user_id": user_id,
            "std_low": CASH_SPG_STD[0], "std_mid": CASH_SPG_STD[1],
            "std_high": CASH_SPG_STD[2],
        }
        arms = _CASH_ARMS

        counts: dict[str, int] = {name: 0 for name, *_ in arms}
        payment_rows = 0
        day = d_from
        while day <= d_to:
            db.execute(text("""
                DELETE FROM tbl_daily_cash_outsider_payment_production
                WHERE prod_date = :d AND branch_id = :branch_id
            """), {"d": day, "branch_id": int(branch_id)})
            for name, sql, extra in arms:
                stmt = text(sql)
                expanding = [k for k, v in extra.items() if isinstance(v, list)]
                if expanding:
                    stmt = stmt.bindparams(
                        *(bindparam(k, expanding=True) for k in expanding))
                counts[name] += db.execute(stmt, {
                    **base, **extra, "d": day,
                }).rowcount or 0
            # Payment build reads the production rows just written above.
            pay_params = {"d": day, "branch_id": int(branch_id)}
            db.execute(text(_CASH_PAYMENT_DELETE), pay_params)
            payment_rows += db.execute(
                text(_CASH_PAYMENT_INSERT), pay_params).rowcount or 0
            db.execute(text(_CASH_PAYMENT_TA), pay_params)
            day += timedelta(days=1)
        db.commit()

        total = sum(counts.values())
        detail = ", ".join(f"{k} {v}" for k, v in counts.items())
        return {"data": {
            "message": (f"Cash/Outsider daily payment built — {total} "
                        f"production rows ({detail}), "
                        f"{payment_rows} payment rows"),
            "rows": total, "payment_rows": payment_rows, "arms": counts,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── EJM Spreader efficiency (legacy WG150426/SPREDEFF.PRG) ─────────────────
#
# Legacy sources → this tenant:
#   sprdtran + sprdbrek (operators, rolls, breakdown hours per spell)
#                                         → vw_spreader_prod_aattendance
#   ALLTREFF code 1 EFF_WT (roll weight)  → spreader_prod_entry.wt_per_roll (in the view)
#   wagerepo + effitran                   → ejm_fne_wages_eff_data
# Every operator on a machine is credited with that machine's full rolls for
# the spell (legacy did the same for both spell operators); hours count once
# per attendance row even when the hand minded two spreaders.
# Target kg per 8 h (legacy hardcoded TGWT=5060) → eff code 19 'Spreader' in
# ejm_fne_eff_code_type for the fortnight only (no fortnight row → the run stops).
SPREADER_TARGET_CODE = 19
SPREADER_DETAILS = "SPREADER"
SPREADER_DETCODE = "01"
# Legacy's fixed deptcode/occucode pairs that get the all-shift efficiency.
SPREADER_OVERALL_OCCU = (("02", "04"), ("02", "22"), ("02", "23"), ("12", "13"))

# Attendance is mostly whole-shift ('A') while production and stoppages are
# entered per half spell (A1/A2) or shift: a shift row takes every entry of
# its letter, a half-spell row only its own. {code} is the entry's spell code.
_SPREADER_SPELL_MATCH = ("({code} = sp.spell_code OR "
                         "(CHAR_LENGTH(sp.spell_code) = 1 AND LEFT({code}, 1) = sp.spell_code))")

# Source: the vw_spreader_prod_aattendance view (user choice 2026-10-02) — one row
# per approved attendance (date, spell, eb) x spreader machine with whrs
# (working - idle), prod (kg = rolls x wt_per_roll for that machine and spell)
# and stophrs (the machine's jute_prod_stoppage_hours in that spell). Here, per
# attendance: kg = Σ prod over its machines, hours = whrs - stoppage; machines
# with no production are left out (legacy dropped roll = 0).
# ponytail: a hand minding two spreaders loses only the smaller stoppage (still
# working the other machine) — per-machine hours if that proves too generous.
# One row per (eb, shift): the kg credited from vw_spreader_prod_aattendance and
# the hand's regular (R) attendance hours in that shift over the whole fortnight
# (user 2026-10-06: EB 18593 shift A = 49,010 kg / 80 hrs x 8 vs 5,060 → 96.86%).
_SPREADER_OPERATOR_SQL = text(f"""
    SELECT p.eb_id, p.shift, p.wt, COALESCE(h.hrs, 0) AS hrs,
           LEFT(CONCAT_WS(' ', NULLIF(TRIM(pd.first_name), ''), NULLIF(TRIM(pd.middle_name), ''),
                          NULLIF(TRIM(pd.last_name), '')), 25) AS name
    FROM (
        SELECT v.eb_id, LEFT(sp.spell_code, 1) AS shift, SUM(v.prod) AS wt
        FROM vw_spreader_prod_aattendance v
        JOIN machine_mst m ON m.machine_id = v.mc_id
        JOIN dept_mst d ON d.dept_id = m.dept_id AND d.branch_id = :branch_id
        JOIN spell_mst sp ON sp.spell_id = v.spell_id
        WHERE v.attendance_date BETWEEN :date_from AND :date_to AND v.prod > 0
        GROUP BY v.eb_id, LEFT(sp.spell_code, 1)
    ) p
    LEFT JOIN (
        SELECT da.eb_id, {SHIFT_SQL} AS shift, SUM(da.working_hours) AS hrs
        FROM daily_attendance da
        {SPELL_JOIN}
        WHERE {ATT_LIVE_SQL} AND da.branch_id = :branch_id AND da.attendance_type = 'R'
          AND da.attendance_date BETWEEN :date_from AND :date_to
        GROUP BY da.eb_id, {SHIFT_SQL}
    ) h ON h.eb_id = p.eb_id AND h.shift = p.shift
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = p.eb_id
""")


def _replace_eff_rows(db: Session, base: dict, rows: list[dict], scope_sql: str = "") -> int:
    """Replace the fortnight's rows of one process (details + detcode, narrowed
    by scope_sql when two processes share a tag) in ejm_fne_wages_eff_data;
    returns how many old rows were removed."""
    replaced = db.execute(text(f"""
        DELETE FROM ejm_fne_wages_eff_data
        WHERE period_id = :period_id AND details = :details AND detcode = :detcode {scope_sql}
    """), base).rowcount
    if rows:
        db.execute(text("""
            INSERT INTO ejm_fne_wages_eff_data
                (period_id, eb_id, shift_id, detcode, details, deptcode, occucode,
                 name, quantity, eff, effcode, tareff, acteff, quality_id, mcno, mc_id, prod_amount,
                 target_qty)
            VALUES (:period_id, :eb_id, :shift_id, :detcode, :details, :deptcode, :occucode,
                    :name, :quantity, :eff, :effcode, :tareff, :acteff, :quality_id, :mcno, :mc_id,
                    :prod_amount, :target_qty)
        """), [{"quality_id": None, "mcno": None, "mc_id": None, "prod_amount": None, **r,
                "acteff": _act_eff(r), "target_qty": _target_qty(r)} for r in rows])
    return replaced


# ejm_fne_wages_eff_data.target_qty is DECIMAL(14,3) on sls (widened 2026-10-04,
# migration 2026-10-04_ejm_fne_wages_eff_data_target_qty_widen.sql); a larger
# figure is left NULL rather than failing the insert (STRICT_TRANS_TABLES).
TARGET_QTY_MAX = 99999999999.999


def _act_eff(row: dict):
    """acteff = eff / tareff x 100 (user 2026-10-06); NULL with no eff or
    target, or when it does not fit DECIMAL(6,2). Occupation 55 is always 100
    (as in Apply Efficiency, EFF_APPLY_NO_EFF_OCCU)."""
    if str(row.get("occucode") or "").strip() == "55":
        return 100.0
    eff, tareff = row.get("eff"), row.get("tareff")
    if eff is None or not tareff or float(tareff) <= 0:
        return None
    value = round(float(eff) / float(tareff) * 100, 2)
    return value if value <= 9999.99 else None


def _target_qty(row: dict):
    """The 100%-efficiency quantity in the row's own unit, so that
    eff = quantity / target_qty x 100 holds on every row and an EB's overall
    efficiency over any set of rows is Σ quantity / Σ target_qty x 100 (user,
    2026-10-04). Every process's eff is quantity over its 100% figure (spreader
    target kg for the hours, drawing capacity meters, spinning / doff 100%
    production, beaming / press / winding / finishing targets allocated to the
    row), so the figure is quantity x 100 / eff. NULL when there is no quantity
    or efficiency, or the value does not fit the column."""
    qty, eff = row.get("quantity"), row.get("eff")
    if qty is None or not eff or float(eff) <= 0:
        return None
    value = round(float(qty) * 100 / float(eff), 3)
    return value if value <= TARGET_QTY_MAX else None


def _spreader_eff(wt: float, hours: float, target_kg: float) -> float:
    """Legacy: eff = kg produced / (TGWT / 8 * hours) * 100, TGWT = target kg per 8 h."""
    return round(wt / (target_kg / 8 * hours) * 100, 2) if hours > 0 and target_kg > 0 else 0.0


def _spreader_summary(operator_rows: list[dict], target_kg: float) -> tuple[list[dict], dict[str, dict], dict]:
    """Per (eb, shift) totals, per-shift totals and the all-shift total.

    Each total is {wt, hours, eff}. Input rows are one per attendance
    (date, spell, eb) with that attendance's hours and the kg it is credited.
    """
    per_eb: dict[tuple, dict] = {}
    for r in operator_rows:
        t = per_eb.setdefault((r["eb_id"], r["shift"]),
                              {"eb_id": r["eb_id"], "shift": r["shift"], "name": r["name"],
                               "wt": 0.0, "hours": 0.0})
        t["wt"] += float(r["wt"] or 0)
        t["hours"] += float(r["hrs"] or 0)
    shifts: dict[str, dict] = {}
    overall = {"wt": 0.0, "hours": 0.0}
    for t in per_eb.values():
        t["eff"] = _spreader_eff(t["wt"], t["hours"], target_kg)
        for acc in (shifts.setdefault(t["shift"], {"wt": 0.0, "hours": 0.0}), overall):
            acc["wt"] += t["wt"]
            acc["hours"] += t["hours"]
    for acc in (*shifts.values(), overall):
        acc["eff"] = _spreader_eff(acc["wt"], acc["hours"], target_kg)
    return list(per_eb.values()), shifts, overall


@router.post("/ejm_spreader_eff_process")
def ejm_spreader_eff_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Spreader individual / shift / overall efficiency for one fortnight into
    ejm_fne_wages_eff_data (details 'SPREADER', detcode '01'):
      per hand per shift   deptcode 02 occucode 06, eb_id + shift_id (legacy wagerepo)
      per shift            effcode 22, shift_id, no eb_id           (legacy effitran)
      all shifts           effcode 1 for 02/04, 02/22, 02/23, 12/13 (legacy wagerepo)
    tareff is 100 throughout. The fortnight's SPREADER rows are replaced, so
    rerunning is safe. Body: { branch_id, date_from, date_to } — must be exactly
    a forthnight_period_mst fortnight (period_id = its forthnight_id).
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        if not branch_id or not date_from or not date_to:
            raise HTTPException(status_code=400, detail="branch_id, date_from and date_to are required")
        # EJM's dept/occu codes; the output table has no branch column.
        if int(branch_id) != EJM_CASH_BRANCH_ID:
            raise HTTPException(
                status_code=400,
                detail=f"Spreader efficiency runs only for branch {EJM_CASH_BRANCH_ID} (EJM FACTORY)")
        period_id = _fortnight_id(db, date_from, date_to)
        shift_ids = {r.shift_name: r.shift_id for r in db.execute(text("""
            SELECT shift_id, shift_name FROM shift_mst WHERE branch_id = :branch_id AND status = 1
        """), {"branch_id": int(branch_id)}).fetchall()}

        params = {"branch_id": int(branch_id), "date_from": date_from, "date_to": date_to}
        target_kg = _eff_targets(db, period_id, [SPREADER_TARGET_CODE]).get(SPREADER_TARGET_CODE, 0)
        if target_kg <= 0:
            raise HTTPException(status_code=400, detail=(
                f"No spreader target (eff code {SPREADER_TARGET_CODE}) for {date_from} to {date_to} — "
                "enter it on the fortnight eff code page"))
        operators = [dict(r._mapping) for r in db.execute(_SPREADER_OPERATOR_SQL, params).fetchall()]
        per_eb, shifts, overall = _spreader_summary(operators, target_kg)

        base = {"period_id": period_id, "details": SPREADER_DETAILS, "detcode": SPREADER_DETCODE,
                "tareff": 100, "eb_id": None, "shift_id": None, "name": None,
                "quantity": None, "effcode": None}
        out = [{**base, "eb_id": t["eb_id"], "shift_id": shift_ids.get(t["shift"]), "name": t["name"],
                "deptcode": "02", "occucode": "06", "quantity": round(t["wt"], 2), "eff": t["eff"]}
               for t in per_eb if t["hours"] > 0]
        out += [{**base, "shift_id": shift_ids.get(s), "deptcode": "02", "occucode": None,
                 "quantity": round(acc["wt"], 2), "eff": acc["eff"], "effcode": 22}
                for s, acc in sorted(shifts.items()) if acc["wt"] > 0]
        if overall["wt"] > 0:
            out += [{**base, "deptcode": d, "occucode": o, "quantity": round(overall["wt"], 2),
                     "eff": overall["eff"], "effcode": 1}
                    for d, o in SPREADER_OVERALL_OCCU]

        replaced = _replace_eff_rows(db, base, out)
        db.commit()

        shift_text = ", ".join(f"{s} {acc['eff']:.2f}%" for s, acc in sorted(shifts.items()))
        hands = sum(1 for t in per_eb if t["hours"] > 0)
        return {"data": {
            "message": (f"Spreader efficiency: {hands} hand-shift row(s); "
                        f"{shift_text or 'no production'}; overall {overall['eff']:.2f}%"
                        + (f"; {replaced} previous row(s) replaced" if replaced else "")),
            "rows": len(out), "overall_eff": overall["eff"],
            "shifts": {s: acc["eff"] for s, acc in shifts.items()},
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── EJM Drawing efficiency (legacy WG150426/DRAWEFF1.PRG, "newsys") ─────────
#
# Legacy sources → this tenant:
#   drawtran / drg30495 (meters per mc group, spell) → jute_prod_drawing_entry
#       diff_meter; capacity cmeter = const_meter / 8 * (wrk_hours - stoppage)
#       (drawmast.cmeter / 8 * dailhour spell hours; stoppage from
#       jute_prod_stoppage_hours, as for spreader)
#   legacy mcno group ('21', '22', ...)              → LEFT(machine_mst.mech_code, 2)
#   ALLTREFF codes 8-13 (target %)                    → ejm_fne_eff_code_type for the
#       fortnight, else the ejm_eff_code_type default
#   daildrgo FEED_1/FEED_2/RECV_1/RECV_2              → vw_drawing_prod_attendance
#   OCCUMAST (effcode → dept/occu)                    → link_wages_occu_eff
#       + dept_mst.dept_code + designation_mst.occu_code
#   wagerepo + effitran                               → ejm_fne_wages_eff_data
# Each operator is credited with the full meters of every machine they minded
# that shift (legacy credited feeder and receiver with the group's meters).
DRAWING_DETAILS = "DRAWING"
DRAWING_DETCODE = "03"
# pcchk: mc group → ALLTREFF target-% code.
DRAWING_GROUP_TARGET_CODE = {"21": 8, "28": 9, "22": 10, "23": 11, "25": 12, "29": 13}
# Fine side (occucode 28); every other group is written as occucode 29.
# (Legacy set 25 to 'F' then overwrote it with 'C'.)
DRAWING_FINE_GROUPS = {"21", "22", "23"}
# newsys group rows: effcode → the mc groups it is measured on, and whether an
# effitran-style per-code row (no dept/occu) is written too.
DRAWING_GROUP_CODES = ((43, {"25"}, True), (44, {"25"}, True), (5, {"25", "29"}, False))

# Capacity meters of one entry d: const_meter / 8 * actual hours, where actual
# hours = wrk_hours - the machine's jute_prod_stoppage_hours in that spell
# (a shift entry 'A' takes stoppages logged as A, A1 or A2).
_DRAWING_CMETER = """d.const_meter / 8 * GREATEST(d.wrk_hours - COALESCE((
        SELECT SUM(s.stoppage_hours) FROM jute_prod_stoppage_hours s
          JOIN spell_mst ss ON ss.spell_id = s.spell_id
         WHERE s.active = 1 AND s.machine_id = d.machine_id AND s.tran_date = d.tran_date
           AND (ss.spell_code = d.spell
                OR (CHAR_LENGTH(d.spell) = 1 AND LEFT(ss.spell_code, 1) = d.spell))), 0), 0)"""

# The legacy mc group is the first two digits of machine_mst.mech_code (21001 =
# 1st drawing 1, 22xxx 2nd, 23xxx inter, 25/29xxx 3rd, 28xxx); the attr table's
# mc_group is '21' on every machine, so it is not used.
_DRAWING_GROUP_OF = "LEFT(m.mech_code, 2)"

# Operators: the vw_drawing_prod_attendance view (user choice 2026-10-02) — one
# row per approved attendance (date, spell, eb) x machine with whrs (working -
# idle) and that machine's drawing entry for the spell (diff_meter, const_meter).
# Capacity = const_meter / 8 * (whrs - the machine's stoppage in that spell).
_DRAWING_OPERATOR_SQL = text(f"""
    SELECT v.eb_id, LEFT(sp.spell_code, 1) AS shift, {_DRAWING_GROUP_OF} AS mc_group,
           LEFT(CONCAT_WS(' ', NULLIF(TRIM(pd.first_name), ''), NULLIF(TRIM(pd.middle_name), ''),
                          NULLIF(TRIM(pd.last_name), '')), 25) AS name,
           SUM(v.diff_meter) AS diffm,
           SUM(v.const_meter / 8 * GREATEST(v.whrs - COALESCE((
               SELECT SUM(s.stoppage_hours) FROM jute_prod_stoppage_hours s
                 JOIN spell_mst ss ON ss.spell_id = s.spell_id
                WHERE s.active = 1 AND s.machine_id = v.mc_id AND s.tran_date = v.attendance_date
                  AND {_SPREADER_SPELL_MATCH.format(code="ss.spell_code")}), 0), 0)) AS cmeter
    FROM vw_drawing_prod_attendance v
    JOIN jute_prod_drawing_machine_attr a ON a.machine_id = v.mc_id AND a.active = 1
    JOIN machine_mst m ON m.machine_id = v.mc_id
    JOIN dept_mst dp ON dp.dept_id = m.dept_id AND dp.branch_id = :branch_id
    JOIN spell_mst sp ON sp.spell_id = v.spell_id
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = v.eb_id
    WHERE v.attendance_date BETWEEN :date_from AND :date_to
      AND v.diff_meter > 0
    GROUP BY v.eb_id, LEFT(sp.spell_code, 1), {_DRAWING_GROUP_OF},
             pd.first_name, pd.middle_name, pd.last_name
""")

_DRAWING_GROUP_SQL = text(f"""
    SELECT {_DRAWING_GROUP_OF} AS mc_group,
           SUM(d.diff_meter) AS diffm, SUM({_DRAWING_CMETER}) AS cmeter
    FROM jute_prod_drawing_entry d
    JOIN jute_prod_drawing_machine_attr a ON a.machine_id = d.machine_id AND a.active = 1
    JOIN machine_mst m ON m.machine_id = d.machine_id
    WHERE d.branch_id = :branch_id AND d.active = 1 AND d.diff_meter > 0
      AND d.tran_date BETWEEN :date_from AND :date_to
    GROUP BY {_DRAWING_GROUP_OF}
""")


def _occu_by_code(db: Session, codes) -> dict[int, list[tuple]]:
    """Legacy OCCUMAST: effcode → the (deptcode, occucode) pairs paid on it."""
    codes = list(codes)
    if not codes:
        return {}
    out: dict[int, list[tuple]] = {}
    for r in db.execute(text("""
        SELECT CAST(l.eff_code AS UNSIGNED) AS code, d.dept_code, g.occu_code
        FROM link_wages_occu_eff l
        JOIN dept_mst d ON d.dept_id = l.dept_id
        JOIN designation_mst g ON g.designation_id = l.desig_id
        WHERE l.eff_code IN :codes
    """).bindparams(bindparam("codes", expanding=True)), {"codes": codes}):
        out.setdefault(int(r.code), []).append((r.dept_code, r.occu_code))
    return out


def _eff_targets(db: Session, period_id: int, codes) -> dict[int, float]:
    """Target per eff code (legacy ALLTREFF): the fortnight's value in
    ejm_fne_eff_code_type only (user rule, 2026-10-04 — eff-code targets come
    from that table; quality-wise targets from ejm_wages_fne_qual_target). A code
    with no row for the fortnight (or rejected / cancelled) has no target."""
    return {int(r.eff_code): float(r.eff_pc or 0) for r in db.execute(text("""
        SELECT eff_code, eff_pc FROM ejm_fne_eff_code_type
        WHERE forthnig_id = :period_id AND eff_code IN :codes
          AND (status_id IS NULL OR status_id NOT IN (4, 6))
    """).bindparams(bindparam("codes", expanding=True)), {"period_id": period_id, "codes": list(codes)})}


def _drawing_eff(diffm: float, tarm: float, cmeter: float) -> tuple[float, float]:
    """Legacy: eff = diffm / cmeter * 100, tareff = tarm / cmeter * 100."""
    if cmeter <= 0:
        return 0.0, 0.0
    return round(diffm / cmeter * 100, 2), round(tarm / cmeter * 100, 2)


def _drawing_summary(operator_rows: list[dict], group_rows: list[dict],
                     targets: dict[int, float]) -> tuple[list[dict], dict[int, tuple]]:
    """Per (eb, shift, side) {diffm, cmeter, tarm, eff, tareff}, and per newsys
    effcode (eff, tareff, diffm). tarm = cmeter * the group's target % / 100."""
    def tarm(group, cmeter):
        return cmeter * targets.get(DRAWING_GROUP_TARGET_CODE.get(str(group).strip(), -1), 0) / 100

    per_eb: dict[tuple, dict] = {}
    for r in operator_rows:
        side = "F" if str(r["mc_group"]).strip() in DRAWING_FINE_GROUPS else "C"
        t = per_eb.setdefault((r["eb_id"], r["shift"], side),
                              {"eb_id": r["eb_id"], "shift": r["shift"], "side": side,
                               "name": r["name"], "diffm": 0.0, "cmeter": 0.0, "tarm": 0.0})
        cm = float(r["cmeter"] or 0)
        t["diffm"] += float(r["diffm"] or 0)
        t["cmeter"] += cm
        t["tarm"] += tarm(r["mc_group"], cm)
    for t in per_eb.values():
        t["eff"], t["tareff"] = _drawing_eff(t["diffm"], t["tarm"], t["cmeter"])

    groups = {str(g["mc_group"]).strip(): g for g in group_rows}
    code_eff = {}
    for code, members, _ in DRAWING_GROUP_CODES:
        rows = [groups[m] for m in members if m in groups]
        df = sum(float(g["diffm"] or 0) for g in rows)
        cm = sum(float(g["cmeter"] or 0) for g in rows)
        tm = sum(tarm(g["mc_group"], float(g["cmeter"] or 0)) for g in rows)
        if df > 0 and cm > 0:
            code_eff[code] = (*_drawing_eff(df, tm, cm), df)
    return list(per_eb.values()), code_eff


@router.post("/ejm_drawing_eff_process")
def ejm_drawing_eff_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Drawing operator / group efficiency for one fortnight into
    ejm_fne_wages_eff_data (details 'DRAWING', detcode '03'):
      per hand per shift per side  deptcode 03, occucode 28 (fine groups) or 29,
                                   eb_id + shift_id, eff + tareff (legacy wagerepo)
      per newsys effcode 43/44/5   one row per link_wages_occu_eff occupation of
                                   that code (legacy wgrepl), plus for 43/44 a
                                   code-only row (legacy effitran, wgrepl1)
    The fortnight's DRAWING rows are replaced, so rerunning is safe.
    Body: { branch_id, date_from, date_to } — exactly a forthnight_period_mst fortnight.
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        if not branch_id or not date_from or not date_to:
            raise HTTPException(status_code=400, detail="branch_id, date_from and date_to are required")
        if int(branch_id) != EJM_CASH_BRANCH_ID:
            raise HTTPException(
                status_code=400,
                detail=f"Drawing efficiency runs only for branch {EJM_CASH_BRANCH_ID} (EJM FACTORY)")
        period_id = _fortnight_id(db, date_from, date_to)
        shift_ids = {r.shift_name: r.shift_id for r in db.execute(text("""
            SELECT shift_id, shift_name FROM shift_mst WHERE branch_id = :branch_id AND status = 1
        """), {"branch_id": int(branch_id)}).fetchall()}

        params = {"branch_id": int(branch_id), "date_from": date_from, "date_to": date_to}
        operators = [dict(r._mapping) for r in db.execute(_DRAWING_OPERATOR_SQL, params).fetchall()]
        group_rows = [dict(r._mapping) for r in db.execute(_DRAWING_GROUP_SQL, params).fetchall()]
        per_eb, code_eff = _drawing_summary(
            operators, group_rows, _eff_targets(db, period_id, DRAWING_GROUP_TARGET_CODE.values()))

        base = {"period_id": period_id, "details": DRAWING_DETAILS, "detcode": DRAWING_DETCODE,
                "eb_id": None, "shift_id": None, "name": None, "effcode": None}
        out = [{**base, "eb_id": t["eb_id"], "shift_id": shift_ids.get(t["shift"]), "name": t["name"],
                "deptcode": "03", "occucode": "28" if t["side"] == "F" else "29",
                "quantity": round(t["diffm"], 2), "eff": t["eff"], "tareff": t["tareff"]}
               for t in per_eb if t["diffm"] > 0 and t["cmeter"] > 0]
        occus = _occu_by_code(db, code_eff)
        for code, _, effitran in DRAWING_GROUP_CODES:
            if code not in code_eff:
                continue
            eff, tareff, df = code_eff[code]
            row = {**base, "effcode": code, "eff": eff, "tareff": tareff, "quantity": round(df, 2)}
            out += [{**row, "deptcode": d, "occucode": o} for d, o in occus.get(code, [])]
            if effitran:
                out.append({**row, "deptcode": None, "occucode": None})

        replaced = _replace_eff_rows(db, base, out)
        db.commit()

        hands = sum(1 for r in out if r["eb_id"])
        codes_text = ", ".join(f"code {c} {v[0]:.2f}%" for c, v in code_eff.items())
        return {"data": {
            "message": (f"Drawing efficiency: {hands} hand-shift row(s)"
                        + (f"; {codes_text}" if codes_text else "; no mc group 25/29 production")
                        + (f"; {replaced} previous row(s) replaced" if replaced else "")),
            "rows": len(out), "codes": {c: v[0] for c, v in code_eff.items()},
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── EJM Spinner efficiency (legacy WG150426/DOFFEFF1.PRG) ───────────────────
#
# Legacy sources → this tenant:
#   daildoff (ebno, weight per doff)    → daily_doff_tbl (eb_id, net_weight)
#   daildoff std_wt / spg10495 tarprd*  → jute_prod_spinning_daily.p100prod of the
#       frame / spell / quality, scaled by (hours - jute_prod_stoppage_hours) / hours
#   spg10495 prd_a/b/c (group actual)   → jute_prod_spinning_daily.act_prod_doff
#   frame '34' coarse / '33' fine       → machine_mst.mech_code starting '34' =
#                                         coarse side, anything else fine
#   q_code warp / weft code lists       → yarn group name ending WP / WT
#   ALLTREFF 2/3/4/5 (2-leg / boxter)   → ejm_fne_eff_code_type / ejm_eff_code_type
#   wagerepo + effitran                 → ejm_fne_wages_eff_data
# eff = production / 100% production, tareff = target production / 100% production.
# ponytail: sls has no boxter flag on frames or qualities, so every frame prices
# at the 2-leg target (codes 2 fine / 4 coarse); add the flag and pick 3 / 5 for it.
SPINNING_DETAILS = "SPINNING"
SPINNING_DETCODE = "04"
SPINNING_TARGET_CODE = {"F": 2, "C": 4}
# Boxter code of each side: the side's target when its 2-leg target is 0 / missing
# (fortnight 18 has 2 / 4 = 0 and only 3 = 60 / 5 = 78 entered).
SPINNING_BOXTER_CODE = {2: 3, 4: 5}


def _spinning_targets(db: Session, period_id: int) -> dict[int, float]:
    """Fine / coarse target % (keys 2 / 4) from ejm_fne_eff_code_type: the 2-leg
    code, else the side's boxter code."""
    t = _eff_targets(db, period_id, [*SPINNING_BOXTER_CODE, *SPINNING_BOXTER_CODE.values()])
    return {c: t.get(c) or t.get(b, 0) for c, b in SPINNING_BOXTER_CODE.items()}
# Group codes (legacy wgrepl = one row per OCCUMAST occupation, wgrepl1 = one
# code-only row): (effcode, per shift?, which units, occupation rows?, code row?)
SPINNING_GROUP_CODES = (
    (2, False, lambda u: True, True, False),
    (3, False, lambda u: u["side"] == "C", True, False),
    (4, False, lambda u: u["side"] == "F", True, False),
    (24, True, lambda u: True, True, True),
    (7, True, lambda u: u["kind"] == "WP", True, False),
    (8, True, lambda u: u["kind"] == "WT", True, False),
    (27, True, lambda u: u["side"] == "F", False, True),
    (28, True, lambda u: u["side"] == "C", False, True),
)

# One row per frame / date / spell-shift / quality: actual (doff) production and
# the 100% production for the hours the frame could run.
_SPINNING_UNIT_SQL = text("""
    SELECT s.tran_date, s.machine_id, s.item_id, LEFT(sp.spell_code, 1) AS shift,
           ig.item_grp_name AS grp, m.mech_code,
           SUM(s.act_prod_doff) AS act,
           SUM(s.p100prod * CASE WHEN s.minutes > 0
                THEN GREATEST(s.minutes / 60 - COALESCE((
                    SELECT SUM(st.stoppage_hours) FROM jute_prod_stoppage_hours st
                      JOIN spell_mst ss ON ss.spell_id = st.spell_id
                     WHERE st.active = 1 AND st.machine_id = s.machine_id
                       AND st.tran_date = s.tran_date
                       AND (ss.spell_code = sp.spell_code
                            OR (CHAR_LENGTH(sp.spell_code) = 1
                                AND LEFT(ss.spell_code, 1) = sp.spell_code))), 0), 0)
                     / (s.minutes / 60)
                ELSE 1 END) AS p100
    FROM jute_prod_spinning_daily s
    JOIN spell_mst sp ON sp.spell_id = s.spell_id
    LEFT JOIN machine_mst m ON m.machine_id = s.machine_id
    LEFT JOIN jute_yarn_mst y ON y.item_id = s.item_id
    LEFT JOIN item_grp_mst ig ON ig.item_grp_id = y.item_grp_id
    WHERE s.branch_id = :branch_id AND s.active = 1
      AND s.tran_date BETWEEN :date_from AND :date_to
    GROUP BY s.tran_date, s.machine_id, s.item_id, LEFT(sp.spell_code, 1), ig.item_grp_name,
             m.mech_code
""")

_SPINNING_DOFF_SQL = text("""
    SELECT d.eb_id, d.doff_date, d.mc_id, d.item_id, LEFT(sp.spell_code, 1) AS shift,
           SUM(d.net_weight) AS wt,
           LEFT(CONCAT_WS(' ', NULLIF(TRIM(pd.first_name), ''), NULLIF(TRIM(pd.middle_name), ''),
                          NULLIF(TRIM(pd.last_name), '')), 25) AS name
    FROM daily_doff_tbl d
    JOIN spell_mst sp ON sp.spell_id = d.spell
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = d.eb_id
    WHERE d.branch_id = :branch_id AND d.active = 1 AND d.eb_id IS NOT NULL
      AND d.doff_date BETWEEN :date_from AND :date_to
    GROUP BY d.eb_id, d.doff_date, d.mc_id, d.item_id, LEFT(sp.spell_code, 1),
             pd.first_name, pd.middle_name, pd.last_name
""")


def _spinning_unit_class(grp, mech_code) -> tuple[str, str | None]:
    """(side, warp/weft kind): frame number (machine_mst.mech_code) starting '33'
    = fine side, '34' = coarse side (user 2026-10-06; branch 29 has only 33 / 34
    frames, anything else is read as fine); warp / weft from the yarn group's
    WP / WT suffix."""
    g = (grp or "").strip().upper()
    side = "C" if str(mech_code or "").strip().startswith("34") else "F"
    return side, (g[-2:] if g[-2:] in ("WP", "WT") else None)


def _spinning_summary(units: list[dict], doffs: list[dict], targets: dict[int, float]):
    """Per (eb, shift, side) {wt, twt, trwt, eff, tareff} and per group code a
    list of (shift or None, eff, tareff, act). A frame's 100% production is
    shared between its spinners by doff weight (one per frame-spell today)."""
    def pct(side):
        return targets.get(SPINNING_TARGET_CODE[side], 0)

    def ratio(a, b):
        return round(a / b * 100, 2) if b > 0 else 0.0

    unit_by_key = {}
    for u in units:
        u["side"], u["kind"] = _spinning_unit_class(u["grp"], u.get("mech_code"))
        u["act"], u["p100"] = float(u["act"] or 0), float(u["p100"] or 0)
        unit_by_key[(u["tran_date"], u["machine_id"], u["item_id"], u["shift"])] = u

    frame_wt: dict[tuple, float] = {}
    for d in doffs:
        k = (d["doff_date"], d["mc_id"], d["item_id"], d["shift"])
        frame_wt[k] = frame_wt.get(k, 0) + float(d["wt"] or 0)

    per_eb: dict[tuple, dict] = {}
    for d in doffs:
        k = (d["doff_date"], d["mc_id"], d["item_id"], d["shift"])
        u = unit_by_key.get(k)
        wt = float(d["wt"] or 0)
        if not u or u["p100"] <= 0 or wt <= 0:
            continue  # no standard for that frame-spell: legacy had none either
        twt = u["p100"] * wt / frame_wt[k]
        t = per_eb.setdefault((d["eb_id"], d["shift"], u["side"]),
                              {"eb_id": d["eb_id"], "shift": d["shift"], "side": u["side"],
                               "name": d["name"], "wt": 0.0, "twt": 0.0, "trwt": 0.0})
        t["wt"] += wt
        t["twt"] += twt
        t["trwt"] += twt * pct(u["side"]) / 100
    for t in per_eb.values():
        t["eff"], t["tareff"] = ratio(t["wt"], t["twt"]), ratio(t["trwt"], t["twt"])

    running = [u for u in units if u["act"] > 0 and u["p100"] > 0]
    codes: dict[int, list[tuple]] = {}
    for code, per_shift, pick, _, _ in SPINNING_GROUP_CODES:
        for shift in (sorted({u["shift"] for u in running}) if per_shift else [None]):
            sel = [u for u in running if pick(u) and (shift is None or u["shift"] == shift)]
            act = sum(u["act"] for u in sel)
            p100 = sum(u["p100"] for u in sel)
            if act > 0:
                tar = sum(u["p100"] * pct(u["side"]) / 100 for u in sel)
                codes.setdefault(code, []).append((shift, ratio(act, p100), ratio(tar, p100), act))
    return list(per_eb.values()), codes


@router.post("/ejm_spinning_eff_process")
def ejm_spinning_eff_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Spinner individual / group efficiency for one fortnight into
    ejm_fne_wages_eff_data (details 'SPINNING', detcode '04'):
      per hand per shift per side  deptcode 04, occucode 01 (fine) or 06 (coarse)
      group codes 2/3/4 (all shifts), 24/7/8 (per shift): one row per
          link_wages_occu_eff occupation of the code; 24/27/28 (per shift) also
          a code-only row (legacy effitran)
    The fortnight's SPINNING rows are replaced, so rerunning is safe.
    Body: { branch_id, date_from, date_to } — exactly a forthnight_period_mst fortnight.
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        if not branch_id or not date_from or not date_to:
            raise HTTPException(status_code=400, detail="branch_id, date_from and date_to are required")
        if int(branch_id) != EJM_CASH_BRANCH_ID:
            raise HTTPException(
                status_code=400,
                detail=f"Spinning efficiency runs only for branch {EJM_CASH_BRANCH_ID} (EJM FACTORY)")
        period_id = _fortnight_id(db, date_from, date_to)
        shift_ids = {r.shift_name: r.shift_id for r in db.execute(text("""
            SELECT shift_id, shift_name FROM shift_mst WHERE branch_id = :branch_id AND status = 1
        """), {"branch_id": int(branch_id)}).fetchall()}

        params = {"branch_id": int(branch_id), "date_from": date_from, "date_to": date_to}
        units = [dict(r._mapping) for r in db.execute(_SPINNING_UNIT_SQL, params).fetchall()]
        doffs = [dict(r._mapping) for r in db.execute(_SPINNING_DOFF_SQL, params).fetchall()]
        per_eb, codes = _spinning_summary(
            units, doffs, _spinning_targets(db, period_id))

        base = {"period_id": period_id, "details": SPINNING_DETAILS, "detcode": SPINNING_DETCODE,
                "eb_id": None, "shift_id": None, "name": None, "effcode": None}
        out = [{**base, "eb_id": t["eb_id"], "shift_id": shift_ids.get(t["shift"]), "name": t["name"],
                "deptcode": "04", "occucode": "01" if t["side"] == "F" else "06",
                "quantity": round(t["wt"], 2), "eff": t["eff"], "tareff": t["tareff"]}
               for t in per_eb]
        occus = _occu_by_code(db, codes)
        for code, _, _, occu_rows, code_row in SPINNING_GROUP_CODES:
            for shift, eff, tareff, act in codes.get(code, []):
                row = {**base, "effcode": code, "shift_id": shift_ids.get(shift) if shift else None,
                       "eff": eff, "tareff": tareff, "quantity": round(act, 2)}
                if occu_rows:
                    out += [{**row, "deptcode": d, "occucode": o} for d, o in occus.get(code, [])]
                if code_row:
                    out.append({**row, "deptcode": None, "occucode": None})

        replaced = _replace_eff_rows(db, base, out, f"AND NOT ({_DOFF_MAZDOOR_SCOPE})")
        db.commit()

        overall = codes.get(2, [(None, 0.0, 0.0, 0)])[0][1]
        shift_text = ", ".join(f"{s} {e:.2f}%" for s, e, *_ in codes.get(24, []))
        return {"data": {
            "message": (f"Spinning efficiency: {len(per_eb)} hand-shift row(s); "
                        f"{shift_text or 'no production'}; overall {overall:.2f}%"
                        + (f"; {replaced} previous row(s) replaced" if replaced else "")),
            "rows": len(out), "overall_eff": overall,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── EJM Doff mazdoor / sliver feeder / piecer efficiency (legacy DOFMAZRP.PRG)
#
# Legacy sources → this tenant:
#   doffmozd / sliverfd / piecerfl (eb + the frames they minded, per spell)
#       → daily_ebmc_attendance of hands whose worked designation is dept 04
#         occupation 08/09 (bobbin coolie = doff mazdoor), 19/20 (sliver feeder)
#         or 02/07 (piecer); the designation's occu_code is the occucode written
#   daildoff weight / std_wt of those frames → daily_doff_tbl net_weight /
#       jute_prod_spinning_daily p100prod (stoppage-scaled), as for spinners
#   LERNSPNR (frames of learner spinners pay nothing) → the frame's spinner
#       worked that day on a designation named '%LEARNER%'
# Written with the spinner rows under 'SPINNING' / '04' as legacy did; each
# process replaces only its own rows (these are the eb rows on the codes below).
DOFF_MAZDOOR_OCCUS = ("08", "09", "19", "20", "02", "07")
_DOFF_MAZDOOR_SCOPE = ("eb_id IS NOT NULL AND occucode IN ("
                       + ", ".join(f"'{o}'" for o in DOFF_MAZDOOR_OCCUS) + ")")

_DOFF_MAZDOOR_HANDS_SQL = text(f"""
    SELECT DISTINCT da.daily_atten_id, da.eb_id, da.attendance_date, x.mc_id,
           {SHIFT_SQL} AS shift, g.occu_code,
           LEFT(CONCAT_WS(' ', NULLIF(TRIM(pd.first_name), ''), NULLIF(TRIM(pd.middle_name), ''),
                          NULLIF(TRIM(pd.last_name), '')), 25) AS name
    FROM daily_ebmc_attendance x
    JOIN daily_attendance da ON da.daily_atten_id = x.daily_atten_id
    {SPELL_JOIN}
    JOIN designation_mst g ON g.designation_id = da.worked_designation_id
    JOIN dept_mst d ON d.dept_id = g.dept_id AND d.dept_code = '04'
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = da.eb_id
    WHERE x.is_active = 1
      AND da.branch_id = :branch_id
      AND da.attendance_date BETWEEN :date_from AND :date_to
      AND {ATT_LIVE_SQL}
      AND g.occu_code IN :occus
""").bindparams(bindparam("occus", expanding=True))

# A frame's doffs per quality, leaving out frames spun by a learner that day.
_DOFF_MAZDOOR_FRAME_SQL = text("""
    SELECT d.doff_date, d.mc_id, d.item_id, LEFT(sp.spell_code, 1) AS shift,
           SUM(d.net_weight) AS wt
    FROM daily_doff_tbl d
    JOIN spell_mst sp ON sp.spell_id = d.spell
    WHERE d.branch_id = :branch_id AND d.active = 1
      AND d.doff_date BETWEEN :date_from AND :date_to
      AND NOT EXISTS (
          SELECT 1 FROM daily_attendance la
          JOIN designation_mst lg ON lg.designation_id = la.worked_designation_id
          WHERE la.eb_id = d.eb_id AND la.attendance_date = d.doff_date
            AND la.branch_id = d.branch_id AND la.is_active = 1
            AND UPPER(lg.desig) LIKE '%LEARNER%')
    GROUP BY d.doff_date, d.mc_id, d.item_id, LEFT(sp.spell_code, 1)
""")


def _doff_mazdoor_summary(hands: list[dict], units: list[dict], frame_wt: list[dict],
                          targets: dict[int, float]) -> list[dict]:
    """Per (eb, shift, occu_code): the doff weight / 100% production / target
    production of every frame-quality the hand minded that shift."""
    unit = {}
    for u in units:
        side, _ = _spinning_unit_class(u["grp"], u.get("mech_code"))
        unit[(u["tran_date"], u["machine_id"], u["item_id"], u["shift"])] = (float(u["p100"] or 0), side)
    by_frame: dict[tuple, list[tuple]] = {}
    for f in frame_wt:
        u = unit.get((f["doff_date"], f["mc_id"], f["item_id"], f["shift"]))
        wt = float(f["wt"] or 0)
        if u and u[0] > 0 and wt > 0:
            p100, side = u
            by_frame.setdefault((f["doff_date"], f["mc_id"], f["shift"]), []).append(
                (wt, p100, p100 * targets.get(SPINNING_TARGET_CODE[side], 0) / 100))

    per_eb: dict[tuple, dict] = {}
    for h in hands:
        for wt, p100, tar in by_frame.get((h["attendance_date"], h["mc_id"], h["shift"]), []):
            t = per_eb.setdefault((h["eb_id"], h["shift"], h["occu_code"]),
                                  {"eb_id": h["eb_id"], "shift": h["shift"], "occu_code": h["occu_code"],
                                   "name": h["name"], "wt": 0.0, "twt": 0.0, "trwt": 0.0})
            t["wt"] += wt
            t["twt"] += p100
            t["trwt"] += tar
    rows = [t for t in per_eb.values() if t["wt"] > 0]
    for t in rows:
        t["eff"] = round(t["wt"] / t["twt"] * 100, 2)
        t["tareff"] = round(t["trwt"] / t["twt"] * 100, 2)
    return rows


@router.post("/ejm_doff_mazdoor_eff_process")
def ejm_doff_mazdoor_eff_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Doff mazdoor (bobbin coolie), sliver feeder and piecer efficiency for
    one fortnight into ejm_fne_wages_eff_data: one row per hand per shift per
    occupation, deptcode 04, details 'SPINNING', detcode '04'. Only these rows
    are replaced on a rerun — the spinner process owns the rest of the tag.
    Body: { branch_id, date_from, date_to } — exactly a forthnight_period_mst fortnight.
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        if not branch_id or not date_from or not date_to:
            raise HTTPException(status_code=400, detail="branch_id, date_from and date_to are required")
        if int(branch_id) != EJM_CASH_BRANCH_ID:
            raise HTTPException(
                status_code=400,
                detail=f"Doff mazdoor efficiency runs only for branch {EJM_CASH_BRANCH_ID} (EJM FACTORY)")
        period_id = _fortnight_id(db, date_from, date_to)
        shift_ids = {r.shift_name: r.shift_id for r in db.execute(text("""
            SELECT shift_id, shift_name FROM shift_mst WHERE branch_id = :branch_id AND status = 1
        """), {"branch_id": int(branch_id)}).fetchall()}

        params = {"branch_id": int(branch_id), "date_from": date_from, "date_to": date_to}
        hands = [dict(r._mapping) for r in db.execute(
            _DOFF_MAZDOOR_HANDS_SQL, {**params, "occus": list(DOFF_MAZDOOR_OCCUS)}).fetchall()]
        units = [dict(r._mapping) for r in db.execute(_SPINNING_UNIT_SQL, params).fetchall()]
        frame_wt = [dict(r._mapping) for r in db.execute(_DOFF_MAZDOOR_FRAME_SQL, params).fetchall()]
        rows = _doff_mazdoor_summary(
            hands, units, frame_wt, _spinning_targets(db, period_id))

        base = {"period_id": period_id, "details": SPINNING_DETAILS, "detcode": SPINNING_DETCODE}
        out = [{**base, "eb_id": t["eb_id"], "shift_id": shift_ids.get(t["shift"]), "name": t["name"],
                "deptcode": "04", "occucode": t["occu_code"], "quantity": round(t["wt"], 2),
                "eff": t["eff"], "tareff": t["tareff"], "effcode": None}
               for t in rows]
        replaced = _replace_eff_rows(db, base, out, f"AND {_DOFF_MAZDOOR_SCOPE}")
        db.commit()

        counts = {o: sum(1 for t in rows if t["occu_code"] == o) for o in DOFF_MAZDOOR_OCCUS}
        return {"data": {
            "message": (f"Doff mazdoor efficiency: {len(rows)} hand-shift row(s) "
                        f"(doff mazdoor {counts['08'] + counts['09']}, "
                        f"sliver feeder {counts['19'] + counts['20']}, piecer {counts['02'] + counts['07']})"
                        + (f"; {replaced} previous row(s) replaced" if replaced else "")),
            "rows": len(out),
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── EJM Loom (weaver) efficiency (legacy WG150426/LOOMEFF1.PRG) ─────────────
#
# Legacy sources → this tenant:
#   loomback (per loom/day: ebno, qty, eff, hrs, q_code per half spell) and
#   wvg10495 (quality-wise yards vs 100% yards)  → the weaving day slice
#       (juteProduction fetch_weaving_day_rows — the same rows DPR reports use):
#       production_yds, std_prod_yds (100% production over working hours that
#       already exclude jute_prod_stoppage_hours), efficiency, target_eff
#   wvwglink.tar_eff (quality target)           → ejm_wages_fne_qual_target of the
#       linked wages quality (dept WEAVING, the fortnight), else eff code 6 (q_code
#       '1…' hessian) / 7 ('2…' sacking) / 14 ('3…') in ejm_fne_eff_code_type
#   loom prefix 41 hessian / 42 sacking         → tbl_loom_type.loom_side of the loom
#   wagerepo + effitran                         → ejm_fne_wages_eff_data
#   dailmist / mistryms (loom mistry → loom group) → the day's attendance of
#       dept 13 fitters on occupation 03 / 05 (legacy's two mistry occupations);
#       each gets the shift efficiency of their loom type (user rule, 2026-09-30):
#       HESSIAN… designations → hessian looms, the rest → sacking looms
LOOM_DETAILS = "WEAVING"
LOOM_WEAVING_DEPT_CODE = "08"  # dept_mst WEAVING(EJM) — its quality links / rates
LOOM_MISTRY_OCCUS = ("03", "05")

_LOOM_MISTRY_SQL = text(f"""
    SELECT DISTINCT da.eb_id, {SHIFT_SQL} AS shift, g.occu_code,
           CASE WHEN UPPER(g.desig) LIKE 'HESSIAN%' THEN 'H' ELSE 'S' END AS side,
           LEFT(CONCAT_WS(' ', NULLIF(TRIM(pd.first_name), ''), NULLIF(TRIM(pd.middle_name), ''),
                          NULLIF(TRIM(pd.last_name), '')), 25) AS name
    FROM daily_attendance da
    {SPELL_JOIN}
    JOIN designation_mst g ON g.designation_id = da.worked_designation_id
    JOIN dept_mst d ON d.dept_id = g.dept_id AND d.dept_code = '13'
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = da.eb_id
    WHERE da.branch_id = :branch_id
      AND da.attendance_date BETWEEN :date_from AND :date_to
      AND {ATT_LIVE_SQL}
      AND g.occu_code IN :occus
""").bindparams(bindparam("occus", expanding=True))
LOOM_DETCODE = "08"
LOOM_QUALITY_TARGET_CODE = {"1": 6, "2": 7, "3": 14}
LOOM_OCCU = {"H": "01", "S": "04"}
# Group codes: (effcode, per shift?, which rows, occupation rows?, code row?, fixed target code)
LOOM_GROUP_CODES = (
    (21, False, lambda r: r["qgrp"] != "1", True, False, None),
    (13, False, lambda r: True, True, False, None),
    (12, True, lambda r: True, True, True, None),
    (30, True, lambda r: r["qgrp"] == "1", False, True, 6),
    (31, True, lambda r: r["qgrp"] != "1", False, True, None),
)


# Weaving sardars / helpers (legacy BLSARDAR.PRG: dailsard names who ran each
# line per spell) → daily_ebmc_attendance on a 'Lines' pseudo-machine
# ("HESS SARDAR LINE NO 3", "S4 LOOM HELPER LINE NO 5"): mech_shr_code is the
# line number (line_no is wrong on some), the name gives role and loom side.
LINE_CREW_OCCU = {("SARDAR", "H"): "02", ("SARDAR", "S"): "05",
                  ("HELPER", "H"): "03", ("HELPER", "S"): "06"}

_LINE_CREW_SQL = text(f"""
    SELECT DISTINCT da.eb_id, da.attendance_date, {SHIFT_SQL} AS shift,
           m.machine_name, TRIM(m.mech_shr_code) AS line,
           LEFT(CONCAT_WS(' ', NULLIF(TRIM(pd.first_name), ''), NULLIF(TRIM(pd.middle_name), ''),
                          NULLIF(TRIM(pd.last_name), '')), 25) AS name
    FROM daily_ebmc_attendance x
    JOIN daily_attendance da ON da.daily_atten_id = x.daily_atten_id
    {SPELL_JOIN}
    JOIN machine_mst m ON m.machine_id = x.mc_id
    JOIN machine_type_mst t ON t.machine_type_id = m.machine_type_id AND t.machine_type_name = 'Lines'
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = da.eb_id
    WHERE x.is_active = 1
      AND da.branch_id = :branch_id
      AND da.attendance_date BETWEEN :date_from AND :date_to
      AND {ATT_LIVE_SQL}
""")


def _line_crew_summary(loom_rows: list[dict], crew: list[dict]) -> list[dict]:
    """Per (eb, role, side, line, shift): hours-weighted eff / target of the
    line's looms on the days and shift the hand ran it (legacy tmp22), and the
    production (yds) of those looms as the row's quantity."""
    lines: dict[tuple, list[float]] = {}
    for r in loom_rows:
        hrs, eff = float(r["working_hours"] or 0), float(r["efficiency"] or 0)
        if r["prod"] > 0 and hrs > 0 and eff > 0:
            g = lines.setdefault((r["tran_date"], r["shift"], r["side"], r["line"]), [0.0, 0.0, 0.0, 0.0])
            g[0] += hrs
            g[1] += eff * hrs
            g[2] += r["tg"] * hrs
            g[3] += r["prod"]
    acc: dict[tuple, list[float]] = {}
    for c in crew:
        mc = (c["machine_name"] or "").upper()
        role = "SARDAR" if "SARDAR" in mc else "HELPER" if "HELPER" in mc else None
        side = "H" if mc.startswith("HESS") else "S"
        g = lines.get((c["attendance_date"], c["shift"], side, str(c["line"] or "").strip()))
        if not role or not g:
            continue
        a = acc.setdefault((c["eb_id"], role, side, str(c["line"]).strip(), c["shift"], c["name"]),
                           [0.0, 0.0, 0.0, 0.0])
        for i in range(4):
            a[i] += g[i]
    return [
        {"eb_id": eb, "shift": shift, "name": name, "occu_code": LINE_CREW_OCCU[(role, side)],
         "mcno": f"{role[0]}000{line.zfill(2)}"[:6],  # legacy 'S000'/'H000' + line
         "eff": round(a[1] / a[0], 2), "tareff": round(a[2] / a[0], 2), "quantity": round(a[3], 2)}
        for (eb, role, side, line, shift, name), a in acc.items()
    ]


def _loom_summary(rows: list[dict], targets: dict[int, float]):
    """Weaver rows per (eb, shift, side, quality) with the weaver's
    hours-weighted eff / target for that shift-side (legacy tmp5), the
    group codes as (shift or None, eff, tareff, production yds), and the
    loom-mistry figure per (side, shift) as (eff, tareff).

    Each input row: eb_id, shift, side, qgrp, quality, production_yds,
    std_prod_yds, efficiency, target_eff, working_hours."""
    for r in rows:
        tg = float(r["target_eff"] or 0) or targets.get(LOOM_QUALITY_TARGET_CODE.get(r["qgrp"], -1), 0)
        r["tg"] = tg
        r["prod"], r["std"] = float(r["production_yds"] or 0), float(r["std_prod_yds"] or 0)

    weaver: dict[tuple, dict] = {}
    for r in rows:
        hrs, eff = float(r["working_hours"] or 0), float(r["efficiency"] or 0)
        if not r["eb_id"] or r["prod"] <= 0 or hrs <= 0 or eff <= 0:
            continue
        w = weaver.setdefault((r["eb_id"], r["shift"], r["side"]),
                              {"hrs": 0.0, "ef_hrs": 0.0, "tg_hrs": 0.0, "q": {}})
        w["hrs"] += hrs
        w["ef_hrs"] += eff * hrs
        w["tg_hrs"] += r["tg"] * hrs
        # prd65: production at the quality's target efficiency (prod / eff * tg)
        w["q"][r["quality"]] = w["q"].get(r["quality"], 0) + r["prod"] / eff * r["tg"]
    per_eb = [
        {"eb_id": eb, "shift": shift, "side": side, "quality": q,
         "eff": round(w["ef_hrs"] / w["hrs"], 2), "tareff": round(w["tg_hrs"] / w["hrs"], 2),
         "quantity": round(prd65 / 16, 2)}  # legacy repl quantity with pd/16
        for (eb, shift, side), w in weaver.items() for q, prd65 in w["q"].items()
    ]

    # Loom mistry: the hours-weighted eff / target of every loom of a type
    # (side) in a shift — legacy blmistry's Σ eff*hrs / Σ hrs over the group.
    group: dict[tuple, list[float]] = {}
    for r in rows:
        hrs, eff = float(r["working_hours"] or 0), float(r["efficiency"] or 0)
        if r["prod"] > 0 and hrs > 0 and eff > 0:
            g = group.setdefault((r["side"], r["shift"]), [0.0, 0.0, 0.0, 0.0])
            g[0] += hrs
            g[1] += eff * hrs
            g[2] += r["tg"] * hrs
            g[3] += r["prod"]
    # (eff, tareff, production yds of that loom type and shift)
    side_shift = {k: (round(g[1] / g[0], 2), round(g[2] / g[0], 2), round(g[3], 2))
                  for k, g in group.items()}

    running = [r for r in rows if r["std"] > 0]
    codes: dict[int, list[tuple]] = {}
    for code, per_shift, pick, _, _, fixed in LOOM_GROUP_CODES:
        for shift in (sorted({r["shift"] for r in running}) if per_shift else [None]):
            sel = [r for r in running if pick(r) and (shift is None or r["shift"] == shift)]
            prod = sum(r["prod"] for r in sel)
            std = sum(r["std"] for r in sel)
            if prod > 0:
                tareff = (targets.get(fixed, 0) if fixed
                          else round(sum(r["std"] * r["tg"] / 100 for r in sel) / std * 100, 2))
                codes.setdefault(code, []).append((shift, round(prod / std * 100, 2), tareff, prod))
    return per_eb, codes, side_shift


@router.post("/ejm_loom_eff_process")
def ejm_loom_eff_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Weaver individual and weaving group efficiency for one fortnight into
    ejm_fne_wages_eff_data (details 'WEAVING', detcode '08'):
      per weaver per shift per side per quality  deptcode 08, occucode 01
          (hessian looms) / 04 (sacking looms), quality_id = weaving_quality_id
          (legacy qcode), quantity = production at
          target eff / 16; eff / tareff = the weaver's hours-weighted figures
      per sardar / helper per line per shift (legacy BLSARDAR.PRG)  deptcode 08,
          occucode sardar 02 / 05, helper 03 / 06 (hessian / sacking line),
          mcno = line, eff / tareff = hours-weighted figures of the line's looms
      per loom mistry per shift  deptcode 13, the fitter's occucode (03 / 05),
          eff / tareff = hours-weighted figures of their loom type that shift
      group codes 21 / 13 (all shifts), 12 (per shift): one row per
          link_wages_occu_eff occupation; 12 / 30 (hessian) / 31 (sacking) per
          shift also a code-only row (legacy effitran)
    The fortnight's WEAVING rows are replaced, so rerunning is safe.
    Body: { branch_id, date_from, date_to } — exactly a forthnight_period_mst fortnight.
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        if not branch_id or not date_from or not date_to:
            raise HTTPException(status_code=400, detail="branch_id, date_from and date_to are required")
        if int(branch_id) != EJM_CASH_BRANCH_ID:
            raise HTTPException(
                status_code=400,
                detail=f"Loom efficiency runs only for branch {EJM_CASH_BRANCH_ID} (EJM FACTORY)")
        period_id = _fortnight_id(db, date_from, date_to)
        branch = db.execute(text("SELECT co_id FROM branch_mst WHERE branch_id = :b"),
                            {"b": int(branch_id)}).fetchone()
        shift_ids = {r.shift_name: r.shift_id for r in db.execute(text("""
            SELECT shift_id, shift_name FROM shift_mst WHERE branch_id = :branch_id AND status = 1
        """), {"branch_id": int(branch_id)}).fetchall()}
        sacking_looms = {r.mechine_id for r in db.execute(text("""
            SELECT o.mechine_id FROM tbl_loom_master_other_details o
            JOIN tbl_loom_type t ON t.loom_type_id = o.loom_type_id
            WHERE t.loom_side = 'Sacking'
        """)).fetchall()}

        rows = []
        day, d_to = date.fromisoformat(str(date_from)[:10]), date.fromisoformat(str(date_to)[:10])
        while day <= d_to:
            for r in fetch_weaving_day_rows(db, branch.co_id, day, int(branch_id)):
                rows.append({
                    "eb_id": r["eb_id"], "shift": (r["spell_code"] or "")[:1],
                    "tran_date": day, "line": str(r["line_no"] or "").strip(),
                    "side": "S" if r["machine_id"] in sacking_looms else "H",
                    "qgrp": (r["weaving_quality_code"] or "")[:1],
                    "quality": r["weaving_quality_id"],
                    "production_yds": r["production_yds"], "std_prod_yds": r["std_prod_yds"],
                    "efficiency": r["efficiency"], "target_eff": r["target_eff"],
                    "working_hours": r["working_hours"],
                })
            day += timedelta(days=1)

        # Weaving quality → wages quality via link_prodquality_wagequality_mst (dept
        # WEAVING), with its rate (prod_amount below) and its fortnight target from
        # ejm_wages_fne_qual_target (user rule 2026-10-04: quality-wise targets come
        # from that table). A quality with no fortnight target falls back to its eff
        # code (6 / 7 / 14) in ejm_fne_eff_code_type inside _loom_summary.
        link_params = {"branch_id": int(branch_id), "dept_code": LOOM_WEAVING_DEPT_CODE,
                       "period_id": period_id}
        wages_of = {r.prod_quality_id: (r.wages_quality_id, float(r.quality_rate or 0))
                    for r in db.execute(text("""
                        SELECT l.prod_quality_id, MIN(l.wages_quality_id) AS wages_quality_id,
                               MIN(r.quality_rate) AS quality_rate
                        FROM link_prodquality_wagequality_mst l
                        JOIN dept_mst d ON d.dept_id = l.dept_id AND d.dept_code = :dept_code
                        LEFT JOIN ejm_wages_quality_rate r ON r.wages_quality_rate_id = l.wages_quality_id
                        WHERE l.active = 1 AND l.branch_id = :branch_id
                        GROUP BY l.prod_quality_id
                    """), link_params)}
        qual_target = {r.quality_id: float(r.eff_pc or 0) for r in db.execute(text("""
            SELECT t.quality_id, MAX(t.eff_pc) AS eff_pc
            FROM ejm_wages_fne_qual_target t
            JOIN dept_mst d ON d.dept_id = t.dept_id AND d.dept_code = :dept_code
                           AND d.branch_id = :branch_id
            WHERE t.period_id = :period_id AND (t.status_id IS NULL OR t.status_id NOT IN (4, 6))
            GROUP BY t.quality_id
        """), link_params)}
        for r in rows:
            r["target_eff"] = qual_target.get(wages_of.get(r["quality"], (None, 0))[0], 0)

        per_eb, codes, side_shift = _loom_summary(
            rows, _eff_targets(db, period_id, set(LOOM_QUALITY_TARGET_CODE.values())))

        mistries = [dict(r._mapping) for r in db.execute(_LOOM_MISTRY_SQL, {
            "branch_id": int(branch_id), "date_from": date_from, "date_to": date_to,
            "occus": list(LOOM_MISTRY_OCCUS),
        }).fetchall()]

        names = {}
        if per_eb:
            names = {r.eb_id: r.name for r in db.execute(text("""
                SELECT eb_id, LEFT(CONCAT_WS(' ', NULLIF(TRIM(first_name), ''), NULLIF(TRIM(middle_name), ''),
                                             NULLIF(TRIM(last_name), '')), 25) AS name
                FROM hrms_ed_personal_details WHERE eb_id IN :ebs
            """).bindparams(bindparam("ebs", expanding=True)),
                {"ebs": list({t["eb_id"] for t in per_eb})}).fetchall()}

        base = {"period_id": period_id, "details": LOOM_DETAILS, "detcode": LOOM_DETCODE,
                "eb_id": None, "shift_id": None, "name": None, "effcode": None}
        # Weaver rows carry the WAGES quality (legacy wvwglink wg_q_code) and are paid
        # prod_amount = quantity x its ejm_wages_quality_rate rate (wages_of above).
        # Two weaving qualities on one wages quality merge into one row.
        weaver: dict[tuple, dict] = {}
        unlinked = set()
        for t in per_eb:
            wages_qc, rate = wages_of.get(t["quality"], (None, 0.0))
            if wages_qc is None:
                unlinked.add(t["quality"])
            w = weaver.setdefault((t["eb_id"], t["shift"], t["side"], wages_qc), {
                **base, "eb_id": t["eb_id"], "shift_id": shift_ids.get(t["shift"]),
                "name": names.get(t["eb_id"]), "deptcode": "08", "occucode": LOOM_OCCU[t["side"]],
                "quality_id": wages_qc, "quantity": 0.0, "prod_amount": None if wages_qc is None else 0.0,
                "eff": t["eff"], "tareff": t["tareff"]})
            w["quantity"] = round(w["quantity"] + t["quantity"], 2)
            if wages_qc is not None:
                w["prod_amount"] = round(w["prod_amount"] + t["quantity"] * rate, 2)
        out = list(weaver.values())
        crew = [dict(r._mapping) for r in db.execute(_LINE_CREW_SQL, {
            "branch_id": int(branch_id), "date_from": date_from, "date_to": date_to,
        }).fetchall()]
        crew_rows = [
            {**base, "eb_id": c["eb_id"], "shift_id": shift_ids.get(c["shift"]), "name": c["name"],
             "deptcode": "08", "occucode": c["occu_code"], "mcno": c["mcno"], "quantity": c["quantity"],
             "eff": c["eff"], "tareff": c["tareff"]}
            for c in _line_crew_summary(rows, crew)
        ]
        out += crew_rows
        mistry_rows = [
            {**base, "eb_id": m["eb_id"], "shift_id": shift_ids.get(m["shift"]), "name": m["name"],
             "deptcode": "13", "occucode": m["occu_code"], "quantity": side_shift[(m["side"], m["shift"])][2],
             "eff": side_shift[(m["side"], m["shift"])][0], "tareff": side_shift[(m["side"], m["shift"])][1]}
            for m in mistries if (m["side"], m["shift"]) in side_shift
        ]
        out += mistry_rows
        occus = _occu_by_code(db, codes)
        for code, _, _, occu_rows, code_row, _ in LOOM_GROUP_CODES:
            for shift, eff, tareff, prod in codes.get(code, []):
                row = {**base, "effcode": code, "shift_id": shift_ids.get(shift) if shift else None,
                       "eff": eff, "tareff": tareff, "quantity": round(prod, 2)}
                if occu_rows:
                    out += [{**row, "deptcode": d, "occucode": o} for d, o in occus.get(code, [])]
                if code_row:
                    out.append({**row, "deptcode": None, "occucode": None})

        replaced = _replace_eff_rows(db, base, out)
        db.commit()

        weavers = len({(t["eb_id"], t["shift"]) for t in per_eb})
        shift_text = ", ".join(f"{s} {e:.2f}%" for s, e, *_ in codes.get(12, []))
        overall = codes.get(13, [(None, 0.0)])[0][1]
        return {"data": {
            "message": (f"Loom efficiency: {weavers} weaver-shift(s), {len(weaver)} row(s), "
                        f"production amount {sum(w['prod_amount'] or 0 for w in weaver.values()):,.2f}, "
                        f"{len(crew_rows)} sardar/helper row(s), {len(mistry_rows)} loom mistry row(s); "
                        f"{shift_text or 'no production'}; overall {overall:.2f}%"
                        + (f"; {len(unlinked)} weaving quality(ies) not linked to a wages quality "
                           f"(no prod_amount)" if unlinked else "")
                        + (f"; {replaced} previous row(s) replaced" if replaced else "")),
            "rows": len(out), "overall_eff": overall,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── EJM Beaming efficiency (legacy WG150426/BEAMEFF1.PRG) ───────────────────
#
# Legacy bem10495 (prd_yds vs yds100 per shift) → the fortnight group tables
# (user, 2026-10-03):
#   production per machine / shift / quality       → ejm_fne_group_production,
#       quality → wages quality via link_prodquality_wagequality_mst (see _BEAMING_SQL)
#   machine hours                                  → ejm_wages_group_mc_hours
#       (mc_hours - idle_hours per period / machine / shift)
#   100% production                                → target per 8 h of the quality,
#       ejm_wages_fne_qual_target.eff_pc (period, machine's dept, quality)
#   prod_amount                                    → production x
#       ejm_wages_quality_rate.quality_rate
# A machine-shift's eff = 800 x Σ(production / target) / hours (several qualities
# converted by their targets, as the winding process does) — with one target this
# is (Σ production / hours x 8) / target x 100 (user 2026-10-06: MC 2 = 72 + 16 + 37
# = 125 beams / 15 hrs x 8 = 66.67 vs 67 → 99.50%). Hessian = machine
# name containing HESS (legacy q_code '1'). Group codes as legacy wgrepl: one row
# per OCCUMAST occupation, tareff 100. Plus one row per machine / shift / wages
# quality carrying production, prod_amount and the machine-shift eff.
BEAMING_DETAILS = "BEAMING"
BEAMING_DETCODE = "07"
# (effcode, per shift?, hessian only?)
BEAMING_GROUP_CODES = ((10, True, True), (11, True, False), (9, False, False))

# Wages quality of a group-production row: through link_prodquality_wagequality_mst
# (Production - Wages Quality Link page, user 2026-10-03) when its quality is a
# linked production quality of the machine's dept, else the stored id itself (the
# MC Group Production page saves wages qualities directly).
# Shared by Beaming and Press (:mc_type = machine type name pattern).
_BEAMING_SQL = text("""
    SELECT x.machine_id, x.mech_code, x.machine_name, x.dept_code, x.shift_id, x.shift,
           x.quality_id, SUM(x.production) AS prod, MAX(r.quality_rate) AS rate,
           -- the fortnight target, else the quality master's target (user 2026-10-06)
           COALESCE(MAX(t.eff_pc), MAX(r.target)) AS target,
           (SELECT SUM(h.mc_hours - COALESCE(h.idle_hours, 0)) FROM ejm_wages_group_mc_hours h
             WHERE h.period_id = :period_id AND h.machine_id = x.machine_id
               AND h.shift_id = x.shift_id) AS hrs
    FROM (
        SELECT g.machine_id, m.mech_code, m.machine_name, m.dept_id, d.dept_code, g.shift_id,
               s.shift_name AS shift, g.production,
               COALESCE((SELECT MIN(l.wages_quality_id) FROM link_prodquality_wagequality_mst l
                          WHERE l.active = 1 AND l.branch_id = :branch_id
                            AND l.dept_id = m.dept_id AND l.prod_quality_id = g.quality_id),
                        g.quality_id) AS quality_id,
               (SELECT MIN(l.process) FROM link_prodquality_wagequality_mst l
                 WHERE l.active = 1 AND l.branch_id = :branch_id
                   AND l.dept_id = m.dept_id AND l.prod_quality_id = g.quality_id) AS link_process
        FROM ejm_fne_group_production g
        JOIN machine_mst m ON m.machine_id = g.machine_id
        JOIN machine_type_mst mt ON mt.machine_type_id = m.machine_type_id
         AND UPPER(mt.machine_type_name) LIKE :mc_type
        JOIN dept_mst d ON d.dept_id = m.dept_id AND d.branch_id = :branch_id
        LEFT JOIN shift_mst s ON s.shift_id = g.shift_id
        WHERE g.period_id = :period_id
    ) x
    LEFT JOIN ejm_wages_quality_rate r ON r.wages_quality_rate_id = x.quality_id
    LEFT JOIN ejm_wages_fne_qual_target t
           ON t.period_id = :period_id AND t.dept_id = x.dept_id AND t.quality_id = x.quality_id
          AND (t.status_id IS NULL OR t.status_id NOT IN (4, 6))
          -- target keyed on (quality, process) like the link row (user 2026-10-06)
          -- (:process = the page's process when the row has no link row, e.g. Press)
          AND UPPER(REPLACE(COALESCE(t.process, ''), ' ', ''))
              = UPPER(REPLACE(COALESCE(x.link_process, :process, ''), ' ', ''))
    GROUP BY x.machine_id, x.mech_code, x.machine_name, x.dept_code, x.shift_id, x.shift, x.quality_id
""")


def _beaming_summary(rows: list[dict], group_codes=BEAMING_GROUP_CODES
                     ) -> tuple[list[dict], dict[int, list[tuple]]]:
    """Detail lines per (machine, shift, wages quality) with the machine-shift
    eff and prod_amount, and per group code a list of (shift or None, eff,
    production). eff = (Σ production / Σ hours x 8) / target x 100, target =
    the production-weighted target of the qualities that have one (user
    2026-10-06). Machines with no target or no hours don't count in eff."""
    units: dict[tuple, dict] = {}
    for r in rows:
        u = units.setdefault((r["machine_id"], r["shift_id"]), {
            "shift": r["shift"], "hrs": float(r["hrs"] or 0), "prod": 0.0, "tw": 0.0, "tp": 0.0,
            "hessian": "HESS" in (r["machine_name"] or "").upper()})
        prod, target = float(r["prod"] or 0), float(r["target"] or 0)
        u["prod"] += prod
        if target > 0:
            u["tw"] += prod * target
            u["tp"] += prod

    def eff(sel):
        sel = [u for u in sel if u["tp"] > 0]
        hrs = sum(u["hrs"] for u in sel)
        if hrs <= 0:
            return None
        target = sum(u["tw"] for u in sel) / sum(u["tp"] for u in sel)
        return round(sum(u["prod"] for u in sel) / hrs * 8 / target * 100, 2)

    lines = []
    for r in rows:
        prod = float(r["prod"] or 0)
        lines.append({**r, "prod": round(prod, 2), "prod_amount": round(prod * float(r["rate"] or 0), 2),
                      "eff": eff([units[(r["machine_id"], r["shift_id"])]]) or 0.0})

    codes: dict[int, list[tuple]] = {}
    all_units = list(units.values())
    for code, per_shift, hessian in group_codes:
        for shift in (sorted({u["shift"] for u in all_units}) if per_shift else [None]):
            sel = [u for u in all_units
                   if (shift is None or u["shift"] == shift) and (not hessian or u["hessian"])]
            e = eff(sel)
            if e is not None:
                codes.setdefault(code, []).append((shift, e, sum(u["prod"] for u in sel)))
    return lines, codes


@router.post("/ejm_beaming_eff_process")
def ejm_beaming_eff_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Beaming efficiency and production amount for one fortnight into
    ejm_fne_wages_eff_data (details 'BEAMING', detcode '07', tareff 100):
      per machine / shift / wages quality  quality_id, mcno = machine code,
          quantity = production, prod_amount = production x quality rate,
          eff = the machine-shift efficiency
      group codes 10 hessian per shift, 11 all machines per shift, 9 all shifts —
          one row per link_wages_occu_eff occupation of the code
    Sources: ejm_fne_group_production + ejm_wages_group_mc_hours of the fortnight.
    The fortnight's BEAMING rows are replaced.
    Body: { branch_id, date_from, date_to } — exactly a forthnight_period_mst fortnight.
    """
    return _group_machine_process(request, db, label="Beaming", mc_type="%BEAM%",
                                  details=BEAMING_DETAILS, detcode=BEAMING_DETCODE,
                                  group_codes=BEAMING_GROUP_CODES)


# Press machines: the same group-machine calculation as Beaming (user, 2026-10-03),
# machine types containing PRESS. Legacy had no press efficiency program, so no
# OCCUMAST group codes — only the per machine / shift / quality rows.
PRESS_DETAILS = "PRESS"
PRESS_DETCODE = "09"  # FINISHING(EJM)'s dept code, unused by the other processes


@router.post("/ejm_press_eff_process")
def ejm_press_eff_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Press machine efficiency and production amount for one fortnight into
    ejm_fne_wages_eff_data (details 'PRESS', detcode '09', tareff 100): one row
    per machine / shift / wages quality — exactly as Beaming, for Press machines.
    Body: { branch_id, date_from, date_to } — exactly a forthnight_period_mst fortnight.
    """
    return _group_machine_process(request, db, label="Press", mc_type="%PRESS%",
                                  details=PRESS_DETAILS, detcode=PRESS_DETCODE, group_codes=(),
                                  target_process="Press")


def _group_machine_process(request: Request, db: Session, *, label: str, mc_type: str,
                           details: str, detcode: str, group_codes,
                           target_process: str | None = None) -> dict:
    """Group-machine (Beaming / Press) efficiency and production amount — see
    ejm_beaming_eff_process. ``target_process`` is the ejm_wages_fne_qual_target
    process the page's targets are entered under when rows have no link row."""
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        if not branch_id or not date_from or not date_to:
            raise HTTPException(status_code=400, detail="branch_id, date_from and date_to are required")
        if int(branch_id) != EJM_CASH_BRANCH_ID:
            raise HTTPException(
                status_code=400,
                detail=f"{label} efficiency runs only for branch {EJM_CASH_BRANCH_ID} (EJM FACTORY)")
        period_id = _fortnight_id(db, date_from, date_to)
        shift_ids = {r.shift_name: r.shift_id for r in db.execute(text("""
            SELECT shift_id, shift_name FROM shift_mst WHERE branch_id = :branch_id AND status = 1
        """), {"branch_id": int(branch_id)}).fetchall()}

        rows = [dict(r._mapping) for r in db.execute(_BEAMING_SQL, {
            "branch_id": int(branch_id), "period_id": period_id, "mc_type": mc_type,
            "process": target_process}).fetchall()]
        lines, codes = _beaming_summary(rows, group_codes)

        base = {"period_id": period_id, "details": details, "detcode": detcode,
                "eb_id": None, "name": None, "tareff": 100}
        out = [{**base, "effcode": None, "shift_id": t["shift_id"], "deptcode": t["dept_code"],
                "occucode": None,
                "quality_id": t["quality_id"], "mcno": (t["mech_code"] or "")[:6] or None,
                "mc_id": t["machine_id"],
                "quantity": t["prod"], "prod_amount": t["prod_amount"], "eff": t["eff"]}
               for t in lines]
        occus = _occu_by_code(db, codes)
        out += [{**base, "effcode": code, "shift_id": shift_ids.get(shift) if shift else None,
                 "deptcode": d, "occucode": o, "eff": eff, "quantity": round(prod, 2)}
                for code, entries in codes.items() for shift, eff, prod in entries
                for d, o in occus.get(code, [])]

        replaced = _replace_eff_rows(db, base, out)
        db.commit()

        # Per-shift / overall figures over all the machines (codes 11 / 9 for Beaming).
        _, totals = _beaming_summary(rows, ((11, True, False), (9, False, False)))
        shift_text = ", ".join(f"{s} {e:.2f}%" for s, e, _ in totals.get(11, []))
        overall = totals.get(9, [(None, 0.0)])[0][1]
        no_target = sum(1 for r in rows if not float(r["target"] or 0))
        no_hours = sum(1 for r in rows if not float(r["hrs"] or 0))
        return {"data": {
            "message": (f"{label} efficiency: {len(lines)} machine-shift-quality row(s), "
                        f"production amount {sum(t['prod_amount'] for t in lines):,.2f}; "
                        f"{shift_text or 'no efficiency'}; overall {overall:.2f}%"
                        + (f"; {no_target} row(s) without a fortnight target" if no_target else "")
                        + (f"; {no_hours} row(s) without machine hours" if no_hours else "")
                        + (f"; {replaced} previous row(s) replaced" if replaced else "")),
            "rows": len(out), "overall_eff": overall,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── EJM Finishing efficiency (legacy WG150426/FINISHEF.PRG) ─────────────────
#
# Worker processes (hemming / herackle / sack sewing) work like the winding
# process (user, 2026-10-04):
#   hemmempl/heraempl/safeempl + files  → jute_prod_finishing_daily rows with eb_id,
#       pieces = prod_qty
#   finiqual quality → wages quality    → link_prodquality_wagequality_mst for the
#       worker's dept, the finishing quality and the link's process
#       (Hemming / Heracle / Handsewer)
#   st_prd_sft (bundles per 8 h)        → ejm_wages_fne_qual_target.eff_pc
#   hours                               → the worker's attendance hours that shift
#   eff = 800 x Σ(bundles / target) / hours, bundles = pieces / 25 (legacy
#       acprd = bundles*25); prod_amount = pieces x ejm_wages_quality_rate rate
#   deptcode / occucode                 → the worker's attended dept and designation
#   (legacy hemopr/heropr wrote 02 hemming / 01 heracle, as the designations do)
# Shift codes 18 / 33 / 20 and fortnight codes 19 / 34 come from the same figures.
# Machine processes (cutting code 26, lapping code 32) have no worker: their
# standard per 8 h is the linked wages quality's fortnight target in
# ejm_wages_fne_qual_target (link page, machine's dept, process Cutting / Lapping
# — user rule 2026-10-04: quality-wise targets come from that table), over spell
# hours less stoppage — eff = Σ production / Σ (standard / 8 × hours) × 100.
# Not ported: calendar (code 14 — no calender production in sls) and the
# '9' joint-check rows (sls finishing qualities carry no legacy q_code).
FINISHING_DETAILS = "FINISHING"
FINISHING_DETCODE = "10"
FINISHING_PROCESSES = ("lapping", "cutting")  # machine processes (no worker)
FINISHING_WORKER_PROCESSES = ("hemming", "herackle", "sacksewing")
# Finishing process → the link page's process name (compared upper-case, no spaces).
FINISHING_LINK_PROCESS = {"hemming": "HEMMING", "herackle": "HERACLE", "sacksewing": "HANDSEWER"}
FINISHING_PIECES_PER_BUNDLE = 25
# Worker targets in ejm_wages_fne_qual_target are pieces per 8 h for all three
# processes (user 2026-10-07: herackle 2150 pcs vs target 2125) — no bundle conversion.
FINISHING_PIECE_PROCESSES = FINISHING_WORKER_PROCESSES


def _finishing_unit(process) -> int:
    """Pieces per target unit: 1 for sack sewing, else a bundle of 25."""
    return 1 if process in FINISHING_PIECE_PROCESSES else FINISHING_PIECES_PER_BUNDLE

_FINISHING_SQL = text("""
    SELECT f.process, f.eb_id, LEFT(sp.spell_code, 1) AS shift, f.prod_qty AS prod,
           GREATEST(COALESCE(sp.working_hours, 0) - COALESCE((
               SELECT SUM(st.stoppage_hours) FROM jute_prod_stoppage_hours st
                 JOIN spell_mst ss ON ss.spell_id = st.spell_id
                WHERE st.active = 1 AND st.machine_id = f.machine_id AND st.tran_date = f.tran_date
                  AND (ss.spell_code = sp.spell_code
                       OR (CHAR_LENGTH(sp.spell_code) = 1 AND LEFT(ss.spell_code, 1) = sp.spell_code))
           ), 0), 0) AS hrs,
           COALESCE((
               SELECT MAX(t.eff_pc) FROM link_prodquality_wagequality_mst l
                 JOIN ejm_wages_fne_qual_target t
                   ON t.quality_id = l.wages_quality_id AND t.dept_id = l.dept_id
                  AND t.period_id = :period_id
                  AND (t.status_id IS NULL OR t.status_id NOT IN (4, 6))
                  AND UPPER(REPLACE(COALESCE(t.process, ''), ' ', '')) = UPPER(REPLACE(COALESCE(l.process, ''), ' ', ''))
                WHERE l.active = 1 AND l.branch_id = :branch_id AND l.dept_id = m.dept_id
                  AND l.prod_quality_id = f.finishing_quality_id
                  AND (l.process IS NULL OR UPPER(REPLACE(l.process, ' ', '')) = UPPER(f.process))
           ), 0) AS std_shift,
           LEFT(CONCAT_WS(' ', NULLIF(TRIM(pd.first_name), ''), NULLIF(TRIM(pd.middle_name), ''),
                          NULLIF(TRIM(pd.last_name), '')), 25) AS name
    FROM jute_prod_finishing_daily f
    JOIN spell_mst sp ON sp.spell_id = f.spell_id
    LEFT JOIN machine_mst m ON m.machine_id = f.machine_id
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = f.eb_id
    WHERE f.branch_id = :branch_id AND f.active = 1
      AND f.tran_date BETWEEN :date_from AND :date_to
      AND f.process IN :processes
""").bindparams(bindparam("processes", expanding=True))


# Worker entries joined to the worker's attendance (date, shift) and, through the
# link page, to the wages quality, its rate and the fortnight target.
_FINISHING_WORKER_SQL = text(f"""
    WITH att AS (
        SELECT da.eb_id, da.attendance_date AS d, LEFT(ds.spell_code, 1) AS shift,
               MIN(dp.dept_id) AS dept_id, MIN(dp.dept_code) AS dept_code,
               MIN(g.occu_code) AS occu_code,
               SUM(da.working_hours - COALESCE(da.idle_hours, 0)) AS hrs
        FROM daily_attendance da
        JOIN spell_mst ds ON ds.spell_id = da.spell_id
        JOIN sub_dept_mst sd ON sd.sub_dept_id = da.worked_department_id
        JOIN dept_mst dp ON dp.dept_id = sd.dept_id
        LEFT JOIN designation_mst g ON g.designation_id = da.worked_designation_id
        WHERE da.branch_id = :branch_id
          AND da.attendance_date BETWEEN :date_from AND :date_to
          AND {ATT_LIVE_SQL}
        GROUP BY da.eb_id, da.attendance_date, LEFT(ds.spell_code, 1)
    ), prod AS (
        SELECT f.eb_id, f.tran_date AS d, LEFT(sp.spell_code, 1) AS shift, f.process,
               f.finishing_quality_id AS fq, SUM(f.prod_qty) AS prod
        FROM jute_prod_finishing_daily f
        JOIN spell_mst sp ON sp.spell_id = f.spell_id
        WHERE f.branch_id = :branch_id AND f.active = 1 AND f.eb_id IS NOT NULL
          AND f.tran_date BETWEEN :date_from AND :date_to AND f.process IN :processes
        GROUP BY f.eb_id, f.tran_date, LEFT(sp.spell_code, 1), f.process, f.finishing_quality_id
    ), lp AS (
        SELECT p.*, CASE p.process {
                   " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in FINISHING_LINK_PROCESS.items())
               } END AS link_process
        FROM prod p
    ), lnk AS (
        SELECT p.*, a.dept_id, a.dept_code, a.occu_code, a.hrs,
               (SELECT MIN(l.wages_quality_id) FROM link_prodquality_wagequality_mst l
                 WHERE l.active = 1 AND l.branch_id = :branch_id AND l.dept_id = a.dept_id
                   AND l.prod_quality_id = p.fq
                   AND (l.process IS NULL
                        OR UPPER(REPLACE(l.process, ' ', '')) = p.link_process)) AS wages_qc_id
        FROM lp p
        JOIN att a ON a.eb_id = p.eb_id AND a.d = p.d AND a.shift = p.shift
    )
    SELECT x.eb_id, x.d, x.shift, x.process, x.dept_id, x.dept_code, x.occu_code, x.hrs,
           x.fq, x.prod, x.wages_qc_id, r.quality_rate,
           -- target keyed on (quality, process): the prod row's process (user 2026-10-06)
           (SELECT MAX(t.eff_pc) FROM ejm_wages_fne_qual_target t
             WHERE t.period_id = :period_id AND t.dept_id = x.dept_id AND t.quality_id = x.wages_qc_id
               AND (t.status_id IS NULL OR t.status_id NOT IN (4, 6))
               AND UPPER(REPLACE(COALESCE(t.process, ''), ' ', '')) = x.link_process) AS target,
           LEFT(CONCAT_WS(' ', NULLIF(TRIM(pd.first_name), ''), NULLIF(TRIM(pd.middle_name), ''),
                          NULLIF(TRIM(pd.last_name), '')), 25) AS name
    FROM lnk x
    LEFT JOIN ejm_wages_quality_rate r ON r.wages_quality_rate_id = x.wages_qc_id
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = x.eb_id
""").bindparams(bindparam("processes", expanding=True))

# Worker group codes: (effcode, processes, per shift?, occupation rows?, code row?)
FINISHING_WORKER_GROUP_CODES = (
    (18, ("hemming",), True, False, True),
    (33, ("herackle",), True, False, True),
    (20, ("sacksewing",), True, False, True),
    (19, ("hemming", "herackle"), False, True, False),
    (34, ("sacksewing",), False, True, False),
)
FINISHING_LAPPING_CODE = 32   # per shift, code row
FINISHING_CUTTING_CODE = 26   # legacy: one row deptcode 10 / occucode 08


def _finishing_worker_summary(rows: list[dict]) -> tuple[list[dict], dict[int, list[tuple]], int]:
    """Winding-style worker lines (one per worker / shift / dept / wages quality,
    eff over all their qualities in bundles, prod_amount on pieces), the worker
    group codes as (shift or None, eff, pieces), and the unlinked entry count."""
    lines, unlinked = _winding_summary(
        [{**r, "prod": float(r["prod"] or 0) / _finishing_unit(r["process"])} for r in rows])
    rate_of = {r["wages_qc_id"]: float(r["quality_rate"] or 0) for r in rows if r["wages_qc_id"]}
    pieces: dict[tuple, float] = {}
    for r in rows:
        k = (r["eb_id"], r["shift"], r["dept_code"], r["wages_qc_id"])
        pieces[k] = pieces.get(k, 0) + float(r["prod"] or 0)
    for t in lines:  # back to pieces for quantity and amount
        t["prod"] = round(pieces[(t["eb_id"], t["shift"], t["dept_code"], t["quality_id"])], 2)
        t["prod_amount"] = round(t["prod"] * rate_of.get(t["quality_id"], 0), 2)

    codes: dict[int, list[tuple]] = {}
    for code, procs, per_shift, _, _ in FINISHING_WORKER_GROUP_CODES:
        mine = [r for r in rows if r["process"] in procs and r["wages_qc_id"]]
        for shift in (sorted({r["shift"] for r in mine}) if per_shift else [None]):
            sel = [r for r in mine if shift is None or r["shift"] == shift]
            hrs = {(r["eb_id"], r["d"], r["shift"]): float(r["hrs"] or 0)
                   for r in sel if float(r["target"] or 0) > 0}
            std = sum(float(r["prod"] or 0) / _finishing_unit(r["process"]) / float(r["target"])
                      for r in sel if float(r["target"] or 0) > 0)
            if hrs and sum(hrs.values()) > 0:
                codes.setdefault(code, []).append(
                    (shift, round(800 * std / sum(hrs.values()), 2), sum(float(r["prod"] or 0) for r in sel)))
    return lines, codes, unlinked


def _finishing_summary(rows: list[dict]):
    """Machine processes (lapping / cutting): lapping shift codes as (shift, eff,
    production) and the cutting overall (eff, production), against the Finishing
    Spec Sheet standard; entries with no standard are left out."""
    def eff(sel):
        prod = sum(r["prod"] for r in sel)
        std = sum(r["std"] for r in sel)
        return (round(prod / std * 100, 2), prod) if prod > 0 and std > 0 else None

    for r in rows:
        r["prod"] = float(r["prod"] or 0)
        r["std"] = float(r["std_shift"] or 0) / 8 * float(r["hrs"] or 0)
    rated = [r for r in rows if r["std"] > 0]
    lapping = [r for r in rated if r["process"] == "lapping"]
    codes = {FINISHING_LAPPING_CODE: [(s, *e) for s in sorted({r["shift"] for r in lapping})
                                      if (e := eff([r for r in lapping if r["shift"] == s]))]}
    return {k: v for k, v in codes.items() if v}, eff([r for r in rated if r["process"] == "cutting"])


@router.post("/ejm_finishing_eff_process")
def ejm_finishing_eff_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Finishing efficiency and production amount for one fortnight into
    ejm_fne_wages_eff_data (details 'FINISHING', detcode '10', tareff 100):
      per worker / shift / wages quality (hemming, herackle, sack sewing — as the
          winding process): worker's dept / designation codes, quality_id,
          quantity = pieces, prod_amount, eff = the worker's shift efficiency
      shift codes 18 hemming / 33 herackle / 20 sack sewing / 32 lapping (code rows);
      19 hemming + herackle and 34 sack sewing over the fortnight (occupation
      rows); cutting code 26 as one 10 / 08 row
    The fortnight's FINISHING rows are replaced, so rerunning is safe.
    Body: { branch_id, date_from, date_to } — exactly a forthnight_period_mst fortnight.
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        if not branch_id or not date_from or not date_to:
            raise HTTPException(status_code=400, detail="branch_id, date_from and date_to are required")
        if int(branch_id) != EJM_CASH_BRANCH_ID:
            raise HTTPException(
                status_code=400,
                detail=f"Finishing efficiency runs only for branch {EJM_CASH_BRANCH_ID} (EJM FACTORY)")
        period_id = _fortnight_id(db, date_from, date_to)
        shift_ids = {r.shift_name: r.shift_id for r in db.execute(text("""
            SELECT shift_id, shift_name FROM shift_mst WHERE branch_id = :branch_id AND status = 1
        """), {"branch_id": int(branch_id)}).fetchall()}

        params = {"branch_id": int(branch_id), "date_from": date_from, "date_to": date_to}
        workers = [dict(r._mapping) for r in db.execute(_FINISHING_WORKER_SQL, {
            **params, "period_id": period_id, "processes": list(FINISHING_WORKER_PROCESSES)}).fetchall()]
        lines, worker_codes, unlinked = _finishing_worker_summary(workers)
        rows = [dict(r._mapping) for r in db.execute(_FINISHING_SQL, {
            **params, "period_id": period_id, "processes": list(FINISHING_PROCESSES)}).fetchall()]
        machine_codes, cutting = _finishing_summary(rows)

        base = {"period_id": period_id, "details": FINISHING_DETAILS, "detcode": FINISHING_DETCODE,
                "eb_id": None, "shift_id": None, "name": None, "effcode": None, "tareff": 100}
        out = [{**base, "eb_id": t["eb_id"], "shift_id": shift_ids.get(t["shift"]), "name": t["name"],
                "deptcode": t["dept_code"], "occucode": t["occu_code"], "quality_id": t["quality_id"],
                "quantity": t["prod"], "eff": t["eff"], "prod_amount": t["prod_amount"]}
               for t in lines]
        occus = _occu_by_code(db, worker_codes)
        group_codes = FINISHING_WORKER_GROUP_CODES + ((FINISHING_LAPPING_CODE, (), True, False, True),)
        codes = {**worker_codes, **machine_codes}
        for code, _, _, occu_rows, code_row in group_codes:
            for shift, eff, prod in codes.get(code, []):
                row = {**base, "effcode": code, "shift_id": shift_ids.get(shift) if shift else None,
                       "eff": eff, "quantity": round(prod, 2)}
                if occu_rows:
                    out += [{**row, "deptcode": d, "occucode": o} for d, o in occus.get(code, [])]
                if code_row:
                    out.append({**row, "deptcode": None, "occucode": None})
        if cutting:
            out.append({**base, "effcode": FINISHING_CUTTING_CODE, "deptcode": "10", "occucode": "08",
                        "eff": cutting[0], "quantity": round(cutting[1], 2)})

        replaced = _replace_eff_rows(db, base, out)
        db.commit()

        unrated = sum(1 for r in rows if r["std"] <= 0)
        no_target = sum(1 for r in workers if r["wages_qc_id"] and not float(r["target"] or 0))
        return {"data": {
            "message": (f"Finishing efficiency: {len({(t['eb_id'], t['shift']) for t in lines})} "
                        f"worker-shift(s), {len(lines)} worker row(s), production amount "
                        f"{sum(t['prod_amount'] for t in lines):,.2f}, {len(out)} row(s) in all"
                        + (f"; {unlinked} worker entr(ies) not linked to a wages quality" if unlinked else "")
                        + (f"; {no_target} worker entr(ies) without a fortnight target" if no_target else "")
                        + (f"; {unrated} cutting/lapping entr(ies) without a linked wages quality "
                           f"target (ejm_wages_fne_qual_target)" if unrated else "")
                        + (f"; {replaced} previous row(s) replaced" if replaced else "")),
            "rows": len(out), "unrated": unrated, "unlinked": unlinked,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── EJM Winding efficiency + production amount (legacy WG150426/WINDCALC.PRG) ─
#
# Legacy sources → this tenant:
#   WINDPROD (eb, dept, occu, shift, qcode, prod)  → jute_prod_winding_doff
#       production_qty per winder (eb_id), yarn item_id
#   qcode (the wages quality)                     → link_prodquality_wagequality_mst
#       (dept_id + prod_qc_id = the doff's item_id → wages_qc_id)
#   QUALTRAG.TARGET_PD (target per 8 h)           → ejm_wages_fne_qual_target.eff_pc
#       for the fortnight, dept and wages quality
#   ATTTRANS wkhrs + othrs (dept 05/06)           → daily_attendance hours of the
#       winder's attendance that date / shift, dept from the worked sub-department
#   prod_amount = production x ejm_wages_quality_rate.quality_rate (user, 2026-10-03)
# eff (legacy, several qualities converted to the highest target) reduces to
#   800 x Σ(prod / target) / hours; no target at all → 100 (legacy EF=100).
WINDING_DETAILS = "WINDING"
WINDING_DETCODE = "05"
WINDING_DEPT_CODES = ("05", "06")  # spool / cop winding

# Per winder, date and shift: the attendance (dept from the worked sub-department,
# hours = working - idle) joined to that shift's doff production per yarn item and
# the item's wages quality. ponytail: a winder with two attendance rows in one
# shift on different winding depts is booked to the lower dept id.
_WINDING_SQL = text(f"""
    WITH att AS (
        SELECT da.eb_id, da.attendance_date AS d, LEFT(ds.spell_code, 1) AS shift,
               MIN(dp.dept_id) AS dept_id, MIN(dp.dept_code) AS dept_code,
               MIN(g.occu_code) AS occu_code,
               SUM(da.working_hours - COALESCE(da.idle_hours, 0)) AS hrs
        FROM daily_attendance da
        JOIN spell_mst ds ON ds.spell_id = da.spell_id
        JOIN sub_dept_mst sd ON sd.sub_dept_id = da.worked_department_id
        JOIN dept_mst dp ON dp.dept_id = sd.dept_id AND dp.dept_code IN :dept_codes
        LEFT JOIN designation_mst g ON g.designation_id = da.worked_designation_id
        WHERE da.branch_id = :branch_id
          AND da.attendance_date BETWEEN :date_from AND :date_to
          AND {ATT_LIVE_SQL}
        GROUP BY da.eb_id, da.attendance_date, LEFT(ds.spell_code, 1)
    ), prod AS (
        SELECT w.eb_id, w.tran_date AS d, LEFT(sp.spell_code, 1) AS shift, w.item_id,
               SUM(w.production_qty) AS prod
        FROM jute_prod_winding_doff w
        JOIN spell_mst sp ON sp.spell_id = w.spell_id
        WHERE w.branch_id = :branch_id AND w.active = 1 AND w.eb_id IS NOT NULL
          AND w.tran_date BETWEEN :date_from AND :date_to
        GROUP BY w.eb_id, w.tran_date, LEFT(sp.spell_code, 1), w.item_id
    )
    SELECT p.eb_id, p.d, p.shift, a.dept_id, a.dept_code, a.occu_code, a.hrs,
           p.item_id, p.prod, l.wages_quality_id AS wages_qc_id,
           r.quality_rate, t.eff_pc AS target,
           LEFT(CONCAT_WS(' ', NULLIF(TRIM(pd.first_name), ''), NULLIF(TRIM(pd.middle_name), ''),
                          NULLIF(TRIM(pd.last_name), '')), 25) AS name
    FROM prod p
    JOIN att a ON a.eb_id = p.eb_id AND a.d = p.d AND a.shift = p.shift
    LEFT JOIN link_prodquality_wagequality_mst l
           ON l.active = 1 AND l.branch_id = :branch_id AND l.dept_id = a.dept_id
          AND l.prod_quality_id = p.item_id
    LEFT JOIN ejm_wages_quality_rate r ON r.wages_quality_rate_id = l.wages_quality_id
    LEFT JOIN ejm_wages_fne_qual_target t
           ON t.period_id = :period_id AND t.dept_id = a.dept_id AND t.quality_id = l.wages_quality_id
          AND (t.status_id IS NULL OR t.status_id NOT IN (4, 6))
    LEFT JOIN hrms_ed_personal_details pd ON pd.eb_id = p.eb_id
""").bindparams(bindparam("dept_codes", expanding=True))


def _winding_summary(rows: list[dict]) -> tuple[list[dict], int]:
    """Per (eb, shift, dept): the winder's eff, and one output line per wages
    quality with its production and amount. Returns (lines, unlinked doff rows).
    Hours count once per attendance (date) however many items were wound."""
    unlinked = 0
    groups: dict[tuple, dict] = {}
    for r in rows:
        if r["wages_qc_id"] is None:
            unlinked += 1
            continue
        g = groups.setdefault((r["eb_id"], r["shift"], r["dept_id"]), {
            "eb_id": r["eb_id"], "shift": r["shift"], "dept_code": r["dept_code"],
            "occu_code": r["occu_code"], "name": r["name"], "hrs": {}, "q": {}})
        g["hrs"][r["d"]] = float(r["hrs"] or 0)
        q = g["q"].setdefault(r["wages_qc_id"], {"prod": 0.0, "rate": float(r["quality_rate"] or 0),
                                                 "target": float(r["target"] or 0)})
        q["prod"] += float(r["prod"] or 0)

    lines = []
    for g in groups.values():
        hrs = sum(g["hrs"].values())
        rated = [q for q in g["q"].values() if q["target"] > 0]
        eff = round(800 * sum(q["prod"] / q["target"] for q in rated) / hrs, 2) if rated and hrs > 0 else 100.0
        for qc, q in g["q"].items():
            lines.append({"eb_id": g["eb_id"], "shift": g["shift"], "dept_code": g["dept_code"],
                          "occu_code": g["occu_code"], "name": g["name"], "quality_id": qc,
                          "prod": round(q["prod"], 2), "eff": eff,
                          "prod_amount": round(q["prod"] * q["rate"], 2)})
    return lines, unlinked


@router.post("/ejm_winding_eff_process")
def ejm_winding_eff_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Winder efficiency and production amount for one fortnight into
    ejm_fne_wages_eff_data (details 'WINDING', detcode '05', tareff 100): one
    row per winder, shift and wages quality — deptcode 05 spool / 06 cop,
    occucode from the worked designation, quality_id = the wages quality,
    quantity = production, prod_amount = production x quality rate, eff = the
    winder's shift efficiency over all their qualities. Doff items with no
    link_prodquality_wagequality_mst row are skipped (counted in the message).
    Body: { branch_id, date_from, date_to } — exactly a forthnight_period_mst fortnight.
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        if not branch_id or not date_from or not date_to:
            raise HTTPException(status_code=400, detail="branch_id, date_from and date_to are required")
        if int(branch_id) != EJM_CASH_BRANCH_ID:
            raise HTTPException(
                status_code=400,
                detail=f"Winding efficiency runs only for branch {EJM_CASH_BRANCH_ID} (EJM FACTORY)")
        period_id = _fortnight_id(db, date_from, date_to)
        shift_ids = {r.shift_name: r.shift_id for r in db.execute(text("""
            SELECT shift_id, shift_name FROM shift_mst WHERE branch_id = :branch_id AND status = 1
        """), {"branch_id": int(branch_id)}).fetchall()}

        rows = [dict(r._mapping) for r in db.execute(_WINDING_SQL, {
            "branch_id": int(branch_id), "date_from": date_from, "date_to": date_to,
            "period_id": period_id, "dept_codes": list(WINDING_DEPT_CODES)}).fetchall()]
        lines, unlinked = _winding_summary(rows)

        base = {"period_id": period_id, "details": WINDING_DETAILS, "detcode": WINDING_DETCODE}
        out = [{**base, "eb_id": t["eb_id"], "shift_id": shift_ids.get(t["shift"]), "name": t["name"],
                "deptcode": t["dept_code"], "occucode": t["occu_code"], "quality_id": t["quality_id"],
                "quantity": t["prod"], "eff": t["eff"], "tareff": 100, "effcode": None,
                "prod_amount": t["prod_amount"]}
               for t in lines]
        replaced = _replace_eff_rows(db, base, out)
        db.commit()

        winders = len({(t["eb_id"], t["shift"]) for t in lines})
        amount = sum(t["prod_amount"] for t in lines)
        return {"data": {
            "message": (f"Winding efficiency: {winders} winder-shift(s), {len(out)} row(s), "
                        f"production amount {amount:,.2f}"
                        + (f"; {unlinked} doff line(s) skipped — yarn not linked to a wages quality "
                           f"(link_prodquality_wagequality_mst)" if unlinked else "")
                        + (f"; {replaced} previous row(s) replaced" if replaced else "")),
            "rows": len(out), "unlinked": unlinked,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── Efficiency → wages data (legacy WG150426/WGRPCALC.PRG) ──────────────────
#
# Applies the fortnight's efficiency results (ejm_fne_wages_eff_data, legacy
# WAGEREPO) to the collected attendance (tbl_ejm_wages_data_collection, legacy
# ATTTRANS), as WGRPCALC did:
#   eff of a row = efficiency / target efficiency x 100 of the matching
#       WAGEREPO rows — dept code (sub_dept_code) + the worked designation's
#       occu_code; first of: the hand's own rows for that shift, the occupation's
#       shift row, its fortnight row, any row of it; none (or occupation 55) = 100;
#       capped at 100 (legacy VOWDTRNS) — user choices 2026-10-04
#   working_hours_eff = working_hours x eff / 100 (effective hours);
#   ot_hours_eff = ot_hours and ns_hrs_eff = ns_hours as they are; act_eff = eff
#   prod_basic = production amount:
#       hands with their own prod_amount rows (weaving, winding, finishing workers)
#           → their prod_amount for that dept / occupation / shift, spread over
#             their attendance rows by (working + OT) hours
#       BEAMING / PRESS (machine rows, no hand) → the machine-shift prod_amount
#           shared by (working + OT) hours over EVERY hand's machine-wise
#           attendance on that machine and shift (legacy prdmchrs proportion)
#   time basic (WGRPCALC lines 1251-1313, rows with working hours):
#       amount = working_hours_eff x f_b_rate of ejm_piece_occupation_rate for the
#       row's dept_code + occu_code, else x 3.45192 (legacy fallback rate);
#       legacy placement — into time_basic when the row has a prod_basic,
#       otherwise the amount IS the row's prod_basic (time-rated hand)
# Re-runnable: the period + scheme's ATT / NSH rows are reset first.
EFF_APPLY_FULL = 100.0
EFF_APPLY_TIME_RATE_DEFAULT = 3.45192
EFF_APPLY_NO_EFF_OCCU = "55"
EFF_APPLY_MACHINE_DETAILS = ("BEAMING", "PRESS")

_EFF_APPLY_ROWS_SQL = text("""
    SELECT a.data_collection_id, a.eb_id, a.update_for, a.dept_code, g.occu_code,
           a.shift, a.mc_id, a.t_p, COALESCE(a.working_hours, 0) AS working_hours,
           COALESCE(a.ot_hours, 0) AS ot_hours, COALESCE(a.ns_hours, 0) AS ns_hours
    FROM tbl_ejm_wages_data_collection a
    LEFT JOIN designation_mst g ON g.designation_id = a.desig_id
    WHERE a.date_from = :date_from AND a.date_to = :date_to
      AND a.pay_scheme_id = :pay_scheme_id AND a.is_active = 1
      AND a.update_for IN ('ATT', 'NSH')
""")

_EFF_APPLY_EFF_SQL = text("""
    SELECT e.eb_id, e.deptcode, e.occucode, s.shift_name AS shift, e.details, e.mc_id,
           e.eff, e.tareff, e.quantity, e.target_qty, e.prod_amount
    FROM ejm_fne_wages_eff_data e
    LEFT JOIN shift_mst s ON s.shift_id = e.shift_id
    WHERE e.period_id = :period_id
""")

# Every hand's machine-wise attendance hours per machine / shift (all schemes):
# the denominator of the BEAMING / PRESS share.
_EFF_APPLY_MC_HOURS_SQL = text("""
    SELECT a.mc_id, a.shift, SUM(COALESCE(a.working_hours, 0) + COALESCE(a.ot_hours, 0)) AS hrs
    FROM tbl_ejm_wages_data_collection a
    WHERE a.date_from = :date_from AND a.date_to = :date_to AND a.is_active = 1
      AND a.update_for = 'ATT' AND a.mc_id IS NOT NULL AND TRIM(UPPER(a.t_p)) = 'P'
    GROUP BY a.mc_id, a.shift
""")


def _eff_ratio(rows: list[dict]):
    """Σ(eff / tareff x 100) weighted by target_qty (or a plain average when the
    rows carry none); None for no usable row. A missing / zero tareff reads 100."""
    pts = [(float(r["eff"]) * 100 / (float(r["tareff"] or 0) or 100), float(r["target_qty"] or 0))
           for r in rows if r["eff"] is not None]
    if not pts:
        return None
    w = sum(t for _, t in pts)
    return sum(e * t for e, t in pts) / w if w > 0 else sum(e for e, _ in pts) / len(pts)


def _eff_apply(att: list[dict], effs: list[dict], mc_hours: dict,
               time_rates: dict | None = None) -> list[dict]:
    """Per collection row: {data_collection_id, working_hours_eff, ot_hours_eff,
    ns_hrs_eff, act_eff, prod_basic, time_basic} — see the section comment.
    time_rates: (dept_code, occu_code) -> f_b_rate (ejm_piece_occupation_rate)."""
    time_rates = time_rates or {}
    by_eb, by_occ_shift, by_occ, mc_amount, eb_amount = {}, {}, {}, {}, {}
    for e in effs:
        key = (e["deptcode"], e["occucode"])
        if e["eb_id"]:
            by_eb.setdefault((e["eb_id"], *key, e["shift"]), []).append(e)
            if e["prod_amount"]:
                k = (e["eb_id"], *key, e["shift"])
                eb_amount[k] = eb_amount.get(k, 0) + float(e["prod_amount"])
        else:
            by_occ_shift.setdefault((*key, e["shift"]), []).append(e)
            if e["details"] in EFF_APPLY_MACHINE_DETAILS and e["mc_id"] and e["prod_amount"]:
                k = (e["mc_id"], e["shift"])
                mc_amount[k] = mc_amount.get(k, 0) + float(e["prod_amount"])
        by_occ.setdefault(key, []).append(e)

    # hours of each hand's rows per (eb, dept, occu, shift): the prod_amount spread
    eb_hours: dict[tuple, float] = {}
    for a in att:
        if a["update_for"] == "ATT" and (a.get("t_p") or "").strip().upper() == "P":
            k = (a["eb_id"], a["dept_code"], a["occu_code"], a["shift"])
            eb_hours[k] = eb_hours.get(k, 0) + float(a["working_hours"]) + float(a["ot_hours"])

    out = []
    for a in att:
        occ = (a["dept_code"], a["occu_code"])
        if a["occu_code"] == EFF_APPLY_NO_EFF_OCCU:
            eff = EFF_APPLY_FULL
        else:
            eff = next((v for v in (
                _eff_ratio(by_eb.get((a["eb_id"], *occ, a["shift"]), [])),
                _eff_ratio(by_occ_shift.get((*occ, a["shift"]), [])),
                _eff_ratio(by_occ_shift.get((*occ, None), [])),
                _eff_ratio(by_occ.get(occ, [])),
            ) if v is not None), EFF_APPLY_FULL)
        eff = round(min(eff, EFF_APPLY_FULL), 2)
        hrs = float(a["working_hours"]) + float(a["ot_hours"])
        prod_basic = 0.0
        piece = (a.get("t_p") or "").strip().upper() == "P"  # user 2026-10-05: prod_basic only for t_p = 'P'
        if piece and a["update_for"] == "ATT" and hrs > 0:
            k = (a["eb_id"], *occ, a["shift"])
            if eb_amount.get(k) and eb_hours.get(k):
                prod_basic += eb_amount[k] * hrs / eb_hours[k]
            mk = (a["mc_id"], a["shift"])
            if a["mc_id"] and mc_amount.get(mk) and mc_hours.get(mk):
                prod_basic += mc_amount[mk] * hrs / mc_hours[mk]
        working_hours_eff = round(float(a["working_hours"]) * eff / 100, 2)
        time_basic = 0.0
        if a["update_for"] == "ATT" and float(a["working_hours"]) > 0:
            rate = time_rates.get(occ, EFF_APPLY_TIME_RATE_DEFAULT)
            amount = working_hours_eff * rate
            if prod_basic > 0 or not piece:
                time_basic = amount
            else:
                prod_basic = amount  # legacy: a piece hand with no production gets the time amount as prod_basic
        out.append({
            "data_collection_id": a["data_collection_id"],
            "working_hours_eff": working_hours_eff,
            "ot_hours_eff": round(float(a["ot_hours"]), 2),
            "ns_hrs_eff": round(float(a["ns_hours"]), 3),
            "act_eff": eff,
            "prod_basic": round(prod_basic, 2),
            "time_basic": round(time_basic, 2),
        })
    return out


@router.post("/ejm_wages_eff_apply")
def ejm_wages_eff_apply(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Apply the fortnight's efficiency results to the collected wages data of
    one pay scheme (legacy WGRPCALC): working_hours_eff, ot_hours_eff,
    ns_hrs_eff, act_eff and prod_basic of its ATT / NSH rows.
    Body: { branch_id, date_from, date_to, pay_scheme_id } — the dates must be
    exactly a forthnight_period_mst fortnight; run after the efficiency processes.
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        pay_scheme_id = body.get("pay_scheme_id")
        if not branch_id or not date_from or not date_to or not pay_scheme_id:
            raise HTTPException(status_code=400,
                                detail="branch_id, date_from, date_to and pay_scheme_id are required")
        if int(branch_id) != EJM_CASH_BRANCH_ID:
            raise HTTPException(
                status_code=400,
                detail=f"Efficiency apply runs only for branch {EJM_CASH_BRANCH_ID} (EJM FACTORY)")
        period_id = _fortnight_id(db, date_from, date_to)
        params = {"date_from": date_from, "date_to": date_to, "pay_scheme_id": int(pay_scheme_id)}

        att = [dict(r._mapping) for r in db.execute(_EFF_APPLY_ROWS_SQL, params).fetchall()]
        effs = [dict(r._mapping) for r in db.execute(_EFF_APPLY_EFF_SQL, {"period_id": period_id}).fetchall()]
        mc_hours = {(r.mc_id, r.shift): float(r.hrs or 0)
                    for r in db.execute(_EFF_APPLY_MC_HOURS_SQL, params).fetchall()}
        time_rates = {(r.dept_code, r.occu_code): float(r.f_b_rate or 0)
                      for r in db.execute(text("""
                          SELECT dept_code, occu_code, MAX(f_b_rate) AS f_b_rate
                          FROM ejm_piece_occupation_rate GROUP BY dept_code, occu_code
                      """)).fetchall() if r.f_b_rate}
        updates = _eff_apply(att, effs, mc_hours, time_rates)
        if updates:
            db.execute(text("""
                UPDATE tbl_ejm_wages_data_collection
                   SET working_hours_eff = :working_hours_eff, ot_hours_eff = :ot_hours_eff,
                       ns_hrs_eff = :ns_hrs_eff, act_eff = :act_eff, prod_basic = :prod_basic,
                       time_basic = :time_basic
                 WHERE data_collection_id = :data_collection_id
            """), updates)
        db.commit()

        below = sum(1 for u in updates if u["act_eff"] < EFF_APPLY_FULL)
        return {"data": {
            "message": (f"Efficiency applied to {len(updates)} wages row(s) "
                        f"({below} below 100%), production basic "
                        f"{sum(u['prod_basic'] for u in updates):,.2f}, time basic "
                        f"{sum(u['time_basic'] for u in updates):,.2f}"
                        + ("" if effs else "; no efficiency data for this fortnight — every row at 100%")),
            "rows": len(updates),
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── EJM Spinning + weaving overall efficiency (legacy WG150426/SPGWVGEF.PRG) ─
#
# Legacy: weaving eff (wvg10495 actyds / actyds_as) and target (Σ actyds_as x
# quality target / Σ actyds_as), spinning eff (spg10495 prd / tarprd) and target
# (2-leg / boxter %), averaged by loom hours (wvg30495) and frame hours
# (spg20495) → effcode 45 (eff = aavtr, tareff = tavtr) and effcode 99 (100 / 100),
# one row per OCCUMAST occupation, details 'OVERALL'. This tenant:
#   weaving eff / target   → the loom process's overall code-13 row (Σ yards /
#                            Σ 100% yards, Σ target / Σ 100%) for the fortnight
#   spinning eff / target  → the spinning process's overall code-2 row
#   loom hours             → Σ working_hours of the fortnight's stored weaving rows
#                            (jute_prod_weaving_log, net of stoppage)
#   frame hours            → Σ (minutes / 60 - stoppage) of running spinning frames
# so this step runs after Spinning and Loom Efficiency.
OVERALL_DETAILS = "OVERALL"
OVERALL_DETCODE = ""
OVERALL_CODE = 45
OVERALL_FULL_CODE = 99

_OVERALL_LOOM_HOURS_SQL = text("""
    SELECT SUM(l.working_hours) FROM jute_prod_weaving_log l
    WHERE l.branch_id = :branch_id AND l.active = 1 AND l.std_prod_yds > 0
      AND l.tran_date BETWEEN :date_from AND :date_to
""")

_OVERALL_FRAME_HOURS_SQL = text("""
    SELECT SUM(GREATEST(s.minutes / 60 - COALESCE((
               SELECT SUM(st.stoppage_hours) FROM jute_prod_stoppage_hours st
                 JOIN spell_mst ss ON ss.spell_id = st.spell_id
                WHERE st.active = 1 AND st.machine_id = s.machine_id AND st.tran_date = s.tran_date
                  AND (ss.spell_code = sp.spell_code
                       OR (CHAR_LENGTH(sp.spell_code) = 1 AND LEFT(ss.spell_code, 1) = sp.spell_code))
           ), 0), 0))
    FROM jute_prod_spinning_daily s
    JOIN spell_mst sp ON sp.spell_id = s.spell_id
    WHERE s.branch_id = :branch_id AND s.active = 1
      AND s.tran_date BETWEEN :date_from AND :date_to
      AND s.act_prod_doff > 0 AND s.p100prod > 0
""")


def _overall_eff(weaving: tuple, spinning: tuple, loom_hrs: float, frame_hrs: float) -> tuple:
    """Legacy aavtr / tavtr: (eff, tareff) of weaving and spinning averaged by
    loom hours and frame hours."""
    total = loom_hrs + frame_hrs
    if total <= 0:
        return 0.0, 0.0
    return (round((weaving[0] * loom_hrs + spinning[0] * frame_hrs) / total, 2),
            round((weaving[1] * loom_hrs + spinning[1] * frame_hrs) / total, 2))


@router.post("/ejm_overall_eff_process")
def ejm_overall_eff_process(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Spinning + weaving overall efficiency (legacy SPGWVGEF) for one fortnight
    into ejm_fne_wages_eff_data (details 'OVERALL'): effcode 45 with eff / tareff
    = the loom- and frame-hour weighted spinning + weaving figures, effcode 99 at
    100 / 100 — one row per link_wages_occu_eff occupation of the code.
    Needs the fortnight's Spinning (code 2) and Loom (code 13) results.
    Body: { branch_id, date_from, date_to } — exactly a forthnight_period_mst fortnight.
    """
    try:
        body = parse_json_body(request)
        branch_id = body.get("branch_id")
        date_from = body.get("date_from")
        date_to = body.get("date_to")
        if not branch_id or not date_from or not date_to:
            raise HTTPException(status_code=400, detail="branch_id, date_from and date_to are required")
        if int(branch_id) != EJM_CASH_BRANCH_ID:
            raise HTTPException(
                status_code=400,
                detail=f"Overall efficiency runs only for branch {EJM_CASH_BRANCH_ID} (EJM FACTORY)")
        period_id = _fortnight_id(db, date_from, date_to)
        params = {"branch_id": int(branch_id), "date_from": date_from, "date_to": date_to}

        def result_of(details, code):
            r = db.execute(text("""
                SELECT eff, tareff FROM ejm_fne_wages_eff_data
                WHERE period_id = :period_id AND details = :details AND effcode = :code
                  AND eb_id IS NULL AND shift_id IS NULL
                ORDER BY fne_wages_eff_id LIMIT 1
            """), {"period_id": period_id, "details": details, "code": code}).fetchone()
            return (float(r.eff or 0), float(r.tareff or 0)) if r else None

        weaving = result_of(LOOM_DETAILS, 13)
        spinning = result_of(SPINNING_DETAILS, 2)
        missing = [n for n, v in (("Loom Efficiency (code 13)", weaving),
                                  ("Spinning Efficiency (code 2)", spinning)) if v is None]
        if missing:
            raise HTTPException(status_code=400, detail=(
                f"Run {' and '.join(missing)} for this fortnight first — the overall figure is built from them"))
        loom_hrs = float(db.execute(_OVERALL_LOOM_HOURS_SQL, params).scalar() or 0)
        frame_hrs = float(db.execute(_OVERALL_FRAME_HOURS_SQL, params).scalar() or 0)
        eff, tareff = _overall_eff(weaving, spinning, loom_hrs, frame_hrs)

        base = {"period_id": period_id, "details": OVERALL_DETAILS, "detcode": OVERALL_DETCODE,
                "eb_id": None, "shift_id": None, "name": None, "quantity": None}
        occus = _occu_by_code(db, (OVERALL_CODE, OVERALL_FULL_CODE))
        out = [{**base, "effcode": code, "eff": e, "tareff": t, "deptcode": d, "occucode": o}
               for code, e, t in ((OVERALL_CODE, eff, tareff), (OVERALL_FULL_CODE, 100, 100))
               for d, o in dict.fromkeys(occus.get(code, []))]  # designations sharing an occu_code
        replaced = _replace_eff_rows(db, base, out)
        db.commit()

        return {"data": {
            "message": (f"Overall efficiency {eff:.2f}% (target {tareff:.2f}%): weaving "
                        f"{weaving[0]:.2f}% over {loom_hrs:,.0f} loom hrs, spinning {spinning[0]:.2f}% over "
                        f"{frame_hrs:,.0f} frame hrs; {len(out)} row(s)"
                        + (f"; {replaced} previous row(s) replaced" if replaced else "")),
            "rows": len(out), "eff": eff, "tareff": tareff,
        }}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ─── OT Advance upload ──────────────────────────────────────────────────────
#
# Excel: column A = EB No, column B = OT Advance amount, first row a header.
# All-or-nothing: any bad row (unknown EB No, not Joined, no / several active
# pay schemes, bad amount, duplicate) rejects the whole file. Each row's pay
# scheme is the employee's own (pay_employee_payscheme), not the screen's.

def _parse_ot_advance_rows(rows):
    """[(eb_no, amount)] + error list from the sheet's rows (header skipped)."""
    parsed, errors, seen = [], [], set()
    for n, r in enumerate(rows[1:], start=2):
        eb_no = str(r[0]).strip() if r and r[0] is not None else ""
        raw = r[1] if r and len(r) > 1 else None
        if not eb_no and raw in (None, ""):
            continue
        if eb_no.endswith(".0"):  # Excel stores numeric EB Nos as floats
            eb_no = eb_no[:-2]
        if not eb_no:
            errors.append(f"Row {n}: EB No missing")
            continue
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            errors.append(f"Row {n}: EB No {eb_no} amount '{raw}' is not a number")
            continue
        if eb_no in seen:
            errors.append(f"Row {n}: EB No {eb_no} repeated")
            continue
        seen.add(eb_no)
        parsed.append((eb_no, amount))
    return parsed, errors


_OT_ADV_HANDS = text(f"""
    SELECT o.emp_code, o.eb_id, p.status_id,
           (SELECT GROUP_CONCAT(DISTINCT t.PAY_SCHEME_ID) FROM pay_employee_payscheme t
             WHERE t.EMPLOYEEID = o.eb_id AND t.STATUS = 1) AS schemes
    FROM hrms_ed_official_details o
    LEFT JOIN hrms_ed_personal_details p ON p.eb_id = o.eb_id
    WHERE o.active = 1 AND o.branch_id = :branch_id AND o.emp_code IN :codes
""").bindparams(bindparam("codes", expanding=True))


@router.post("/ejm_ot_advance_upload")
def ejm_ot_advance_upload(
    file: UploadFile = File(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    branch_id: int = Form(...),
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Replace the period's OT advance rows (update_for 'OTADV') for this
    branch with the uploaded file."""
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="To date cannot be earlier than From date")
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="File must be an Excel .xlsx")
    try:
        ws = load_workbook(BytesIO(file.file.read()), data_only=True, read_only=True).active
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read Excel file: {e}")

    parsed, errors = _parse_ot_advance_rows(rows)
    if not parsed and not errors:
        raise HTTPException(status_code=400, detail="No OT advance rows in the file")

    hands = {}
    if parsed:
        for r in db.execute(_OT_ADV_HANDS, {"branch_id": branch_id,
                                            "codes": [c for c, _ in parsed]}).mappings():
            hands.setdefault(r["emp_code"], []).append(r)
    inserts = []
    for eb_no, amount in parsed:
        found = hands.get(eb_no, [])
        if not found:
            errors.append(f"EB No {eb_no} not found in this branch")
        elif len(found) > 1:
            errors.append(f"EB No {eb_no} matches {len(found)} employees")
        elif found[0]["status_id"] != EMPLOYEE_LIFECYCLE_STATUS["JOINED"]:
            errors.append(f"EB No {eb_no} is not in Joined status")
        elif not found[0]["schemes"]:
            errors.append(f"EB No {eb_no} has no active pay scheme")
        elif "," in str(found[0]["schemes"]):
            errors.append(f"EB No {eb_no} has several active pay schemes ({found[0]['schemes']})")
        else:
            inserts.append({"eb_id": found[0]["eb_id"], "amount": amount,
                            "pay_scheme_id": int(found[0]["schemes"])})
    if errors:
        shown = errors[:20]
        more = f" … and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise HTTPException(status_code=400, detail="Nothing uploaded. " + "; ".join(shown) + more)

    params = {"date_from": date_from, "date_to": date_to, "branch_id": branch_id}
    try:
        cleared = db.execute(text("""
            DELETE a FROM tbl_ejm_wages_data_collection a
            JOIN hrms_ed_official_details o ON o.eb_id = a.eb_id AND o.active = 1
            WHERE a.update_for = 'OTADV' AND o.branch_id = :branch_id
              AND a.date_from = :date_from AND a.date_to = :date_to
        """), params).rowcount
        db.execute(text("""
            INSERT INTO tbl_ejm_wages_data_collection
                (date_from, date_to, eb_id, ot_advance, pay_scheme_id, payschme_id, update_for)
            VALUES (:date_from, :date_to, :eb_id, :amount, :pay_scheme_id, :pay_scheme_id, 'OTADV')
        """), [{**params, **r} for r in inserts])
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "message": f"{len(inserts)} OT advance record(s) uploaded"
                   + (f"; {cleared} previous record(s) replaced" if cleared else ""),
        "inserted": len(inserts), "cleared": cleared,
    }

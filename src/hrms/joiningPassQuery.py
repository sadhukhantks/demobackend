"""Raw SQL for the Joining Pass router (src/hrms/joiningPass.py).

Every query is a ``text()`` factory with named binds (listed in each docstring).

Reused rather than copied (Long Absenteeism, src/juteProduction/long_absenteeism_query.py):
  * the "seen" rule (active, non-rejected attendance + approved leave days) ->
    get_last_seen_query / get_last_attendance_query / _DA_SEEN / _LEAVE_HEADER_SEEN
  * worker snapshot                -> get_worker_snapshot_query
  * live Long Absenteeism case     -> get_live_case_query
  * company letterhead             -> long_absenteeism_letters_query.get_company_letterhead_query

hrms_ed_official_details keeps several rows per eb after a branch transfer, so the
row is always picked by a scalar MAX, never a bare join.
"""

from sqlalchemy import text

from src.hrms.reportQueries import _SEPARATED_STATUS_SQL
from src.juteProduction.long_absenteeism_query import _DA_SEEN, _LEAVE_HEADER_SEEN
from src.juteProduction.reportQueries import _WORKER_NAME

PASS_COLUMNS = """
    p.joining_pass_id, p.pass_no, p.co_id, p.branch_id, p.eb_id, p.emp_code, p.emp_name,
    p.dept_id, p.dept_desc, p.designation, p.last_seen_date, p.absent_from,
    p.absent_working_days, p.absent_calendar_days, p.join_date, p.issue_date, p.spell_id, p.spell_code, p.authority,
    p.fn_from, p.fn_to, p.fn_days_present, p.fn_working_days, p.fn_efficiency, p.fn_eff_dept,
    p.la_case_id, p.remarks, p.active, p.cancel_reason, p.cancelled_by, p.cancelled_date_time,
    p.issued_by, p.updated_date_time,
    NULLIF(TRIM(ui.name), '') AS issued_by_name,
    bm.branch_name
"""


def get_eb_by_code_query():
    """eb_id of the worker whose LATEST active official row is in the branch and
    carries this EB no (emp_code, case-insensitive). Binds: :branch_id, :emp_code.
    """
    return text(
        """
        SELECT od.eb_id
        FROM hrms_ed_official_details od
        WHERE od.branch_id = :branch_id AND od.active = 1
          AND UPPER(TRIM(od.emp_code)) = UPPER(TRIM(:emp_code))
          AND od.tbl_hrms_ed_official_detail_id = (
              SELECT MAX(o2.tbl_hrms_ed_official_detail_id)
              FROM hrms_ed_official_details o2
              WHERE o2.eb_id = od.eb_id AND o2.active = 1)
        ORDER BY od.tbl_hrms_ed_official_detail_id DESC
        LIMIT 1
        """
    )


def get_branch_spells_query():
    """Active spells of the branch (spell_mst through shift_mst.branch_id), with
    the shift letter. Binds: :branch_id."""
    return text(
        """
        SELECT sp.spell_id, sp.spell_code, sh.shift_name
        FROM spell_mst sp
        JOIN shift_mst sh ON sh.shift_id = sp.shift_id
        WHERE sh.branch_id = :branch_id AND COALESCE(sp.status, 1) = 1
        ORDER BY sp.spell_code
        """
    )


def get_off_days_query(with_non_working: bool = True):
    """Days the mill does not work in the branch between :d_from and :d_to: an
    approved holiday (holiday_master status 3) of the branch, or an active
    non-working day (tbl_non_working_days) of the company for the branch or for
    all branches (branch_id NULL). ``with_non_working=False`` for a tenant without
    tbl_non_working_days (dev3 on 2026-10-24). Binds: :co_id, :branch_id, :d_from, :d_to.
    """
    sql = """
        SELECT h.holiday_date AS off_date
        FROM holiday_master h
        WHERE h.branch_id = :branch_id AND h.status = 3
          AND h.holiday_date BETWEEN :d_from AND :d_to
    """
    if with_non_working:
        sql += """
        UNION
        SELECT n.non_working_date
        FROM tbl_non_working_days n
        WHERE n.company_id = :co_id AND n.is_active = 1
          AND (n.branch_id IS NULL OR n.branch_id = :branch_id)
          AND n.non_working_date BETWEEN :d_from AND :d_to
        """
    return text(sql)


def get_table_exists_query():
    """1 when the table exists in the current tenant DB. Binds: :table_name."""
    return text(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = :table_name"
    )


def get_presence_dates_query():
    """Distinct days the eb attended in the branch (active, non-rejected rows)
    between :d_from and :d_to. Binds: :eb_id, :branch_id, :d_from, :d_to.
    """
    return text(
        f"""
        SELECT DISTINCT da.attendance_date
        FROM daily_attendance da
        WHERE da.eb_id = :eb_id AND da.branch_id = :branch_id
          AND {_DA_SEEN}
          AND da.attendance_date BETWEEN :d_from AND :d_to
        """
    )


def get_pass_for_absence_query():
    """The active pass already issued for this absence (same eb, branch and last
    seen day). Binds: :eb_id, :branch_id, :last_seen_date.
    """
    return text(
        """
        SELECT joining_pass_id, pass_no, join_date, issue_date
        FROM hrms_joining_pass
        WHERE eb_id = :eb_id AND branch_id = :branch_id AND active = 1
          AND last_seen_date = :last_seen_date
        ORDER BY joining_pass_id DESC
        LIMIT 1
        """
    )


def get_pass_list_query():
    """Passes of the branch issued between :date_from and :date_to (issue date),
    newest first. :search (nullable) matches pass no, EB no or name (LIKE
    pattern). :include_cancelled 0 = active only. Binds: :co_id, :branch_id,
    :date_from, :date_to, :search, :include_cancelled.
    """
    return text(
        f"""
        SELECT {PASS_COLUMNS}
        FROM hrms_joining_pass p
        LEFT JOIN user_mst ui ON ui.user_id = p.issued_by
        LEFT JOIN branch_mst bm ON bm.branch_id = p.branch_id
        WHERE p.co_id = :co_id AND p.branch_id = :branch_id
          AND p.issue_date BETWEEN :date_from AND :date_to
          AND (:include_cancelled = 1 OR p.active = 1)
          AND (:search IS NULL OR p.pass_no LIKE :search OR p.emp_code LIKE :search OR p.emp_name LIKE :search)
        ORDER BY p.issue_date DESC, p.joining_pass_id DESC
        LIMIT 1000
        """
    )


def get_pass_query():
    """One pass by id within the company. Binds: :co_id, :joining_pass_id."""
    return text(
        f"""
        SELECT {PASS_COLUMNS}
        FROM hrms_joining_pass p
        LEFT JOIN user_mst ui ON ui.user_id = p.issued_by
        LEFT JOIN branch_mst bm ON bm.branch_id = p.branch_id
        WHERE p.co_id = :co_id AND p.joining_pass_id = :joining_pass_id
        """
    )


def get_absentees_query():
    """Workers on the roll of the branch last seen (attendance or approved leave
    day, the Long Absenteeism rule) between :from_date and :before_date — i.e.
    absent at least the threshold in CALENDAR days; the router then counts the
    working days and keeps those above the threshold. The active pass already
    issued for that absence (same last seen day), if any, comes along.
    ``:exclude_regex`` = the Long Absenteeism EB-prefix exclusion (outsiders),
    NULL = none. Binds: :co_id, :branch_id, :from_date, :before_date, :as_on,
    :exclude_regex, :dept_id (nullable).
    """
    return text(
        f"""
        WITH seen AS (
            SELECT g.eb_id, MAX(g.seen_date) AS last_seen
            FROM (
                SELECT da.eb_id, MAX(da.attendance_date) AS seen_date
                FROM daily_attendance da
                WHERE {_DA_SEEN}
                  AND da.branch_id = :branch_id
                  AND da.attendance_date >= :from_date AND da.attendance_date <= :as_on
                GROUP BY da.eb_id
                UNION ALL
                SELECT lt.eb_id, MAX(ltd.leave_date) AS seen_date
                FROM leave_tran_details ltd
                JOIN leave_transactions lt ON lt.leave_transaction_id = ltd.ltran_id
                WHERE ltd.is_active = 1 AND lt.status = 3 AND lt.branch_id = :branch_id
                  AND ltd.leave_date >= :from_date AND ltd.leave_date <= :as_on
                GROUP BY lt.eb_id
                UNION ALL
                SELECT lt.eb_id, MAX(LEAST(lt.leave_to_date, CAST(:as_on AS DATE))) AS seen_date
                FROM leave_transactions lt
                WHERE {_LEAVE_HEADER_SEEN} AND lt.leave_to_date >= :from_date
                GROUP BY lt.eb_id
            ) g
            GROUP BY g.eb_id
        )
        SELECT pd.eb_id,
               NULLIF(TRIM(od.emp_code), '') AS emp_code,
               NULLIF({_WORKER_NAME}, '') AS emp_name,
               dm.dept_id, dm.dept_desc,
               dsg.desig AS designation,
               cm.cata_desc AS category,
               seen.last_seen,
               jp.joining_pass_id, jp.pass_no, jp.join_date AS pass_join_date
        FROM seen
        JOIN hrms_ed_personal_details pd
             ON pd.eb_id = seen.eb_id AND pd.active = 1
            AND pd.status_id NOT IN ({_SEPARATED_STATUS_SQL})
        JOIN hrms_ed_official_details od
             ON od.eb_id = pd.eb_id AND od.active = 1
            AND od.branch_id = :branch_id
            AND od.tbl_hrms_ed_official_detail_id = (
                SELECT MAX(o2.tbl_hrms_ed_official_detail_id)
                FROM hrms_ed_official_details o2
                WHERE o2.eb_id = pd.eb_id AND o2.active = 1)
        LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = od.sub_dept_id
        LEFT JOIN dept_mst dm ON dm.dept_id = sd.dept_id
        LEFT JOIN designation_mst dsg ON dsg.designation_id = od.designation_id
        LEFT JOIN category_mst cm ON cm.cata_id = od.catagory_id
        LEFT JOIN hrms_joining_pass jp
             ON jp.joining_pass_id = (
                SELECT MAX(j2.joining_pass_id) FROM hrms_joining_pass j2
                WHERE j2.eb_id = pd.eb_id AND j2.branch_id = :branch_id
                  AND j2.active = 1 AND j2.last_seen_date = seen.last_seen)
        WHERE seen.last_seen < :before_date
          AND (:exclude_regex IS NULL OR od.emp_code IS NULL OR od.emp_code NOT REGEXP :exclude_regex)
          AND (:dept_id IS NULL OR dm.dept_id = :dept_id)
        ORDER BY seen.last_seen DESC, dm.dept_desc, od.emp_code
        """
    )

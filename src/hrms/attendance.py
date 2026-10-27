"""
HRMS Daily Attendance (transaction) API endpoints.

Backed by the `daily_attendance` table, scoped by `branch_id` (the branch
selected in the portal left sidebar). A record links an employee (eb_id,
validated by emp_code in hrms_ed_official_details *within the selected branch*)
to a spell / worked department (sub_dept_mst) / worked designation
(designation_mst) for a date, with worked / idle hours.

Machine assignments (optional) live in `daily_ebmc_attendance`
(daily_atten_id, eb_id, mc_id, branch_id, spell_id, is_active).

Endpoints (registered under /api/hrms):
    GET  /daily_attendance_list
    GET  /daily_attendance_by_id/{daily_atten_id}
    POST /daily_attendance_create
    PUT  /daily_attendance_edit/{daily_atten_id}
    PUT  /daily_attendance_approve/{daily_atten_id}
    PUT  /daily_attendance_reject/{daily_atten_id}
    GET  /attendance_create_setup
    GET  /attendance_machines_by_designation
    GET  /attendance_open_close_status
    POST /attendance_open_close
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.common.utils import parse_json_body

router = APIRouter()

# Status workflow IDs (kept in sync with the frontend STATUS_LABELS map).
STATUS_OPEN = 1
STATUS_PENDING_APPROVAL = 20
STATUS_APPROVED = 3
STATUS_REJECTED = 4
# Statuses from which a record can still be approved / rejected / edited.
PENDING_STATUSES = (1, 20, 21)
LOCKED_STATUSES = (STATUS_APPROVED, STATUS_REJECTED)

# A rejected row is a dead record: it must not block a re-entry for the same
# employee/date/spell, nor count toward the spell-hours cap. NULL status
# (legacy / bio-imported rows) still counts.
_NOT_REJECTED = f"AND COALESCE(CAST(status_id AS UNSIGNED), 0) <> {STATUS_REJECTED}"

# One active, non-rejected record per employee + date + spell. Shared by create
# and edit — edit passes its own id as :exclude_id so it never matches itself.
_DUP_ATTENDANCE_SQL = f"""
    SELECT 1 FROM daily_attendance
    WHERE eb_id = :eb_id AND attendance_date = :attendance_date
      AND COALESCE(spell_id, 0) = COALESCE(:spell_id, 0)
      AND is_active = 1
      AND daily_atten_id <> COALESCE(:exclude_id, 0)
      {_NOT_REJECTED}
    LIMIT 1
"""


# ─── SQL Queries ────────────────────────────────────────────────────


# FROM + WHERE shared by the list and its COUNT — the branch holds ~300k rows on
# live tenants, so the page slice must happen in SQL, never in Python.
_ATTENDANCE_LIST_FROM_WHERE = """
        FROM daily_attendance da
        LEFT JOIN hrms_ed_official_details o    ON o.eb_id = da.eb_id AND o.active = 1
        LEFT JOIN hrms_ed_personal_details  p   ON p.eb_id = da.eb_id
        LEFT JOIN sub_dept_mst    sd            ON sd.sub_dept_id = da.worked_department_id
        LEFT JOIN designation_mst des           ON des.designation_id = da.worked_designation_id
        LEFT JOIN spell_mst       sp            ON sp.spell_id = da.spell_id
        WHERE da.branch_id = :branch_id
          AND da.is_active = 1
          AND (:from_date IS NULL OR da.attendance_date >= :from_date)
          AND (:to_date IS NULL OR da.attendance_date <= :to_date)
          AND (:eb_no IS NULL OR o.emp_code = :eb_no)
          AND (:attendance_type IS NULL OR da.attendance_type = :attendance_type)
          AND (:status_id IS NULL OR CAST(da.status_id AS UNSIGNED) = :status_id)
          AND (:search IS NULL
               OR p.first_name LIKE :search
               OR p.last_name LIKE :search
               OR o.emp_code LIKE :search)
"""


def get_daily_attendance_count_query():
    return text("SELECT COUNT(*) AS total" + _ATTENDANCE_LIST_FROM_WHERE)


def get_daily_attendance_list_query():
    return text("""
        SELECT
            da.daily_atten_id,
            da.daily_atten_id                                AS id,
            da.eb_id,
            COALESCE(o.emp_code, '')                         AS eb_no,
            TRIM(CONCAT(
                COALESCE(p.first_name, ''), ' ',
                COALESCE(p.middle_name, ''), ' ',
                COALESCE(p.last_name, '')))                  AS worker_name,
            da.attendance_date,
            da.attendance_mark,
            da.attendance_source,
            da.attendance_type,
            COALESCE(sp.spell_name, da.spell, '')            AS spell,
            COALESCE(sd.sub_dept_desc, '')                   AS worked_department,
            da.worked_department_id,
            COALESCE(des.desig, '')                          AS worked_designation,
            da.worked_designation_id,
            COALESCE(da.spell_hours, 0)                      AS spell_hours,
            COALESCE(da.working_hours, 0)                    AS working_hours,
            COALESCE(da.idle_hours, 0)                       AS idle_hours,
            TIME_FORMAT(da.entry_time, '%H:%i')              AS entry_time,
            TIME_FORMAT(da.exit_time, '%H:%i')               AS exit_time,
            CAST(da.status_id AS UNSIGNED)                   AS status_id
""" + _ATTENDANCE_LIST_FROM_WHERE + """
        ORDER BY da.attendance_date DESC, da.daily_atten_id DESC
        LIMIT :limit OFFSET :offset
    """)


def get_daily_attendance_by_id_query():
    # entry/exit come back as 'HH:MM' STRINGS, not bare TIME: pymysql maps a
    # MySQL TIME to a timedelta, which FastAPI then serialises as a NUMBER of
    # seconds — the edit form binds these straight into <input type="time">
    # and its schema rightly rejects 21600. 'HH:MM' also round-trips through
    # _combine_datetime on the way back in.
    return text("""
        SELECT
            da.daily_atten_id,
            da.eb_id,
            da.branch_id,
            COALESCE(o.emp_code, '')                         AS eb_no,
            TRIM(CONCAT(
                COALESCE(p.first_name, ''), ' ',
                COALESCE(p.middle_name, ''), ' ',
                COALESCE(p.last_name, '')))                  AS worker_name,
            da.attendance_date,
            da.attendance_mark,
            da.attendance_source,
            da.attendance_type,
            COALESCE(sp.spell_name, da.spell, '')            AS spell,
            da.spell_id,
            da.spell_hours,
            da.worked_department_id,
            COALESCE(sd.sub_dept_desc, '')                   AS worked_department,
            da.worked_designation_id,
            COALESCE(des.desig, '')                          AS worked_designation,
            da.working_hours,
            da.idle_hours,
            TIME_FORMAT(da.entry_time, '%H:%i')              AS entry_time,
            TIME_FORMAT(da.exit_time, '%H:%i')               AS exit_time,
            da.remarks,
            da.geo_location,
            CAST(da.status_id AS UNSIGNED)                   AS status_id
        FROM daily_attendance da
        LEFT JOIN hrms_ed_official_details o    ON o.eb_id = da.eb_id AND o.active = 1
        LEFT JOIN hrms_ed_personal_details  p   ON p.eb_id = da.eb_id
        LEFT JOIN sub_dept_mst    sd            ON sd.sub_dept_id = da.worked_department_id
        LEFT JOIN designation_mst des           ON des.designation_id = da.worked_designation_id
        LEFT JOIN spell_mst       sp            ON sp.spell_id = da.spell_id
        WHERE da.daily_atten_id = :daily_atten_id
    """)


def get_machines_for_attendance_query():
    return text("""
        SELECT dea.mc_id,
               COALESCE(m.machine_name, m.mech_code, '')     AS machine_name
        FROM daily_ebmc_attendance dea
        LEFT JOIN machine_mst m ON m.machine_id = dea.mc_id
        WHERE dea.daily_atten_id = :daily_atten_id AND dea.is_active = 1
        ORDER BY m.machine_name
    """)


def get_attendance_setup_query():
    """Spells + sub-departments for the Mark Attendance form, scoped to a branch."""
    return text("""
        SELECT 'spells' AS source,
               sp.spell_id                                   AS id,
               sp.spell_name                                 AS name,
               TIME_FORMAT(sp.starting_time, '%H:%i')        AS extra1,
               TIME_FORMAT(sp.end_time, '%H:%i')             AS extra2,
               CAST(COALESCE(sp.working_hours, 0) AS CHAR)   AS extra3
        FROM spell_mst sp
        LEFT JOIN shift_mst sh ON sh.shift_id = sp.shift_id
        WHERE (:branch_id IS NULL OR sh.branch_id = :branch_id)
        UNION ALL
        SELECT 'sub_departments',
               sd.sub_dept_id,
               CONCAT(sd.sub_dept_desc, ' (', COALESCE(d.dept_desc, ''), ')'),
               NULL, NULL, NULL
        FROM sub_dept_mst sd
        LEFT JOIN dept_mst d ON d.dept_id = sd.dept_id
        WHERE (:branch_id IS NULL OR d.branch_id = :branch_id)
    """)


# Columns an edit may NOT touch. Mark and source identify WHICH attendance a
# row is and how it was captured, so they are fixed at creation and the form
# disables them. Kept as a named set so
# src/test/test_attendance_edit_immutable.py can hold the rule to the SQL.
ATTENDANCE_EDIT_IMMUTABLE_COLUMNS = (
    "attendance_mark",
    "attendance_source",
)


def get_daily_attendance_edit_query():
    """Update the correctable fields of one attendance row.

    Deliberately does not SET any of ATTENDANCE_EDIT_IMMUTABLE_COLUMNS.
    """
    return text("""
        UPDATE daily_attendance
        SET attendance_type       = :attendance_type,
            spell_id              = :spell_id,
            spell_hours           = :spell_hours,
            worked_department_id  = :worked_department_id,
            worked_designation_id = :worked_designation_id,
            working_hours         = :working_hours,
            idle_hours            = :idle_hours,
            remarks               = :remarks,
            entry_time            = :entry_time,
            exit_time             = :exit_time,
            update_date_time      = :now
        WHERE daily_atten_id = :id
    """)


def get_leave_on_date_query():
    """One row (with its leave-type `payable` flag) if the employee is on
    leave on the given date, per the `vw_leave_dates` view."""
    return text("""
        SELECT payable
        FROM vw_leave_dates
        WHERE eb_id = :eb_id AND leave_date = :attendance_date
        LIMIT 1
    """)


def get_machines_by_designation_branch_query():
    """Machines linked to a designation (occupation) via mech_occu_link."""
    return text("""
        SELECT m.machine_id                                  AS mc_id,
               COALESCE(m.machine_name, m.mech_code, '')     AS machine_name
        FROM mech_occu_link mol
        JOIN machine_mst m ON m.machine_id = mol.mech_id
        WHERE mol.occu_id = :designation_id
          AND (m.active = 1 OR m.active IS NULL)
        ORDER BY m.machine_name
    """)


# ─── Helpers ────────────────────────────────────────────────────────


def _require_branch_id(request: Request) -> int:
    branch_id = request.query_params.get("branch_id")
    if not branch_id:
        raise HTTPException(status_code=400, detail="branch_id is required")
    return int(branch_id)


def _combine_datetime(att_date, time_str):
    """Build a 'YYYY-MM-DD HH:MM:SS' string from a date + 'HH:MM' time, or None."""
    if not att_date or not time_str:
        return None
    t = str(time_str).strip()
    if len(t) == 5:  # HH:MM
        t = f"{t}:00"
    return f"{att_date} {t}"


def _sync_machines(db: Session, daily_atten_id: int, eb_id: int, branch_id: int, machine_ids,
                   spell_id=None, attendance_date=None, designation_id=None):
    """Deactivate existing machine rows, then insert the supplied selection.

    attendace_date (sic) and designation_id are denormalised copies of the
    header — the man-machine deployment report filters/groups on them directly
    instead of joining back to daily_attendance.
    """
    db.execute(
        text("UPDATE daily_ebmc_attendance SET is_active = 0 WHERE daily_atten_id = :id"),
        {"id": daily_atten_id},
    )
    for mc_id in machine_ids or []:
        if mc_id in (None, ""):
            continue
        db.execute(text("""
            INSERT INTO daily_ebmc_attendance
                (daily_atten_id, eb_id, mc_id, branch_id, spell_id, attendace_date,
                 designation_id, is_active, updated_date_time)
            VALUES (:daily_atten_id, :eb_id, :mc_id, :branch_id, :spell_id, :attendace_date,
                    :designation_id, 1, :now)
        """), {
            "daily_atten_id": daily_atten_id,
            "eb_id": eb_id,
            "mc_id": int(mc_id),
            "branch_id": branch_id,
            "spell_id": spell_id,
            "attendace_date": attendance_date,
            "designation_id": designation_id,
            "now": datetime.now(),
        })


def _check_leave_conflict(db: Session, eb_id, attendance_date, attendance_type):
    """Block attendance that collides with a leave on the same date.

    `vw_leave_dates` has one row per employee per leave date. Rule:
      - payable = 'Y' (paid leave): the day is already paid, so only Cash / OT
        attendance is allowed — a Regular ('R') mark would double-pay the day.
      - otherwise (unpaid leave): no attendance may be saved for that date.
    """
    if not eb_id or not attendance_date:
        return
    row = db.execute(
        get_leave_on_date_query(),
        {"eb_id": int(eb_id), "attendance_date": attendance_date},
    ).fetchone()
    if not row:
        return
    payable = str(row.payable or "").strip().upper()
    att_type = str(attendance_type or "R").strip().upper()
    if payable == "Y":
        if att_type == "R":
            raise HTTPException(
                status_code=400,
                detail="Employee is on paid leave on this date — only Cash or "
                       "Over Time attendance is allowed, not Regular",
            )
    else:
        raise HTTPException(
            status_code=400,
            detail="Employee is on leave on this date — attendance cannot be saved",
        )


def _validate_attendance_payload(body: dict):
    working_hours = float(body.get("working_hours") or 0)
    idle_hours = float(body.get("idle_hours") or 0)
    if working_hours <= 0:
        raise HTTPException(status_code=400, detail="Working hours must be greater than 0")
    if idle_hours < 0:
        raise HTTPException(status_code=400, detail="Idle hours cannot be negative")
    if idle_hours > working_hours:
        raise HTTPException(status_code=400, detail="Idle hours cannot exceed working hours")


def _check_spell_hours_cap(db: Session, eb_id, attendance_date, spell_id,
                           spell_hours, working_hours, idle_hours,
                           exclude_atten_id=None):
    """Validate the submitted spell_id against spell_mst, then block the save
    when the employee's net hours (working - idle) for the same date + spell —
    existing active rows plus this entry — would exceed the spell's working
    hours. Mirrors the mobile app rule
    (mobileapp attendance._spell_hours_conflict). exclude_atten_id skips the
    row being edited so an update doesn't count itself."""
    if not spell_id or not attendance_date:
        return
    cap_row = db.execute(
        text("SELECT working_hours FROM spell_mst WHERE spell_id = :spell_id"),
        {"spell_id": int(spell_id)},
    ).fetchone()
    if not cap_row:
        raise HTTPException(status_code=400, detail="Invalid spell selected")
    cap = float(cap_row.working_hours or spell_hours or 0)
    if cap <= 0:
        return
    new_net = float(working_hours or 0) - float(idle_hours or 0)
    sql = f"""
        SELECT COALESCE(SUM(COALESCE(working_hours, 0) - COALESCE(idle_hours, 0)), 0) AS worked
        FROM daily_attendance
        WHERE eb_id = :eb_id AND attendance_date = :attendance_date
          AND spell_id = :spell_id AND is_active = 1
          {_NOT_REJECTED}
    """
    params = {"eb_id": int(eb_id), "attendance_date": attendance_date,
              "spell_id": int(spell_id)}
    if exclude_atten_id is not None:
        sql += " AND daily_atten_id <> :exclude_id"
        params["exclude_id"] = int(exclude_atten_id)
    already = float(db.execute(text(sql), params).fetchone().worked or 0)
    if already + new_net > cap:
        raise HTTPException(
            status_code=400,
            detail=f"Worked hours for this date/spell would total {already + new_net:g} "
                   f"({already:g} already saved + {new_net:g} new) — more than the "
                   f"spell hours ({cap:g})",
        )


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/daily_attendance_list")
def daily_attendance_list(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated list of attendance records for the selected branch."""
    try:
        branch_id = _require_branch_id(request)

        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")
        eb_no = request.query_params.get("eb_no")
        attendance_type = request.query_params.get("attendance_type")
        status_id = request.query_params.get("status_id")
        search = request.query_params.get("search")

        page = max(1, int(request.query_params.get("page", 1)))
        page_size = max(1, min(200, int(request.query_params.get("page_size", 20))))

        filters = {
            "branch_id": branch_id,
            "from_date": from_date or None,
            "to_date": to_date or None,
            "eb_no": eb_no or None,
            "attendance_type": attendance_type or None,
            "status_id": int(status_id) if status_id else None,
            "search": f"%{search}%" if search else None,
        }
        total = db.execute(get_daily_attendance_count_query(), filters).scalar() or 0
        rows = db.execute(get_daily_attendance_list_query(), {
            **filters,
            "limit": page_size,
            "offset": (page - 1) * page_size,
        }).fetchall()

        return {
            "data": [dict(r._mapping) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/daily_attendance_by_id/{daily_atten_id}")
def daily_attendance_by_id(
    daily_atten_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Fetch a single attendance record with its machine list + action flags."""
    try:
        row = db.execute(get_daily_attendance_by_id_query(), {
            "daily_atten_id": daily_atten_id,
        }).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Attendance record not found")

        header = dict(row._mapping)
        machines = db.execute(get_machines_for_attendance_query(), {
            "daily_atten_id": daily_atten_id,
        }).fetchall()
        status = int(header.get("status_id") or 0)
        can_act = status in PENDING_STATUSES

        return {
            "data": {
                "header": header,
                "machines": [dict(m._mapping) for m in machines],
                "approve_button": can_act,
                "reject_button": can_act,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/daily_attendance_create")
def daily_attendance_create(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create (mark) a new attendance record under the selected branch."""
    try:
        body = parse_json_body(request)

        branch_id = body.get("branch_id")
        eb_id = body.get("eb_id")
        attendance_date = body.get("attendance_date")

        if not branch_id:
            raise HTTPException(status_code=400, detail="branch_id is required")
        if not eb_id:
            raise HTTPException(status_code=400, detail="A valid employee (eb_id) is required")
        if not attendance_date:
            raise HTTPException(status_code=400, detail="Attendance date is required")
        _validate_attendance_payload(body)

        # The employee must belong to the selected branch.
        belongs = db.execute(
            text("""SELECT 1 FROM hrms_ed_official_details
                    WHERE eb_id = :eb_id AND branch_id = :branch_id AND active = 1 LIMIT 1"""),
            {"eb_id": int(eb_id), "branch_id": int(branch_id)},
        ).fetchone()
        if not belongs:
            raise HTTPException(
                status_code=400,
                detail="Employee does not belong to the selected branch",
            )

        _check_leave_conflict(
            db, int(eb_id), attendance_date, body.get("attendance_type") or "R"
        )

        spell_id = int(body["spell_id"]) if body.get("spell_id") else None
        dup = db.execute(text(_DUP_ATTENDANCE_SQL), {
            "eb_id": int(eb_id),
            "attendance_date": attendance_date,
            "spell_id": spell_id,
            "exclude_id": None,
        }).fetchone()
        if dup:
            raise HTTPException(
                status_code=400,
                detail="Attendance already exists for this employee, date and spell",
            )

        _check_spell_hours_cap(
            db, int(eb_id), attendance_date, spell_id,
            body.get("spell_hours"),
            body.get("working_hours"), body.get("idle_hours"),
        )

        result = db.execute(text("""
            INSERT INTO daily_attendance
                (eb_id, attendance_date, attendance_source, attendance_type,
                 attendance_mark, is_active, branch_id, spell_id, spell_hours,
                 worked_department_id, worked_designation_id, status_id,
                 working_hours, idle_hours, remarks, geo_location,
                 entry_time, exit_time, update_date_time)
            VALUES
                (:eb_id, :attendance_date, :attendance_source, :attendance_type,
                 :attendance_mark, 1, :branch_id, :spell_id, :spell_hours,
                 :worked_department_id, :worked_designation_id, :status_id,
                 :working_hours, :idle_hours, :remarks, :geo_location,
                 :entry_time, :exit_time, :now)
        """), {
            "eb_id": int(eb_id),
            "attendance_date": attendance_date,
            "attendance_source": body.get("attendance_source") or "M",
            "attendance_type": body.get("attendance_type") or "R",
            "attendance_mark": body.get("attendance_mark") or "P",
            "branch_id": int(branch_id),
            "spell_id": spell_id,
            "spell_hours": float(body.get("spell_hours") or 0),
            "worked_department_id": int(body["worked_department_id"]) if body.get("worked_department_id") else None,
            "worked_designation_id": int(body["worked_designation_id"]) if body.get("worked_designation_id") else None,
            "status_id": str(STATUS_PENDING_APPROVAL),
            "working_hours": float(body.get("working_hours") or 0),
            "idle_hours": float(body.get("idle_hours") or 0),
            "remarks": body.get("remarks") or None,
            "geo_location": body.get("geo_location") or None,
            "entry_time": _combine_datetime(attendance_date, body.get("entry_time")),
            "exit_time": _combine_datetime(attendance_date, body.get("exit_time")),
            "now": datetime.now(),
        })
        new_id = result.lastrowid
        _sync_machines(
            db, new_id, int(eb_id), int(branch_id), body.get("machine_ids"), spell_id,
            attendance_date,
            int(body["worked_designation_id"]) if body.get("worked_designation_id") else None,
        )
        db.commit()

        return {"message": "Attendance marked successfully", "daily_atten_id": new_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/daily_attendance_edit/{daily_atten_id}")
def daily_attendance_edit(
    daily_atten_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Update an existing (not yet approved/rejected) attendance record.

    Attendance mark and attendance source are fixed at creation — they identify
    WHICH attendance this row is and how it was captured — so they are read
    from the stored row and never from the body: the form disables them, and
    this ignores them regardless. The spell IS editable (a wrong spell is the
    common correction), and moving it re-runs the same duplicate and
    spell-hours-cap checks as create.
    """
    try:
        body = parse_json_body(request)

        existing = db.execute(
            text("""SELECT eb_id, branch_id, attendance_date, spell_id, spell_hours,
                           CAST(status_id AS UNSIGNED) AS status_id
                    FROM daily_attendance WHERE daily_atten_id = :id"""),
            {"id": daily_atten_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Attendance record not found")
        if int(existing.status_id or 0) in LOCKED_STATUSES:
            raise HTTPException(
                status_code=400,
                detail="An approved or rejected attendance record cannot be edited",
            )
        _validate_attendance_payload(body)

        attendance_date = body.get("attendance_date")
        _check_leave_conflict(
            db,
            int(existing.eb_id),
            attendance_date or existing.attendance_date,
            body.get("attendance_type") or "R",
        )
        # Spell may be moved by an edit; fall back to the stored one when the
        # body omits it. Everything downstream keys off the SUBMITTED spell.
        spell_id = int(body["spell_id"]) if body.get("spell_id") else (
            int(existing.spell_id) if existing.spell_id else None
        )
        spell_hours = float(body.get("spell_hours") or existing.spell_hours or 0)

        # Same one-row-per-employee/date/spell rule as create — moving a row
        # onto a spell that already has one would otherwise duplicate it.
        dup = db.execute(text(_DUP_ATTENDANCE_SQL), {
            "eb_id": int(existing.eb_id),
            "attendance_date": attendance_date or existing.attendance_date,
            "spell_id": spell_id,
            "exclude_id": daily_atten_id,
        }).fetchone()
        if dup:
            raise HTTPException(
                status_code=400,
                detail="Attendance already exists for this employee, date and spell",
            )

        _check_spell_hours_cap(
            db, int(existing.eb_id),
            attendance_date or existing.attendance_date,
            spell_id,
            spell_hours,
            body.get("working_hours"), body.get("idle_hours"),
            exclude_atten_id=daily_atten_id,
        )
        db.execute(get_daily_attendance_edit_query(), {
            "id": daily_atten_id,
            "attendance_type": body.get("attendance_type") or "R",
            "spell_id": spell_id,
            "spell_hours": spell_hours,
            "worked_department_id": int(body["worked_department_id"]) if body.get("worked_department_id") else None,
            "worked_designation_id": int(body["worked_designation_id"]) if body.get("worked_designation_id") else None,
            "working_hours": float(body.get("working_hours") or 0),
            "idle_hours": float(body.get("idle_hours") or 0),
            "remarks": body.get("remarks") or None,
            "entry_time": _combine_datetime(attendance_date, body.get("entry_time")),
            "exit_time": _combine_datetime(attendance_date, body.get("exit_time")),
            "now": datetime.now(),
        })
        _sync_machines(
            db, daily_atten_id, int(existing.eb_id), int(existing.branch_id),
            body.get("machine_ids"),
            spell_id,
            attendance_date or existing.attendance_date,
            int(body["worked_designation_id"]) if body.get("worked_designation_id") else None,
        )
        db.commit()

        return {"message": "Attendance updated successfully", "daily_atten_id": daily_atten_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


def _set_status(db: Session, daily_atten_id: int, new_status: int, action: str):
    existing = db.execute(
        text("""SELECT CAST(status_id AS UNSIGNED) AS status_id
                FROM daily_attendance WHERE daily_atten_id = :id"""),
        {"id": daily_atten_id},
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    if int(existing.status_id or 0) in LOCKED_STATUSES:
        raise HTTPException(status_code=400, detail=f"Attendance has already been {action}")

    db.execute(text("""
        UPDATE daily_attendance
        SET status_id = :status_id, update_date_time = :now
        WHERE daily_atten_id = :id
    """), {"id": daily_atten_id, "status_id": str(new_status), "now": datetime.now()})
    db.commit()


@router.put("/daily_attendance_approve/{daily_atten_id}")
def daily_attendance_approve(
    daily_atten_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Approve an attendance record."""
    try:
        _set_status(db, daily_atten_id, STATUS_APPROVED, "processed")
        return {"message": "Attendance approved", "daily_atten_id": daily_atten_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/daily_attendance_reject/{daily_atten_id}")
def daily_attendance_reject(
    daily_atten_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Reject an attendance record."""
    try:
        _set_status(db, daily_atten_id, STATUS_REJECTED, "processed")
        return {"message": "Attendance rejected", "daily_atten_id": daily_atten_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/attendance_create_setup")
def attendance_create_setup(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Dropdown master data for the Mark Attendance form (spells, sub-departments)."""
    try:
        branch_id = request.query_params.get("branch_id")
        rows = db.execute(get_attendance_setup_query(), {
            "branch_id": int(branch_id) if branch_id else None,
        }).fetchall()

        spells = []
        sub_departments = []
        for r in rows:
            m = r._mapping
            if m["source"] == "spells":
                spells.append({
                    "spell_id": int(m["id"]),
                    "spell_name": m["name"],
                    "starting_time": m["extra1"],
                    "end_time": m["extra2"],
                    "working_hours": float(m["extra3"]) if m["extra3"] else 0,
                })
            else:
                sub_departments.append({"sub_dept_id": int(m["id"]), "sub_dept_desc": m["name"]})

        return {"data": {"spells": spells, "sub_departments": sub_departments}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/attendance_leave_status")
async def attendance_leave_status(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Is the employee on leave on the given date? Drives the create form's
    leave check (from `vw_leave_dates`). Returns on_leave + payable ('Y'/'N')."""
    try:
        eb_id = request.query_params.get("eb_id")
        attendance_date = request.query_params.get("attendance_date")
        if not eb_id or not attendance_date:
            return {"data": {"on_leave": False, "payable": None}}
        row = db.execute(
            get_leave_on_date_query(),
            {"eb_id": int(eb_id), "attendance_date": attendance_date},
        ).fetchone()
        if not row:
            return {"data": {"on_leave": False, "payable": None}}
        payable = str(row.payable or "").strip().upper() or None
        return {"data": {"on_leave": True, "payable": payable}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/attendance_machines_by_designation")
def attendance_machines_by_designation(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Machines linked to a designation (via mech_occu_link)."""
    try:
        designation_id = request.query_params.get("designation_id")
        if not designation_id:
            return {"data": []}
        rows = db.execute(get_machines_by_designation_branch_query(), {
            "designation_id": int(designation_id),
        }).fetchall()
        return {"data": [dict(r._mapping) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Attendance Open / Close ────────────────────────────────────────
#
# Attendance lands Closed (status 3 — every writer inserts '3', see
# src/mobileapp/src/attendance/query.py). To let a department correct a day the
# whole (branch, date, spell, department) slice is re-opened to 1, and closed
# back to 3 once the corrections are in. Nothing else moves, so records sitting
# in any other status (rejected, pending) are untouched by both directions.
#
# Re-opening is time-boxed: a record can only go back to Open within
# OPEN_WINDOW_HOURS of when it was CREATED. Closing has no such limit — the way
# back to a locked day is always available. The cutoff is evaluated as
# `NOW() - INTERVAL ... HOUR` in SQL, i.e. entirely in DB time: the tenant DB is
# remote, so comparing a DB `created_date_time` against an app-server clock
# would silently skew the window whenever the two drift or sit in different
# timezones.
OPEN_WINDOW_HOURS = 96

# `spell` matches on spell_id when the row carries one and on the legacy spell
# NAME string otherwise — older rows were written before spell_id existed and
# the backfill left the ambiguous ones NULL (see
# dbqueries/migrations/add_daily_attendance_spell_id.sql).
_SLICE_DAY_WHERE = """
    da.branch_id = :branch_id
      AND da.attendance_date = :attendance_date
      AND da.is_active = 1
"""

_SLICE_SPELL_WHERE = """
      AND (da.spell_id = :spell_id
           OR (da.spell_id IS NULL AND da.spell = :spell_name))
"""

# worked_department_id = 0 means "every department of the spell", including the
# rows with no worked_department_id at all — those are unreachable one department
# at a time, so an all-departments close is the only thing that can lock them.
_SLICE_DEPT_WHERE = """
      AND (:worked_department_id = 0
           OR da.worked_department_id = :worked_department_id)
"""


def get_attendance_open_close_status_query():
    """Open/closed roll-up per (spell, department) for one branch + date.

    Deliberately NOT spell-filtered: the page's Spell and Department dropdowns
    are both built from this result, so they can only ever offer spells and
    departments that actually marked attendance that day. One query feeds both
    lists and the grid.

    Grouped by the spell LABEL rather than spell_id, because a spell can appear
    both as an id and as a bare name string on the same day (older rows predate
    spell_id). `spell_id` comes back as MIN over the group — any resolved id
    seen for that label — and is NULL only when every row of the group is
    unresolved, which is exactly the ambiguous case the backfill refused to
    guess at.
    """
    return text(f"""
        SELECT MIN(da.spell_id)                               AS spell_id,
               COALESCE(sp.spell_name, da.spell, '')          AS spell_name,
               da.worked_department_id                        AS dept_id,
               COALESCE(sd.sub_dept_desc, '')                 AS dept_name,
               SUM(CASE WHEN CAST(da.status_id AS UNSIGNED) = {STATUS_OPEN}
                        THEN 1 ELSE 0 END)                    AS open_count,
               COUNT(*)                                       AS total_count
        FROM daily_attendance da
        LEFT JOIN sub_dept_mst sd ON sd.sub_dept_id = da.worked_department_id
        LEFT JOIN spell_mst    sp ON sp.spell_id = da.spell_id
        WHERE {_SLICE_DAY_WHERE}
        GROUP BY COALESCE(sp.spell_name, da.spell, ''),
                 da.worked_department_id,
                 COALESCE(sd.sub_dept_desc, '')
        ORDER BY spell_name, dept_name
    """)


def get_attendance_open_close_update_query():
    """Flip every row of the slice that sits in :from_status over to :to_status.

    :enforce_open_window = 1 additionally caps the flip to rows created within
    the last OPEN_WINDOW_HOURS — passed only for the open direction.
    """
    return text(f"""
        UPDATE daily_attendance da
        SET da.status_id = :to_status,
            da.update_date_time = :now,
            da.updated_by = :updated_by
        WHERE {_SLICE_DAY_WHERE} {_SLICE_SPELL_WHERE} {_SLICE_DEPT_WHERE}
          AND CAST(da.status_id AS UNSIGNED) = :from_status
          AND (:enforce_open_window = 0
               OR da.created_date_time >= NOW() - INTERVAL {OPEN_WINDOW_HOURS} HOUR)
    """)


def get_attendance_open_close_stuck_query():
    """Rows of the slice still sitting in :from_status after an update ran.

    For an open, that is exactly the set the {OPEN_WINDOW_HOURS}-hour window
    held back — it lets the caller be told what did NOT move instead of
    reporting a silent partial success.
    """
    return text(f"""
        SELECT COUNT(*) AS stuck
        FROM daily_attendance da
        WHERE {_SLICE_DAY_WHERE} {_SLICE_SPELL_WHERE} {_SLICE_DEPT_WHERE}
          AND CAST(da.status_id AS UNSIGNED) = :from_status
    """)


def _slice_day_params(source) -> dict:
    """branch_id / attendance_date, from a query-string or a JSON body."""
    branch_id = source.get("branch_id")
    attendance_date = source.get("attendance_date")
    if not branch_id or not attendance_date:
        raise HTTPException(
            status_code=400,
            detail="branch_id and attendance_date are required",
        )
    return {
        "branch_id": int(branch_id),
        "attendance_date": str(attendance_date),
    }


def _slice_spell_params(source) -> dict:
    """The day params plus the spell keys the update is scoped by. spell_name is
    optional — it only widens the match to rows that never got a spell_id."""
    spell_id = source.get("spell_id")
    if not spell_id:
        raise HTTPException(status_code=400, detail="spell_id is required")
    return {
        **_slice_day_params(source),
        "spell_id": int(spell_id),
        "spell_name": source.get("spell_name") or None,
    }


@router.get("/attendance_open_close_status")
def attendance_open_close_status(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Every (spell, department) that has attendance on one branch + date, with
    its Open/Closed state. A department is Open when *any* of its records is
    still in status 1.

    The page builds its Spell and Department dropdowns from this, so both only
    ever offer what the day's attendance actually contains.
    """
    try:
        params = _slice_day_params(request.query_params)
        rows = db.execute(get_attendance_open_close_status_query(), params).fetchall()
        return {
            "data": [
                {
                    "spell_id": int(r.spell_id) if r.spell_id is not None else None,
                    "spell_name": r.spell_name or "(No spell)",
                    "dept_id": int(r.dept_id) if r.dept_id is not None else None,
                    "dept_name": r.dept_name or "(Unassigned)",
                    "attendance_date": params["attendance_date"],
                    "status": "Open" if int(r.open_count or 0) > 0 else "Close",
                    "open_count": int(r.open_count or 0),
                    "total_count": int(r.total_count or 0),
                }
                for r in rows
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/attendance_open_close")
def attendance_open_close(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Open or close a whole (branch, date, spell, department) attendance slice.

    `worked_department_id` may be "all" (or 0) to cover every department of the
    spell in one go — accepted for CLOSE only.

    Only OPENING is gated by the shared password held in
    `attendance_open_close_password` — it unlocks already-finalised records, so
    a plain menu permission isn't enough of a brake. Closing just puts records
    back into the state they are written in, so it needs no password.

    Opening only reaches records created within the last OPEN_WINDOW_HOURS.
    Closing is always allowed.
    """
    try:
        body = parse_json_body(request)
        params = _slice_spell_params(body)

        action = str(body.get("action") or "").strip().lower()
        if action not in ("open", "close"):
            raise HTTPException(status_code=400, detail="action must be 'open' or 'close'")

        raw_dept = str(body.get("worked_department_id") or "").strip().lower()
        if raw_dept != "all" and not raw_dept.isdigit():
            raise HTTPException(
                status_code=400,
                detail="worked_department_id must be a department id or 'all'",
            )
        worked_department_id = 0 if raw_dept == "all" else int(raw_dept)

        is_open = action == "open"
        # "All" locks a whole spell down in one go, which is safe. Unlocking
        # wholesale is not — opening stays one department at a time.
        if is_open and worked_department_id == 0:
            raise HTTPException(
                status_code=400,
                detail="Attendance can only be opened one department at a time",
            )
        if is_open:
            stored = db.execute(text("""
                SELECT password FROM attendance_open_close_password
                ORDER BY id LIMIT 1
            """)).fetchone()
            if not stored or not stored.password:
                raise HTTPException(
                    status_code=400,
                    detail="No open/close password is configured for this tenant",
                )
            if str(body.get("password") or "") != str(stored.password):
                raise HTTPException(status_code=401, detail="Incorrect password")

        from_status, to_status = (
            (STATUS_APPROVED, STATUS_OPEN) if is_open
            else (STATUS_OPEN, STATUS_APPROVED)
        )
        flip_params = {
            **params,
            "worked_department_id": worked_department_id,
            "from_status": from_status,
        }
        result = db.execute(get_attendance_open_close_update_query(), {
            **flip_params,
            "to_status": str(to_status),
            "now": datetime.now(),
            "updated_by": token_data.get("user_id") if token_data else None,
            "enforce_open_window": 1 if is_open else 0,
        })
        updated = result.rowcount or 0

        # Anything left in the source status after an open is what the window
        # blocked. Reported rather than swallowed — a partial open that looked
        # like a full one would send someone editing records that never opened.
        stuck = 0
        if is_open:
            stuck = int(db.execute(
                get_attendance_open_close_stuck_query(), flip_params
            ).scalar() or 0)
            if updated == 0 and stuck > 0:
                db.rollback()
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{stuck} record(s) were created more than "
                        f"{OPEN_WINDOW_HOURS} hours ago and can no longer be opened"
                    ),
                )

        db.commit()

        message = (
            f"Attendance {'opened' if is_open else 'closed'} for {updated} record(s)"
        )
        if stuck:
            message += (
                f" — {stuck} stayed closed, created more than "
                f"{OPEN_WINDOW_HOURS} hours ago"
            )
        return {"message": message, "updated": updated, "blocked": stuck}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

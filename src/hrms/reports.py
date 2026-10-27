"""
HRMS / labour report endpoints, surfaced under Jute Production > Production
Reports and HRMS > HRMS Reports. Mounted at /api/hrmsReports.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.hrms.reportQueries import (
    get_attendance_summary_query,
    get_worker_master_query,
    get_man_machine_query,
    get_attendance_register_query,
    get_employee_working_query,
    get_full_attendance_query,
    get_absenteeism_query,
    get_half_day_query,
    get_overstay_query,
    get_occupation_deviation_query,
    get_cash_attendance_query,
    get_employee_headcount_query,
    get_spell_wise_query,
    get_shift_wise_query,
    get_bank_statement_query,
    get_hands_complement_query,
    get_pay_periods_query,
    get_pay_register_print_query,
    get_attendance_payscheme_query,
    get_deactivate_rejected_attendance_query,
    get_daily_hands_complement_query,
    get_extra_advance_payment_query,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/attendance-summary")
async def get_attendance_summary_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    spell: str | None = None,
):
    """
    Attendance summary by department + designation: worked hours and Hands
    (man-days) over a date range. Only active, approved (status_id = 3) rows.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional), spell (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_attendance_summary_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
            "spell": spell if spell else None,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "department": m.get("department"),
                "designation": m.get("designation"),
                "work_hours": float(m.get("work_hours") or 0),
                "hands": float(m.get("hands") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching attendance summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/worker-master")
async def get_worker_master_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    search: str | None = None,
):
    """
    Worker / employee master listing (with last working day), from the dev3 HRMS
    employee tables. Query params: co_id (required), branch_id (optional),
    search (optional emp_code / name).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        rows = db.execute(get_worker_master_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "search_like": f"%{search.strip()}%" if search else None,
        }).fetchall()

        def iso(v):
            return v.isoformat() if hasattr(v, "isoformat") else v

        data = []
        for row in rows:
            m = dict(row._mapping)
            data.append({
                "id": m.get("eb_id"),
                "emp_code": m.get("emp_code"),
                "emp_name": (m.get("emp_name") or "").strip(),
                "gender": m.get("gender"),
                "dept_desc": m.get("dept_desc"),
                "sub_dept_desc": m.get("sub_dept_desc"),
                "designation": m.get("designation"),
                "category": m.get("category"),
                "date_of_birth": iso(m.get("date_of_birth")),
                "date_of_join": iso(m.get("date_of_join")),
                "contractor_name": m.get("contractor_name"),
                "esi_no": m.get("esi_no"),
                "pf_no": m.get("pf_no"),
                "pf_uan_no": m.get("pf_uan_no"),
                "bank_acc_no": m.get("bank_acc_no"),
                "bank_name": m.get("bank_name"),
                "pay_scheme": m.get("pay_scheme"),
                "status_name": m.get("status_name"),
                "is_active": m.get("is_active"),
                "last_working_day": iso(m.get("last_working_day")),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching worker master: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/man-machine")
async def get_man_machine_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Man-machine deployment: workers (hands) and stoppage hours per machine,
    designation and spell over a date range (from daily_ebmc_attendance).

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_man_machine_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "machine_name": m.get("machine_name"),
                "department": m.get("department"),
                "designation": m.get("designation"),
                "spell": m.get("spell"),
                "hands": int(m.get("hands") or 0),
                "stoppage_hours": float(m.get("stoppage_hours") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching man-machine report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/attendance-register")
async def get_attendance_register_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Attendance register: detail rows from daily_attendance (EB No, name, date,
    department, designation, mark P/A/HD/L, spell, hours) over a date range.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_attendance_register_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        def iso(v):
            return v.isoformat() if hasattr(v, "isoformat") else v

        data = []
        for row in rows:
            m = dict(row._mapping)
            data.append({
                "id": m.get("tran_no"),
                "eb_no": m.get("eb_no"),
                "emp_name": (m.get("emp_name") or "").strip(),
                "attendance_date": iso(m.get("attendance_date")),
                "department": m.get("department"),
                "designation": m.get("designation"),
                "mark": m.get("mark"),
                "spell": m.get("spell"),
                "idle_hours": float(m.get("idle_hours") or 0),
                "spell_hours": float(m.get("spell_hours") or 0),
                "working_hours": float(m.get("working_hours") or 0),
                "status_name": m.get("status_name"),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching attendance register: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/employee-working")
async def get_employee_working_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Employee working details: per employee per month, work days / leave days /
    total over a date range. Query params: co_id (required), date_from, date_to
    (required), branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_employee_working_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "yearmn": m.get("yearmn"),
                "emp_code": m.get("emp_code"),
                "emp_name": (m.get("emp_name") or "").strip(),
                "department": m.get("department"),
                "sub_department": m.get("sub_department"),
                "category": m.get("category"),
                "status_name": m.get("status_name"),
                "wdays": float(m.get("wdays") or 0),
                "lvdays": float(m.get("lvdays") or 0),
                "hldays": float(m.get("hldays") or 0),
                "total_days": float(m.get("total_days") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching employee working details: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


@router.get("/employee-headcount")
async def get_employee_headcount_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
):
    """
    Employee headcount at (department, sub-department, designation, category)
    grain for active employees. One endpoint feeds all the legacy summary
    reports (505/506/508/509/517) — the frontend aggregates per report.

    Query params: co_id (required), branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        rows = db.execute(get_employee_headcount_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "department": m.get("department"),
                "sub_department": m.get("sub_department"),
                "designation": m.get("designation"),
                "category": m.get("category"),
                "emp_count": int(m.get("emp_count") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching employee headcount: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/hands-complement")
async def get_hands_complement_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Hands Complement: hands (worked hours / 8) per attendance date, master
    department and designation, split by shift (A/B/C) and by worker category.
    This is the second sheet of the Attendance Check List Excel export.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_hands_complement_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        num = ("shift_a", "shift_b", "shift_c", "shift_total", "permanent",
               "budli", "retired", "new_budli", "contract", "outsider",
               "apprentice", "total_hands")
        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            item = {
                "id": i,
                "attendance_date": _iso(m.get("attendance_date")),
                "department": m.get("department"),
                "designation": m.get("designation"),
            }
            for k in num:
                item[k] = float(m.get(k) or 0)
            data.append(item)

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching hands complement: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/spell-wise")
async def get_spell_wise_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Spell-wise hands (legacy 559): per worked department + designation and
    spell, hands = worked hours / 8. Long format — the frontend pivots spells
    into columns.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_spell_wise_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "department": m.get("department"),
                "designation": m.get("designation"),
                "spell": m.get("spell"),
                "hands": float(m.get("hands") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching spell-wise report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/shift-wise")
async def get_shift_wise_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Shift-wise hands: per worked department + designation and shift (first
    letter of the spell), hands = worked hours / 8. Long format — the
    frontend pivots shifts into columns.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_shift_wise_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "department": m.get("department"),
                "designation": m.get("designation"),
                "shift": m.get("shift"),
                "hands": float(m.get("hands") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching shift-wise report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bank-statement")
async def get_bank_statement_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Employee bank statement (legacy 534): net pay per employee for approved/
    processed pay periods inside the date range, with bank account details.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_bank_statement_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "emp_code": m.get("emp_code"),
                "emp_name": (m.get("emp_name") or "").strip(),
                "bank_name": m.get("bank_name"),
                "bank_acc_no": m.get("bank_acc_no"),
                "ifsc_code": m.get("ifsc_code"),
                "from_date": _iso(m.get("from_date")),
                "to_date": _iso(m.get("to_date")),
                "net_pay": float(m.get("net_pay") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching bank statement: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/full-attendance")
async def get_full_attendance_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Full attendance (legacy 603): daily_attendance detail rows with decoded
    source (Manual/Facial/Logs), type (Regular/OT/Cash) and remarks.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_full_attendance_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        data = []
        for row in rows:
            m = dict(row._mapping)
            data.append({
                "id": m.get("tran_no"),
                "eb_no": m.get("eb_no"),
                "emp_name": (m.get("emp_name") or "").strip(),
                "attendance_date": _iso(m.get("attendance_date")),
                "department": m.get("department"),
                "designation": m.get("designation"),
                "mark": m.get("mark"),
                "spell": m.get("spell"),
                "idle_hours": float(m.get("idle_hours") or 0),
                "spell_hours": float(m.get("spell_hours") or 0),
                "working_hours": float(m.get("working_hours") or 0),
                "source": m.get("source"),
                "att_type": m.get("att_type"),
                "status_name": m.get("status_name"),
                "remarks": m.get("remarks"),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching full attendance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/absenteeism")
def get_absenteeism_report(  # sync: every DB call below is blocking (CLAUDE.md)
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    as_on: str | None = None,
    min_days: int = 1,
    max_days: int | None = None,
):
    """
    Absenteeism (legacy 673): per on-roll employee, last seen date (attendance
    or approved leave) and days absent as on :as_on. The gap is bounded by
    min_days and, when given, max_days — past max_days the employee has left
    rather than gone absent (see get_absenteeism_query).

    Query params: co_id (required), as_on (required), branch_id (optional),
    min_days (optional, default 1), max_days (optional, default uncapped).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not as_on:
            raise HTTPException(status_code=400, detail="as_on is required")

        min_days = max(int(min_days), 1)
        max_days = int(max_days) if max_days is not None else None
        if max_days is not None and max_days < min_days:
            raise HTTPException(
                status_code=400, detail="max_days must be >= min_days"
            )

        rows = db.execute(get_absenteeism_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "as_on": as_on,
            "min_days": min_days,
            "max_days": max_days,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "emp_code": m.get("emp_code"),
                "emp_name": (m.get("emp_name") or "").strip(),
                "department": m.get("department"),
                "sub_department": m.get("sub_department"),
                "category": m.get("category"),
                "last_seen": _iso(m.get("last_seen")),
                "absent_for": int(m.get("absent_for") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching absenteeism report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/half-day")
async def get_half_day_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Half-day absenteeism (legacy 687): days where only the first spell of a
    shift was worked and the day's total hours are not a full 8, with per-month
    and range half-day counts per employee.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_half_day_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "emp_code": m.get("emp_code"),
                "emp_name": (m.get("emp_name") or "").strip(),
                "department": m.get("department"),
                "attendance_date": _iso(m.get("attendance_date")),
                "shift": m.get("shift"),
                "day_hours": float(m.get("day_hours") or 0),
                "halfdays_month": int(m.get("halfdays_month") or 0),
                "halfdays_total": int(m.get("halfdays_total") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching half-day report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/overstay")
async def get_overstay_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Overstay after leave (legacy 686): approved leaves ending in the range where
    the employee resumed late (or not at all) — with rejoin date and overstay days.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_overstay_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "emp_code": m.get("emp_code"),
                "emp_name": (m.get("emp_name") or "").strip(),
                "department": m.get("department"),
                "sub_department": m.get("sub_department"),
                "category": m.get("category"),
                "leave_type": m.get("leave_type"),
                "leave_from_date": _iso(m.get("leave_from_date")),
                "leave_to_date": _iso(m.get("leave_to_date")),
                "rejoin_date": _iso(m.get("rejoin_date")),
                "overstay_days": int(m.get("overstay_days") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching overstay report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/occupation-deviation")
async def get_occupation_deviation_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Occupation deviation (legacy 601): approved attendance rows where the worked
    department/designation differs from the employee's advised one.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_occupation_deviation_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        data = []
        for row in rows:
            m = dict(row._mapping)
            data.append({
                "id": m.get("tran_no"),
                "eb_no": m.get("eb_no"),
                "emp_name": (m.get("emp_name") or "").strip(),
                "attendance_date": _iso(m.get("attendance_date")),
                "actual_dept": m.get("actual_dept"),
                "actual_desig": m.get("actual_desig"),
                "advised_dept": m.get("advised_dept"),
                "advised_desig": m.get("advised_desig"),
                "spell": m.get("spell"),
                "work_hours": float(m.get("work_hours") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching occupation deviation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cash-attendance")
async def get_cash_attendance_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Cash attendance (legacy 610): cash-type attendance hours per employee, date
    and spell. Hours only — the tenant schema has no cash rate column.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_cash_attendance_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "eb_no": m.get("eb_no"),
                "emp_name": (m.get("emp_name") or "").strip(),
                "department": m.get("department"),
                "designation": m.get("designation"),
                "attendance_date": _iso(m.get("attendance_date")),
                "spell": m.get("spell"),
                "hours": float(m.get("hours") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching cash attendance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pay-periods")
async def get_pay_periods(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    payscheme_id: int | None = None,
    branch_id: int | None = None,
):
    """
    Non-cancelled pay periods for one payscheme, newest first — the period
    picker for the Pay Register / Payslip print pages.

    Query params: payscheme_id (required), branch_id (optional; branchless
    periods always pass).
    """
    try:
        if not payscheme_id:
            raise HTTPException(status_code=400, detail="payscheme_id is required")

        rows = db.execute(get_pay_periods_query(), {
            "payscheme_id": int(payscheme_id),
            "branch_id": int(branch_id) if branch_id else None,
        }).fetchall()

        data = []
        for row in rows:
            m = dict(row._mapping)
            data.append({
                "payperiod_id": m.get("payperiod_id"),
                "from_date": _iso(m.get("from_date")),
                "to_date": _iso(m.get("to_date")),
                "status": m.get("status"),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching pay periods: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pay-register-print")
async def get_pay_register_print(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    payperiod_id: int | None = None,
):
    """
    Pay Register / Payslip print data (legacy Data_entry payregisterprint /
    payslipprint, "Main Pay Roll"): one row per employee for one pay period
    with the legacy register's pivoted component columns, ordered by
    department then EB no. Employees with zero total earn are skipped.

    Query params: payperiod_id (required).
    """
    try:
        if not payperiod_id:
            raise HTTPException(status_code=400, detail="payperiod_id is required")

        rows = db.execute(get_pay_register_print_query(), {
            "payperiod_id": int(payperiod_id),
        }).fetchall()

        text_fields = {"eb_no", "wname", "dept_code", "dept_desc", "desig",
                       "time_piece", "esi_no", "pf_no", "pf_uan_no", "shift"}
        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            out = {"id": i}
            for key, value in m.items():
                if key in text_fields:
                    out[key] = (str(value).strip() if value is not None else "")
                else:
                    out[key] = float(value or 0)
            data.append(out)

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching pay register print data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/attendance-payscheme")
async def get_attendance_payscheme_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    pay_scheme_id: int | None = None,
    sub_dept_id: int | None = None,
):
    """
    "Attendance With PaySchemes" checklist for NJM Wages Data Preparation:
    one row per employee with worked hours (net of idle) for the wage period.

    Query params: branch_id, date_from, date_to, pay_scheme_id (required),
    sub_dept_id (optional department filter).
    """
    try:
        if not branch_id:
            raise HTTPException(status_code=400, detail="branch_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")
        if not pay_scheme_id:
            raise HTTPException(status_code=400, detail="pay_scheme_id is required")

        # Rejected attendance in the period is deactivated first, so the
        # checklist and the wages build that follows it agree.
        db.execute(get_deactivate_rejected_attendance_query(), {
            "branch_id": int(branch_id),
            "date_from": date_from,
            "date_to": date_to,
        })
        db.commit()

        rows = db.execute(get_attendance_payscheme_query(), {
            "branch_id": int(branch_id),
            "date_from": date_from,
            "date_to": date_to,
            "pay_scheme_id": int(pay_scheme_id),
            "sub_dept_id": int(sub_dept_id) if sub_dept_id else None,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "emp_code": m.get("emp_code"),
                "emp_name": (m.get("emp_name") or "").strip(),
                "department": m.get("department"),
                "sub_department": m.get("sub_department"),
                "category": m.get("category"),
                "status_name": m.get("status_name"),
                "pay_scheme": m.get("pay_scheme"),
                "work_hours": float(m.get("work_hours") or 0),
                "date_of_join": _iso(m.get("date_of_join")),
                "pf_uan_no": m.get("pf_uan_no"),
                "esi_no": m.get("esi_no"),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching attendance with payschemes: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/daily-hands-complement")
async def get_daily_hands_complement_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """
    Daily Hands Complement (legacy "Other Entry/Reports" option 2): hands per
    date / sub-department / occupation, split by shift for regular attendance
    and for overtime.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_daily_hands_complement_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
        }).fetchall()

        num = ("shift_a", "shift_b", "shift_c", "shift_total",
               "ot_a", "ot_b", "ot_c", "ot_total")
        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            item = {
                "id": i,
                "attendance_date": _iso(m.get("attendance_date")),
                "dept_code": m.get("dept_code"),
                "department": m.get("department"),
                "occupation": m.get("occupation"),
            }
            for k in num:
                item[k] = float(m.get(k) or 0)
            data.append(item)

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching daily hands complement: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/extra-advance-payment")
async def get_extra_advance_payment_report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    co_id: int | None = None,
    branch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    shift: str | None = None,
):
    """
    Daily Extra Advance Payment (legacy "Other Entry/Reports" option 8):
    overtime attendance priced from the extra_advance_rate slabs. A branch with
    no rate rows returns nothing.

    Query params: co_id (required), date_from, date_to (required),
    branch_id (optional), shift (optional — A / B / C).
    """
    try:
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="date_from and date_to are required")

        rows = db.execute(get_extra_advance_payment_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
            "date_from": date_from,
            "date_to": date_to,
            "shift": (shift or "").strip().upper()[:1] or None,
        }).fetchall()

        data = []
        for i, row in enumerate(rows):
            m = dict(row._mapping)
            data.append({
                "id": i,
                "attendance_date": _iso(m.get("attendance_date")),
                "eb_no": m.get("eb_no"),
                "worker_name": m.get("worker_name"),
                "shift": m.get("shift"),
                "department": m.get("department"),
                "occupation": m.get("occupation"),
                "work_hours": float(m.get("work_hours") or 0),
                "advance_amount": float(m.get("advance_amount") or 0),
            })

        return {"data": data, "total": len(data)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching extra advance payment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

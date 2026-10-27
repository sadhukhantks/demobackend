"""
Holiday & Attendance Incentive Eligibility master
(tbl_holiday_att_inc_eligibility).

One active row per employee saying whether they draw holiday pay and/or the
attendance incentive, plus the fortnightly and monthly incentive rates the
payroll process picks up. The table carries no co_id/branch_id, so every read
is scoped through the employee's branch (hrms_ed_personal_details.branch_id ->
branch_mst.co_id) using the portal company/branch sidebar selection.
"""

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db
from src.models.hrms import HolidayAttIncEligibility

router = APIRouter()

# Same lifecycle as Holiday Master: a new row is Open until someone approves it.
# Legacy sls data already carries both states (6,148 approved / 680 open), so this
# is the shape the ported payroll queries expect. Display names come from status_mst.
STATUS_OPEN = 1
STATUS_APPROVED = 3
STATUS_REJECTED = 4


# ─── SQL Queries ────────────────────────────────────────────────────


def get_eligibility_list_query():
    """List rows joined to employee + category, scoped by company/branch."""
    return text("""
        SELECT
            e.hl_att_inc_id,
            e.eb_id,
            o.emp_code,
            CONCAT(IFNULL(p.first_name, ''), ' ', IFNULL(p.middle_name, ''), ' ',
                   IFNULL(p.last_name, '')) AS emp_name,
            c.cata_desc AS category,
            e.holiday_eligibility,
            e.att_incn_eligibility,
            e.fn_att_inc_rate,
            e.mn_att_inc_rate,
            e.status,
            COALESCE(st.status_name, '') AS status_name
        FROM tbl_holiday_att_inc_eligibility e
        INNER JOIN hrms_ed_personal_details p ON p.eb_id = e.eb_id
        INNER JOIN branch_mst bm ON bm.branch_id = p.branch_id
        LEFT JOIN hrms_ed_official_details o ON o.eb_id = e.eb_id AND o.active = 1
        LEFT JOIN category_mst c ON c.cata_id = o.catagory_id
        LEFT JOIN status_mst st ON st.status_id = e.status
        WHERE e.is_active = 1
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR p.branch_id = :branch_id)
          AND (:search IS NULL
               OR o.emp_code LIKE :search
               OR CONCAT(IFNULL(p.first_name, ''), ' ', IFNULL(p.last_name, '')) LIKE :search)
        ORDER BY o.emp_code IS NULL, o.emp_code, e.hl_att_inc_id DESC
    """)


def get_eligibility_by_id_query():
    return text("""
        SELECT hl_att_inc_id, eb_id, holiday_eligibility, att_incn_eligibility,
               fn_att_inc_rate, mn_att_inc_rate, status
        FROM tbl_holiday_att_inc_eligibility
        WHERE hl_att_inc_id = :record_id AND is_active = 1
    """)


def get_eligibility_employees_query():
    """Active employees for the dropdown, scoped by company/branch."""
    return text("""
        SELECT
            p.eb_id,
            o.emp_code,
            CONCAT(IFNULL(p.first_name, ''), ' ', IFNULL(p.middle_name, ''), ' ',
                   IFNULL(p.last_name, '')) AS full_name
        FROM hrms_ed_personal_details p
        INNER JOIN branch_mst bm ON bm.branch_id = p.branch_id
        LEFT JOIN hrms_ed_official_details o ON o.eb_id = p.eb_id AND o.active = 1
        WHERE COALESCE(p.active, 1) = 1
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR p.branch_id = :branch_id)
        -- Employees with no official record (and so no emp_code) are kept but
        -- sorted last, so the dropdown opens on real EB numbers.
        ORDER BY o.emp_code IS NULL, o.emp_code, p.eb_id
    """)


def _duplicate_query():
    """The employee already has an active eligibility row (ignoring one id)."""
    return text("""
        SELECT COUNT(*) AS cnt FROM tbl_holiday_att_inc_eligibility
        WHERE is_active = 1
          AND eb_id = :eb_id
          AND (:record_id IS NULL OR hl_att_inc_id <> :record_id)
    """)


# ─── Helpers ────────────────────────────────────────────────────────


def _int(value, default: int, label: str) -> int:
    """Parse a numeric query param; anything else is a 400, never a 500."""
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} must be a number")


def _branch_param(request: Request) -> int | None:
    raw = request.query_params.get("branch_id")
    return _int(raw, 0, "Branch") if raw not in (None, "", "null") else None


def _rate(value, label: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise HTTPException(status_code=400, detail=f"{label} must be a number")
    if rate < 0:
        raise HTTPException(status_code=400, detail=f"{label} cannot be negative")
    return rate


def _flag(value, label: str) -> str:
    flag = str(value or "N").upper()
    if flag not in ("Y", "N"):
        raise HTTPException(status_code=400, detail=f"{label} must be Y or N")
    return flag


def _validate(body: dict) -> dict:
    """Same rules the legacy PHP screen enforced before enabling Save."""
    eb_id = body.get("eb_id")
    if not eb_id:
        raise HTTPException(status_code=400, detail="Employee is required")

    holiday = _flag(body.get("holiday_eligibility"), "Holiday eligibility")
    incentive = _flag(body.get("att_incn_eligibility"), "Attendance incentive")
    fn_rate = _rate(body.get("fn_att_inc_rate"), "Fortnightly incentive rate")
    mn_rate = _rate(body.get("mn_att_inc_rate"), "Monthly incentive rate")

    if incentive == "Y" and fn_rate + mn_rate <= 0:
        raise HTTPException(
            status_code=400,
            detail="Enter a fortnightly or monthly rate when the attendance incentive is Yes",
        )
    if incentive == "N" and fn_rate + mn_rate > 0:
        raise HTTPException(
            status_code=400,
            detail="Incentive rates must be zero when the attendance incentive is No",
        )

    return {
        "eb_id": int(eb_id),
        "holiday_eligibility": holiday,
        "att_incn_eligibility": incentive,
        "fn_att_inc_rate": fn_rate,
        "mn_att_inc_rate": mn_rate,
    }


def _check_duplicate(db: Session, eb_id: int, record_id: int | None):
    dup = db.execute(_duplicate_query(), {
        "eb_id": eb_id, "record_id": record_id,
    }).fetchone()
    if dup and dup.cnt > 0:
        raise HTTPException(
            status_code=400,
            detail="This employee already has an eligibility record",
        )


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/hol_inc_elig_setup")
def hol_inc_elig_setup(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Employee dropdown options for the branch."""
    try:
        co_id = request.query_params.get("co_id")
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        employees = db.execute(get_eligibility_employees_query(), {
            "co_id": _int(co_id, 0, "co_id"), "branch_id": _branch_param(request),
        }).fetchall()

        return {
            "data": {
                "employees": [
                    {
                        "value": str(m["eb_id"]),
                        "label": f"{m['emp_code'] or m['eb_id']} - {(m['full_name'] or '').strip()}",
                    }
                    for m in (dict(r._mapping) for r in employees)
                ],
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_hol_inc_elig_table")
def get_hol_inc_elig_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated list of eligibility records."""
    try:
        co_id = request.query_params.get("co_id")
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        search = request.query_params.get("search")
        page = _int(request.query_params.get("page"), 1, "Page")
        limit = _int(request.query_params.get("limit"), 10, "Limit")

        rows = db.execute(get_eligibility_list_query(), {
            "co_id": _int(co_id, 0, "co_id"),
            "branch_id": _branch_param(request),
            "search": f"%{search}%" if search else None,
        }).fetchall()

        all_data = []
        for row in rows:
            m = dict(row._mapping)
            m["emp_name"] = (m.get("emp_name") or "").strip()
            # The legacy tenants (sls) declare the ids as DOUBLE — cast so the
            # grid keys and the by-id URL are plain integers everywhere.
            m["hl_att_inc_id"] = int(m["hl_att_inc_id"])
            m["eb_id"] = int(m["eb_id"]) if m.get("eb_id") is not None else None
            m["fn_att_inc_rate"] = float(m["fn_att_inc_rate"] or 0)
            m["mn_att_inc_rate"] = float(m["mn_att_inc_rate"] or 0)
            all_data.append(m)

        total = len(all_data)
        start = (page - 1) * limit
        return {
            "data": all_data[start:start + limit],
            "total": total,
            "page": page,
            "limit": limit,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_hol_inc_elig_by_id/{record_id}")
def get_hol_inc_elig_by_id(
    record_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Single eligibility row, for the edit dialog."""
    try:
        row = db.execute(get_eligibility_by_id_query(), {"record_id": record_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Eligibility record not found")

        m = dict(row._mapping)
        m["hl_att_inc_id"] = int(m["hl_att_inc_id"])
        m["eb_id"] = int(m["eb_id"]) if m.get("eb_id") is not None else None
        m["fn_att_inc_rate"] = float(m["fn_att_inc_rate"] or 0)
        m["mn_att_inc_rate"] = float(m["mn_att_inc_rate"] or 0)
        return {"data": m}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/hol_inc_elig_create")
def hol_inc_elig_create(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create an eligibility record."""
    try:
        values = _validate(parse_json_body(request))
        _check_duplicate(db, values["eb_id"], None)

        record = HolidayAttIncEligibility(
            **values,
            is_active=1,
            status=STATUS_OPEN,
            updated_by=token_data.get("user_id") if token_data else None,
        )
        db.add(record)
        db.commit()
        db.refresh(record)

        return {
            "message": "Eligibility created successfully",
            "hl_att_inc_id": int(record.hl_att_inc_id),
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/hol_inc_elig_edit/{record_id}")
def hol_inc_elig_edit(
    record_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Update an eligibility record."""
    try:
        values = _validate(parse_json_body(request))

        existing = db.query(HolidayAttIncEligibility).filter(
            HolidayAttIncEligibility.hl_att_inc_id == record_id,
        ).first()
        if not existing:
            raise HTTPException(status_code=404, detail="Eligibility record not found")
        if existing.status == STATUS_APPROVED:
            raise HTTPException(
                status_code=400,
                detail="Approved eligibility records cannot be edited",
            )

        _check_duplicate(db, values["eb_id"], record_id)

        for field, value in values.items():
            setattr(existing, field, value)
        existing.updated_by = token_data.get("user_id") if token_data else None
        db.commit()

        return {
            "message": "Eligibility updated successfully",
            "hl_att_inc_id": record_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


def _set_status(db: Session, record_id: int, status: int, already: str,
                token_data: dict | None):
    """Shared approve/reject body — the two differ only in the target status."""
    existing = db.query(HolidayAttIncEligibility).filter(
        HolidayAttIncEligibility.hl_att_inc_id == record_id,
    ).first()
    if not existing:
        raise HTTPException(status_code=404, detail="Eligibility record not found")
    if existing.status == status:
        raise HTTPException(status_code=400, detail=already)

    existing.status = status
    existing.updated_by = token_data.get("user_id") if token_data else None
    db.commit()


@router.post("/hol_inc_elig_approve/{record_id}")
def hol_inc_elig_approve(
    record_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Approve an eligibility record. Approved records are no longer editable."""
    try:
        _set_status(db, record_id, STATUS_APPROVED,
                    "Eligibility record is already approved", token_data)
        return {
            "message": "Eligibility approved successfully",
            "hl_att_inc_id": record_id,
            "status": STATUS_APPROVED,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/hol_inc_elig_reject/{record_id}")
def hol_inc_elig_reject(
    record_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Reject an eligibility record. Rejected records stay editable and re-approvable.

    Approval is final, so an approved record cannot be rejected — the UI hides the
    button and the API enforces the same rule.
    """
    try:
        existing = db.query(HolidayAttIncEligibility).filter(
            HolidayAttIncEligibility.hl_att_inc_id == record_id,
        ).first()
        if existing and existing.status == STATUS_APPROVED:
            raise HTTPException(
                status_code=400,
                detail="Approved eligibility records cannot be rejected",
            )
        _set_status(db, record_id, STATUS_REJECTED,
                    "Eligibility record is already rejected", token_data)
        return {
            "message": "Eligibility rejected successfully",
            "hl_att_inc_id": record_id,
            "status": STATUS_REJECTED,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/hol_inc_elig_delete/{record_id}")
def hol_inc_elig_delete(
    record_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft-delete an eligibility record (is_active = 0), matching the list filter."""
    try:
        existing = db.query(HolidayAttIncEligibility).filter(
            HolidayAttIncEligibility.hl_att_inc_id == record_id,
        ).first()
        if not existing:
            raise HTTPException(status_code=404, detail="Eligibility record not found")

        existing.is_active = 0
        existing.updated_by = token_data.get("user_id") if token_data else None
        db.commit()
        return {"message": "Eligibility deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

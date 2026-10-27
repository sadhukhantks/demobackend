"""
Non Working Days master (tbl_non_working_days).

One row per (branch, date) the mill does not work. vw_leave_dates flags leave
falling on an active row (non_working_day = 'Y') so the EJM wages STL count can
skip it. Legacy rows may carry a NULL branch_id; they are listed as
"All branches" but vw_leave_dates only matches rows with a branch.

No delete: is_active = 0 switches a date off, which is how legacy rows did it.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db

router = APIRouter()


# ─── SQL Queries ────────────────────────────────────────────────────


def get_list_query():
    return text("""
        SELECT n.non_working_days_id, n.company_id, n.branch_id, n.non_working_date,
               n.is_active, n.offday_leave, bm.branch_name
        FROM tbl_non_working_days n
        LEFT JOIN branch_mst bm ON bm.branch_id = n.branch_id
        WHERE n.company_id = :co_id
          AND (:branch_id IS NULL OR n.branch_id = :branch_id OR n.branch_id IS NULL)
        ORDER BY n.non_working_date DESC, n.non_working_days_id DESC
    """)


def get_by_id_query():
    return text("""
        SELECT non_working_days_id, company_id, branch_id, non_working_date, is_active, offday_leave
        FROM tbl_non_working_days
        WHERE non_working_days_id = :record_id
    """)


def _duplicate_query():
    return text("""
        SELECT COUNT(*) AS cnt FROM tbl_non_working_days
        WHERE company_id = :co_id
          AND branch_id <=> :branch_id
          AND non_working_date = :non_working_date
          AND (:record_id IS NULL OR non_working_days_id <> :record_id)
    """)


# ─── Helpers ────────────────────────────────────────────────────────


def _int(value, default: int, label: str) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} must be a number")


def _branch_param(request: Request) -> int | None:
    raw = request.query_params.get("branch_id")
    return _int(raw, 0, "Branch") if raw not in (None, "", "null") else None


def _require_co_id(request: Request) -> int:
    co_id = request.query_params.get("co_id")
    if not co_id:
        raise HTTPException(status_code=400, detail="co_id is required")
    return _int(co_id, 0, "co_id")


def _flag(value, label: str) -> int:
    flag = _int(value, 1, label)
    if flag not in (0, 1):
        raise HTTPException(status_code=400, detail=f"{label} must be Yes or No")
    return flag


def _validate(body: dict) -> dict:
    raw_date = body.get("non_working_date")
    if not raw_date:
        raise HTTPException(status_code=400, detail="Date is required")
    try:
        nw_date = date.fromisoformat(str(raw_date)[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")
    return {
        "non_working_date": nw_date,
        "offday_leave": _flag(body.get("offday_leave"), "Off-day leave"),
        "is_active": _flag(body.get("is_active"), "Active"),
    }


def _check_duplicate(db: Session, co_id: int, branch_id: int | None, nw_date: date, record_id: int | None):
    dup = db.execute(_duplicate_query(), {
        "co_id": co_id, "branch_id": branch_id,
        "non_working_date": nw_date, "record_id": record_id,
    }).fetchone()
    if dup and dup.cnt > 0:
        raise HTTPException(status_code=400, detail=f"{nw_date} is already a non-working day for this branch")


def _row(m: dict) -> dict:
    d = m.get("non_working_date")
    return {**m, "non_working_date": d.isoformat() if d else None}


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/get_non_working_days_table")
def get_non_working_days_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        co_id = _require_co_id(request)
        search = (request.query_params.get("search") or "").strip().lower()
        page = _int(request.query_params.get("page"), 1, "Page")
        limit = _int(request.query_params.get("limit"), 10, "Limit")

        rows = db.execute(get_list_query(), {"co_id": co_id, "branch_id": _branch_param(request)}).fetchall()
        all_data = []
        for row in rows:
            item = _row(dict(row._mapping))
            item["branch_name"] = item["branch_name"] or "All branches"
            # ponytail: search in Python — a few hundred rows per company.
            if search and search not in f"{item['non_working_date']} {item['branch_name']}".lower():
                continue
            all_data.append(item)

        start = (page - 1) * limit
        return {"data": all_data[start:start + limit], "total": len(all_data), "page": page, "limit": limit}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_non_working_day_by_id/{record_id}")
def get_non_working_day_by_id(
    record_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        row = db.execute(get_by_id_query(), {"record_id": record_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Non-working day not found")
        return {"data": _row(dict(row._mapping))}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/non_working_day_create")
def non_working_day_create(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        body = parse_json_body(request)
        if not body.get("co_id"):
            raise HTTPException(status_code=400, detail="Company is required")
        if not body.get("branch_id"):
            raise HTTPException(status_code=400, detail="Select a branch in the sidebar before saving")
        co_id = _int(body["co_id"], 0, "Company")
        branch_id = _int(body["branch_id"], 0, "Branch")
        values = _validate(body)
        _check_duplicate(db, co_id, branch_id, values["non_working_date"], None)

        result = db.execute(text("""
            INSERT INTO tbl_non_working_days (company_id, branch_id, non_working_date, is_active, offday_leave)
            VALUES (:co_id, :branch_id, :non_working_date, :is_active, :offday_leave)
        """), {"co_id": co_id, "branch_id": branch_id, **values})
        db.commit()
        return {"message": "Non-working day created successfully", "non_working_days_id": int(result.lastrowid)}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/non_working_day_edit/{record_id}")
def non_working_day_edit(
    record_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Date and flags only; company/branch stay as created."""
    try:
        values = _validate(parse_json_body(request))
        existing = db.execute(get_by_id_query(), {"record_id": record_id}).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Non-working day not found")
        _check_duplicate(db, existing.company_id, existing.branch_id, values["non_working_date"], record_id)

        db.execute(text("""
            UPDATE tbl_non_working_days
            SET non_working_date = :non_working_date, is_active = :is_active, offday_leave = :offday_leave
            WHERE non_working_days_id = :record_id
        """), {**values, "record_id": record_id})
        db.commit()
        return {"message": "Non-working day updated successfully", "non_working_days_id": record_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

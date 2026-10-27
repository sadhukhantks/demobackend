"""
EJM wages quality-rate API (`ejm_wages_quality_rate`).

One row per (department, wages quality) holding the piece rate that combination
is paid at in the fortnightly wages run.

    quality_id            -> tbl_wages_quality_master.q_code_id  (the wages rate card)
    dept_id               -> dept_mst.dept_id
    wages_quality_rate_id -> AUTO_INCREMENT row id

The table has no unique key, so one rate per (dept_id, quality_id) is enforced
here.

ponytail: the department and quality pickers are the ones the EJM fortnightly
quality-target page already serves (`get_ejm_fne_qual_target_departments` /
`..._qualities`) -- same masters, same scoping, so this module adds no lookup
endpoints of its own.
"""

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db

router = APIRouter()

RATE_MAX = Decimal("9999.999999")


# ─── SQL Queries ────────────────────────────────────────────────────


def get_wages_quality_rate_list_query():
    return text("""
        SELECT
            r.wages_quality_rate_id,
            r.dept_id,
            d.dept_desc,
            r.quality_id,
            COALESCE(q.q_code, r.quality_code) AS q_code,
            COALESCE(q.quality, r.quality_desc) AS quality,
            r.quality_rate
        FROM ejm_wages_quality_rate r
        LEFT JOIN dept_mst d ON d.dept_id = r.dept_id
        LEFT JOIN tbl_wages_quality_master q ON q.q_code_id = r.quality_id
        WHERE (:branch_id IS NULL OR d.branch_id = :branch_id)
          AND (:search IS NULL
               OR d.dept_desc LIKE :search
               OR COALESCE(q.q_code, r.quality_code) LIKE :search
               OR COALESCE(q.quality, r.quality_desc) LIKE :search)
        ORDER BY d.dept_desc, q_code
    """)


def get_wages_quality_rate_by_id_query():
    return text("""
        SELECT
            r.wages_quality_rate_id,
            r.dept_id,
            d.dept_desc,
            r.quality_id,
            COALESCE(q.q_code, r.quality_code) AS q_code,
            COALESCE(q.quality, r.quality_desc) AS quality,
            r.quality_rate
        FROM ejm_wages_quality_rate r
        LEFT JOIN dept_mst d ON d.dept_id = r.dept_id
        LEFT JOIN tbl_wages_quality_master q ON q.q_code_id = r.quality_id
        WHERE r.wages_quality_rate_id = :wages_quality_rate_id
    """)


def get_wages_quality_rate_duplicate_query():
    return text("""
        SELECT wages_quality_rate_id
        FROM ejm_wages_quality_rate
        WHERE dept_id = :dept_id AND quality_id = :quality_id
    """)


# ─── Helpers ────────────────────────────────────────────────────────


def _clean_rate(body: dict) -> Decimal:
    raw = body.get("quality_rate")
    if raw is None or str(raw).strip() == "":
        raise HTTPException(status_code=400, detail="Quality rate is required")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Quality rate must be a number")
    if not (-RATE_MAX <= value <= RATE_MAX):
        raise HTTPException(status_code=400, detail="Quality rate is out of range")
    return value


def _clean_int(body: dict, field: str, label: str) -> int:
    raw = body.get(field)
    if raw in (None, ""):
        raise HTTPException(status_code=400, detail=f"{label} is required")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} is invalid")


def _assert_known(db: Session, dept_id: int, quality_id: int):
    if not db.execute(text("SELECT 1 FROM dept_mst WHERE dept_id = :id"),
                      {"id": dept_id}).fetchone():
        raise HTTPException(status_code=400, detail="Unknown department")
    if not db.execute(text("SELECT 1 FROM tbl_wages_quality_master WHERE q_code_id = :id"),
                      {"id": quality_id}).fetchone():
        raise HTTPException(status_code=400, detail="Unknown wages quality")


def _assert_free(db: Session, dept_id: int, quality_id: int, record_id=None):
    """One rate per (department, quality) — the table has no unique key of its own."""
    for row in db.execute(get_wages_quality_rate_duplicate_query(), {
        "dept_id": dept_id,
        "quality_id": quality_id,
    }).fetchall():
        if record_id is not None and row.wages_quality_rate_id == record_id:
            continue
        raise HTTPException(
            status_code=400,
            detail="This department already has a rate for that quality",
        )


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/get_ejm_wages_quality_rate_table")
def get_ejm_wages_quality_rate_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated quality-rate list, with department and quality names."""
    try:
        search = request.query_params.get("search")
        branch_id = request.query_params.get("branch_id")

        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 10))

        rows = db.execute(get_wages_quality_rate_list_query(), {
            "search": f"%{search}%" if search else None,
            "branch_id": int(branch_id) if branch_id else None,
        }).fetchall()

        all_data = [dict(r._mapping) for r in rows]
        start_idx = (page - 1) * limit

        return {
            "data": all_data[start_idx: start_idx + limit],
            "total": len(all_data),
            "page": page,
            "limit": limit,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_ejm_wages_quality_rate_by_id/{wages_quality_rate_id}")
def get_ejm_wages_quality_rate_by_id(
    wages_quality_rate_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Get a single quality-rate row by its id."""
    try:
        row = db.execute(get_wages_quality_rate_by_id_query(), {
            "wages_quality_rate_id": wages_quality_rate_id,
        }).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Quality rate not found")
        return {"data": dict(row._mapping)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ejm_wages_quality_rate_create")
def ejm_wages_quality_rate_create(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create a quality rate."""
    try:
        body = parse_json_body(request)
        dept_id = _clean_int(body, "dept_id", "Department")
        quality_id = _clean_int(body, "quality_id", "Wages quality")
        quality_rate = _clean_rate(body)

        _assert_known(db, dept_id, quality_id)
        _assert_free(db, dept_id, quality_id)

        result = db.execute(text("""
            INSERT INTO ejm_wages_quality_rate (quality_id, dept_id, quality_rate)
            VALUES (:quality_id, :dept_id, :quality_rate)
        """), {
            "quality_id": quality_id,
            "dept_id": dept_id,
            "quality_rate": quality_rate,
        })
        db.commit()

        return {
            "message": "Quality rate created successfully",
            "wages_quality_rate_id": result.lastrowid,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/ejm_wages_quality_rate_edit/{wages_quality_rate_id}")
def ejm_wages_quality_rate_edit(
    wages_quality_rate_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Update a quality rate."""
    try:
        body = parse_json_body(request)
        dept_id = _clean_int(body, "dept_id", "Department")
        quality_id = _clean_int(body, "quality_id", "Wages quality")
        quality_rate = _clean_rate(body)

        existing = db.execute(text(
            "SELECT wages_quality_rate_id FROM ejm_wages_quality_rate "
            "WHERE wages_quality_rate_id = :id"
        ), {"id": wages_quality_rate_id}).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Quality rate not found")

        _assert_known(db, dept_id, quality_id)
        _assert_free(db, dept_id, quality_id, record_id=wages_quality_rate_id)

        db.execute(text("""
            UPDATE ejm_wages_quality_rate
               SET quality_id   = :quality_id,
                   dept_id      = :dept_id,
                   quality_rate = :quality_rate
             WHERE wages_quality_rate_id = :wages_quality_rate_id
        """), {
            "quality_id": quality_id,
            "dept_id": dept_id,
            "quality_rate": quality_rate,
            "wages_quality_rate_id": wages_quality_rate_id,
        })
        db.commit()

        return {
            "message": "Quality rate updated successfully",
            "wages_quality_rate_id": wages_quality_rate_id,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

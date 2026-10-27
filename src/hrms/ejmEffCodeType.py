"""
EJM Efficiency Code Type master API endpoints.

CRUD for `ejm_eff_code_type` — the legacy FoxPro EFFCODE lookup (OCCUMAST.EFFCODE)
that carries the efficiency percentage each occupation is measured against.

The table is a plain tenant-wide lookup: no co_id / branch_id / is_active columns,
so rows are scoped only by the tenant DB that `get_tenant_db` resolves from the
subdomain. `effocode` is treated as the business key and kept unique here (there
is no DB unique constraint on it).
"""

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db

router = APIRouter()


# ─── SQL Queries ────────────────────────────────────────────────────


def get_ejm_eff_code_type_list_query():
    return text("""
        SELECT
            e.eff_code_type_id,
            e.effocode,
            e.code_details,
            e.eff_pc,
            e.eff_prod
        FROM ejm_eff_code_type e
        WHERE (:search IS NULL
               OR e.effocode LIKE :search
               OR e.code_details LIKE :search)
        ORDER BY e.effocode
    """)


def get_ejm_eff_code_type_by_id_query():
    return text("""
        SELECT
            e.eff_code_type_id,
            e.effocode,
            e.code_details,
            e.eff_pc,
            e.eff_prod
        FROM ejm_eff_code_type e
        WHERE e.eff_code_type_id = :eff_code_type_id
    """)


def get_ejm_eff_code_duplicate_query():
    """Duplicate `effocode` check; :eff_code_type_id is 0 on create."""
    return text("""
        SELECT COUNT(*) AS cnt
        FROM ejm_eff_code_type
        WHERE effocode = :effocode
          AND eff_code_type_id != :eff_code_type_id
    """)


# ─── Helpers ────────────────────────────────────────────────────────


def _clean_effocode(body: dict) -> str:
    effocode = str(body.get("effocode") or "").strip()
    if not effocode:
        raise HTTPException(status_code=400, detail="Efficiency code is required")
    if len(effocode) > 10:
        raise HTTPException(status_code=400, detail="Efficiency code must be 10 characters or less")
    return effocode


def _clean_eff_pc(body: dict):
    """eff_pc is DECIMAL(6,2) and optional -> None, or a value within range."""
    raw = body.get("eff_pc")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Efficiency % must be a number")
    if not (Decimal("-9999.99") <= value <= Decimal("9999.99")):
        raise HTTPException(status_code=400, detail="Efficiency % is out of range")
    return value


def _clean_eff_prod(body: dict):
    """eff_prod: 'E' = eff_pc is an efficiency %, 'P' = a production figure
    (e.g. 19 Spreader = kg per 8 h); optional."""
    value = str(body.get("eff_prod") or "").strip().upper() or None
    if value not in (None, "E", "P"):
        raise HTTPException(status_code=400, detail="Eff / Prod must be E or P")
    return value


def _assert_unique(db: Session, effocode: str, eff_code_type_id: int = 0):
    dup = db.execute(get_ejm_eff_code_duplicate_query(), {
        "effocode": effocode,
        "eff_code_type_id": eff_code_type_id,
    }).fetchone()
    if dup and dup.cnt > 0:
        raise HTTPException(status_code=400, detail="This efficiency code already exists")


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/get_ejm_eff_code_type_table")
def get_ejm_eff_code_type_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Get the paginated efficiency-code list."""
    try:
        search = request.query_params.get("search")
        search_param = f"%{search}%" if search else None

        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 10))

        result = db.execute(get_ejm_eff_code_type_list_query(), {
            "search": search_param,
        }).fetchall()

        all_data = [dict(row._mapping) for row in result]
        total = len(all_data)
        start_idx = (page - 1) * limit

        return {
            "data": all_data[start_idx: start_idx + limit],
            "total": total,
            "page": page,
            "limit": limit,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_ejm_eff_code_type_by_id/{eff_code_type_id}")
def get_ejm_eff_code_type_by_id(
    eff_code_type_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Get a single efficiency-code record by ID."""
    try:
        result = db.execute(get_ejm_eff_code_type_by_id_query(), {
            "eff_code_type_id": eff_code_type_id,
        }).fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Efficiency code not found")

        return {"data": dict(result._mapping)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ejm_eff_code_type_create")
def ejm_eff_code_type_create(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create an efficiency-code record."""
    try:
        body = parse_json_body(request)
        effocode = _clean_effocode(body)
        eff_pc = _clean_eff_pc(body)
        eff_prod = _clean_eff_prod(body)
        _assert_unique(db, effocode)

        result = db.execute(text("""
            INSERT INTO ejm_eff_code_type (effocode, code_details, eff_pc, eff_prod)
            VALUES (:effocode, :code_details, :eff_pc, :eff_prod)
        """), {
            "effocode": effocode,
            "code_details": (body.get("code_details") or None),
            "eff_pc": eff_pc,
            "eff_prod": eff_prod,
        })
        db.commit()

        return {
            "message": "Efficiency code created successfully",
            "eff_code_type_id": result.lastrowid,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/ejm_eff_code_type_edit/{eff_code_type_id}")
def ejm_eff_code_type_edit(
    eff_code_type_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Update an efficiency-code record."""
    try:
        body = parse_json_body(request)
        effocode = _clean_effocode(body)
        eff_pc = _clean_eff_pc(body)
        eff_prod = _clean_eff_prod(body)

        existing = db.execute(get_ejm_eff_code_type_by_id_query(), {
            "eff_code_type_id": eff_code_type_id,
        }).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Efficiency code not found")

        _assert_unique(db, effocode, eff_code_type_id)

        db.execute(text("""
            UPDATE ejm_eff_code_type
               SET effocode = :effocode,
                   code_details = :code_details,
                   eff_pc = :eff_pc,
                   eff_prod = :eff_prod
             WHERE eff_code_type_id = :eff_code_type_id
        """), {
            "effocode": effocode,
            "code_details": (body.get("code_details") or None),
            "eff_pc": eff_pc,
            "eff_prod": eff_prod,
            "eff_code_type_id": eff_code_type_id,
        })
        db.commit()

        return {
            "message": "Efficiency code updated successfully",
            "eff_code_type_id": eff_code_type_id,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

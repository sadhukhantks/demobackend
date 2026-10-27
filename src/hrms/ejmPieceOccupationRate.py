"""
EJM piece-rate per department/occupation API (`ejm_piece_occupation_rate`).

Ported from the FoxPro OCCURATE.DBF: the natural key is the legacy
(`dept_code`, `occu_code`) pair, and `dept_id` / `desig_id` resolve it onto the
sls masters. `p_occu_id` is a surrogate single-column row id (the real primary
key is composite) — the backend assigns it, the UI only displays it.

`dept_code` is never typed: `dept_mst.dept_code` equals the legacy DEPTCODE
verbatim, so picking a designation determines dept_id and dept_code both.
`desig_id` carries a UNIQUE key — one rate per designation.
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


def get_piece_occupation_rate_list_query():
    return text("""
        SELECT
            r.p_occu_id,
            r.dept_code,
            r.occu_code,
            r.f_b_rate,
            r.dept_id,
            d.dept_desc,
            r.desig_id,
            g.desig
        FROM ejm_piece_occupation_rate r
        LEFT JOIN dept_mst d ON d.dept_id = r.dept_id
        LEFT JOIN designation_mst g ON g.designation_id = r.desig_id
        WHERE (:search IS NULL
               OR r.dept_code LIKE :search
               OR r.occu_code LIKE :search
               OR d.dept_desc LIKE :search
               OR g.desig LIKE :search)
        ORDER BY r.dept_code, r.occu_code
    """)


def get_piece_occupation_rate_by_id_query():
    return text("""
        SELECT
            r.p_occu_id,
            r.dept_code,
            r.occu_code,
            r.f_b_rate,
            r.dept_id,
            d.dept_desc,
            r.desig_id,
            g.desig
        FROM ejm_piece_occupation_rate r
        LEFT JOIN dept_mst d ON d.dept_id = r.dept_id
        LEFT JOIN designation_mst g ON g.designation_id = r.desig_id
        WHERE r.p_occu_id = :p_occu_id
    """)


def get_piece_occupation_designation_query():
    """Designations available for the picker, with their department's legacy code."""
    return text("""
        SELECT
            g.designation_id AS desig_id,
            g.desig,
            g.dept_id,
            d.dept_desc,
            d.dept_code
        FROM designation_mst g
        JOIN dept_mst d ON d.dept_id = g.dept_id
        WHERE g.branch_id = :branch_id
          AND g.active = 1
          AND d.dept_code IS NOT NULL
          AND d.dept_code <> ''
        ORDER BY d.dept_code, g.desig
    """)


# ─── Helpers ────────────────────────────────────────────────────────


def _clean_occu_code(body: dict) -> str:
    """Legacy occupation codes are zero-padded 2-char strings ('01' … '55')."""
    occu_code = str(body.get("occu_code") or "").strip()
    if not occu_code:
        raise HTTPException(status_code=400, detail="Occupation code is required")
    if len(occu_code) > 2:
        raise HTTPException(status_code=400, detail="Occupation code must be 2 characters or less")
    return occu_code.zfill(2) if occu_code.isdigit() else occu_code


def _clean_rate(body: dict) -> Decimal:
    raw = body.get("f_b_rate")
    if raw is None or str(raw).strip() == "":
        raise HTTPException(status_code=400, detail="F/B rate is required")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="F/B rate must be a number")
    if not (-RATE_MAX <= value <= RATE_MAX):
        raise HTTPException(status_code=400, detail="F/B rate is out of range")
    return value


def _resolve_designation(db: Session, body: dict):
    """desig_id -> (desig_id, dept_id, dept_code). dept_code is never client-supplied."""
    raw = body.get("desig_id")
    if raw in (None, ""):
        raise HTTPException(status_code=400, detail="Designation is required")
    try:
        desig_id = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Designation is invalid")

    row = db.execute(text("""
        SELECT g.designation_id, g.dept_id, d.dept_code
        FROM designation_mst g
        JOIN dept_mst d ON d.dept_id = g.dept_id
        WHERE g.designation_id = :desig_id
    """), {"desig_id": desig_id}).fetchone()

    if not row:
        raise HTTPException(status_code=400, detail="Unknown designation")
    if not row.dept_code:
        raise HTTPException(
            status_code=400,
            detail="That designation's department has no legacy department code",
        )
    return desig_id, row.dept_id, row.dept_code


def _assert_free(db: Session, dept_code: str, occu_code: str, desig_id: int, p_occu_id=None):
    """(dept_code, occu_code) is the PK and desig_id is UNIQUE — check both."""
    clash = db.execute(text("""
        SELECT p_occu_id, dept_code, occu_code, desig_id
        FROM ejm_piece_occupation_rate
        WHERE (dept_code = :dept_code AND occu_code = :occu_code)
           OR desig_id = :desig_id
    """), {"dept_code": dept_code, "occu_code": occu_code, "desig_id": desig_id}).fetchall()

    for row in clash:
        if p_occu_id is not None and row.p_occu_id == p_occu_id:
            continue
        if row.dept_code == dept_code and row.occu_code == occu_code:
            raise HTTPException(
                status_code=400,
                detail=f"A rate already exists for department {dept_code} / occupation {occu_code}",
            )
        raise HTTPException(status_code=400, detail="That designation already has a rate")


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/get_ejm_piece_occupation_designations")
def get_ejm_piece_occupation_designations(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Designations for the picker; branch_id comes from the sidebar scope."""
    try:
        branch_id = request.query_params.get("branch_id")
        if not branch_id:
            raise HTTPException(status_code=400, detail="branch_id is required")

        rows = db.execute(get_piece_occupation_designation_query(), {
            "branch_id": int(branch_id),
        }).fetchall()
        return {"data": [dict(r._mapping) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_ejm_piece_occupation_rate_table")
def get_ejm_piece_occupation_rate_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated piece-rate list, with department and designation names."""
    try:
        search = request.query_params.get("search")
        search_param = f"%{search}%" if search else None

        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 10))

        rows = db.execute(get_piece_occupation_rate_list_query(), {
            "search": search_param,
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


@router.get("/get_ejm_piece_occupation_rate_by_id/{p_occu_id}")
def get_ejm_piece_occupation_rate_by_id(
    p_occu_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Get a single piece-rate row by its surrogate id."""
    try:
        row = db.execute(get_piece_occupation_rate_by_id_query(), {
            "p_occu_id": p_occu_id,
        }).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Piece rate not found")
        return {"data": dict(row._mapping)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ejm_piece_occupation_rate_create")
def ejm_piece_occupation_rate_create(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create a piece rate. dept_code/dept_id follow the chosen designation."""
    try:
        body = parse_json_body(request)
        occu_code = _clean_occu_code(body)
        f_b_rate = _clean_rate(body)
        desig_id, dept_id, dept_code = _resolve_designation(db, body)
        _assert_free(db, dept_code, occu_code, desig_id)

        # p_occu_id is not AUTO_INCREMENT; the backend owns it.
        next_id = db.execute(
            text("SELECT COALESCE(MAX(p_occu_id), 0) + 1 AS next_id FROM ejm_piece_occupation_rate")
        ).fetchone().next_id

        db.execute(text("""
            INSERT INTO ejm_piece_occupation_rate
                (dept_code, occu_code, f_b_rate, p_occu_id, dept_id, desig_id)
            VALUES (:dept_code, :occu_code, :f_b_rate, :p_occu_id, :dept_id, :desig_id)
        """), {
            "dept_code": dept_code,
            "occu_code": occu_code,
            "f_b_rate": f_b_rate,
            "p_occu_id": next_id,
            "dept_id": dept_id,
            "desig_id": desig_id,
        })
        db.commit()

        return {"message": "Piece rate created successfully", "p_occu_id": next_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/ejm_piece_occupation_rate_edit/{p_occu_id}")
def ejm_piece_occupation_rate_edit(
    p_occu_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Update a piece rate. Changing the designation also moves dept_code/dept_id."""
    try:
        body = parse_json_body(request)
        occu_code = _clean_occu_code(body)
        f_b_rate = _clean_rate(body)
        desig_id, dept_id, dept_code = _resolve_designation(db, body)

        existing = db.execute(
            text("SELECT p_occu_id FROM ejm_piece_occupation_rate WHERE p_occu_id = :p_occu_id"),
            {"p_occu_id": p_occu_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Piece rate not found")

        _assert_free(db, dept_code, occu_code, desig_id, p_occu_id=p_occu_id)

        db.execute(text("""
            UPDATE ejm_piece_occupation_rate
               SET dept_code = :dept_code,
                   occu_code = :occu_code,
                   f_b_rate  = :f_b_rate,
                   dept_id   = :dept_id,
                   desig_id  = :desig_id
             WHERE p_occu_id = :p_occu_id
        """), {
            "dept_code": dept_code,
            "occu_code": occu_code,
            "f_b_rate": f_b_rate,
            "dept_id": dept_id,
            "desig_id": desig_id,
            "p_occu_id": p_occu_id,
        })
        db.commit()

        return {"message": "Piece rate updated successfully", "p_occu_id": p_occu_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

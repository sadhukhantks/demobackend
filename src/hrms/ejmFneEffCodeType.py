"""
EJM fortnight-wise Efficiency Code API endpoints (`ejm_fne_eff_code_type`).

One row per (fortnight, efficiency code) holding the efficiency % that fortnight
is measured against; `ejm_eff_code_type` supplies the code list and its standing
default. `eff_code` stores the numeric code value itself (= the legacy FoxPro
OCCUMAST.EFFCODE), not the `ejm_eff_code_type` primary key.

APPROVAL: the *fortnight* is the document, not the row — every row of a
fortnight is written and moved together, so `status_id` is uniform across them.
Save + Approve only: the fortnight can be re-saved any time until approved.

    21 Drafted ── approve ──▶ 3 Approved (locked)

ponytail: one `/ejm_fne_eff_code_type_status` endpoint driven by the transition
table below; add rows to it if a reopen/reject step is ever needed.
"""

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db

router = APIRouter()

STATUS_DRAFT = 21
STATUS_OPEN = 1
STATUS_PENDING = 20
STATUS_APPROVED = 3
STATUS_REJECTED = 4
STATUS_CANCELLED = 6

STATUS_LABELS = {
    STATUS_DRAFT: "Drafted",
    STATUS_OPEN: "Open",
    STATUS_PENDING: "Pending Approval",
    STATUS_APPROVED: "Approved",
    STATUS_REJECTED: "Rejected",
    STATUS_CANCELLED: "Cancelled",
}

# action -> {status it must currently be in: status it moves to}
# Any not-yet-approved status (incl. rows left in 1/20/4/6 by the old flow) approves.
TRANSITIONS = {
    "approve": {
        s: STATUS_APPROVED
        for s in (STATUS_DRAFT, STATUS_OPEN, STATUS_PENDING, STATUS_REJECTED, STATUS_CANCELLED)
    },
}

# Rows may be saved any time until the fortnight is approved.
EDITABLE_STATUSES = (STATUS_DRAFT, STATUS_OPEN, STATUS_PENDING, STATUS_REJECTED, STATUS_CANCELLED)


# ─── SQL Queries ────────────────────────────────────────────────────


def get_forthnight_period_list_query():
    return text("""
        SELECT
            f.forthnight_id,
            f.start_date,
            f.end_date,
            f.type_of_period
        FROM forthnight_period_mst f
        WHERE f.end_date < CURDATE()   -- completed fortnights only
        ORDER BY f.start_date DESC
    """)


def get_fne_eff_code_list_query():
    """Every efficiency code, with this fortnight's % where one was entered."""
    return text("""
        SELECT
            e.eff_code_type_id,
            e.effocode,
            e.code_details,
            e.eff_pc           AS default_eff_pc,
            f.wages_eff_code_id,
            f.eff_pc           AS eff_pc,
            f.status_id
        FROM ejm_eff_code_type e
        LEFT JOIN ejm_fne_eff_code_type f
               ON f.eff_code = CAST(e.effocode AS UNSIGNED)
              AND f.forthnig_id = :forthnig_id
        ORDER BY CAST(e.effocode AS UNSIGNED)
    """)


def get_fne_status_query():
    """The fortnight's document status — its rows always share one status."""
    return text("""
        SELECT MIN(status_id) AS status_id, COUNT(*) AS row_count
        FROM ejm_fne_eff_code_type
        WHERE forthnig_id = :forthnig_id
    """)


# ─── Helpers ────────────────────────────────────────────────────────


def _forthnight_id(request: Request) -> int:
    raw = request.query_params.get("forthnig_id")
    if not raw:
        raise HTTPException(status_code=400, detail="forthnig_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="forthnig_id must be a number")


def _clean_eff_pc(raw):
    """eff_pc is DECIMAL(10,3) and optional -> None, or a value within range."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Efficiency % must be a number")
    if not (Decimal("-9999999.999") <= value <= Decimal("9999999.999")):
        raise HTTPException(status_code=400, detail="Efficiency % is out of range")
    return value


def _current_status(db: Session, forthnig_id: int):
    """(status_id, row_count) for a fortnight; status is None when it has no rows."""
    row = db.execute(get_fne_status_query(), {"forthnig_id": forthnig_id}).fetchone()
    if not row or not row.row_count:
        return None, 0
    return row.status_id, row.row_count


def _assert_fortnight_exists(db: Session, forthnig_id: int):
    exists = db.execute(
        text("SELECT 1 FROM forthnight_period_mst WHERE forthnight_id = :id"),
        {"id": forthnig_id},
    ).fetchone()
    if not exists:
        raise HTTPException(status_code=400, detail="Unknown fortnight period")


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/get_forthnight_periods")
def get_forthnight_periods(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Completed fortnight periods for the page's dropdown, newest (the last fortnight) first."""
    try:
        rows = db.execute(get_forthnight_period_list_query()).fetchall()
        return {"data": [dict(r._mapping) for r in rows]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_ejm_fne_eff_code_type")
def get_ejm_fne_eff_code_type(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Every efficiency code with this fortnight's %, plus the document status."""
    try:
        forthnig_id = _forthnight_id(request)
        rows = db.execute(get_fne_eff_code_list_query(), {
            "forthnig_id": forthnig_id,
        }).fetchall()

        status_id, row_count = _current_status(db, forthnig_id)
        effective_status = status_id if row_count else STATUS_DRAFT

        return {
            "data": [dict(r._mapping) for r in rows],
            "status_id": effective_status,
            "status_name": STATUS_LABELS.get(effective_status, "Drafted"),
            "row_count": row_count,
            "editable": effective_status in EDITABLE_STATUSES,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ejm_fne_eff_code_type_save")
def ejm_fne_eff_code_type_save(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Replace a fortnight's efficiency rows. Lines with a blank % are dropped."""
    try:
        body = parse_json_body(request)

        raw_id = body.get("forthnig_id")
        if raw_id in (None, ""):
            raise HTTPException(status_code=400, detail="forthnig_id is required")
        forthnig_id = int(raw_id)
        _assert_fortnight_exists(db, forthnig_id)

        status_id, row_count = _current_status(db, forthnig_id)
        if row_count and status_id not in EDITABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"This fortnight is {STATUS_LABELS.get(status_id, status_id)} and can no longer be edited",
            )
        # Keep Open once opened; a fresh fortnight starts as Drafted.
        new_status = status_id if row_count else STATUS_DRAFT

        lines = body.get("lines") or []
        if not isinstance(lines, list):
            raise HTTPException(status_code=400, detail="lines must be a list")

        cleaned, seen = [], set()
        for line in lines:
            eff_pc = _clean_eff_pc(line.get("eff_pc"))
            if eff_pc is None:
                continue
            try:
                eff_code = int(line.get("eff_code"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Each line needs a numeric eff_code")
            if eff_code in seen:
                raise HTTPException(
                    status_code=400,
                    detail=f"Efficiency code {eff_code} appears more than once",
                )
            seen.add(eff_code)
            cleaned.append({"eff_code": eff_code, "eff_pc": eff_pc})

        db.execute(
            text("DELETE FROM ejm_fne_eff_code_type WHERE forthnig_id = :forthnig_id"),
            {"forthnig_id": forthnig_id},
        )
        for line in cleaned:
            db.execute(text("""
                INSERT INTO ejm_fne_eff_code_type (forthnig_id, eff_code, eff_pc, status_id)
                VALUES (:forthnig_id, :eff_code, :eff_pc, :status_id)
            """), {
                "forthnig_id": forthnig_id,
                "eff_code": line["eff_code"],
                "eff_pc": line["eff_pc"],
                "status_id": new_status,
            })
        db.commit()

        return {
            "message": "Efficiency codes saved successfully",
            "forthnig_id": forthnig_id,
            "saved": len(cleaned),
            "status_id": new_status,
            "status_name": STATUS_LABELS.get(new_status),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ejm_fne_eff_code_type_status")
def ejm_fne_eff_code_type_status(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Move a fortnight through the approval flow. Body: {forthnig_id, action}."""
    try:
        body = parse_json_body(request)

        raw_id = body.get("forthnig_id")
        if raw_id in (None, ""):
            raise HTTPException(status_code=400, detail="forthnig_id is required")
        forthnig_id = int(raw_id)

        action = str(body.get("action") or "").strip().lower()
        allowed = TRANSITIONS.get(action)
        if not allowed:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown action '{action}'; expected one of {', '.join(TRANSITIONS)}",
            )

        status_id, row_count = _current_status(db, forthnig_id)
        if not row_count:
            raise HTTPException(status_code=400, detail="Save the fortnight before changing its status")

        new_status = allowed.get(status_id)
        if new_status is None:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot {action} a fortnight that is {STATUS_LABELS.get(status_id, status_id)}",
            )

        db.execute(text("""
            UPDATE ejm_fne_eff_code_type
               SET status_id = :new_status
             WHERE forthnig_id = :forthnig_id
        """), {"new_status": new_status, "forthnig_id": forthnig_id})
        db.commit()

        return {
            "message": f"Fortnight moved to {STATUS_LABELS[new_status]}",
            "forthnig_id": forthnig_id,
            "status_id": new_status,
            "status_name": STATUS_LABELS[new_status],
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

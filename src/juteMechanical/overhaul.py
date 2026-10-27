"""Overhaul register endpoints (Jute Mechanical & Maintenance module).

Prefix ``/api/mechOverhaul``. One row per overhaul of one machine, with the spare
parts consumed as detail lines — a header + detail pair with NO approval workflow
(direct save, per the module spec: maintenance logging must be fast).

Persona: Portal — ``get_tenant_db`` + ``get_current_user_with_refresh``,
``{"data": ...}`` responses (list endpoints add ``"total"``), soft delete via
``active = 0`` cascading to the spare lines. Conventions mirror
``src/juteProduction/stoppage_entry.py`` (``_require_co_id`` / ``_optional_branch_id``
/ ``_f`` / ``_serialize_dates`` helpers, and the GET ``HTTPException raise / 500`` —
write ``db.rollback()`` then raise — try/except shape).

DERIVED FIELDS (computed here in Python, never by the DB):

    downtime_days = (to_date - from_date).days + 1      # only when to_date is given
    spare_cost    = SUM(amount) over the spare lines    # amount = qty * rate if omitted
    total_cost    = labour_cost + spare_cost
    next_due_date = base_date + overhaul_freq_months    # months added calendar-correctly
    branch_id     = machine_mst -> dept_mst.branch_id   # machine_mst has no branch_id

    base_date = to_date, falling back to from_date while the overhaul is still running.

FEEDBACK INTO ``jute_mech_machine_attr`` — the point of this module:
every create and edit upserts the machine's maintenance-attribute row with
``last_overhaul_date`` and ``next_overhaul_due``. That satellite row is what the
``/maintenance-due`` report reads, so without this write-back an overhaul would be
invisible to the due/overdue view. The attr row is CREATED when absent, which is
the normal case — most machines carry no attr row until their first overhaul.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteMechanical.date_utils import add_months
from src.juteMechanical.constants import (
    DONE_BY_LABELS,
    DONE_BY_OPTIONS,
    DONE_BY_VENDOR,
    OVERHAUL_TYPE_LABELS,
    OVERHAUL_TYPES,
)
from src.juteMechanical.overhaul_query import (
    get_machine_attr_query,
    get_machine_context_query,
    get_overhaul_active_by_id_query,
    get_overhaul_by_id_query,
    get_overhaul_machines_query,
    get_overhaul_parties_query,
    get_overhaul_spares_query,
    get_overhaul_table_count_query,
    get_overhaul_table_query,
    get_party_active_query,
    insert_overhaul_query,
    insert_overhaul_spare_query,
    soft_delete_overhaul_query,
    soft_delete_overhaul_spares_query,
    update_overhaul_query,
    upsert_machine_attr_overhaul_query,
    get_latest_overhaul_date_query,
    clear_machine_attr_overhaul_query,
)


router = APIRouter()


NOT_FOUND_MSG = "Overhaul entry not found"
BAD_TYPE_MSG = "overhaul_type must be one of: " + ", ".join(OVERHAUL_TYPES)
BAD_DONE_BY_MSG = "done_by must be one of: " + ", ".join(DONE_BY_OPTIONS)
VENDOR_PARTY_MSG = "party_id is required when done_by is VENDOR"
UNKNOWN_PARTY_MSG = "Unknown or inactive party_id"
UNKNOWN_MACHINE_MSG = "Unknown or inactive machine_id"
DATE_ORDER_MSG = "to_date cannot be earlier than from_date"

MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 10


# =============================================================================
# Pydantic models
# =============================================================================


class OverhaulSpareLine(BaseModel):
    """One spare part consumed during the overhaul.

    ``amount`` is optional: when omitted it is derived as qty * rate, so the
    frontend grid may send either the typed total or just qty and rate.
    """

    item_id: int
    qty: Optional[float] = Field(default=None, ge=0)
    uom_id: Optional[int] = None
    rate: Optional[float] = Field(default=None, ge=0)
    amount: Optional[float] = Field(default=None, ge=0)
    remarks: Optional[str] = None


class OverhaulCreate(BaseModel):
    co_id: int
    branch_id: Optional[int] = None  # derived from machine -> dept when omitted
    machine_id: int
    overhaul_type: str = "MINOR"  # MAJOR | MINOR
    from_date: date
    to_date: Optional[date] = None  # NULL while the overhaul is still running
    work_done: Optional[str] = None
    done_by: str = "INHOUSE"  # INHOUSE | VENDOR
    party_id: Optional[int] = None  # required when done_by = VENDOR
    labour_cost: Optional[float] = Field(default=None, ge=0)
    remarks: Optional[str] = None
    spares: List[OverhaulSpareLine] = []


class OverhaulUpdate(OverhaulCreate):
    """Edit is a full replace of the header and the spare set.

    Same shape as create (co_id included — the edit path carries no query string),
    so every derived field is recomputed from scratch and the machine-attribute
    feedback is rewritten with the corrected dates.
    """


# =============================================================================
# Helpers (mirror stoppage_entry.py)
# =============================================================================


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id")


def _optional_branch_id(request: Request) -> Optional[int]:
    raw = request.query_params.get("branch_id")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid branch_id")


def _optional_int(request: Request, name: str) -> Optional[int]:
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _optional_date(request: Request, name: str) -> Optional[date]:
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name} format")


def _optional_str(request: Request, name: str) -> Optional[str]:
    raw = request.query_params.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _pagination(request: Request):
    """page / limit -> (page, limit, offset), limit capped at MAX_PAGE_LIMIT."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
        limit = max(
            1,
            min(
                MAX_PAGE_LIMIT,
                int(request.query_params.get("limit", str(DEFAULT_PAGE_LIMIT))),
            ),
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid pagination parameter")
    return page, limit, (page - 1) * limit


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _fo(v) -> Optional[float]:
    """Cast to float but preserve NULL as None (for response payloads)."""
    return None if v is None else float(v)


def _serialize_dates(row: dict, keys=("from_date", "to_date", "next_due_date")) -> dict:
    for k in keys:
        if row.get(k) is not None:
            row[k] = str(row[k])
    return row


def _add_months(base: date, months: int) -> date:
    """Add whole calendar months, clamping a day-of-month overflow.

    Delegates to the shared helper: the machine-attribute master computes the
    same next_overhaul_due for the same machine, and two copies that drifted
    would disagree about when a machine is due.
    """
    return add_months(base, months)


def _machine_context(db: Session, machine_id: int) -> dict:
    """Validate the machine and return its identity + derived branch (400 if unknown)."""
    row = db.execute(
        get_machine_context_query(), {"machine_id": int(machine_id)}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=UNKNOWN_MACHINE_MSG)
    return dict(row._mapping)


def _validate_header(db: Session, body: OverhaulCreate) -> None:
    """Enum, date-order and vendor-party gates — all 400s with readable detail."""
    overhaul_type = (body.overhaul_type or "").strip().upper()
    if overhaul_type not in OVERHAUL_TYPES:
        raise HTTPException(status_code=400, detail=BAD_TYPE_MSG)

    done_by = (body.done_by or "").strip().upper()
    if done_by not in DONE_BY_OPTIONS:
        raise HTTPException(status_code=400, detail=BAD_DONE_BY_MSG)

    if body.to_date is not None and body.to_date < body.from_date:
        raise HTTPException(status_code=400, detail=DATE_ORDER_MSG)

    if done_by == DONE_BY_VENDOR:
        if body.party_id is None:
            raise HTTPException(status_code=400, detail=VENDOR_PARTY_MSG)
        party = db.execute(
            get_party_active_query(),
            {"party_id": int(body.party_id), "co_id": int(body.co_id)},
        ).fetchone()
        if not party:
            raise HTTPException(status_code=400, detail=UNKNOWN_PARTY_MSG)


def _spare_amount(line: OverhaulSpareLine) -> float:
    """Line value: the typed amount, else qty * rate, else 0."""
    if line.amount is not None:
        return float(line.amount)
    if line.qty is not None and line.rate is not None:
        return float(line.qty) * float(line.rate)
    return 0.0


def _derive_costs(body: OverhaulCreate) -> dict:
    """downtime_days, spare_cost and total_cost for one overhaul body."""
    downtime_days = None
    if body.to_date is not None:
        downtime_days = (body.to_date - body.from_date).days + 1

    spare_cost = 0.0
    for line in body.spares or []:
        spare_cost += _spare_amount(line)

    labour_cost = _f(body.labour_cost)
    return {
        "downtime_days": downtime_days,
        "spare_cost": spare_cost,
        "total_cost": labour_cost + spare_cost,
        "labour_cost": labour_cost,
    }


def _next_due_date(
    db: Session, co_id: int, machine_id: int, base_date: date
) -> Optional[date]:
    """base_date + the machine's overhaul_freq_months (None when no interval is set).

    The interval lives on jute_mech_machine_attr; a machine with no attr row (or
    with the interval left blank) simply gets no next due date rather than a
    guessed one — the due report then shows it as "interval not configured".
    """
    attr = db.execute(
        get_machine_attr_query(), {"co_id": int(co_id), "machine_id": int(machine_id)}
    ).fetchone()
    if not attr:
        return None
    months = attr._mapping.get("overhaul_freq_months")
    if not months:
        return None
    return _add_months(base_date, int(months))


def _write_machine_attr_feedback(
    db: Session,
    co_id: int,
    machine_id: int,
    user_id,
) -> None:
    """Recompute last_overhaul_date + next_overhaul_due on jute_mech_machine_attr.

    Call AFTER the overhaul rows have been written, on create, edit and delete.

    The dates are derived from MAX(COALESCE(to_date, from_date)) over the machine's
    live overhaul rows, never from the row just saved. Taking the saved row's date
    would be wrong in two ordinary situations: editing a back-dated overhaul (a
    January one corrected after a June one exists) would rewind the machine to
    January and make it wrongly reappear as due, and deleting the latest overhaul
    would leave the date pointing at a removed record.

    When the last live overhaul is deleted, both columns are cleared but the attr
    row is kept — make/model/criticality and the PM fields are unrelated to
    overhaul history.

    Creates the attr row when the machine has none (the common case). Note the
    interval itself (overhaul_freq_months) lives on that row, so a machine whose
    first overhaul creates the row gets a NULL next_overhaul_due until someone
    sets the interval in the machine-attribute master.
    """
    latest = db.execute(
        get_latest_overhaul_date_query(),
        {"co_id": int(co_id), "machine_id": int(machine_id)},
    ).fetchone()
    last_overhaul_date = latest._mapping.get("last_overhaul_date") if latest else None

    if last_overhaul_date is None:
        db.execute(
            clear_machine_attr_overhaul_query(),
            {"co_id": int(co_id), "machine_id": int(machine_id), "updated_by": user_id},
        )
        return

    db.execute(
        upsert_machine_attr_overhaul_query(),
        {
            "co_id": int(co_id),
            "machine_id": int(machine_id),
            "last_overhaul_date": last_overhaul_date,
            "next_overhaul_due": _next_due_date(
                db, co_id, machine_id, last_overhaul_date
            ),
            "updated_by": user_id,
        },
    )


def _insert_spares(
    db: Session, overhaul_id: int, spares: List[OverhaulSpareLine], user_id
) -> None:
    """Insert the spare lines of one overhaul (amount defaulted from qty * rate)."""
    for line in spares or []:
        db.execute(
            insert_overhaul_spare_query(),
            {
                "overhaul_id": int(overhaul_id),
                "item_id": int(line.item_id),
                "qty": _fo(line.qty),
                "uom_id": int(line.uom_id) if line.uom_id is not None else None,
                "rate": _fo(line.rate),
                "amount": _spare_amount(line),
                "remarks": line.remarks,
                "updated_by": user_id,
            },
        )


def _save_overhaul(
    db: Session,
    body: OverhaulCreate,
    user_id,
    overhaul_id: Optional[int] = None,
) -> int:
    """Shared create/edit body: validate, derive, write header + spares + feedback.

    ``overhaul_id`` None inserts; otherwise the existing header is replaced and its
    spare lines are soft-deleted before the new set is inserted. Returns the
    overhaul_id. The caller owns the transaction (commit / rollback).
    """
    _validate_header(db, body)
    machine = _machine_context(db, int(body.machine_id))

    branch_id = body.branch_id if body.branch_id is not None else machine.get("branch_id")
    overhaul_type = body.overhaul_type.strip().upper()
    done_by = body.done_by.strip().upper()
    party_id = int(body.party_id) if (done_by == DONE_BY_VENDOR and body.party_id is not None) else None

    costs = _derive_costs(body)

    # Base date for both the next-due computation and the attr feedback: the day the
    # machine came back, falling back to the start date while the overhaul is running.
    base_date = body.to_date or body.from_date
    next_due_date = _next_due_date(db, int(body.co_id), int(body.machine_id), base_date)

    params = {
        "co_id": int(body.co_id),
        "branch_id": int(branch_id) if branch_id is not None else None,
        "machine_id": int(body.machine_id),
        "overhaul_type": overhaul_type,
        "from_date": body.from_date,
        "to_date": body.to_date,
        "downtime_days": costs["downtime_days"],
        "work_done": body.work_done,
        "done_by": done_by,
        "party_id": party_id,
        "labour_cost": costs["labour_cost"],
        "spare_cost": costs["spare_cost"],
        "total_cost": costs["total_cost"],
        "next_due_date": next_due_date,
        "remarks": body.remarks,
        "updated_by": user_id,
    }

    if overhaul_id is None:
        result = db.execute(insert_overhaul_query(), params)
        saved_id = result.lastrowid
    else:
        saved_id = int(overhaul_id)
        params["overhaul_id"] = saved_id
        db.execute(update_overhaul_query(), params)
        db.execute(
            soft_delete_overhaul_spares_query(),
            {"overhaul_id": saved_id, "updated_by": user_id},
        )

    _insert_spares(db, saved_id, body.spares, user_id)

    _write_machine_attr_feedback(
        db,
        co_id=int(body.co_id),
        machine_id=int(body.machine_id),
        user_id=user_id,
    )
    return saved_id


# =============================================================================
# Endpoints — setup + reads
# =============================================================================


@router.get("/overhaul_create_setup")
def overhaul_create_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Dropdown payload for the overhaul entry screen: machines (with dept/branch
    for the cascade), vendor parties, and the two fixed enums."""
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    try:
        machines = [
            dict(r._mapping)
            for r in db.execute(
                get_overhaul_machines_query(), {"branch_id": branch_id}
            ).fetchall()
        ]

        parties = [
            dict(r._mapping)
            for r in db.execute(
                get_overhaul_parties_query(), {"co_id": co_id}
            ).fetchall()
        ]

        overhaul_types = [
            {"code": code, "label": OVERHAUL_TYPE_LABELS.get(code, code)}
            for code in OVERHAUL_TYPES
        ]
        done_by_options = [
            {"code": code, "label": DONE_BY_LABELS.get(code, code)}
            for code in DONE_BY_OPTIONS
        ]

        return {
            "data": {
                "machines": machines,
                "parties": parties,
                "overhaul_types": overhaul_types,
                "done_by_options": done_by_options,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/overhaul_table")
def overhaul_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated overhaul register.

    Filters: branch_id, date_from / date_to (applied to from_date), machine_id,
    overhaul_type — all optional, co_id required.
    """
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    try:
        date_from = _optional_date(request, "date_from")
        date_to = _optional_date(request, "date_to")
        machine_id = _optional_int(request, "machine_id")
        overhaul_type = _optional_str(request, "overhaul_type")
        if overhaul_type is not None:
            overhaul_type = overhaul_type.upper()
            if overhaul_type not in OVERHAUL_TYPES:
                raise HTTPException(status_code=400, detail=BAD_TYPE_MSG)
        page, limit, offset = _pagination(request)

        filters = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": date_from,
            "date_to": date_to,
            "machine_id": machine_id,
            "overhaul_type": overhaul_type,
        }

        total_row = db.execute(get_overhaul_table_count_query(), filters).fetchone()
        total = int(total_row.total) if total_row else 0

        params = dict(filters)
        params["limit"] = limit
        params["offset"] = offset
        rows = []
        for r in db.execute(get_overhaul_table_query(), params).fetchall():
            row = dict(r._mapping)
            row["downtime_days"] = _fo(row.get("downtime_days"))
            row["labour_cost"] = _fo(row.get("labour_cost"))
            row["spare_cost"] = _fo(row.get("spare_cost"))
            row["total_cost"] = _fo(row.get("total_cost"))
            rows.append(_serialize_dates(row))

        return {"data": rows, "total": total, "page": page, "page_size": limit}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/overhaul_by_id/{overhaul_id}")
def overhaul_by_id(
    overhaul_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """One overhaul: full header plus its active ``spares`` lines."""
    co_id = _require_co_id(request)
    try:
        header_row = db.execute(
            get_overhaul_by_id_query(),
            {"overhaul_id": int(overhaul_id), "co_id": co_id},
        ).fetchone()
        if not header_row:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        header = dict(header_row._mapping)
        header["downtime_days"] = _fo(header.get("downtime_days"))
        header["labour_cost"] = _fo(header.get("labour_cost"))
        header["spare_cost"] = _fo(header.get("spare_cost"))
        header["total_cost"] = _fo(header.get("total_cost"))
        _serialize_dates(header)

        spares = []
        for r in db.execute(
            get_overhaul_spares_query(), {"overhaul_id": int(overhaul_id)}
        ).fetchall():
            line = dict(r._mapping)
            line["qty"] = _fo(line.get("qty"))
            line["rate"] = _fo(line.get("rate"))
            line["amount"] = _fo(line.get("amount"))
            spares.append(line)

        header["spares"] = spares
        return {"data": header}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — CRUD
# =============================================================================


@router.post("/overhaul_create")
def overhaul_create(
    body: OverhaulCreate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Insert one overhaul with its spares, then feed the machine attributes.

    Computes downtime_days / spare_cost / total_cost / next_due_date, derives
    branch_id from machine -> dept when omitted, and upserts
    jute_mech_machine_attr.last_overhaul_date + next_overhaul_due (creating the
    attr row if the machine has none).
    """
    try:
        user_id = token_data.get("user_id")
        overhaul_id = _save_overhaul(db, body, user_id)
        db.commit()
        return {"data": {"overhaul_id": overhaul_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/overhaul_edit/{overhaul_id}")
def overhaul_edit(
    overhaul_id: int,
    body: OverhaulUpdate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Full replace of one overhaul: header, spare set, and machine-attr feedback.

    Every derived field is recomputed, the old spare lines are soft-deleted and the
    posted set inserted, and last_overhaul_date / next_overhaul_due are rewritten
    for the (possibly changed) machine.
    """
    try:
        existing = db.execute(
            get_overhaul_active_by_id_query(),
            {"overhaul_id": int(overhaul_id), "co_id": int(body.co_id)},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        user_id = token_data.get("user_id")
        _save_overhaul(db, body, user_id, overhaul_id=int(overhaul_id))
        db.commit()
        return {"data": {"overhaul_id": int(overhaul_id)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/overhaul_delete/{overhaul_id}")
def overhaul_delete(
    overhaul_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete (active = 0), cascading to the overhaul's spare lines.

    The machine-attribute feedback is recomputed afterwards, so deleting the
    machine's latest overhaul rolls last_overhaul_date back to the one before it
    (or clears it when none remain) rather than leaving it pointing at a removed
    record.
    """
    co_id = _require_co_id(request)
    try:
        existing = db.execute(
            get_overhaul_active_by_id_query(),
            {"overhaul_id": int(overhaul_id), "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        user_id = token_data.get("user_id")
        machine_id = existing._mapping.get("machine_id")
        db.execute(
            soft_delete_overhaul_spares_query(),
            {"overhaul_id": int(overhaul_id), "updated_by": user_id},
        )
        db.execute(
            soft_delete_overhaul_query(),
            {"overhaul_id": int(overhaul_id), "co_id": co_id, "updated_by": user_id},
        )
        if machine_id is not None:
            _write_machine_attr_feedback(
                db, co_id=co_id, machine_id=int(machine_id), user_id=user_id
            )
        db.commit()
        return {"data": {"overhaul_id": int(overhaul_id)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

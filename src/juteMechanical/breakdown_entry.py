"""Breakdown Entry endpoints — Jute Mechanical & Maintenance (``/api/mechBreakdown``).

The heart of the module: a machine-breakdown incident log that replaces the mill's
"MILL SIDE MECHANICAL BREAK DOWN SUMMARY" workbook. One row of ``jute_mech_breakdown``
per incident (many per machine per day are normal), with spares consumed hanging off
it in ``jute_mech_breakdown_spare``.

Persona: Portal — ``get_tenant_db`` + ``get_current_user_with_refresh``, ``{"data": ...}``
responses, soft delete via ``active = 0``. Conventions mirror
``src/juteProduction/stoppage_entry.py`` (the closest analogue: machine + date + spell +
reason keyed downtime): ``_require_co_id`` / ``_optional_branch_id`` / ``_f`` /
``_serialize_dates`` helpers, Pydantic bodies on writes, plain ``def`` handlers, and the
``try / except HTTPException: raise / except Exception: rollback + 500`` shape.

NO approval workflow — downtime logging must be fast, so entries save directly.
``status`` tracks the REPAIR (OPEN / UNDER_REPAIR / CLOSED), not a document lifecycle,
and is derived from the datetimes rather than accepted from the client.

Derived fields (spec "Derived fields" — computed in Python on save, never by the DB)::

    tran_date    = stop_datetime.date()
    duration_min = body.duration_min AS TYPED, else
                   (restart_datetime  - stop_datetime) // 60    NULL while open
    response_min = (attended_datetime - stop_datetime) // 60    NULL if not recorded
    branch_id    = body.branch_id or machine_mst -> dept_mst.branch_id
    status       = CLOSED if (restart or duration_min) else
                   (UNDER_REPAIR if attended else OPEN)
    component_id = body.component_id or the reason's component_id
    discipline   = body.discipline  or the reason's discipline

Each lives in its own small module-level function so it is unit-testable without a DB.

``duration_min`` is INDEPENDENT of the timestamps: when the body carries one it is
stored exactly as typed and never cross-checked against stop/restart (the mill often
knows the real downtime without keying a restart time). Only when it is absent is it
derived. A duration-only incident is a resolved one, so it counts as CLOSED and drops
off the live open board.

Validation (400): restart >= stop; attended between stop and restart (no future-time
check — the server clock may not be in the mill's zone); machine active; reason active, same company, and either scoped to the machine's
machine_type_id or global (``machine_type_id IS NULL``) — that last gate is what stops
a Spreader code being logged against a Draw Head.

Who did what (one row, three hands): the saving user is stamped as the REPORTER on
create (production logs the stoppage), the user who enters the restart / duration is
stamped as the FIXER (mechanical), and production may then RECONFIRM the fix through
``/entry_confirm`` — agreeing, or disputing it with a remark and/or its own restart
time. Production's time is recorded beside mechanical's, never substituted for it:
``duration_min`` and every report keep using the restart mechanical entered. The
stamps are written inside the SQL (see ``breakdown_query.py``), not accepted from the
client.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteMechanical.breakdown_query import (
    close_breakdown_query,
    confirm_breakdown_query,
    get_breakdown_active_by_id_query,
    get_breakdown_by_id_query,
    get_breakdown_components_query,
    get_breakdown_entries_count_query,
    get_breakdown_entries_query,
    get_breakdown_machines_query,
    get_breakdown_spares_query,
    get_breakdown_spells_query,
    get_breakdown_sub_departments_query,
    get_machine_for_validation_query,
    get_open_breakdowns_query,
    get_reason_for_validation_query,
    get_reasons_by_machine_query,
    insert_breakdown_query,
    insert_breakdown_spare_query,
    soft_delete_breakdown_query,
    soft_delete_breakdown_spares_query,
    update_breakdown_query,
)
from src.juteMechanical.constants import (
    BREAKDOWN_STATUS_LABELS,
    BREAKDOWN_STATUSES,
    DISCIPLINE_LABELS,
    DISCIPLINE_MECHANICAL,
    DISCIPLINES,
    DOWNTIME_BREAKDOWN,
    DOWNTIME_TYPE_LABELS,
    DOWNTIME_TYPES,
    PROD_CONFIRM_CONFIRMED,
    PROD_CONFIRM_DISPUTED,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_UNDER_REPAIR,
)


router = APIRouter()


# =============================================================================
# Messages / limits
# =============================================================================

NOT_FOUND_MSG = "Breakdown entry not found"
MACHINE_NOT_FOUND_MSG = "Unknown or inactive machine_id"
REASON_NOT_FOUND_MSG = "Unknown or inactive reason_id for this company"
RESTART_BEFORE_STOP_MSG = "restart_datetime cannot be earlier than stop_datetime"
ATTENDED_BEFORE_STOP_MSG = "attended_datetime cannot be earlier than stop_datetime"
ATTENDED_AFTER_RESTART_MSG = "attended_datetime cannot be later than restart_datetime"
BAD_DISCIPLINE_MSG = "discipline must be one of: " + ", ".join(DISCIPLINES)
BAD_DOWNTIME_TYPE_MSG = "downtime_type must be one of: " + ", ".join(DOWNTIME_TYPES)
BAD_STATUS_FILTER_MSG = "status must be one of: " + ", ".join(BREAKDOWN_STATUSES)
CONFIRM_NOT_CLOSED_MSG = (
    "Only a closed breakdown can be reconfirmed — the fix has not been entered yet"
)
CONFIRM_DISPUTE_EMPTY_MSG = (
    "Give a remark or the actual restart time when you do not agree with the fix"
)
CONFIRM_RESTART_BEFORE_STOP_MSG = (
    "The actual restart time cannot be earlier than stop_datetime"
)

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000


# =============================================================================
# Pydantic models
# =============================================================================


class BreakdownSpareLine(BaseModel):
    """One spare part consumed on an incident (jute_mech_breakdown_spare)."""

    item_id: int
    qty: Optional[float] = Field(default=None, gt=0)
    uom_id: Optional[int] = None
    rate: Optional[float] = Field(default=None, ge=0)
    amount: Optional[float] = Field(default=None, ge=0)  # derived qty * rate when omitted
    remarks: Optional[str] = None


class BreakdownCreate(BaseModel):
    """POST /entry_create body (spec §2 `BreakdownCreate`)."""

    co_id: int
    machine_id: int
    stop_datetime: datetime
    reason_id: int
    branch_id: Optional[int] = None  # derived machine -> dept -> branch when None
    spell_id: Optional[int] = None
    attended_datetime: Optional[datetime] = None
    restart_datetime: Optional[datetime] = None
    duration_min: Optional[int] = Field(default=None, ge=1)  # as typed; else derived
    component_id: Optional[int] = None  # defaults from the reason
    discipline: Optional[str] = None  # defaults from the reason
    downtime_type: str = DOWNTIME_BREAKDOWN
    attended_dept_id: Optional[int] = None  # sub_dept_mst.sub_dept_id
    attended_by: Optional[str] = None
    reported_by: Optional[str] = None
    root_cause: Optional[str] = None
    action_taken: Optional[str] = None
    preventive_action: Optional[str] = None
    production_loss_qty: Optional[float] = Field(default=None, ge=0)
    remarks: Optional[str] = None
    spares: List[BreakdownSpareLine] = Field(default_factory=list)


class BreakdownUpdate(BaseModel):
    """PUT /entry_edit body — every field optional.

    Merged onto the stored row by ``model_fields_set``, so an explicit ``null``
    CLEARS a value (reopening a closed incident is
    ``{"restart_datetime": null, "duration_min": null}``) while an omitted key
    leaves it untouched. ``spares`` replaces the whole set only when the key is
    present.

    ``duration_min`` is the exception to "omitted keeps the stored value": it is a
    derived column, so an omitted (or explicitly null) key means "recompute from
    the merged timestamps" — only a value sent on THIS edit is kept verbatim.
    """

    co_id: Optional[int] = None  # else taken from the co_id query param
    machine_id: Optional[int] = None
    stop_datetime: Optional[datetime] = None
    reason_id: Optional[int] = None
    branch_id: Optional[int] = None
    spell_id: Optional[int] = None
    attended_datetime: Optional[datetime] = None
    restart_datetime: Optional[datetime] = None
    duration_min: Optional[int] = Field(default=None, ge=1)
    component_id: Optional[int] = None
    discipline: Optional[str] = None
    downtime_type: Optional[str] = None
    attended_dept_id: Optional[int] = None
    attended_by: Optional[str] = None
    reported_by: Optional[str] = None
    root_cause: Optional[str] = None
    action_taken: Optional[str] = None
    preventive_action: Optional[str] = None
    production_loss_qty: Optional[float] = Field(default=None, ge=0)
    remarks: Optional[str] = None
    spares: Optional[List[BreakdownSpareLine]] = None


class BreakdownClose(BaseModel):
    """POST /entry_close body — the quick close used by the live open board."""

    restart_datetime: datetime
    duration_min: Optional[int] = Field(default=None, ge=1)  # override the computed one
    action_taken: Optional[str] = None
    co_id: Optional[int] = None  # else taken from the co_id query param


class BreakdownConfirm(BaseModel):
    """POST /entry_confirm body — production's reconfirmation of a closed incident.

    ``agreed = True`` accepts the fix as mechanical entered it. ``agreed = False``
    disputes it and must carry a remark and/or production's own restart time.
    """

    agreed: bool
    restart_datetime: Optional[datetime] = None  # production's restart, when disputing
    remarks: Optional[str] = Field(default=None, max_length=500)
    co_id: Optional[int] = None  # else taken from the co_id query param


# =============================================================================
# Request helpers (mirror stoppage_entry.py)
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


def _body_or_query_co_id(request: Request, body_co_id: Optional[int]) -> int:
    """co_id is mandatory on every endpoint; writes may carry it either way."""
    if body_co_id is not None:
        try:
            return int(body_co_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid co_id")
    return _require_co_id(request)


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


def _paging(request: Request) -> tuple:
    """(limit, offset) from page/limit query params, limit capped at MAX_PAGE_LIMIT."""
    page = _optional_int(request, "page") or 1
    limit = _optional_int(request, "limit") or DEFAULT_PAGE_LIMIT
    if page < 1:
        page = 1
    if limit < 1:
        limit = DEFAULT_PAGE_LIMIT
    limit = min(limit, MAX_PAGE_LIMIT)
    return limit, (page - 1) * limit


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _of(v) -> Optional[float]:
    """Cast to float but preserve NULL (Decimal / Double columns)."""
    return None if v is None else float(v)


def _serialize_dates(
    row: dict,
    keys=(
        "tran_date",
        "stop_datetime",
        "attended_datetime",
        "restart_datetime",
        "reported_date_time",
        "fixed_date_time",
        "prod_confirm_date_time",
        "prod_restart_datetime",
    ),
) -> dict:
    for k in keys:
        if row.get(k) is not None:
            row[k] = str(row[k])
    return row


def _serialize_row(row: dict) -> dict:
    """Row shaping shared by every breakdown read (dates + numeric columns)."""
    _serialize_dates(row)
    if "production_loss_qty" in row:
        row["production_loss_qty"] = _of(row.get("production_loss_qty"))
    for k in ("duration_min", "response_min", "elapsed_min"):
        if k in row and row.get(k) is not None:
            row[k] = int(row[k])
    return row


# =============================================================================
# Derived-field helpers — the core of this module (pure, unit-testable)
# =============================================================================


def compute_tran_date(stop_datetime: datetime) -> date:
    """tran_date = the date part of stop_datetime (the incident's reporting day)."""
    return stop_datetime.date()


def compute_duration_min(
    stop_datetime: datetime, restart_datetime: Optional[datetime]
) -> Optional[int]:
    """duration_min = whole minutes from stop to restart; NULL while still open."""
    if restart_datetime is None:
        return None
    return int((restart_datetime - stop_datetime).total_seconds() // 60)


def resolve_duration_min(
    entered_duration_min: Optional[int],
    stop_datetime: datetime,
    restart_datetime: Optional[datetime],
) -> Optional[int]:
    """An entered duration is stored AS TYPED; only its absence derives one.

    Deliberately NOT cross-validated against the timestamps (user ruling): the
    fitter's own figure wins even when it contradicts stop/restart.
    """
    if entered_duration_min is not None:
        return int(entered_duration_min)
    return compute_duration_min(stop_datetime, restart_datetime)


def compute_response_min(
    stop_datetime: datetime, attended_datetime: Optional[datetime]
) -> Optional[int]:
    """response_min = whole minutes from stop until a fitter reached the machine."""
    if attended_datetime is None:
        return None
    return int((attended_datetime - stop_datetime).total_seconds() // 60)


def resolve_status(
    attended_datetime: Optional[datetime],
    restart_datetime: Optional[datetime],
    duration_min: Optional[int] = None,
) -> str:
    """The repair state, derived — never taken from the client.

    Restarted OR carrying a duration -> CLOSED (a duration is a finished repair,
    however it was recorded). Attended but neither -> UNDER_REPAIR. Otherwise the
    machine is down with nobody on it -> OPEN.
    """
    if restart_datetime is not None or duration_min is not None:
        return STATUS_CLOSED
    if attended_datetime is not None:
        return STATUS_UNDER_REPAIR
    return STATUS_OPEN


def resolve_component_id(
    body_component_id: Optional[int], reason_component_id: Optional[int]
) -> Optional[int]:
    """Explicit override wins; otherwise inherit the reason code's component."""
    if body_component_id is not None:
        return int(body_component_id)
    return int(reason_component_id) if reason_component_id is not None else None


def resolve_discipline(
    body_discipline: Optional[str], reason_discipline: Optional[str]
) -> str:
    """Explicit override wins; otherwise inherit the reason code's discipline."""
    for candidate in (body_discipline, reason_discipline):
        if candidate:
            value = str(candidate).strip().upper()
            if value not in DISCIPLINES:
                raise HTTPException(status_code=400, detail=BAD_DISCIPLINE_MSG)
            return value
    return DISCIPLINE_MECHANICAL


def validate_downtime_type(downtime_type: Optional[str]) -> str:
    if not downtime_type:
        return DOWNTIME_BREAKDOWN
    value = str(downtime_type).strip().upper()
    if value not in DOWNTIME_TYPES:
        raise HTTPException(status_code=400, detail=BAD_DOWNTIME_TYPE_MSG)
    return value


def validate_datetimes(
    stop_datetime: datetime,
    attended_datetime: Optional[datetime],
    restart_datetime: Optional[datetime],
    now: Optional[datetime] = None,
) -> None:
    """Gate the incident timeline (400 on any violation): stop <= attended <= restart.

    There is deliberately NO "stop may not be in the future" rule (owner ruling):
    the times are keyed in mill-local time while the server clock may run in
    another zone, so comparing them rejected genuine entries. ``now`` is accepted
    for backward compatibility and ignored.
    """
    if stop_datetime is None:
        raise HTTPException(status_code=400, detail="stop_datetime is required")

    if restart_datetime is not None and restart_datetime < stop_datetime:
        raise HTTPException(status_code=400, detail=RESTART_BEFORE_STOP_MSG)

    if attended_datetime is not None:
        if attended_datetime < stop_datetime:
            raise HTTPException(status_code=400, detail=ATTENDED_BEFORE_STOP_MSG)
        if restart_datetime is not None and attended_datetime > restart_datetime:
            raise HTTPException(status_code=400, detail=ATTENDED_AFTER_RESTART_MSG)


def compute_spare_amount(
    qty: Optional[float], rate: Optional[float], amount: Optional[float]
) -> Optional[float]:
    """Keep an explicit amount; otherwise roll it up from qty * rate."""
    if amount is not None:
        return float(amount)
    if qty is not None and rate is not None:
        return float(qty) * float(rate)
    return None


# =============================================================================
# DB-backed resolution helpers
# =============================================================================


def _load_machine(db: Session, machine_id: int):
    """Active machine row (400 if unknown/inactive). Carries machine_type_id for the
    reason gate and dept_mst.branch_id for the branch derivation."""
    row = db.execute(
        get_machine_for_validation_query(), {"machine_id": int(machine_id)}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=MACHINE_NOT_FOUND_MSG)
    return row


def _load_reason(db: Session, co_id: int, reason_id: int, machine):
    """Active reason for this company, gated to the machine's type or global.

    A reason scoped to machine_type X may only be logged against a machine of type
    X; ``machine_type_id IS NULL`` codes (NR, MT, PP, the electrical/utility set)
    are always allowed.
    """
    row = db.execute(
        get_reason_for_validation_query(),
        {"reason_id": int(reason_id), "co_id": int(co_id)},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=REASON_NOT_FOUND_MSG)

    if row.machine_type_id is not None:
        machine_type_id = machine.machine_type_id
        if machine_type_id is None or int(row.machine_type_id) != int(machine_type_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Reason '{row.reason_code}' does not apply to machine "
                    f"'{machine.machine_name}' — it is scoped to a different "
                    "machine type"
                ),
            )
    return row


def _derive_branch_id(machine, body_branch_id: Optional[int]) -> Optional[int]:
    """branch_id = explicit value, else machine -> dept_mst.branch_id.

    machine_mst has no branch of its own, and dept_mst.branch_id is itself
    nullable, so this may legitimately resolve to NULL.
    """
    if body_branch_id is not None:
        return int(body_branch_id)
    return int(machine.branch_id) if machine.branch_id is not None else None


def _replace_spares(
    db: Session,
    breakdown_id: int,
    spares: List[BreakdownSpareLine],
    user_id: Optional[int],
) -> int:
    """Soft-delete the incident's existing spare lines, then insert the new set."""
    db.execute(
        soft_delete_breakdown_spares_query(),
        {"breakdown_id": int(breakdown_id), "updated_by": user_id},
    )
    return _insert_spares(db, breakdown_id, spares, user_id)


def _insert_spares(
    db: Session,
    breakdown_id: int,
    spares: List[BreakdownSpareLine],
    user_id: Optional[int],
) -> int:
    inserted = 0
    for line in spares or []:
        db.execute(
            insert_breakdown_spare_query(),
            {
                "breakdown_id": int(breakdown_id),
                "item_id": int(line.item_id),
                "qty": _of(line.qty),
                "uom_id": int(line.uom_id) if line.uom_id is not None else None,
                "rate": _of(line.rate),
                "amount": compute_spare_amount(line.qty, line.rate, line.amount),
                "remarks": line.remarks,
                "updated_by": user_id,
            },
        )
        inserted += 1
    return inserted


def _fetch_spares(db: Session, breakdown_id: int) -> List[Dict[str, Any]]:
    rows = db.execute(
        get_breakdown_spares_query(), {"breakdown_id": int(breakdown_id)}
    ).fetchall()
    spares = []
    for r in rows:
        row = dict(r._mapping)
        row["qty"] = _of(row.get("qty"))
        row["rate"] = _of(row.get("rate"))
        row["amount"] = _of(row.get("amount"))
        spares.append(row)
    return spares


def _load_active_breakdown(db: Session, co_id: int, breakdown_id: int):
    row = db.execute(
        get_breakdown_active_by_id_query(),
        {"breakdown_id": int(breakdown_id), "co_id": int(co_id)},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
    return row


def _picked(body: BaseModel, field: str, existing):
    """Merge helper: an omitted key keeps the stored value, an explicit null clears it."""
    if field in body.model_fields_set:
        return getattr(body, field)
    return existing


# =============================================================================
# Endpoints — setup + lookups
# =============================================================================


@router.get("/entry_create_setup")
def entry_create_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """One call, every dropdown the breakdown entry screen needs.

    Reason codes are NOT here — they depend on the chosen machine's type and come
    from /reasons_by_machine once a machine is picked.
    """
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    try:
        machines = [
            dict(r._mapping)
            for r in db.execute(
                get_breakdown_machines_query(), {"branch_id": branch_id}
            ).fetchall()
        ]

        spells = []
        seen = set()
        for r in db.execute(
            get_breakdown_spells_query(), {"branch_id": branch_id}
        ).fetchall():
            m = dict(r._mapping)
            code = m["spell_code"]
            if code in seen:
                continue
            seen.add(code)
            spells.append(
                {
                    "spell_id": int(m["spell_id"]),
                    "spell_code": code,
                    "spell_name": m["spell_name"],
                    "working_hours": _f(m.get("working_hours")),
                }
            )

        components = [
            dict(r._mapping)
            for r in db.execute(
                get_breakdown_components_query(), {"co_id": co_id}
            ).fetchall()
        ]

        sub_departments = [
            dict(r._mapping)
            for r in db.execute(
                get_breakdown_sub_departments_query(), {"branch_id": branch_id}
            ).fetchall()
        ]

        return {
            "data": {
                "machines": machines,
                "spells": spells,
                "components": components,
                "sub_departments": sub_departments,
                "disciplines": [
                    {"code": c, "label": DISCIPLINE_LABELS.get(c, c)}
                    for c in DISCIPLINES
                ],
                "downtime_types": [
                    {"code": c, "label": DOWNTIME_TYPE_LABELS.get(c, c)}
                    for c in DOWNTIME_TYPES
                ],
                "statuses": [
                    {"code": c, "label": BREAKDOWN_STATUS_LABELS.get(c, c)}
                    for c in BREAKDOWN_STATUSES
                ],
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/reasons_by_machine")
def reasons_by_machine(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Reason codes selectable for one machine: its machine type PLUS all globals.

    Drives the entry screen's reason dropdown, and is the read-side twin of the
    save-side gate in _load_reason.
    """
    co_id = _require_co_id(request)
    machine_id = _optional_int(request, "machine_id")
    if machine_id is None:
        raise HTTPException(status_code=400, detail="machine_id is required")
    try:
        machine = _load_machine(db, machine_id)
        machine_type_id = (
            int(machine.machine_type_id) if machine.machine_type_id is not None else None
        )
        rows = db.execute(
            get_reasons_by_machine_query(),
            {"co_id": co_id, "machine_type_id": machine_type_id},
        ).fetchall()
        return {
            "data": [dict(r._mapping) for r in rows],
            "master": {
                "machine_id": int(machine.machine_id),
                "machine_name": machine.machine_name,
                "machine_type_id": machine_type_id,
                "machine_type_name": machine.machine_type_name,
                "branch_id": (
                    int(machine.branch_id) if machine.branch_id is not None else None
                ),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — reads
# =============================================================================


@router.get("/entries")
def entries(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Filtered, paginated incident grid -> {"data": [...], "total": n}.

    Machine / department / machine type / reason / component names are joined in so
    the grid shows names, not ids.
    """
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    date_from = _optional_date(request, "date_from")
    date_to = _optional_date(request, "date_to")
    machine_id = _optional_int(request, "machine_id")
    machine_type_id = _optional_int(request, "machine_type_id")

    discipline = request.query_params.get("discipline") or None
    if discipline:
        discipline = discipline.strip().upper()
        if discipline not in DISCIPLINES:
            raise HTTPException(status_code=400, detail=BAD_DISCIPLINE_MSG)

    status = request.query_params.get("status") or None
    if status:
        status = status.strip().upper()
        if status not in BREAKDOWN_STATUSES:
            raise HTTPException(status_code=400, detail=BAD_STATUS_FILTER_MSG)

    limit, offset = _paging(request)

    try:
        filters = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": date_from,
            "date_to": date_to,
            "machine_id": machine_id,
            "machine_type_id": machine_type_id,
            "discipline": discipline,
            "status": status,
        }

        total_row = db.execute(get_breakdown_entries_count_query(), filters).fetchone()
        total = int(total_row.total) if total_row and total_row.total else 0

        rows = db.execute(
            get_breakdown_entries_query(),
            dict(filters, limit=limit, offset=offset),
        ).fetchall()

        return {
            "data": [_serialize_row(dict(r._mapping)) for r in rows],
            "total": total,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entry_by_id/{breakdown_id}")
def entry_by_id(
    breakdown_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """One incident: header (with display names) + its active spare lines."""
    co_id = _require_co_id(request)
    try:
        row = db.execute(
            get_breakdown_by_id_query(),
            {"co_id": co_id, "breakdown_id": int(breakdown_id)},
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        header = _serialize_row(dict(row._mapping))
        header["spares"] = _fetch_spares(db, breakdown_id)
        return {"data": header}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/open_breakdowns")
def open_breakdowns(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Live board: incidents with restart_datetime IS NULL, longest-running first.

    elapsed_min is computed against the server clock at read time — an open
    incident has no restart to store a duration against.
    """
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    machine_id = _optional_int(request, "machine_id")
    try:
        rows = db.execute(
            get_open_breakdowns_query(),
            {"co_id": co_id, "branch_id": branch_id, "machine_id": machine_id},
        ).fetchall()
        data = [_serialize_row(dict(r._mapping)) for r in rows]
        return {
            "data": data,
            "total": len(data),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — writes
# =============================================================================


@router.post("/entry_create")
def entry_create(
    body: BreakdownCreate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Log one breakdown incident (+ any spares consumed). Direct save, no approval.

    Validates the machine, the reason-to-machine-type gate and the incident
    timeline, then computes every derived column before the INSERT.
    """
    try:
        co_id = int(body.co_id)
        downtime_type = validate_downtime_type(body.downtime_type)

        machine = _load_machine(db, body.machine_id)
        reason = _load_reason(db, co_id, body.reason_id, machine)

        validate_datetimes(
            body.stop_datetime, body.attended_datetime, body.restart_datetime
        )

        branch_id = _derive_branch_id(machine, body.branch_id)
        tran_date = compute_tran_date(body.stop_datetime)
        duration_min = resolve_duration_min(
            body.duration_min, body.stop_datetime, body.restart_datetime
        )
        response_min = compute_response_min(body.stop_datetime, body.attended_datetime)
        status = resolve_status(
            body.attended_datetime, body.restart_datetime, duration_min
        )
        component_id = resolve_component_id(body.component_id, reason.component_id)
        discipline = resolve_discipline(body.discipline, reason.discipline)

        user_id = token_data.get("user_id")
        result = db.execute(
            insert_breakdown_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "machine_id": int(body.machine_id),
                "tran_date": tran_date,
                "spell_id": int(body.spell_id) if body.spell_id is not None else None,
                "stop_datetime": body.stop_datetime,
                "attended_datetime": body.attended_datetime,
                "restart_datetime": body.restart_datetime,
                "duration_min": duration_min,
                "response_min": response_min,
                "reason_id": int(body.reason_id),
                "component_id": component_id,
                "discipline": discipline,
                "downtime_type": downtime_type,
                "attended_dept_id": (
                    int(body.attended_dept_id)
                    if body.attended_dept_id is not None
                    else None
                ),
                "attended_by": body.attended_by,
                "reported_by": body.reported_by,
                "root_cause": body.root_cause,
                "action_taken": body.action_taken,
                "preventive_action": body.preventive_action,
                "production_loss_qty": _of(body.production_loss_qty),
                "status": status,
                "remarks": body.remarks,
                "updated_by": user_id,
            },
        )
        breakdown_id = result.lastrowid
        spare_count = _insert_spares(db, breakdown_id, body.spares, user_id)

        db.commit()
        return {
            "data": {
                "breakdown_id": breakdown_id,
                "tran_date": str(tran_date),
                "branch_id": branch_id,
                "duration_min": duration_min,
                "response_min": response_min,
                "status": status,
                "component_id": component_id,
                "discipline": discipline,
                "downtime_type": downtime_type,
                "spare_count": spare_count,
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/entry_edit/{breakdown_id}")
def entry_edit(
    breakdown_id: int,
    body: BreakdownUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Edit an incident. Partial body merged onto the stored row, then every derived
    column is recomputed from the merged result (an edited restart_datetime moves
    duration_min and status together, and clearing BOTH restart_datetime and
    duration_min reopens the incident). A duration_min sent on this edit is kept
    verbatim; omitting it (or sending null) re-derives it from the timestamps.

    When ``spares`` is present the whole set is replaced: old lines soft-deleted,
    new lines inserted.
    """
    try:
        co_id = _body_or_query_co_id(request, body.co_id)
        existing = _load_active_breakdown(db, co_id, breakdown_id)

        machine_id = int(_picked(body, "machine_id", existing.machine_id))
        reason_id = int(_picked(body, "reason_id", existing.reason_id))
        stop_datetime = _picked(body, "stop_datetime", existing.stop_datetime)
        attended_datetime = _picked(
            body, "attended_datetime", existing.attended_datetime
        )
        restart_datetime = _picked(body, "restart_datetime", existing.restart_datetime)

        if stop_datetime is None:
            raise HTTPException(status_code=400, detail="stop_datetime is required")

        machine = _load_machine(db, machine_id)
        reason = _load_reason(db, co_id, reason_id, machine)
        validate_datetimes(stop_datetime, attended_datetime, restart_datetime)

        downtime_type = validate_downtime_type(
            _picked(body, "downtime_type", existing.downtime_type)
        )

        # branch: an explicit body value wins, otherwise re-derive from the machine
        # (the stored branch could belong to a machine that has just been swapped).
        if "branch_id" in body.model_fields_set and body.branch_id is not None:
            branch_id = int(body.branch_id)
        elif machine_id != int(existing.machine_id) or existing.branch_id is None:
            branch_id = _derive_branch_id(machine, None)
        else:
            branch_id = int(existing.branch_id)

        component_source = _picked(body, "component_id", existing.component_id)
        discipline_source = _picked(body, "discipline", existing.discipline)

        tran_date = compute_tran_date(stop_datetime)
        # An omitted duration_min recomputes (it is derived, not "stored"), and so
        # does an explicit null — the two coincide, so no sentinel branch is needed.
        duration_min = resolve_duration_min(
            body.duration_min, stop_datetime, restart_datetime
        )
        response_min = compute_response_min(stop_datetime, attended_datetime)
        status = resolve_status(attended_datetime, restart_datetime, duration_min)
        component_id = resolve_component_id(component_source, reason.component_id)
        discipline = resolve_discipline(discipline_source, reason.discipline)

        spell_id = _picked(body, "spell_id", existing.spell_id)
        attended_dept_id = _picked(body, "attended_dept_id", existing.attended_dept_id)
        production_loss_qty = _picked(
            body, "production_loss_qty", existing.production_loss_qty
        )

        user_id = token_data.get("user_id")
        db.execute(
            update_breakdown_query(),
            {
                "breakdown_id": int(breakdown_id),
                "co_id": co_id,
                "branch_id": branch_id,
                "machine_id": machine_id,
                "tran_date": tran_date,
                "spell_id": int(spell_id) if spell_id is not None else None,
                "stop_datetime": stop_datetime,
                "attended_datetime": attended_datetime,
                "restart_datetime": restart_datetime,
                "duration_min": duration_min,
                "response_min": response_min,
                "reason_id": reason_id,
                "component_id": component_id,
                "discipline": discipline,
                "downtime_type": downtime_type,
                "attended_dept_id": (
                    int(attended_dept_id) if attended_dept_id is not None else None
                ),
                "attended_by": _picked(body, "attended_by", existing.attended_by),
                "reported_by": _picked(body, "reported_by", existing.reported_by),
                "root_cause": _picked(body, "root_cause", existing.root_cause),
                "action_taken": _picked(body, "action_taken", existing.action_taken),
                "preventive_action": _picked(
                    body, "preventive_action", existing.preventive_action
                ),
                "production_loss_qty": _of(production_loss_qty),
                "status": status,
                "remarks": _picked(body, "remarks", existing.remarks),
                "updated_by": user_id,
            },
        )

        spare_count = None
        if "spares" in body.model_fields_set and body.spares is not None:
            spare_count = _replace_spares(db, breakdown_id, body.spares, user_id)

        db.commit()
        return {
            "data": {
                "breakdown_id": int(breakdown_id),
                "tran_date": str(tran_date),
                "branch_id": branch_id,
                "duration_min": duration_min,
                "response_min": response_min,
                "status": status,
                "component_id": component_id,
                "discipline": discipline,
                "downtime_type": downtime_type,
                "spare_count": spare_count,
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/entry_delete/{breakdown_id}")
def entry_delete(
    breakdown_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete the incident and cascade the same to its spare lines."""
    co_id = _require_co_id(request)
    try:
        _load_active_breakdown(db, co_id, breakdown_id)

        user_id = token_data.get("user_id")
        db.execute(
            soft_delete_breakdown_query(),
            {"breakdown_id": int(breakdown_id), "co_id": co_id, "updated_by": user_id},
        )
        db.execute(
            soft_delete_breakdown_spares_query(),
            {"breakdown_id": int(breakdown_id), "updated_by": user_id},
        )
        db.commit()
        return {"data": {"breakdown_id": int(breakdown_id)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/entry_close/{breakdown_id}")
def entry_close(
    breakdown_id: int,
    body: BreakdownClose,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Quick close from the live open board: stamp the restart, take duration_min
    as typed or recompute it from the STORED stop_datetime, and set status CLOSED.

    Deliberately narrow — the board needs one field and a tap, not the full edit
    form. action_taken is appended only when supplied.
    """
    try:
        co_id = _body_or_query_co_id(request, body.co_id)
        existing = _load_active_breakdown(db, co_id, breakdown_id)

        stop_datetime = existing.stop_datetime
        validate_datetimes(stop_datetime, existing.attended_datetime, body.restart_datetime)

        duration_min = resolve_duration_min(
            body.duration_min, stop_datetime, body.restart_datetime
        )
        status = resolve_status(
            existing.attended_datetime, body.restart_datetime, duration_min
        )

        user_id = token_data.get("user_id")
        db.execute(
            close_breakdown_query(),
            {
                "breakdown_id": int(breakdown_id),
                "co_id": co_id,
                "restart_datetime": body.restart_datetime,
                "duration_min": duration_min,
                "action_taken": body.action_taken,
                "status": status,
                "updated_by": user_id,
            },
        )
        db.commit()
        return {
            "data": {
                "breakdown_id": int(breakdown_id),
                "restart_datetime": str(body.restart_datetime),
                "duration_min": duration_min,
                "status": status,
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/entry_confirm/{breakdown_id}")
def entry_confirm(
    breakdown_id: int,
    body: BreakdownConfirm,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Production's optional reconfirmation of a fix mechanical has entered.

    Agreeing stores CONFIRMED. Disagreeing stores DISPUTED with a remark and/or
    production's own restart time. Either way only the prod_* columns move — the
    restart, duration and status mechanical entered stay as the record, so the two
    versions sit side by side. May be repeated; the latest answer wins.
    """
    try:
        co_id = _body_or_query_co_id(request, body.co_id)
        existing = _load_active_breakdown(db, co_id, breakdown_id)

        if existing.status != STATUS_CLOSED:
            raise HTTPException(status_code=400, detail=CONFIRM_NOT_CLOSED_MSG)

        remarks = (body.remarks or "").strip() or None
        if body.agreed:
            prod_confirm_status = PROD_CONFIRM_CONFIRMED
            prod_restart_datetime = None
        else:
            prod_confirm_status = PROD_CONFIRM_DISPUTED
            prod_restart_datetime = body.restart_datetime
            if prod_restart_datetime is None and remarks is None:
                raise HTTPException(status_code=400, detail=CONFIRM_DISPUTE_EMPTY_MSG)
            if (
                prod_restart_datetime is not None
                and prod_restart_datetime < existing.stop_datetime
            ):
                raise HTTPException(
                    status_code=400, detail=CONFIRM_RESTART_BEFORE_STOP_MSG
                )

        db.execute(
            confirm_breakdown_query(),
            {
                "breakdown_id": int(breakdown_id),
                "co_id": co_id,
                "prod_confirm_status": prod_confirm_status,
                "prod_restart_datetime": prod_restart_datetime,
                "prod_confirm_remarks": remarks,
                "updated_by": token_data.get("user_id"),
            },
        )
        db.commit()
        return {
            "data": {
                "breakdown_id": int(breakdown_id),
                "prod_confirm_status": prod_confirm_status,
                "prod_restart_datetime": (
                    str(prod_restart_datetime)
                    if prod_restart_datetime is not None
                    else None
                ),
                "prod_confirm_remarks": remarks,
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

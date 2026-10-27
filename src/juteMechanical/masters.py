"""Jute Mechanical & Maintenance — MASTERS endpoints (prefix /api/mechMaintMasters).

Four maintenance masters behind one router, plus a single dropdown payload:

    /masters_setup        machines, machine_types, departments, sub_departments,
                          spells, components + the app enums (disciplines,
                          downtime_types, criticalities, pm_frequencies)
    /reason_*             jute_mech_reason_mst    breakdown reason codes
    /component_*          jute_mech_component_mst failure components
    /machine_attr_*       jute_mech_machine_attr  maintenance satellite on machine_mst
    /pm_schedule_*        jute_mech_pm_schedule   PM checklist template

Persona: Portal — ``Depends(get_tenant_db)`` + ``Depends(get_current_user_with_refresh)``,
plain ``def`` handlers (never ``async def``; bodies via ``parse_json_body``),
``{"data": ...}`` responses (list endpoints add ``total``/``page``/``limit``),
soft delete via ``active = 0``, and NO approval workflow anywhere in this module.

``co_id`` is required on EVERY endpoint (400 when missing) — it is the tenant-DB
company scope and all four tables carry it. ``branch_id`` is optional (NULL = all)
and is never stored on a master: on the machine-attribute screen it filters
through the canonical machine_mst -> dept_mst.branch_id derivation, because
machine_mst has no branch_id of its own.

Two behaviours worth knowing before reading the code:

  * ``/machine_attr_table`` is driven FROM machine_mst with a LEFT JOIN to the attr
    table, so all 600+ machines list whether or not anyone has filled in their
    maintenance attributes yet.
  * ``/machine_attr_create`` is an UPSERT on (co_id, machine_id) — the DB unique
    key makes a second insert impossible, and the screen's natural gesture is
    "save the attributes for this machine", not "create a row".

Both due dates are computed here in Python, never by the DB:
    next_overhaul_due = last_overhaul_date + overhaul_freq_months
    next_pm_due       = last_pm_date       + pm_freq_days

SQL: src/juteMechanical/masters_query.py
ORM: src/juteMechanical/models.py     Enums: src/juteMechanical/constants.py
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db
from src.juteMechanical.date_utils import add_months
from src.juteMechanical.constants import (
    CRITICALITIES,
    CRITICALITY_LABELS,
    DISCIPLINE_LABELS,
    DISCIPLINE_MECHANICAL,
    DISCIPLINES,
    DOWNTIME_TYPE_LABELS,
    DOWNTIME_TYPES,
    PM_FREQUENCIES,
    PM_FREQUENCY_DAYS,
    PM_FREQUENCY_LABELS,
)
from src.juteMechanical.masters_query import (
    check_component_duplicate_query,
    check_reason_duplicate_query,
    count_breakdowns_for_component_query,
    count_breakdowns_for_reason_query,
    count_reasons_for_component_query,
    get_component_by_id_query,
    get_component_list_query,
    get_components_lookup_query,
    get_departments_query,
    get_machine_attr_by_id_query,
    get_machine_attr_by_machine_query,
    get_machine_attr_list_query,
    get_machine_types_query,
    get_machines_query,
    get_pm_schedule_by_id_query,
    get_pm_schedule_list_query,
    get_reason_by_id_query,
    get_reason_list_query,
    get_spells_query,
    get_sub_departments_query,
    insert_component_query,
    insert_machine_attr_query,
    insert_pm_schedule_query,
    insert_reason_query,
    machine_exists_query,
    soft_delete_component_query,
    soft_delete_machine_attr_query,
    soft_delete_pm_schedule_query,
    soft_delete_reason_query,
    update_component_query,
    update_machine_attr_query,
    update_pm_schedule_query,
    update_reason_query,
)

router = APIRouter()


REASON_NOT_FOUND_MSG = "Reason not found"
COMPONENT_NOT_FOUND_MSG = "Component not found"
MACHINE_ATTR_NOT_FOUND_MSG = "Machine attribute record not found"
PM_SCHEDULE_NOT_FOUND_MSG = "PM schedule task not found"

BAD_DISCIPLINE_MSG = "discipline must be one of: " + ", ".join(DISCIPLINES)
BAD_FREQUENCY_MSG = "frequency must be one of: " + ", ".join(PM_FREQUENCIES)
BAD_CRITICALITY_MSG = "criticality must be one of: " + ", ".join(CRITICALITIES)


# =============================================================================
# Helpers (mirror src/juteProduction/stoppage_entry.py)
# =============================================================================


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid co_id")


def _body_co_id(body: dict, request: Request) -> int:
    """co_id for write endpoints: body first, query string as a fallback."""
    raw = body.get("co_id") if isinstance(body, dict) else None
    if raw in (None, ""):
        return _require_co_id(request)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid co_id")


def _optional_branch_id(request: Request) -> Optional[int]:
    raw = request.query_params.get("branch_id")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid branch_id")


def _optional_query_int(request: Request, name: str) -> Optional[int]:
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _optional_query_str(request: Request, name: str) -> Optional[str]:
    raw = request.query_params.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _search_param(request: Request) -> Optional[str]:
    """LIKE pattern for the optional ``search`` filter (None = no filter)."""
    search = request.query_params.get("search")
    if search is None:
        return None
    search = search.strip()
    return f"%{search}%" if search else None


def _pagination(request: Request):
    try:
        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 10))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid page or limit")
    if page < 1:
        page = 1
    if limit < 1:
        limit = 10
    return page, limit


def _paginate(rows: list, page: int, limit: int) -> list:
    start = (page - 1) * limit
    return rows[start:start + limit]


def _opt_int(value, field: str) -> Optional[int]:
    """Optional integer body field — empty string / None both mean SQL NULL."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")


def _req_int(value, field: str) -> int:
    if value in (None, ""):
        raise HTTPException(status_code=400, detail=f"{field} is required")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {field}")


def _opt_str(value) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _req_str(value, field: str) -> str:
    value = _opt_str(value)
    if not value:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    return value


def _active_flag(value, default: int = 1) -> int:
    if value in (None, ""):
        return default
    try:
        return 1 if int(value) else 0
    except (TypeError, ValueError):
        return default


def _opt_date(value, field: str) -> Optional[date]:
    """Parse an optional ISO date body field ('' / None -> SQL NULL)."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {field} format (expected YYYY-MM-DD)")


def _serialize_row(row: dict) -> dict:
    """JSON-safe row: dates/datetimes -> str, Decimal -> float."""
    for key, value in list(row.items()):
        if isinstance(value, (date, datetime)):
            row[key] = str(value)
        elif isinstance(value, Decimal):
            row[key] = float(value)
    return row


def _rows(result) -> list:
    return [_serialize_row(dict(r._mapping)) for r in result]


def _add_months(start: date, months: int) -> date:
    """Add whole months, clamping to the last valid day of the target month.

    Delegates to the shared helper: the overhaul register computes the same
    next_overhaul_due for the same machine, and two copies that drifted would
    disagree about when a machine is due.
    """
    return add_months(start, months)


def _next_overhaul_due(last_overhaul_date: Optional[date],
                       overhaul_freq_months: Optional[int]) -> Optional[date]:
    """last_overhaul_date + overhaul_freq_months (NULL if either is missing)."""
    if last_overhaul_date is None or not overhaul_freq_months:
        return None
    return _add_months(last_overhaul_date, int(overhaul_freq_months))


def _next_pm_due(last_pm_date: Optional[date],
                 pm_freq_days: Optional[int]) -> Optional[date]:
    """last_pm_date + pm_freq_days (NULL if either is missing)."""
    if last_pm_date is None or not pm_freq_days:
        return None
    return last_pm_date + timedelta(days=int(pm_freq_days))


def _validate_discipline(value, default: str = DISCIPLINE_MECHANICAL) -> str:
    discipline = _opt_str(value) or default
    discipline = discipline.upper()
    if discipline not in DISCIPLINES:
        raise HTTPException(status_code=400, detail=BAD_DISCIPLINE_MSG)
    return discipline


def _validate_frequency(value) -> str:
    frequency = _opt_str(value)
    if not frequency:
        raise HTTPException(status_code=400, detail="frequency is required")
    frequency = frequency.upper()
    if frequency not in PM_FREQUENCIES:
        raise HTTPException(status_code=400, detail=BAD_FREQUENCY_MSG)
    return frequency


def _validate_criticality(value) -> Optional[str]:
    criticality = _opt_str(value)
    if criticality is None:
        return None
    criticality = criticality.upper()
    if criticality not in CRITICALITIES:
        raise HTTPException(status_code=400, detail=BAD_CRITICALITY_MSG)
    return criticality


def _resolve_freq_days(freq_days, frequency: str) -> Optional[int]:
    """Explicit freq_days wins; otherwise resolve it from PM_FREQUENCY_DAYS."""
    resolved = _opt_int(freq_days, "freq_days")
    if resolved is not None:
        return resolved
    return PM_FREQUENCY_DAYS.get(frequency)


def _enum_options(values, labels: dict) -> list:
    """Serialize an app enum for a dropdown.

    Key is "code", not "value", to match the house shape used by
    src/juteProduction/stoppage_entry.py and by this module's own breakdown,
    overhaul and PM setup endpoints.
    """
    return [{"code": v, "label": labels.get(v, v)} for v in values]


def _user_id(token_data: dict):
    return token_data.get("user_id") if token_data else None


# =============================================================================
# SETUP
# =============================================================================


@router.get("/masters_setup")
def masters_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Every dropdown the four master screens need, in one call.

    Reused masters are read with their own activity conventions: machine/machine
    type use ``active = 1``, spell_mst uses ``status = 1``, and dept_mst /
    sub_dept_mst have no active column at all. Enums come from constants.py.
    """
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    try:
        machines = _rows(
            db.execute(
                get_machines_query(),
                {"branch_id": branch_id, "machine_type_id": None},
            ).fetchall()
        )

        machine_types = _rows(db.execute(get_machine_types_query()).fetchall())

        departments = _rows(
            db.execute(get_departments_query(), {"branch_id": branch_id}).fetchall()
        )

        sub_departments = _rows(
            db.execute(get_sub_departments_query(), {"branch_id": branch_id}).fetchall()
        )

        spells = []
        seen_spell_codes = set()
        for r in db.execute(get_spells_query(), {"branch_id": branch_id}).fetchall():
            m = dict(r._mapping)
            code = m.get("spell_code")
            if code in seen_spell_codes:
                continue
            seen_spell_codes.add(code)
            spells.append(
                {
                    "spell_id": int(m["spell_id"]),
                    "spell_code": code,
                    "spell_name": m.get("spell_name"),
                    "working_hours": (
                        float(m["working_hours"]) if m.get("working_hours") is not None else None
                    ),
                }
            )

        components = _rows(
            db.execute(get_components_lookup_query(), {"co_id": co_id}).fetchall()
        )

        return {
            "data": {
                "machines": machines,
                "machine_types": machine_types,
                "departments": departments,
                "sub_departments": sub_departments,
                "spells": spells,
                "components": components,
                "disciplines": _enum_options(DISCIPLINES, DISCIPLINE_LABELS),
                "downtime_types": _enum_options(DOWNTIME_TYPES, DOWNTIME_TYPE_LABELS),
                "criticalities": _enum_options(CRITICALITIES, CRITICALITY_LABELS),
                "pm_frequencies": [
                    {
                        "code": f,
                        "label": PM_FREQUENCY_LABELS.get(f, f),
                        "freq_days": PM_FREQUENCY_DAYS.get(f),
                    }
                    for f in PM_FREQUENCIES
                ],
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# REASON MASTER
# =============================================================================


@router.get("/reason_table")
def reason_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated reason list. Filters: search, machine_type_id, discipline."""
    co_id = _require_co_id(request)
    page, limit = _pagination(request)
    try:
        rows = _rows(
            db.execute(
                get_reason_list_query(),
                {
                    "co_id": co_id,
                    "machine_type_id": _optional_query_int(request, "machine_type_id"),
                    "discipline": _optional_query_str(request, "discipline"),
                    "search": _search_param(request),
                },
            ).fetchall()
        )
        return {
            "data": _paginate(rows, page, limit),
            "total": len(rows),
            "page": page,
            "limit": limit,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/reason_by_id/{reason_id}")
def reason_by_id(
    reason_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """One reason code with its machine-type and component display names."""
    co_id = _require_co_id(request)
    try:
        row = db.execute(
            get_reason_by_id_query(), {"reason_id": reason_id, "co_id": co_id}
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=REASON_NOT_FOUND_MSG)
        return {"data": _serialize_row(dict(row._mapping))}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reason_create")
def reason_create(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create a reason code. Dup check on (co_id, machine_type_id, reason_code).

    machine_type_id NULL is meaningful — it makes the code global (NR, MT, PP and
    the electrical set), so it is preserved as NULL rather than defaulted.
    """
    try:
        body = parse_json_body(request)
        co_id = _body_co_id(body, request)

        reason_code = _req_str(body.get("reason_code"), "reason_code")
        reason_desc = _req_str(body.get("reason_desc"), "reason_desc")
        machine_type_id = _opt_int(body.get("machine_type_id"), "machine_type_id")
        discipline = _validate_discipline(body.get("discipline"))
        component_id = _opt_int(body.get("component_id"), "component_id")

        dup = db.execute(
            check_reason_duplicate_query(),
            {
                "co_id": co_id,
                "reason_code": reason_code,
                "machine_type_id": machine_type_id,
                "reason_id": None,
            },
        ).fetchone()
        if dup:
            detail = "A reason with this code already exists for this machine type"
            if not dup.active:
                detail += " but is inactive — reactivate it instead of creating a duplicate"
            raise HTTPException(status_code=400, detail=detail)

        result = db.execute(
            insert_reason_query(),
            {
                "co_id": co_id,
                "machine_type_id": machine_type_id,
                "reason_code": reason_code,
                "reason_desc": reason_desc,
                "discipline": discipline,
                "component_id": component_id,
                "remarks": _opt_str(body.get("remarks")),
                "active": _active_flag(body.get("active")),
                "updated_by": _user_id(token_data),
                "updated_date_time": datetime.now(),
            },
        )
        db.commit()
        return {"data": {"reason_id": result.lastrowid}}
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Invalid machine type or component reference")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/reason_edit/{reason_id}")
def reason_edit(
    reason_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Update a reason code. Dup check excludes the row being edited."""
    try:
        body = parse_json_body(request)
        co_id = _body_co_id(body, request)

        existing = db.execute(
            get_reason_by_id_query(), {"reason_id": reason_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=REASON_NOT_FOUND_MSG)

        reason_code = _req_str(body.get("reason_code"), "reason_code")
        reason_desc = _req_str(body.get("reason_desc"), "reason_desc")
        machine_type_id = _opt_int(body.get("machine_type_id"), "machine_type_id")
        discipline = _validate_discipline(body.get("discipline"))
        component_id = _opt_int(body.get("component_id"), "component_id")

        dup = db.execute(
            check_reason_duplicate_query(),
            {
                "co_id": co_id,
                "reason_code": reason_code,
                "machine_type_id": machine_type_id,
                "reason_id": reason_id,
            },
        ).fetchone()
        if dup:
            detail = "A reason with this code already exists for this machine type"
            if not dup.active:
                detail += " but is inactive — reactivate it instead of creating a duplicate"
            raise HTTPException(status_code=400, detail=detail)

        db.execute(
            update_reason_query(),
            {
                "reason_id": reason_id,
                "co_id": co_id,
                "machine_type_id": machine_type_id,
                "reason_code": reason_code,
                "reason_desc": reason_desc,
                "discipline": discipline,
                "component_id": component_id,
                "remarks": _opt_str(body.get("remarks")),
                "active": _active_flag(body.get("active"), default=int(existing.active or 0)),
                "updated_by": _user_id(token_data),
                "updated_date_time": datetime.now(),
            },
        )
        db.commit()
        return {"data": {"reason_id": reason_id}}
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Invalid machine type or component reference")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/reason_delete/{reason_id}")
def reason_delete(
    reason_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete (active = 0), refused while a live breakdown cites the code."""
    co_id = _require_co_id(request)
    try:
        existing = db.execute(
            get_reason_by_id_query(), {"reason_id": reason_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=REASON_NOT_FOUND_MSG)

        usage = db.execute(
            count_breakdowns_for_reason_query(), {"reason_id": reason_id}
        ).fetchone()
        if usage and usage.cnt:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot delete: this reason is used by {usage.cnt} breakdown "
                    "record(s). Mark it inactive on the breakdowns first."
                ),
            )

        db.execute(
            soft_delete_reason_query(),
            {
                "reason_id": reason_id,
                "co_id": co_id,
                "updated_by": _user_id(token_data),
                "updated_date_time": datetime.now(),
            },
        )
        db.commit()
        return {"data": {"reason_id": reason_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# COMPONENT MASTER
# =============================================================================


@router.get("/component_table")
def component_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated component list. Filters: search, component_group."""
    co_id = _require_co_id(request)
    page, limit = _pagination(request)
    try:
        rows = _rows(
            db.execute(
                get_component_list_query(),
                {
                    "co_id": co_id,
                    "component_group": _optional_query_str(request, "component_group"),
                    "search": _search_param(request),
                },
            ).fetchall()
        )
        return {
            "data": _paginate(rows, page, limit),
            "total": len(rows),
            "page": page,
            "limit": limit,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/component_by_id/{component_id}")
def component_by_id(
    component_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """One component row."""
    co_id = _require_co_id(request)
    try:
        row = db.execute(
            get_component_by_id_query(), {"component_id": component_id, "co_id": co_id}
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=COMPONENT_NOT_FOUND_MSG)
        return {"data": _serialize_row(dict(row._mapping))}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/component_create")
def component_create(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create a component. Dup check on (co_id, component_name)."""
    try:
        body = parse_json_body(request)
        co_id = _body_co_id(body, request)

        component_name = _req_str(body.get("component_name"), "component_name")

        dup = db.execute(
            check_component_duplicate_query(),
            {"co_id": co_id, "component_name": component_name, "component_id": None},
        ).fetchone()
        if dup:
            detail = "A component with this name already exists"
            if not dup.active:
                detail += " but is inactive — reactivate it instead of creating a duplicate"
            raise HTTPException(status_code=400, detail=detail)

        result = db.execute(
            insert_component_query(),
            {
                "co_id": co_id,
                "component_name": component_name,
                "component_group": _opt_str(body.get("component_group")),
                "remarks": _opt_str(body.get("remarks")),
                "active": _active_flag(body.get("active")),
                "updated_by": _user_id(token_data),
                "updated_date_time": datetime.now(),
            },
        )
        db.commit()
        return {"data": {"component_id": result.lastrowid}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/component_edit/{component_id}")
def component_edit(
    component_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Update a component. Dup check excludes the row being edited."""
    try:
        body = parse_json_body(request)
        co_id = _body_co_id(body, request)

        existing = db.execute(
            get_component_by_id_query(), {"component_id": component_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=COMPONENT_NOT_FOUND_MSG)

        component_name = _req_str(body.get("component_name"), "component_name")

        dup = db.execute(
            check_component_duplicate_query(),
            {
                "co_id": co_id,
                "component_name": component_name,
                "component_id": component_id,
            },
        ).fetchone()
        if dup:
            detail = "A component with this name already exists"
            if not dup.active:
                detail += " but is inactive — reactivate it instead of creating a duplicate"
            raise HTTPException(status_code=400, detail=detail)

        db.execute(
            update_component_query(),
            {
                "component_id": component_id,
                "co_id": co_id,
                "component_name": component_name,
                "component_group": _opt_str(body.get("component_group")),
                "remarks": _opt_str(body.get("remarks")),
                "active": _active_flag(body.get("active"), default=int(existing.active or 0)),
                "updated_by": _user_id(token_data),
                "updated_date_time": datetime.now(),
            },
        )
        db.commit()
        return {"data": {"component_id": component_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/component_delete/{component_id}")
def component_delete(
    component_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete (active = 0), refused while a reason or breakdown cites it."""
    co_id = _require_co_id(request)
    try:
        existing = db.execute(
            get_component_by_id_query(), {"component_id": component_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=COMPONENT_NOT_FOUND_MSG)

        reason_usage = db.execute(
            count_reasons_for_component_query(), {"component_id": component_id}
        ).fetchone()
        if reason_usage and reason_usage.cnt:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot delete: this component is tagged on {reason_usage.cnt} "
                    "reason code(s)"
                ),
            )

        breakdown_usage = db.execute(
            count_breakdowns_for_component_query(), {"component_id": component_id}
        ).fetchone()
        if breakdown_usage and breakdown_usage.cnt:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot delete: this component is used by {breakdown_usage.cnt} "
                    "breakdown record(s)"
                ),
            )

        db.execute(
            soft_delete_component_query(),
            {
                "component_id": component_id,
                "co_id": co_id,
                "updated_by": _user_id(token_data),
                "updated_date_time": datetime.now(),
            },
        )
        db.commit()
        return {"data": {"component_id": component_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# MACHINE MAINTENANCE ATTRIBUTES
# =============================================================================


@router.get("/machine_attr_table")
def machine_attr_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Every active machine, with maintenance attributes where they exist.

    Driven FROM machine_mst via LEFT JOIN, so the 600+ machines that have no attr
    row yet still list (attr columns NULL) — that is the point of the screen.
    branch_id filters through the machine -> dept derivation.
    """
    co_id = _require_co_id(request)
    page, limit = _pagination(request)
    try:
        rows = _rows(
            db.execute(
                get_machine_attr_list_query(),
                {
                    "co_id": co_id,
                    "branch_id": _optional_branch_id(request),
                    "machine_type_id": _optional_query_int(request, "machine_type_id"),
                    "search": _search_param(request),
                },
            ).fetchall()
        )
        return {
            "data": _paginate(rows, page, limit),
            "total": len(rows),
            "page": page,
            "limit": limit,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/machine_attr_by_id/{machine_attr_id}")
def machine_attr_by_id(
    machine_attr_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """One attribute row with its machine / type / dept resolved."""
    co_id = _require_co_id(request)
    try:
        row = db.execute(
            get_machine_attr_by_id_query(),
            {"machine_attr_id": machine_attr_id, "co_id": co_id},
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=MACHINE_ATTR_NOT_FOUND_MSG)
        return {"data": _serialize_row(dict(row._mapping))}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/machine_attr_create")
def machine_attr_create(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """UPSERT the maintenance attributes for one machine.

    (co_id, machine_id) is unique in the DB, so a second "create" for the same
    machine updates the existing row (reviving it if it was soft-deleted) instead
    of failing on the unique key. Both due dates are recomputed here:
        next_overhaul_due = last_overhaul_date + overhaul_freq_months
        next_pm_due       = last_pm_date       + pm_freq_days
    """
    try:
        body = parse_json_body(request)
        co_id = _body_co_id(body, request)

        machine_id = _req_int(body.get("machine_id"), "machine_id")
        machine = db.execute(
            machine_exists_query(), {"machine_id": machine_id}
        ).fetchone()
        if not machine:
            raise HTTPException(status_code=400, detail="Unknown machine_id")

        last_overhaul_date = _opt_date(body.get("last_overhaul_date"), "last_overhaul_date")
        overhaul_freq_months = _opt_int(body.get("overhaul_freq_months"), "overhaul_freq_months")
        last_pm_date = _opt_date(body.get("last_pm_date"), "last_pm_date")
        pm_freq_days = _opt_int(body.get("pm_freq_days"), "pm_freq_days")

        params = {
            "co_id": co_id,
            "machine_id": machine_id,
            "make": _opt_str(body.get("make")),
            "model": _opt_str(body.get("model")),
            "serial_no": _opt_str(body.get("serial_no")),
            "commissioning_date": _opt_date(body.get("commissioning_date"), "commissioning_date"),
            "criticality": _validate_criticality(body.get("criticality")),
            "last_overhaul_date": last_overhaul_date,
            "overhaul_freq_months": overhaul_freq_months,
            "next_overhaul_due": _next_overhaul_due(last_overhaul_date, overhaul_freq_months),
            "last_pm_date": last_pm_date,
            "pm_freq_days": pm_freq_days,
            "next_pm_due": _next_pm_due(last_pm_date, pm_freq_days),
            "remarks": _opt_str(body.get("remarks")),
            "active": _active_flag(body.get("active")),
            "updated_by": _user_id(token_data),
            "updated_date_time": datetime.now(),
        }

        existing = db.execute(
            get_machine_attr_by_machine_query(),
            {"co_id": co_id, "machine_id": machine_id},
        ).fetchone()

        if existing:
            machine_attr_id = int(existing.machine_attr_id)
            db.execute(
                update_machine_attr_query(),
                dict(params, machine_attr_id=machine_attr_id),
            )
            created = False
        else:
            result = db.execute(insert_machine_attr_query(), params)
            machine_attr_id = result.lastrowid
            created = True

        db.commit()
        return {
            "data": {
                "machine_attr_id": machine_attr_id,
                "machine_id": machine_id,
                "created": created,
                "next_overhaul_due": (
                    str(params["next_overhaul_due"]) if params["next_overhaul_due"] else None
                ),
                "next_pm_due": (
                    str(params["next_pm_due"]) if params["next_pm_due"] else None
                ),
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/machine_attr_edit/{machine_attr_id}")
def machine_attr_edit(
    machine_attr_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Update one attribute row, recomputing both due dates.

    machine_id may be omitted (it stays as stored); when supplied it is validated
    and re-checked against the (co_id, machine_id) unique key.
    """
    try:
        body = parse_json_body(request)
        co_id = _body_co_id(body, request)

        existing = db.execute(
            get_machine_attr_by_id_query(),
            {"machine_attr_id": machine_attr_id, "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=MACHINE_ATTR_NOT_FOUND_MSG)

        machine_id = _opt_int(body.get("machine_id"), "machine_id")
        if machine_id is None:
            machine_id = int(existing.machine_id)
        elif machine_id != int(existing.machine_id):
            machine = db.execute(
                machine_exists_query(), {"machine_id": machine_id}
            ).fetchone()
            if not machine:
                raise HTTPException(status_code=400, detail="Unknown machine_id")
            clash = db.execute(
                get_machine_attr_by_machine_query(),
                {"co_id": co_id, "machine_id": machine_id},
            ).fetchone()
            if clash and int(clash.machine_attr_id) != machine_attr_id:
                raise HTTPException(
                    status_code=400,
                    detail="Maintenance attributes already exist for that machine",
                )

        last_overhaul_date = _opt_date(body.get("last_overhaul_date"), "last_overhaul_date")
        overhaul_freq_months = _opt_int(body.get("overhaul_freq_months"), "overhaul_freq_months")
        last_pm_date = _opt_date(body.get("last_pm_date"), "last_pm_date")
        pm_freq_days = _opt_int(body.get("pm_freq_days"), "pm_freq_days")

        db.execute(
            update_machine_attr_query(),
            {
                "machine_attr_id": machine_attr_id,
                "co_id": co_id,
                "machine_id": machine_id,
                "make": _opt_str(body.get("make")),
                "model": _opt_str(body.get("model")),
                "serial_no": _opt_str(body.get("serial_no")),
                "commissioning_date": _opt_date(
                    body.get("commissioning_date"), "commissioning_date"
                ),
                "criticality": _validate_criticality(body.get("criticality")),
                "last_overhaul_date": last_overhaul_date,
                "overhaul_freq_months": overhaul_freq_months,
                "next_overhaul_due": _next_overhaul_due(
                    last_overhaul_date, overhaul_freq_months
                ),
                "last_pm_date": last_pm_date,
                "pm_freq_days": pm_freq_days,
                "next_pm_due": _next_pm_due(last_pm_date, pm_freq_days),
                "remarks": _opt_str(body.get("remarks")),
                "active": _active_flag(body.get("active"), default=int(existing.active or 0)),
                "updated_by": _user_id(token_data),
                "updated_date_time": datetime.now(),
            },
        )
        db.commit()
        return {"data": {"machine_attr_id": machine_attr_id, "machine_id": machine_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/machine_attr_delete/{machine_attr_id}")
def machine_attr_delete(
    machine_attr_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete (active = 0). No FK guard — the attr row is a satellite that
    nothing references; the machine itself stays untouched in machine_mst."""
    co_id = _require_co_id(request)
    try:
        existing = db.execute(
            get_machine_attr_by_id_query(),
            {"machine_attr_id": machine_attr_id, "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=MACHINE_ATTR_NOT_FOUND_MSG)

        db.execute(
            soft_delete_machine_attr_query(),
            {
                "machine_attr_id": machine_attr_id,
                "co_id": co_id,
                "updated_by": _user_id(token_data),
                "updated_date_time": datetime.now(),
            },
        )
        db.commit()
        return {"data": {"machine_attr_id": machine_attr_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# PM SCHEDULE (checklist template)
# =============================================================================


@router.get("/pm_schedule_table")
def pm_schedule_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated checklist template. Filters: search, machine_type_id,
    machine_id, frequency."""
    co_id = _require_co_id(request)
    page, limit = _pagination(request)
    try:
        rows = _rows(
            db.execute(
                get_pm_schedule_list_query(),
                {
                    "co_id": co_id,
                    "machine_type_id": _optional_query_int(request, "machine_type_id"),
                    "machine_id": _optional_query_int(request, "machine_id"),
                    "frequency": _optional_query_str(request, "frequency"),
                    "search": _search_param(request),
                },
            ).fetchall()
        )
        return {
            "data": _paginate(rows, page, limit),
            "total": len(rows),
            "page": page,
            "limit": limit,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pm_schedule_by_id/{pm_schedule_id}")
def pm_schedule_by_id(
    pm_schedule_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """One checklist task."""
    co_id = _require_co_id(request)
    try:
        row = db.execute(
            get_pm_schedule_by_id_query(),
            {"pm_schedule_id": pm_schedule_id, "co_id": co_id},
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=PM_SCHEDULE_NOT_FOUND_MSG)
        return {"data": _serialize_row(dict(row._mapping))}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pm_schedule_create")
def pm_schedule_create(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create a checklist task.

    freq_days is resolved from PM_FREQUENCY_DAYS when the caller does not supply
    it — it is what next_pm_due is computed from, so it must not stay NULL just
    because the form only offered a frequency name.
    """
    try:
        body = parse_json_body(request)
        co_id = _body_co_id(body, request)

        task_desc = _req_str(body.get("task_desc"), "task_desc")
        frequency = _validate_frequency(body.get("frequency"))
        machine_type_id = _opt_int(body.get("machine_type_id"), "machine_type_id")
        machine_id = _opt_int(body.get("machine_id"), "machine_id")

        if machine_id is not None:
            machine = db.execute(
                machine_exists_query(), {"machine_id": machine_id}
            ).fetchone()
            if not machine:
                raise HTTPException(status_code=400, detail="Unknown machine_id")

        result = db.execute(
            insert_pm_schedule_query(),
            {
                "co_id": co_id,
                "machine_type_id": machine_type_id,
                "machine_id": machine_id,
                "task_desc": task_desc,
                "frequency": frequency,
                "freq_days": _resolve_freq_days(body.get("freq_days"), frequency),
                "seq_no": _opt_int(body.get("seq_no"), "seq_no"),
                "remarks": _opt_str(body.get("remarks")),
                "active": _active_flag(body.get("active")),
                "updated_by": _user_id(token_data),
                "updated_date_time": datetime.now(),
            },
        )
        db.commit()
        return {"data": {"pm_schedule_id": result.lastrowid}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/pm_schedule_edit/{pm_schedule_id}")
def pm_schedule_edit(
    pm_schedule_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Update a checklist task (freq_days re-resolved from the frequency)."""
    try:
        body = parse_json_body(request)
        co_id = _body_co_id(body, request)

        existing = db.execute(
            get_pm_schedule_by_id_query(),
            {"pm_schedule_id": pm_schedule_id, "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=PM_SCHEDULE_NOT_FOUND_MSG)

        task_desc = _req_str(body.get("task_desc"), "task_desc")
        frequency = _validate_frequency(body.get("frequency"))
        machine_type_id = _opt_int(body.get("machine_type_id"), "machine_type_id")
        machine_id = _opt_int(body.get("machine_id"), "machine_id")

        if machine_id is not None and machine_id != (
            int(existing.machine_id) if existing.machine_id is not None else None
        ):
            machine = db.execute(
                machine_exists_query(), {"machine_id": machine_id}
            ).fetchone()
            if not machine:
                raise HTTPException(status_code=400, detail="Unknown machine_id")

        db.execute(
            update_pm_schedule_query(),
            {
                "pm_schedule_id": pm_schedule_id,
                "co_id": co_id,
                "machine_type_id": machine_type_id,
                "machine_id": machine_id,
                "task_desc": task_desc,
                "frequency": frequency,
                "freq_days": _resolve_freq_days(body.get("freq_days"), frequency),
                "seq_no": _opt_int(body.get("seq_no"), "seq_no"),
                "remarks": _opt_str(body.get("remarks")),
                "active": _active_flag(body.get("active"), default=int(existing.active or 0)),
                "updated_by": _user_id(token_data),
                "updated_date_time": datetime.now(),
            },
        )
        db.commit()
        return {"data": {"pm_schedule_id": pm_schedule_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/pm_schedule_delete/{pm_schedule_id}")
def pm_schedule_delete(
    pm_schedule_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete (active = 0).

    No FK guard: jute_mech_pm_execution_dtl snapshots task_desc at execution time
    and keeps pm_schedule_id only as a nullable back-reference, so retiring a task
    never rewrites PM history.
    """
    co_id = _require_co_id(request)
    try:
        existing = db.execute(
            get_pm_schedule_by_id_query(),
            {"pm_schedule_id": pm_schedule_id, "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=PM_SCHEDULE_NOT_FOUND_MSG)

        db.execute(
            soft_delete_pm_schedule_query(),
            {
                "pm_schedule_id": pm_schedule_id,
                "co_id": co_id,
                "updated_by": _user_id(token_data),
                "updated_date_time": datetime.now(),
            },
        )
        db.commit()
        return {"data": {"pm_schedule_id": pm_schedule_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

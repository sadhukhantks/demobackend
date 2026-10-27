"""Preventive-maintenance execution endpoints (Jute Mechanical & Maintenance).

Prefix ``/api/mechPM``. One PM execution = one PM visit on one machine, plus a
detail row per checklist task worked through on the day. Header + detail, NO
approval workflow — direct save.

Persona: Portal — ``get_tenant_db`` + ``get_current_user_with_refresh``,
``{"data": ...}`` responses (list endpoints add ``"total"``), soft delete via
``active = 0`` cascading to the task rows. Conventions mirror
``src/juteProduction/stoppage_entry.py``.

THE CHECKLIST, AND WHY task_desc IS SNAPSHOTTED
    ``/pm_checklist`` seeds the entry grid from ``jute_mech_pm_schedule`` — every
    template task that targets the machine directly or its machine type. On save,
    each task's wording is COPIED into ``jute_mech_pm_execution_dtl.task_desc``, so
    editing or retiring a template row later never rewrites what a fitter actually
    signed off months ago. A task with no ``pm_schedule_id`` is an ad-hoc job done
    on the day and is recorded just the same.

DERIVED FIELDS (computed here in Python, never by the DB):

    next_due_date = pm_date + pm_freq_days
    branch_id     = machine_mst -> dept_mst.branch_id   # machine_mst has no branch_id

    pm_freq_days resolves in order: the machine's jute_mech_machine_attr.pm_freq_days,
    else the matching template's freq_days, else PM_FREQUENCY_DAYS[frequency].

FEEDBACK INTO ``jute_mech_machine_attr`` — the point of this module:
every create and edit upserts the machine's maintenance-attribute row with
``last_pm_date`` and ``next_pm_due``. That satellite row is what the
``/maintenance-due`` report reads, so without this write-back a completed PM would
never clear the machine off the overdue list. The attr row is CREATED when absent,
which is the normal case — most machines carry no attr row until their first PM.
"""

from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteMechanical.constants import (
    PM_FREQUENCIES,
    PM_FREQUENCY_DAYS,
    PM_FREQUENCY_LABELS,
    PM_RESULT_LABELS,
    PM_RESULTS,
)
from src.juteMechanical.pm_query import (
    get_machine_attr_query,
    get_machine_context_query,
    get_pm_checklist_query,
    get_pm_execution_active_by_id_query,
    get_pm_execution_by_id_query,
    get_pm_execution_table_count_query,
    get_pm_execution_table_query,
    get_pm_execution_tasks_query,
    get_pm_machines_query,
    get_pm_schedule_freq_days_query,
    get_pm_schedule_task_desc_query,
    insert_pm_execution_dtl_query,
    insert_pm_execution_query,
    soft_delete_pm_execution_dtl_query,
    soft_delete_pm_execution_query,
    update_pm_execution_query,
    upsert_machine_attr_pm_query,
    get_latest_pm_date_query,
    clear_machine_attr_pm_query,
)


router = APIRouter()


NOT_FOUND_MSG = "PM execution not found"
BAD_FREQUENCY_MSG = "frequency must be one of: " + ", ".join(PM_FREQUENCIES)
BAD_RESULT_MSG = "result must be one of: " + ", ".join(PM_RESULTS)
UNKNOWN_MACHINE_MSG = "Unknown or inactive machine_id"
NO_TASK_DESC_MSG = "task_desc is required for a task with no pm_schedule_id"

MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 10


# =============================================================================
# Pydantic models
# =============================================================================


class PmTaskLine(BaseModel):
    """One checklist task result.

    ``pm_schedule_id`` is optional — an ad-hoc task not in the template is valid,
    but then ``task_desc`` must be supplied, since there is no template row to
    snapshot the wording from.
    """

    pm_schedule_id: Optional[int] = None
    task_desc: Optional[str] = None
    result: str = "OK"  # OK | NOT_OK | NA
    observation: Optional[str] = None


class PmExecutionCreate(BaseModel):
    co_id: int
    branch_id: Optional[int] = None  # derived from machine -> dept when omitted
    machine_id: int
    pm_date: date
    frequency: Optional[str] = None  # DAILY | WEEKLY | MONTHLY | QUARTERLY | HALF_YEARLY | ANNUAL
    done_by: Optional[str] = None  # free text: the fitter / crew who did the round
    downtime_min: Optional[int] = Field(default=None, ge=0)
    remarks: Optional[str] = None
    tasks: List[PmTaskLine] = []


class PmExecutionUpdate(PmExecutionCreate):
    """Edit is a full replace of the header and the task set.

    Same shape as create (co_id included — the edit path carries no query string),
    so next_due_date is recomputed and the machine-attribute feedback rewritten
    with the corrected date.
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


def _require_int(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
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


def _optional_frequency(request: Request) -> Optional[str]:
    """frequency query param, validated against PM_FREQUENCIES when present."""
    raw = request.query_params.get("frequency")
    if raw is None:
        return None
    freq = raw.strip().upper()
    if not freq:
        return None
    if freq not in PM_FREQUENCIES:
        raise HTTPException(status_code=400, detail=BAD_FREQUENCY_MSG)
    return freq


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


def _serialize_dates(row: dict, keys=("pm_date", "next_due_date")) -> dict:
    for k in keys:
        if row.get(k) is not None:
            row[k] = str(row[k])
    return row


def _machine_context(db: Session, machine_id: int) -> dict:
    """Validate the machine and return its identity, type and derived branch."""
    row = db.execute(
        get_machine_context_query(), {"machine_id": int(machine_id)}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=UNKNOWN_MACHINE_MSG)
    return dict(row._mapping)


def _validate_frequency(frequency: Optional[str]) -> Optional[str]:
    """frequency is optional on the body, but must be a known cycle when given."""
    if frequency is None:
        return None
    freq = frequency.strip().upper()
    if not freq:
        return None
    if freq not in PM_FREQUENCIES:
        raise HTTPException(status_code=400, detail=BAD_FREQUENCY_MSG)
    return freq


def _validate_result(result: Optional[str], index: int) -> str:
    """Each task's result must be OK / NOT_OK / NA (400 naming the task's row)."""
    value = (result or "").strip().upper()
    if value not in PM_RESULTS:
        raise HTTPException(
            status_code=400, detail=f"Task {index + 1}: {BAD_RESULT_MSG}"
        )
    return value


def _resolve_freq_days(
    db: Session,
    co_id: int,
    machine_id: int,
    machine_type_id: Optional[int],
    frequency: Optional[str],
) -> Optional[int]:
    """The PM interval in days: machine attribute, else template, else the enum map.

    The machine's own ``pm_freq_days`` wins because it is the maintenance engineer's
    deliberate override for that machine; the template's resolved ``freq_days`` is
    next; PM_FREQUENCY_DAYS is the last resort so a plain "MONTHLY" round still
    yields a due date. Returns None when nothing resolves — the visit is recorded
    with no next due date rather than an invented one.
    """
    attr = db.execute(
        get_machine_attr_query(), {"co_id": int(co_id), "machine_id": int(machine_id)}
    ).fetchone()
    if attr is not None:
        attr_days = attr._mapping.get("pm_freq_days")
        if attr_days:
            return int(attr_days)

    sched = db.execute(
        get_pm_schedule_freq_days_query(),
        {
            "co_id": int(co_id),
            "machine_id": int(machine_id),
            "machine_type_id": machine_type_id,
            "frequency": frequency,
        },
    ).fetchone()
    if sched is not None and sched._mapping.get("freq_days"):
        return int(sched._mapping["freq_days"])

    if frequency:
        mapped = PM_FREQUENCY_DAYS.get(frequency)
        if mapped:
            return int(mapped)

    return None


def _next_due_date(pm_date: date, freq_days: Optional[int]) -> Optional[date]:
    """pm_date + freq_days (None when no interval could be resolved)."""
    if not freq_days:
        return None
    return pm_date + timedelta(days=int(freq_days))


def _write_machine_attr_feedback(
    db: Session,
    co_id: int,
    machine_id: int,
    machine_type_id,
    frequency: Optional[str],
    user_id,
) -> None:
    """Recompute last_pm_date + next_pm_due on jute_mech_machine_attr.

    Call AFTER the PM execution rows have been written, on create, edit and delete.

    The dates are derived from MAX(pm_date) over the machine's live PM executions,
    never from the row just saved. Taking the saved row's date would be wrong in two
    ordinary situations: editing a back-dated PM (a January visit corrected after a
    June one exists) would rewind the machine to January and make it wrongly
    reappear as due, and deleting the latest PM would leave the date pointing at a
    removed record.

    When the last live PM execution is deleted, both columns are cleared but the
    attr row is kept — make/model/criticality and the overhaul fields are unrelated
    to PM history.

    This is what clears a completed PM off the due/overdue report.
    """
    latest = db.execute(
        get_latest_pm_date_query(),
        {"co_id": int(co_id), "machine_id": int(machine_id)},
    ).fetchone()
    last_pm_date = latest._mapping.get("last_pm_date") if latest else None

    if last_pm_date is None:
        db.execute(
            clear_machine_attr_pm_query(),
            {"co_id": int(co_id), "machine_id": int(machine_id), "updated_by": user_id},
        )
        return

    freq_days = _resolve_freq_days(
        db, int(co_id), int(machine_id), machine_type_id, frequency
    )
    db.execute(
        upsert_machine_attr_pm_query(),
        {
            "co_id": int(co_id),
            "machine_id": int(machine_id),
            "last_pm_date": last_pm_date,
            "next_pm_due": _next_due_date(last_pm_date, freq_days),
            "updated_by": user_id,
        },
    )


def _insert_tasks(
    db: Session, co_id: int, pm_exec_id: int, tasks: List[PmTaskLine], user_id
) -> None:
    """Insert the task rows, SNAPSHOTTING task_desc at execution time.

    Wording comes from the posted ``task_desc`` when present, otherwise it is read
    off the template row named by ``pm_schedule_id``. A task with neither is a 400 —
    a checklist line with no description is not history worth keeping.
    """
    for index, task in enumerate(tasks or []):
        result = _validate_result(task.result, index)

        task_desc = (task.task_desc or "").strip() or None
        schedule_id = int(task.pm_schedule_id) if task.pm_schedule_id is not None else None

        if task_desc is None and schedule_id is not None:
            sched = db.execute(
                get_pm_schedule_task_desc_query(),
                {"pm_schedule_id": schedule_id, "co_id": int(co_id)},
            ).fetchone()
            if not sched:
                raise HTTPException(
                    status_code=400,
                    detail=f"Task {index + 1}: unknown pm_schedule_id",
                )
            task_desc = sched._mapping.get("task_desc")

        if not task_desc:
            raise HTTPException(
                status_code=400, detail=f"Task {index + 1}: {NO_TASK_DESC_MSG}"
            )

        db.execute(
            insert_pm_execution_dtl_query(),
            {
                "pm_exec_id": int(pm_exec_id),
                "pm_schedule_id": schedule_id,
                "task_desc": task_desc,
                "result": result,
                "observation": task.observation,
                "updated_by": user_id,
            },
        )


def _save_pm_execution(
    db: Session,
    body: PmExecutionCreate,
    user_id,
    pm_exec_id: Optional[int] = None,
) -> int:
    """Shared create/edit body: validate, derive, write header + tasks + feedback.

    ``pm_exec_id`` None inserts; otherwise the existing header is replaced and its
    task rows are soft-deleted before the new set is inserted. Returns the
    pm_exec_id. The caller owns the transaction (commit / rollback).
    """
    frequency = _validate_frequency(body.frequency)
    machine = _machine_context(db, int(body.machine_id))

    branch_id = body.branch_id if body.branch_id is not None else machine.get("branch_id")

    freq_days = _resolve_freq_days(
        db,
        co_id=int(body.co_id),
        machine_id=int(body.machine_id),
        machine_type_id=machine.get("machine_type_id"),
        frequency=frequency,
    )
    next_due_date = _next_due_date(body.pm_date, freq_days)

    params = {
        "co_id": int(body.co_id),
        "branch_id": int(branch_id) if branch_id is not None else None,
        "machine_id": int(body.machine_id),
        "pm_date": body.pm_date,
        "frequency": frequency,
        "done_by": body.done_by,
        "downtime_min": int(body.downtime_min) if body.downtime_min is not None else None,
        "next_due_date": next_due_date,
        "remarks": body.remarks,
        "updated_by": user_id,
    }

    if pm_exec_id is None:
        result = db.execute(insert_pm_execution_query(), params)
        saved_id = result.lastrowid
    else:
        saved_id = int(pm_exec_id)
        params["pm_exec_id"] = saved_id
        db.execute(update_pm_execution_query(), params)
        db.execute(
            soft_delete_pm_execution_dtl_query(),
            {"pm_exec_id": saved_id, "updated_by": user_id},
        )

    _insert_tasks(db, int(body.co_id), saved_id, body.tasks, user_id)

    _write_machine_attr_feedback(
        db,
        co_id=int(body.co_id),
        machine_id=int(body.machine_id),
        machine_type_id=machine.get("machine_type_id"),
        frequency=frequency,
        user_id=user_id,
    )
    return saved_id


# =============================================================================
# Endpoints — setup + reads
# =============================================================================


@router.get("/pm_create_setup")
def pm_create_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Dropdown payload for the PM entry screen: machines (with dept/branch for the
    cascade) plus the frequency and result enums."""
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    try:
        machines = [
            dict(r._mapping)
            for r in db.execute(
                get_pm_machines_query(), {"branch_id": branch_id}
            ).fetchall()
        ]

        pm_frequencies = [
            {
                "code": code,
                "label": PM_FREQUENCY_LABELS.get(code, code),
                "freq_days": PM_FREQUENCY_DAYS.get(code),
            }
            for code in PM_FREQUENCIES
        ]
        pm_results = [
            {"code": code, "label": PM_RESULT_LABELS.get(code, code)}
            for code in PM_RESULTS
        ]

        return {
            "data": {
                "machines": machines,
                "pm_frequencies": pm_frequencies,
                "pm_results": pm_results,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pm_checklist")
def pm_checklist(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Template tasks that seed the checklist grid for one machine.

    Returns every active ``jute_mech_pm_schedule`` row targeting the machine
    directly OR its machine type, ordered by seq_no then pm_schedule_id, optionally
    narrowed to one ``frequency``.
    """
    co_id = _require_co_id(request)
    machine_id = _require_int(request, "machine_id")
    try:
        frequency = _optional_frequency(request)
        machine = _machine_context(db, machine_id)

        rows = db.execute(
            get_pm_checklist_query(),
            {
                "co_id": co_id,
                "machine_id": machine_id,
                "machine_type_id": machine.get("machine_type_id"),
                "frequency": frequency,
            },
        ).fetchall()
        tasks = [dict(r._mapping) for r in rows]

        return {
            "data": {
                "machine_id": machine_id,
                "machine_name": machine.get("machine_name"),
                "machine_type_id": machine.get("machine_type_id"),
                "branch_id": machine.get("branch_id"),
                "frequency": frequency,
                "tasks": tasks,
            },
            "total": len(tasks),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pm_execution_table")
def pm_execution_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated PM execution list.

    Filters: branch_id, date_from / date_to (on pm_date), machine_id — all
    optional, co_id required. Each row carries task_count and not_ok_count so the
    grid can flag a visit that found problems.
    """
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    try:
        date_from = _optional_date(request, "date_from")
        date_to = _optional_date(request, "date_to")
        machine_id = _optional_int(request, "machine_id")
        page, limit, offset = _pagination(request)

        filters = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": date_from,
            "date_to": date_to,
            "machine_id": machine_id,
        }

        total_row = db.execute(get_pm_execution_table_count_query(), filters).fetchone()
        total = int(total_row.total) if total_row else 0

        params = dict(filters)
        params["limit"] = limit
        params["offset"] = offset
        rows = [
            _serialize_dates(dict(r._mapping))
            for r in db.execute(get_pm_execution_table_query(), params).fetchall()
        ]

        return {"data": rows, "total": total, "page": page, "page_size": limit}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pm_execution_by_id/{pm_exec_id}")
def pm_execution_by_id(
    pm_exec_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """One PM execution: header plus its active ``tasks`` (snapshotted wording)."""
    co_id = _require_co_id(request)
    try:
        header_row = db.execute(
            get_pm_execution_by_id_query(),
            {"pm_exec_id": int(pm_exec_id), "co_id": co_id},
        ).fetchone()
        if not header_row:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        header = _serialize_dates(dict(header_row._mapping))
        header["tasks"] = [
            dict(r._mapping)
            for r in db.execute(
                get_pm_execution_tasks_query(), {"pm_exec_id": int(pm_exec_id)}
            ).fetchall()
        ]
        return {"data": header}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — CRUD
# =============================================================================


@router.post("/pm_execution_create")
def pm_execution_create(
    body: PmExecutionCreate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Insert one PM execution with its task results, then feed machine attributes.

    Snapshots each task's ``task_desc`` (from the body, else from the template row),
    computes next_due_date from pm_date + the resolved interval, derives branch_id
    from machine -> dept when omitted, and upserts
    jute_mech_machine_attr.last_pm_date + next_pm_due (creating the attr row if the
    machine has none).
    """
    try:
        user_id = token_data.get("user_id")
        pm_exec_id = _save_pm_execution(db, body, user_id)
        db.commit()
        return {"data": {"pm_exec_id": pm_exec_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/pm_execution_edit/{pm_exec_id}")
def pm_execution_edit(
    pm_exec_id: int,
    body: PmExecutionUpdate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Full replace of one PM execution: header, task set, and machine-attr feedback.

    The old task rows are soft-deleted and the posted set inserted with freshly
    snapshotted wording; next_due_date is recomputed and last_pm_date / next_pm_due
    rewritten for the (possibly changed) machine.
    """
    try:
        existing = db.execute(
            get_pm_execution_active_by_id_query(),
            {"pm_exec_id": int(pm_exec_id), "co_id": int(body.co_id)},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        user_id = token_data.get("user_id")
        _save_pm_execution(db, body, user_id, pm_exec_id=int(pm_exec_id))
        db.commit()
        return {"data": {"pm_exec_id": int(pm_exec_id)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/pm_execution_delete/{pm_exec_id}")
def pm_execution_delete(
    pm_exec_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete (active = 0), cascading to the execution's task rows.

    The machine-attribute feedback is recomputed afterwards, so deleting the
    machine's latest PM rolls last_pm_date back to the visit before it (or clears it
    when none remain) rather than leaving it pointing at a removed record.
    """
    co_id = _require_co_id(request)
    try:
        existing = db.execute(
            get_pm_execution_active_by_id_query(),
            {"pm_exec_id": int(pm_exec_id), "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        user_id = token_data.get("user_id")
        existing_map = dict(existing._mapping)
        machine_id = existing_map.get("machine_id")
        db.execute(
            soft_delete_pm_execution_dtl_query(),
            {"pm_exec_id": int(pm_exec_id), "updated_by": user_id},
        )
        db.execute(
            soft_delete_pm_execution_query(),
            {"pm_exec_id": int(pm_exec_id), "co_id": co_id, "updated_by": user_id},
        )
        if machine_id is not None:
            machine = _machine_context(db, int(machine_id))
            _write_machine_attr_feedback(
                db,
                co_id=co_id,
                machine_id=int(machine_id),
                machine_type_id=machine.get("machine_type_id"),
                frequency=existing_map.get("frequency"),
                user_id=user_id,
            )
        db.commit()
        return {"data": {"pm_exec_id": int(pm_exec_id)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

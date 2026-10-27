"""
EJM fortnightly group machine hours API (`ejm_wages_group_mc_hours`).

One row per (fortnight, machine, shift) with the machine hours and idle hours of
the Beaming / Press group machines — same machine set as the MC Group Production
page (ejmFneGroupProduction).

    period_id  -> forthnight_period_mst.forthnight_id
    machine_id -> machine_mst.machine_id (machine type name contains BEAM or PRESS)
    dept_id    -> copied from the machine's department; the client never sends it
    shift_id   -> shift_mst.shift_id (the machine's branch)

No status column, so rows are simply saved. One row per (period, machine, shift) is
enforced here — the table has only its PRIMARY KEY.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db
from src.hrms.ejmFneEffCodeType import _assert_fortnight_exists
from src.hrms.ejmFneGroupProduction import (
    _clean_decimal,
    _clean_int,
    get_branch_shifts_query,
    get_group_machines_query,
)

router = APIRouter()


# ─── SQL Queries ────────────────────────────────────────────────────


def get_group_mc_hours_list_query():
    """One line per fortnight that has rows for the branch — the index page."""
    return text("""
        SELECT h.period_id, f.start_date, f.end_date,
               COUNT(*) AS row_count,
               SUM(h.mc_hours) AS total_mc_hours,
               SUM(h.idle_hours) AS total_idle_hours
        FROM ejm_wages_group_mc_hours h
        INNER JOIN dept_mst d ON d.dept_id = h.dept_id
        LEFT JOIN forthnight_period_mst f ON f.forthnight_id = h.period_id
        WHERE d.branch_id = :branch_id
        GROUP BY h.period_id, f.start_date, f.end_date
        ORDER BY f.start_date DESC
    """)


def get_group_mc_hours_rows_query():
    """A fortnight's rows for the branch, in entry order."""
    return text("""
        SELECT h.grp_mc_hrs_id, h.dept_id, h.machine_id, h.shift_id, h.mc_hours, h.idle_hours
        FROM ejm_wages_group_mc_hours h
        INNER JOIN dept_mst d ON d.dept_id = h.dept_id
        WHERE h.period_id = :period_id AND d.branch_id = :branch_id
        ORDER BY h.grp_mc_hrs_id
    """)


def get_group_mc_hours_duplicate_query():
    return text("""
        SELECT grp_mc_hrs_id FROM ejm_wages_group_mc_hours
        WHERE period_id = :period_id AND machine_id = :machine_id AND shift_id = :shift_id
          AND (:grp_mc_hrs_id IS NULL OR grp_mc_hrs_id <> :grp_mc_hrs_id)
    """)


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/ejm_group_mc_hours_setup")
def ejm_group_mc_hours_setup(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Machine and shift dropdowns for a branch."""
    try:
        branch_id = _clean_int(request.query_params.get("branch_id"), "branch_id")
        machines = db.execute(get_group_machines_query(), {"branch_id": branch_id}).fetchall()
        shifts = db.execute(get_branch_shifts_query(), {"branch_id": branch_id}).fetchall()
        return {
            "data": {
                "machines": [
                    {"value": m.machine_id, "label": f"{m.machine_name} ({m.machine_type_name})",
                     "dept_id": m.dept_id, "dept_desc": m.dept_desc}
                    for m in machines
                ],
                "shifts": [{"value": s.shift_id, "label": s.shift_name} for s in shifts],
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_ejm_group_mc_hours_table")
def get_ejm_group_mc_hours_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated fortnights that have machine-hour rows for the branch."""
    try:
        branch_id = _clean_int(request.query_params.get("branch_id"), "branch_id")
        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 10))

        rows = [dict(r._mapping) for r in db.execute(get_group_mc_hours_list_query(),
                                                       {"branch_id": branch_id}).fetchall()]
        start = (page - 1) * limit
        return {"data": rows[start: start + limit], "total": len(rows), "page": page, "limit": limit}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_ejm_group_mc_hours_rows")
def get_ejm_group_mc_hours_rows(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """A fortnight's saved rows for the branch."""
    try:
        rows = db.execute(get_group_mc_hours_rows_query(), {
            "period_id": _clean_int(request.query_params.get("period_id"), "period_id"),
            "branch_id": _clean_int(request.query_params.get("branch_id"), "branch_id"),
        }).fetchall()
        return {"data": [dict(r._mapping) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ejm_group_mc_hours_save")
def ejm_group_mc_hours_save(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Insert (no grp_mc_hrs_id) or update one row.

    Body: {period_id, branch_id, grp_mc_hrs_id?, machine_id, shift_id, mc_hours, idle_hours}.
    """
    try:
        body = parse_json_body(request)
        period_id = _clean_int(body.get("period_id"), "Fortnight")
        _assert_fortnight_exists(db, period_id)
        branch_id = _clean_int(body.get("branch_id"), "Branch")
        machine_id = _clean_int(body.get("machine_id"), "Machine")
        shift_id = _clean_int(body.get("shift_id"), "Shift")
        row_id = body.get("grp_mc_hrs_id")
        row_id = _clean_int(row_id, "Row") if row_id not in (None, "") else None
        mc_hours = _clean_decimal(body.get("mc_hours"), "MC hours")
        idle_hours = _clean_decimal(body.get("idle_hours"), "Idle hours")

        machine = next((m for m in db.execute(get_group_machines_query(), {"branch_id": branch_id}).fetchall()
                        if m.machine_id == machine_id), None)
        if not machine:
            raise HTTPException(status_code=400, detail="Machine must be a Beaming or Press machine of this branch")
        if shift_id not in {s.shift_id for s in db.execute(get_branch_shifts_query(),
                                                            {"branch_id": branch_id}).fetchall()}:
            raise HTTPException(status_code=400, detail="Shift does not belong to this branch")

        if db.execute(get_group_mc_hours_duplicate_query(), {
            "period_id": period_id, "machine_id": machine_id, "shift_id": shift_id,
            "grp_mc_hrs_id": row_id,
        }).fetchone():
            raise HTTPException(status_code=400,
                                detail="This machine and shift already has a row for the fortnight")

        params = {"period_id": period_id, "dept_id": machine.dept_id, "machine_id": machine_id,
                  "shift_id": shift_id, "mc_hours": mc_hours, "idle_hours": idle_hours}
        if row_id is None:
            result = db.execute(text("""
                INSERT INTO ejm_wages_group_mc_hours
                    (period_id, dept_id, shift_id, machine_id, mc_hours, idle_hours)
                VALUES (:period_id, :dept_id, :shift_id, :machine_id, :mc_hours, :idle_hours)
            """), params)
            row_id = result.lastrowid
        else:
            result = db.execute(text("""
                UPDATE ejm_wages_group_mc_hours
                   SET period_id = :period_id, dept_id = :dept_id, shift_id = :shift_id,
                       machine_id = :machine_id, mc_hours = :mc_hours, idle_hours = :idle_hours
                 WHERE grp_mc_hrs_id = :grp_mc_hrs_id
            """), {**params, "grp_mc_hrs_id": row_id})
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Row not found")
        db.commit()

        return {"message": "Machine hours saved", "grp_mc_hrs_id": row_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

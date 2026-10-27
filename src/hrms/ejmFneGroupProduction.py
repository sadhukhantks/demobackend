"""
EJM fortnightly machine-group production API (`ejm_fne_group_production`).

One row per (fortnight, machine, shift, quality) with the fortnight's production
figures for Beaming and Press machines — the machine groups whose wages are paid
on group production rather than per operator.

    period_id   -> forthnight_period_mst.forthnight_id
    machine_id  -> machine_mst.machine_id (machine type name contains BEAM or PRESS)
    shift_id    -> shift_mst.shift_id (the machine's branch)
    quality_id  -> ejm_wages_quality_rate.wages_quality_rate_id, a rate row of the
                   machine's department

No status column on the table, so rows are simply saved (no approval step).
ponytail: one-row-per-(period, machine, shift, quality) is enforced here; the table
has only its PRIMARY KEY. Upgrade path: a UNIQUE key on those four columns.
"""

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.sql import bindparam, text

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db
from src.hrms.ejmFneEffCodeType import _assert_fortnight_exists
from src.hrms.prodWagesQualLink import _ejm_rate_label

router = APIRouter()

# production, target_prod, max_target_production, conv_production — all DECIMAL(10,3).
NUMBER_FIELDS = (
    ("production", "Production"),
    ("target_prod", "Target production"),
    ("max_target_production", "Max target production"),
    ("conv_production", "Conv production"),
)
MAX_DECIMAL = Decimal("9999999.999")


# ─── SQL Queries ────────────────────────────────────────────────────


def get_group_machines_query():
    """Active Beaming / Press machines of a branch."""
    return text("""
        SELECT m.machine_id, m.machine_name, m.dept_id, d.dept_desc, t.machine_type_name
        FROM machine_mst m
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        INNER JOIN machine_type_mst t ON t.machine_type_id = m.machine_type_id
        WHERE d.branch_id = :branch_id
          AND m.active = 1
          AND (UPPER(t.machine_type_name) LIKE '%BEAM%' OR UPPER(t.machine_type_name) LIKE '%PRESS%')
        ORDER BY t.machine_type_name, m.machine_name
    """)


def get_branch_shifts_query():
    return text("""
        SELECT shift_id, shift_name
        FROM shift_mst
        WHERE branch_id = :branch_id AND status = 1
        ORDER BY shift_name
    """)


def get_dept_quality_rates_query():
    return text("""
        SELECT wages_quality_rate_id, dept_id, quality_code, quality_desc, quality_rate
        FROM ejm_wages_quality_rate
        WHERE dept_id IN :dept_ids
        ORDER BY dept_id, quality_code
    """).bindparams(bindparam("dept_ids", expanding=True))


def get_group_production_list_query():
    """One line per fortnight that has rows for the branch — the index page."""
    return text("""
        SELECT g.period_id, f.start_date, f.end_date,
               COUNT(*) AS row_count,
               SUM(g.production) AS total_production
        FROM ejm_fne_group_production g
        INNER JOIN machine_mst m ON m.machine_id = g.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        LEFT JOIN forthnight_period_mst f ON f.forthnight_id = g.period_id
        WHERE d.branch_id = :branch_id
        GROUP BY g.period_id, f.start_date, f.end_date
        ORDER BY f.start_date DESC
    """)


def get_group_production_rows_query():
    """A fortnight's rows for the branch, oldest first (entry order)."""
    return text("""
        SELECT g.grp_prod_id, g.machine_id, g.shift_id, g.quality_id,
               g.production, g.target_prod, g.max_target_production, g.conv_production
        FROM ejm_fne_group_production g
        INNER JOIN machine_mst m ON m.machine_id = g.machine_id
        INNER JOIN dept_mst d ON d.dept_id = m.dept_id
        WHERE g.period_id = :period_id AND d.branch_id = :branch_id
        ORDER BY g.grp_prod_id
    """)


def get_group_production_duplicate_query():
    return text("""
        SELECT grp_prod_id FROM ejm_fne_group_production
        WHERE period_id = :period_id AND machine_id = :machine_id
          AND shift_id = :shift_id AND quality_id = :quality_id
          AND (:grp_prod_id IS NULL OR grp_prod_id <> :grp_prod_id)
    """)


# ─── Helpers ────────────────────────────────────────────────────────


def _clean_int(raw, label: str) -> int:
    if raw in (None, ""):
        raise HTTPException(status_code=400, detail=f"{label} is required")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} is invalid")


def _clean_decimal(raw, label: str):
    """DECIMAL(10,3), optional -> None, or a value within range."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} must be a number")
    if not (-MAX_DECIMAL <= value <= MAX_DECIMAL):
        raise HTTPException(status_code=400, detail=f"{label} is out of range")
    return value


def _quality_options_by_dept(db: Session, machines: list[dict]) -> dict[int, list[dict]]:
    """dept_id -> that department's rated wages qualities (ejm_wages_quality_rate)."""
    dept_ids = list({m["dept_id"] for m in machines})
    by_dept = {d: [] for d in dept_ids}
    if not dept_ids:
        return by_dept
    for r in db.execute(get_dept_quality_rates_query(), {"dept_ids": dept_ids}).fetchall():
        by_dept[r.dept_id].append({"value": int(r.wages_quality_rate_id),
                                   "label": _ejm_rate_label(r.quality_code, r.quality_desc, r.quality_rate)})
    return by_dept


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/ejm_fne_group_production_setup")
def ejm_fne_group_production_setup(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Machine, shift and per-department quality dropdowns for a branch."""
    try:
        branch_id = _clean_int(request.query_params.get("branch_id"), "branch_id")
        machines = [dict(r._mapping) for r in db.execute(get_group_machines_query(),
                                                           {"branch_id": branch_id}).fetchall()]
        shifts = db.execute(get_branch_shifts_query(), {"branch_id": branch_id}).fetchall()
        return {
            "data": {
                "machines": [
                    {"value": m["machine_id"], "label": f"{m['machine_name']} ({m['machine_type_name']})",
                     "dept_id": m["dept_id"]}
                    for m in machines
                ],
                "shifts": [{"value": s.shift_id, "label": s.shift_name} for s in shifts],
                "qualities_by_dept": {
                    str(k): v for k, v in _quality_options_by_dept(db, machines).items()
                },
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_ejm_fne_group_production_table")
def get_ejm_fne_group_production_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated fortnights that have group production rows for the branch."""
    try:
        branch_id = _clean_int(request.query_params.get("branch_id"), "branch_id")
        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 10))

        rows = [dict(r._mapping) for r in db.execute(get_group_production_list_query(),
                                                       {"branch_id": branch_id}).fetchall()]
        start = (page - 1) * limit
        return {"data": rows[start: start + limit], "total": len(rows), "page": page, "limit": limit}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_ejm_fne_group_production_rows")
def get_ejm_fne_group_production_rows(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """A fortnight's saved rows for the branch."""
    try:
        rows = db.execute(get_group_production_rows_query(), {
            "period_id": _clean_int(request.query_params.get("period_id"), "period_id"),
            "branch_id": _clean_int(request.query_params.get("branch_id"), "branch_id"),
        }).fetchall()
        return {"data": [dict(r._mapping) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ejm_fne_group_production_save")
def ejm_fne_group_production_save(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Insert (no grp_prod_id) or update one row.

    Body: {period_id, branch_id, grp_prod_id?, machine_id, shift_id, quality_id,
           production, target_prod, max_target_production, conv_production}.
    """
    try:
        body = parse_json_body(request)
        period_id = _clean_int(body.get("period_id"), "Fortnight")
        _assert_fortnight_exists(db, period_id)
        branch_id = _clean_int(body.get("branch_id"), "Branch")
        machine_id = _clean_int(body.get("machine_id"), "Machine")
        shift_id = _clean_int(body.get("shift_id"), "Shift")
        quality_id = _clean_int(body.get("quality_id"), "Quality")
        grp_prod_id = body.get("grp_prod_id")
        grp_prod_id = _clean_int(grp_prod_id, "Row") if grp_prod_id not in (None, "") else None
        numbers = {field: _clean_decimal(body.get(field), label) for field, label in NUMBER_FIELDS}

        machines = [dict(r._mapping) for r in db.execute(get_group_machines_query(),
                                                           {"branch_id": branch_id}).fetchall()]
        machine = next((m for m in machines if m["machine_id"] == machine_id), None)
        if not machine:
            raise HTTPException(status_code=400, detail="Machine must be a Beaming or Press machine of this branch")
        shifts = {s.shift_id for s in db.execute(get_branch_shifts_query(), {"branch_id": branch_id}).fetchall()}
        if shift_id not in shifts:
            raise HTTPException(status_code=400, detail="Shift does not belong to this branch")
        qualities = _quality_options_by_dept(db, [machine])[machine["dept_id"]]
        if quality_id not in {q["value"] for q in qualities}:
            raise HTTPException(status_code=400, detail="Quality does not belong to this machine's department")

        if db.execute(get_group_production_duplicate_query(), {
            "period_id": period_id, "machine_id": machine_id, "shift_id": shift_id,
            "quality_id": quality_id, "grp_prod_id": grp_prod_id,
        }).fetchone():
            raise HTTPException(status_code=400,
                                detail="This machine, shift and quality already has a row for the fortnight")

        params = {"period_id": period_id, "machine_id": machine_id, "shift_id": shift_id,
                  "quality_id": quality_id, **numbers}
        if grp_prod_id is None:
            result = db.execute(text("""
                INSERT INTO ejm_fne_group_production
                    (period_id, machine_id, shift_id, quality_id,
                     production, target_prod, max_target_production, conv_production)
                VALUES (:period_id, :machine_id, :shift_id, :quality_id,
                        :production, :target_prod, :max_target_production, :conv_production)
            """), params)
            grp_prod_id = result.lastrowid
        else:
            result = db.execute(text("""
                UPDATE ejm_fne_group_production
                   SET period_id = :period_id, machine_id = :machine_id, shift_id = :shift_id,
                       quality_id = :quality_id, production = :production, target_prod = :target_prod,
                       max_target_production = :max_target_production, conv_production = :conv_production
                 WHERE grp_prod_id = :grp_prod_id
            """), {**params, "grp_prod_id": grp_prod_id})
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Row not found")
        db.commit()

        return {"message": "Group production saved", "grp_prod_id": grp_prod_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

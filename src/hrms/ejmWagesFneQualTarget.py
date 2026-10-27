"""
EJM fortnightly wages quality target API (`ejm_wages_fne_qual_target`).

One row per (fortnight, department, production quality) holding the Target
Prod/Eff(5) that combination is measured against in the fortnightly wages run.

    period_id          -> forthnight_period_mst.forthnight_id
    dept_id            -> dept_mst.dept_id
    process            -> one of link_prodquality_wagequality_mst's processes (Hemming /
                          Heracle / Handsewer / Press), optional; a quality may carry
                          one target per process plus a process-less one
    quality_id         -> ejm_wages_quality_rate.wages_quality_rate_id, limited to
                          the department's own rate rows (rate.dept_id = dept_id)
    fne_qual_traget_id -> surrogate row id (legacy spelling kept)
    status_id          -> 21 Drafted (editable) -> 3 Approved (locked), per row

The table carries no primary key, no unique key and no auto-increment, so this
module owns all three: it assigns `fne_qual_traget_id` as MAX+1 and keys rows on
(period_id, dept_id, quality_id, process) itself.

ponytail: uniqueness in code rather than an ALTER on a table the tenant created.
Upgrade path when it matters: PRIMARY KEY (fne_qual_traget_id) + UNIQUE
(period_id, dept_id, quality_id).
"""

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db
from src.hrms.ejmFneEffCodeType import _assert_fortnight_exists
# The wages-quality picker the Quality Rate page uses and the rate label live on
# the Prod/Wages Quality Link page — reuse them.
from src.hrms.prodWagesQualLink import _ejm_rate_label, get_wages_qualities_query, quality_type_for

router = APIRouter()

STATUS_DRAFT = 21
STATUS_APPROVED = 3


# ─── SQL Queries ────────────────────────────────────────────────────


def get_fne_qual_target_rows_query():
    """This fortnight's saved targets for one department."""
    return text("""
        SELECT fne_qual_traget_id, quality_id, process, eff_pc, status_id
        FROM ejm_wages_fne_qual_target
        WHERE period_id = :period_id AND dept_id = :dept_id
    """)


def get_fne_qual_target_list_query():
    """One line per saved target (fortnight, department, quality) — the index page's rows."""
    return text("""
        SELECT
            t.fne_qual_traget_id,
            t.period_id,
            f.start_date,
            f.end_date,
            t.dept_id,
            d.dept_desc,
            t.quality_id,
            r.quality_code,
            r.quality_desc,
            r.quality_rate,
            r.target,
            t.process,
            t.eff_pc,
            t.status_id
        FROM ejm_wages_fne_qual_target t
        INNER JOIN dept_mst d ON d.dept_id = t.dept_id
        LEFT JOIN forthnight_period_mst f ON f.forthnight_id = t.period_id
        LEFT JOIN ejm_wages_quality_rate r ON r.wages_quality_rate_id = t.quality_id
        WHERE d.branch_id = :branch_id
          AND (:search IS NULL
               OR d.dept_desc LIKE :search
               OR r.quality_code LIKE :search
               OR r.quality_desc LIKE :search)
        ORDER BY f.start_date DESC, d.dept_desc, r.quality_code, t.process
    """)


def get_fne_qual_target_departments_query():
    return text("""
        SELECT d.dept_id, d.dept_desc, d.dept_code, d.branch_id
        FROM dept_mst d
        WHERE d.branch_id = :branch_id
          AND (:rated = 0 OR EXISTS (
                SELECT 1 FROM ejm_wages_quality_rate r WHERE r.dept_id = d.dept_id))
        ORDER BY d.dept_desc
    """)


# ─── Helpers ────────────────────────────────────────────────────────


def _rate_options(db: Session, dept_id: int) -> list[dict]:
    """The department's ejm_wages_quality_rate rows as {value, label, target}."""
    rows = db.execute(text("""
        SELECT wages_quality_rate_id, quality_code, quality_desc, quality_rate, target
        FROM ejm_wages_quality_rate
        WHERE dept_id = :dept_id
        ORDER BY quality_code
    """), {"dept_id": dept_id}).fetchall()
    return [
        {
            "value": r.wages_quality_rate_id,
            "label": _ejm_rate_label(r.quality_code, r.quality_desc, r.quality_rate),
            # standard production (legacy QUALRATE TARGET), shown beside the entered target
            "target": str(r.target) if r.target is not None else None,
        }
        for r in rows
    ]


# Daily production table + its production-quality column per department type
# (link_prodquality_wagequality_mst.prod_quality_id points at that column).
_PROD_SOURCES = {
    "weaving": ("jute_prod_weaving_daily", "weaving_quality_id"),
    "beaming": ("jute_prod_beaming_daily", "bm_quality_id"),
    "finishing": ("jute_prod_finishing_daily", "finishing_quality_id"),
    "winding": ("jute_prod_winding_doff", "item_id"),
}


def _ran_quality_ids(db: Session, period_id: int, dept_id: int) -> set[int] | None:
    """Wages qualities that ran in the fortnight: a linked production quality has
    daily production in the period, or the quality has a group-production row.
    None when the department has no production source (JUTE, BATCHING) — no filter.
    ponytail: dept type by name via quality_type_for, as on the link master."""
    dept = db.execute(text("SELECT dept_desc, branch_id FROM dept_mst WHERE dept_id = :id"),
                      {"id": dept_id}).fetchone()
    source = _PROD_SOURCES.get(quality_type_for(dept.dept_desc if dept else None))
    if not source:
        return None
    table, col = source
    rows = db.execute(text(f"""
        SELECT l.wages_quality_id AS quality_id
        FROM link_prodquality_wagequality_mst l
        JOIN forthnight_period_mst f ON f.forthnight_id = :period_id
        WHERE l.dept_id = :dept_id AND l.active = 1
          AND EXISTS (SELECT 1 FROM {table} p
                      WHERE p.{col} = l.prod_quality_id AND p.branch_id = :branch_id
                        AND p.active = 1 AND p.tran_date BETWEEN f.start_date AND f.end_date)
        UNION
        SELECT g.quality_id
        FROM ejm_fne_group_production g
        JOIN ejm_wages_quality_rate r ON r.wages_quality_rate_id = g.quality_id AND r.dept_id = :dept_id
        WHERE g.period_id = :period_id
    """), {"period_id": period_id, "dept_id": dept_id, "branch_id": dept.branch_id}).fetchall()
    return {r.quality_id for r in rows}


def _assert_department(db: Session, dept_id: int) -> int:
    if not db.execute(text("SELECT 1 FROM dept_mst WHERE dept_id = :id"), {"id": dept_id}).fetchone():
        raise HTTPException(status_code=400, detail="Department not found")
    return dept_id


def _clean_eff_pc(raw):
    """DECIMAL(6,2), optional -> None, or a value within range."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Target Prod/Eff(5) must be a number")
    if not (Decimal("-9999.99") <= value <= Decimal("9999.99")):
        raise HTTPException(status_code=400, detail="Target Prod/Eff(5) is out of range")
    return value


def _clean_int(raw, label: str) -> int:
    if raw in (None, ""):
        raise HTTPException(status_code=400, detail=f"{label} is required")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} is invalid")


def _link_processes(db: Session) -> list[str]:
    """The link page's process names (Hemming / Heracle / Handsewer / Press)."""
    return [r.process for r in db.execute(text("""
        SELECT DISTINCT TRIM(process) AS process FROM link_prodquality_wagequality_mst
        WHERE TRIM(COALESCE(process, '')) <> '' ORDER BY 1
    """)).fetchall()]


def _existing_rows(db: Session, period_id: int, dept_id: int) -> dict:
    """(quality_id, process) -> saved row, for one fortnight + department."""
    return {
        (r.quality_id, r.process or None): r
        for r in db.execute(get_fne_qual_target_rows_query(), {
            "period_id": period_id,
            "dept_id": dept_id,
        }).fetchall()
    }


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/get_ejm_fne_qual_target_departments")
def get_ejm_fne_qual_target_departments(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Departments for the branch (from the sidebar). ``rated=1`` keeps only those
    with rows in ejm_wages_quality_rate — the target page's picker."""
    try:
        branch_id = request.query_params.get("branch_id")
        if not branch_id:
            raise HTTPException(status_code=400, detail="branch_id is required")

        rows = db.execute(get_fne_qual_target_departments_query(), {
            "branch_id": int(branch_id),
            "rated": 1 if request.query_params.get("rated") in ("1", "true") else 0,
        }).fetchall()
        return {"data": [dict(r._mapping) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_ejm_fne_qual_target_qualities")
def get_ejm_fne_qual_target_qualities(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Wages-quality rate cards for the Quality Rate page's picker."""
    try:
        co_id = request.query_params.get("co_id")
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        branch_id = request.query_params.get("branch_id")

        rows = db.execute(get_wages_qualities_query(), {
            "co_id": int(co_id),
            "branch_id": int(branch_id) if branch_id else None,
        }).fetchall()
        return {"data": [dict(r._mapping) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_ejm_fne_qual_target_table")
def get_ejm_fne_qual_target_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated saved targets (one per quality) for the sidebar branch."""
    try:
        branch_id = _clean_int(request.query_params.get("branch_id"), "branch_id")
        search = request.query_params.get("search")
        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 10))

        rows = []
        for r in db.execute(get_fne_qual_target_list_query(), {
            "branch_id": branch_id,
            "search": f"%{search}%" if search else None,
        }).fetchall():
            m = dict(r._mapping)
            m["quality_label"] = (_ejm_rate_label(m.get("quality_code"), m.get("quality_desc"), m.get("quality_rate"))
                                  or str(m.get("quality_id") or ""))
            rows.append(m)
        start = (page - 1) * limit
        return {"data": rows[start: start + limit], "total": len(rows), "page": page, "limit": limit}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_ejm_fne_qual_target_grid")
def get_ejm_fne_qual_target_grid(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """This fortnight's saved rows for the department, plus its quality dropdown options."""
    try:
        period_id = _clean_int(request.query_params.get("period_id"), "period_id")
        dept_id = _assert_department(db, _clean_int(request.query_params.get("dept_id"), "dept_id"))
        options = _rate_options(db, dept_id)
        labels = {o["value"]: o["label"] for o in options}

        saved = _existing_rows(db, period_id, dept_id)
        rows = [
            {
                "fne_qual_traget_id": r.fne_qual_traget_id,
                "quality_id": r.quality_id,
                "quality_label": labels.get(r.quality_id, str(r.quality_id)),
                "process": r.process or None,
                "eff_pc": r.eff_pc,
                "status_id": r.status_id or STATUS_DRAFT,
            }
            for r in sorted(saved.values(), key=lambda r: r.fne_qual_traget_id)
        ]
        # Dropdown: every quality of the department, the ones that ran this fortnight first.
        ran = _ran_quality_ids(db, period_id, dept_id) or set()
        options = [{**o, "ran": o["value"] in ran} for o in options]
        options.sort(key=lambda o: not o["ran"])  # stable: quality_code order kept within each group
        return {"data": rows, "qualities": options, "processes": _link_processes(db)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ejm_fne_qual_target_save")
def ejm_fne_qual_target_save(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Upsert targets for one fortnight + department.

    Body: {period_id, dept_id, approve, lines: [{id, quality_id, process, eff_pc}]}.
    ``process`` is optional, one of the link page's processes. ``id`` names a saved
    row to modify (its process may change); without it rows match on (quality, process).
    Blank lines are skipped; approved rows are locked; ``approve: true`` saves the
    lines straight to Approved (the grid's per-row Approve button sends one line).
    """
    try:
        body = parse_json_body(request)
        period_id = _clean_int(body.get("period_id"), "Fortnight")
        _assert_fortnight_exists(db, period_id)
        dept_id = _assert_department(db, _clean_int(body.get("dept_id"), "Department"))
        status_id = STATUS_APPROVED if body.get("approve") is True else STATUS_DRAFT

        lines = body.get("lines") or []
        if not isinstance(lines, list):
            raise HTTPException(status_code=400, detail="lines must be a list")

        valid = {o["value"] for o in _rate_options(db, dept_id)}
        processes = set(_link_processes(db))
        saved = _existing_rows(db, period_id, dept_id)
        saved_by_id = {r.fne_qual_traget_id: r for r in saved.values()}
        next_id = db.execute(text(
            "SELECT COALESCE(MAX(fne_qual_traget_id), 0) + 1 AS next_id "
            "FROM ejm_wages_fne_qual_target"
        )).fetchone().next_id

        seen, count = set(), 0
        for line in lines:
            eff_pc = _clean_eff_pc(line.get("eff_pc"))
            if eff_pc is None:
                continue
            quality_id = _clean_int(line.get("quality_id"), "Quality")
            if quality_id not in valid:
                raise HTTPException(status_code=400, detail="Quality does not belong to this department")
            process = str(line.get("process") or "").strip() or None
            if process is not None and process not in processes:
                raise HTTPException(status_code=400, detail="Unknown process")
            key = (quality_id, process)
            if key in seen:
                raise HTTPException(status_code=400, detail="A quality / process appears more than once")
            seen.add(key)

            row = saved_by_id.get(line.get("id")) if line.get("id") else saved.get(key)
            if row and row.status_id == STATUS_APPROVED:
                raise HTTPException(status_code=400, detail="An approved target is locked")
            other = saved.get(key)
            if row and other and other.fne_qual_traget_id != row.fne_qual_traget_id:
                raise HTTPException(status_code=400, detail="This quality / process already has a target")
            if row:
                db.execute(text("""
                    UPDATE ejm_wages_fne_qual_target
                       SET quality_id = :quality_id, process = :process, eff_pc = :eff_pc, status_id = :status_id
                     WHERE fne_qual_traget_id = :id
                """), {"quality_id": quality_id, "process": process, "eff_pc": eff_pc,
                       "status_id": status_id, "id": row.fne_qual_traget_id})
            else:
                db.execute(text("""
                    INSERT INTO ejm_wages_fne_qual_target
                        (fne_qual_traget_id, period_id, dept_id, quality_id, process, eff_pc, status_id)
                    VALUES (:id, :period_id, :dept_id, :quality_id, :process, :eff_pc, :status_id)
                """), {
                    "id": next_id, "period_id": period_id, "dept_id": dept_id, "quality_id": quality_id,
                    "process": process, "eff_pc": eff_pc, "status_id": status_id,
                })
                next_id += 1
            count += 1

        if status_id == STATUS_APPROVED and not count:
            raise HTTPException(status_code=400, detail="Enter Target Prod/Eff(5) before approving")
        db.commit()

        return {
            "message": ("Quality target approved" if status_id == STATUS_APPROVED
                        else f"{count} quality target(s) saved"),
            "saved": count,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

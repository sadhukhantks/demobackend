"""
Production Quality ↔ Wages Quality link master (link_prodquality_wagequality_mst).

One row per (department, production quality) saying which wages-quality rate card
(tbl_wages_quality_master) that production quality is paid on. The production
quality source depends on the department:

    weaving   -> jute_prod_weaving_quality
    beaming   -> jute_prod_bm_quality
    finishing -> jute_prod_finishing_quality (labelled by its item)
    winding   -> yarn items (the only "winding quality" this app has)

The department's type is derived from dept_mst.dept_desc (WEAV/WIND/FINISH/BEAM;
SACK SEWING counts as finishing),
which is how tenants name them (WEAVING(EJM), COP WINDING(EJM), ...). branch_id on
the link row is copied from the department, so the client never sends it.

wages_quality_id points at one of two rate cards, chosen by branch: a branch with
rows in ejm_wages_quality_rate (EJM, branch 29) links to
ejm_wages_quality_rate.wages_quality_rate_id; every other branch (NJM 87) keeps
tbl_wages_quality_master.q_code_id. A rate row carries its own dept_id, so the
list resolves it only when that dept matches the link's dept.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.sql import text

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import parse_json_body
from src.config.db import get_tenant_db
from src.juteProduction.beaming_query import get_bm_quality_list_query
from src.juteProduction.finishing_query import get_finishing_quality_list_query
from src.juteProduction.spinning_query import get_yarn_qualities_query
from src.juteProduction.weaving_query import get_weaving_quality_list_query
from src.models.hrms import LinkProdQualityWageQuality

router = APIRouter()

# ponytail: substring match on the department name; a dept_mst type column is the
# upgrade path if a tenant ever names departments without these words.
QUALITY_TYPES = (
    ("WEAV", "weaving"),
    ("WIND", "winding"),
    ("FINISH", "finishing"),
    ("BEAM", "beaming"),
    ("SACK", "finishing"),  # sack sewing is paid on finishing qualities
)

# Optional finishing process on a link; blank/None is stored as NULL.
PROCESSES = ("Hemming", "Heracle", "Handsewer", "Press")


def quality_type_for(dept_desc: str | None) -> str | None:
    name = (dept_desc or "").upper()
    for needle, qtype in QUALITY_TYPES:
        if needle in name:
            return qtype
    return None


# ─── SQL Queries ────────────────────────────────────────────────────


def get_link_list_query():
    """Link rows joined to every possible quality source; Python picks the label
    that matches the department's type."""
    return text("""
        SELECT
            l.link_prod_wages_id,
            l.prod_quality_id,
            l.wages_quality_id,
            l.branch_id,
            l.dept_id,
            l.active,
            l.process,
            d.dept_desc,
            bm.branch_name,
            wg.q_code AS wages_q_code,
            wg.quality AS wages_quality,
            er.quality_code AS ejm_quality_code,
            er.quality_desc AS ejm_quality_desc,
            er.quality_rate AS ejm_quality_rate,
            wq.weaving_quality_code,
            wq.weaving_quality_name,
            bq.bm_quality_code,
            bq.bm_quality_name,
            bi.item_name AS bm_item_name,
            fi.item_code AS finishing_item_code,
            fi.item_name AS finishing_item_name,
            wi.item_code AS winding_item_code,
            wi.item_name AS winding_item_name
        FROM link_prodquality_wagequality_mst l
        INNER JOIN dept_mst d ON d.dept_id = l.dept_id
        INNER JOIN branch_mst bm ON bm.branch_id = l.branch_id
        LEFT JOIN tbl_wages_quality_master wg ON wg.q_code_id = l.wages_quality_id
        LEFT JOIN ejm_wages_quality_rate er
               ON er.wages_quality_rate_id = l.wages_quality_id AND er.dept_id = l.dept_id
        LEFT JOIN jute_prod_weaving_quality wq ON wq.weaving_quality_id = l.prod_quality_id
        LEFT JOIN jute_prod_bm_quality bq ON bq.bm_quality_id = l.prod_quality_id
        LEFT JOIN item_mst bi ON bi.item_id = bq.item_id
        LEFT JOIN jute_prod_finishing_quality fq ON fq.finishing_quality_id = l.prod_quality_id
        LEFT JOIN item_mst fi ON fi.item_id = fq.item_id
        LEFT JOIN item_mst wi ON wi.item_id = l.prod_quality_id
        WHERE l.active = 1
          AND bm.co_id = :co_id
          AND (:branch_id IS NULL OR l.branch_id = :branch_id)
        ORDER BY d.dept_desc, l.link_prod_wages_id DESC
    """)


def get_link_by_id_query():
    return text("""
        SELECT link_prod_wages_id, prod_quality_id, wages_quality_id, branch_id, dept_id, active, process
        FROM link_prodquality_wagequality_mst
        WHERE link_prod_wages_id = :record_id AND active = 1
    """)


def get_departments_query():
    """Departments in scope; the caller keeps only those with a quality type."""
    return text("""
        SELECT d.dept_id, d.dept_desc, d.branch_id, bm.co_id
        FROM dept_mst d
        INNER JOIN branch_mst bm ON bm.branch_id = d.branch_id
        WHERE bm.co_id = :co_id
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY d.dept_desc
    """)


def get_department_query():
    return text("""
        SELECT d.dept_id, d.dept_desc, d.branch_id, bm.co_id
        FROM dept_mst d
        INNER JOIN branch_mst bm ON bm.branch_id = d.branch_id
        WHERE d.dept_id = :dept_id
    """)


def get_wages_qualities_query():
    return text("""
        SELECT q_code_id, q_code, quality, master_dept_id, branch_id
        FROM tbl_wages_quality_master
        WHERE is_active = 1
          AND company_id = :co_id
          AND (:branch_id IS NULL OR branch_id = :branch_id OR branch_id IS NULL)
        ORDER BY q_code
    """)


def get_ejm_rate_branches_query():
    """Branches whose departments are rated in ejm_wages_quality_rate."""
    return text("""
        SELECT DISTINCT d.branch_id
        FROM ejm_wages_quality_rate r
        INNER JOIN dept_mst d ON d.dept_id = r.dept_id
    """)


def get_ejm_rates_query():
    return text("""
        SELECT r.wages_quality_rate_id, r.dept_id, r.quality_code, r.quality_desc, r.quality_rate
        FROM ejm_wages_quality_rate r
        INNER JOIN dept_mst d ON d.dept_id = r.dept_id
        INNER JOIN branch_mst bm ON bm.branch_id = d.branch_id
        WHERE bm.co_id = :co_id
          AND (:branch_id IS NULL OR d.branch_id = :branch_id)
        ORDER BY r.dept_id, r.quality_code
    """)


def _duplicate_query():
    return text("""
        SELECT COUNT(*) AS cnt FROM link_prodquality_wagequality_mst
        WHERE active = 1
          AND dept_id = :dept_id
          AND prod_quality_id = :prod_quality_id
          AND process <=> :process
          AND (:record_id IS NULL OR link_prod_wages_id <> :record_id)
    """)


# ─── Helpers ────────────────────────────────────────────────────────


def _int(value, default: int, label: str) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} must be a number")


def _branch_param(request: Request) -> int | None:
    raw = request.query_params.get("branch_id")
    return _int(raw, 0, "Branch") if raw not in (None, "", "null") else None


def _require_co_id(request: Request) -> int:
    co_id = request.query_params.get("co_id")
    if not co_id:
        raise HTTPException(status_code=400, detail="co_id is required")
    return _int(co_id, 0, "co_id")


def _load_department(db: Session, dept_id: int) -> dict:
    row = db.execute(get_department_query(), {"dept_id": dept_id}).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Department not found")
    dept = dict(row._mapping)
    dept["quality_type"] = quality_type_for(dept.get("dept_desc"))
    if not dept["quality_type"]:
        raise HTTPException(
            status_code=400,
            detail="Department must be a weaving, winding, finishing or beaming department",
        )
    return dept


def _code_name(code, name) -> str:
    """'CODE - Name' dropdown label; degrades to whichever half exists."""
    return f"{code or ''} - {name or ''}".strip(" -")


def _ejm_rate_label(code, desc, rate) -> str:
    """'002 - JUTE ISSU (0.050000)'."""
    label = _code_name(code, desc)
    return f"{label} ({rate:f})" if rate is not None else label


def _ejm_rate_branches(db: Session) -> set[int]:
    return {r.branch_id for r in db.execute(get_ejm_rate_branches_query()).fetchall()}


def _check_wages_quality(db: Session, values: dict, dept: dict):
    """wages_quality_id must exist in the rate card the department's branch uses."""
    if dept["branch_id"] in _ejm_rate_branches(db):
        ok = db.execute(text(
            "SELECT 1 FROM ejm_wages_quality_rate WHERE wages_quality_rate_id = :id AND dept_id = :dept_id"
        ), {"id": values["wages_quality_id"], "dept_id": values["dept_id"]}).fetchone()
    else:
        ok = db.execute(text(
            "SELECT 1 FROM tbl_wages_quality_master WHERE q_code_id = :id"
        ), {"id": values["wages_quality_id"]}).fetchone()
    if not ok:
        raise HTTPException(status_code=400, detail="Wages quality is not rated for this department")


def _prod_quality_options(db: Session, dept: dict) -> list[dict]:
    """Production qualities for the department's type, as {value, label}."""
    params = {"co_id": dept["co_id"], "branch_id": dept["branch_id"], "item_id": None}
    qtype = dept["quality_type"]
    if qtype == "weaving":
        rows = db.execute(get_weaving_quality_list_query(), {**params, "search": None}).fetchall()
        return [{"value": str(r.weaving_quality_id), "label": _code_name(r.weaving_quality_code, r.weaving_quality_name)}
                for r in rows]
    if qtype == "beaming":
        rows = db.execute(get_bm_quality_list_query(), params).fetchall()
        # bm_quality_name is often blank; the beam's item names it then.
        return [{"value": str(r.bm_quality_id), "label": _code_name(r.bm_quality_code, r.bm_quality_name or r.item_name)}
                for r in rows]
    if qtype == "finishing":
        rows = db.execute(get_finishing_quality_list_query(), {**params, "quality_type": None}).fetchall()
        return [{"value": str(r.finishing_quality_id), "label": _code_name(r.item_code, r.item_name)}
                for r in rows]
    rows = db.execute(get_yarn_qualities_query(), {"co_id": dept["co_id"], "branch_id": dept["branch_id"]}).fetchall()
    return [{"value": str(r.item_id), "label": _code_name(r.item_code, r.item_name)}
            for r in rows]


def _prod_quality_label(m: dict) -> str:
    qtype = quality_type_for(m.get("dept_desc"))
    label = {
        "weaving": _code_name(m.get("weaving_quality_code"), m.get("weaving_quality_name")),
        "beaming": _code_name(m.get("bm_quality_code"), m.get("bm_quality_name") or m.get("bm_item_name")),
        "finishing": _code_name(m.get("finishing_item_code"), m.get("finishing_item_name")),
        "winding": _code_name(m.get("winding_item_code"), m.get("winding_item_name")),
    }.get(qtype)
    return label or str(m.get("prod_quality_id") or "")


def _validate(body: dict) -> dict:
    dept_id = body.get("dept_id")
    prod_quality_id = body.get("prod_quality_id")
    wages_quality_id = body.get("wages_quality_id")
    if not dept_id:
        raise HTTPException(status_code=400, detail="Department is required")
    if not prod_quality_id:
        raise HTTPException(status_code=400, detail="Production quality is required")
    if not wages_quality_id:
        raise HTTPException(status_code=400, detail="Wages quality is required")
    process = (body.get("process") or "").strip() or None
    if process is not None and process not in PROCESSES:
        raise HTTPException(status_code=400, detail=f"Process must be one of {', '.join(PROCESSES)}")
    return {
        "process": process,
        "dept_id": _int(dept_id, 0, "Department"),
        "prod_quality_id": _int(prod_quality_id, 0, "Production quality"),
        "wages_quality_id": _int(wages_quality_id, 0, "Wages quality"),
    }


def _check_duplicate(db: Session, values: dict, record_id: int | None):
    dup = db.execute(_duplicate_query(), {
        "dept_id": values["dept_id"],
        "prod_quality_id": values["prod_quality_id"],
        "process": values["process"],
        "record_id": record_id,
    }).fetchone()
    if dup and dup.cnt > 0:
        raise HTTPException(
            status_code=400,
            detail="This production quality is already linked for the department and process",
        )


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/prod_wages_link_setup")
def prod_wages_link_setup(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Department + wages-quality dropdown options for the sidebar scope."""
    try:
        params = {"co_id": _require_co_id(request), "branch_id": _branch_param(request)}
        depts = [dict(r._mapping) for r in db.execute(get_departments_query(), params).fetchall()]
        ejm_branches = _ejm_rate_branches(db)
        wages = [
            {
                "value": str(w["q_code_id"]),
                "label": _code_name(w["q_code"], w["quality"]),
                "dept_id": str(w["master_dept_id"]) if w["master_dept_id"] is not None else None,
            }
            for w in (dict(r._mapping) for r in db.execute(get_wages_qualities_query(), params).fetchall())
            if w["branch_id"] not in ejm_branches
        ] + [
            {
                "value": str(r.wages_quality_rate_id),
                "label": _ejm_rate_label(r.quality_code, r.quality_desc, r.quality_rate),
                "dept_id": str(r.dept_id),
            }
            for r in db.execute(get_ejm_rates_query(), params).fetchall()
        ]
        return {
            "data": {
                "departments": [
                    {"value": str(d["dept_id"]), "label": d["dept_desc"], "quality_type": qtype}
                    for d in depts
                    if (qtype := quality_type_for(d["dept_desc"]))
                ],
                "wages_qualities": wages,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/prod_wages_link_qualities")
def prod_wages_link_qualities(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Production-quality options for one department (source picked by its type)."""
    try:
        dept_id = request.query_params.get("dept_id")
        if not dept_id:
            raise HTTPException(status_code=400, detail="dept_id is required")
        dept = _load_department(db, _int(dept_id, 0, "dept_id"))
        return {"data": {"quality_type": dept["quality_type"], "qualities": _prod_quality_options(db, dept)}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_prod_wages_link_table")
def get_prod_wages_link_table(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Paginated list of links."""
    try:
        co_id = _require_co_id(request)
        search = (request.query_params.get("search") or "").strip().lower()
        page = _int(request.query_params.get("page"), 1, "Page")
        limit = _int(request.query_params.get("limit"), 10, "Limit")

        rows = db.execute(get_link_list_query(), {
            "co_id": co_id, "branch_id": _branch_param(request),
        }).fetchall()

        all_data = []
        for row in rows:
            m = dict(row._mapping)
            item = {
                "link_prod_wages_id": int(m["link_prod_wages_id"]),
                "dept_id": m["dept_id"],
                "dept_desc": m["dept_desc"],
                "branch_id": m["branch_id"],
                "branch_name": m["branch_name"],
                "prod_quality_id": m["prod_quality_id"],
                "prod_quality_name": _prod_quality_label(m),
                "wages_quality_id": m["wages_quality_id"],
                "wages_quality_name": (
                    _ejm_rate_label(m["ejm_quality_code"], m.get("ejm_quality_desc"), m["ejm_quality_rate"])
                    if m.get("ejm_quality_code") is not None
                    else _code_name(m.get("wages_q_code"), m.get("wages_quality"))
                ),
                "active": m["active"],
                "process": m.get("process"),
            }
            # ponytail: search in Python — the table is a few hundred rows per tenant.
            if search and search not in " ".join(
                str(item[k] or "") for k in ("dept_desc", "prod_quality_name", "wages_quality_name", "process")
            ).lower():
                continue
            all_data.append(item)

        total = len(all_data)
        start = (page - 1) * limit
        return {"data": all_data[start:start + limit], "total": total, "page": page, "limit": limit}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_prod_wages_link_by_id/{record_id}")
def get_prod_wages_link_by_id(
    record_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        row = db.execute(get_link_by_id_query(), {"record_id": record_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Link record not found")
        return {"data": dict(row._mapping)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/prod_wages_link_create")
def prod_wages_link_create(
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        values = _validate(parse_json_body(request))
        dept = _load_department(db, values["dept_id"])
        _check_wages_quality(db, values, dept)
        _check_duplicate(db, values, None)

        record = LinkProdQualityWageQuality(**values, branch_id=dept["branch_id"], active=1)
        db.add(record)
        db.commit()
        db.refresh(record)
        return {"message": "Link created successfully", "link_prod_wages_id": int(record.link_prod_wages_id)}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/prod_wages_link_edit/{record_id}")
def prod_wages_link_edit(
    record_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        values = _validate(parse_json_body(request))
        existing = db.query(LinkProdQualityWageQuality).filter(
            LinkProdQualityWageQuality.link_prod_wages_id == record_id,
        ).first()
        if not existing:
            raise HTTPException(status_code=404, detail="Link record not found")

        dept = _load_department(db, values["dept_id"])
        _check_wages_quality(db, values, dept)
        _check_duplicate(db, values, record_id)

        for field, value in values.items():
            setattr(existing, field, value)
        existing.branch_id = dept["branch_id"]
        db.commit()
        return {"message": "Link updated successfully", "link_prod_wages_id": record_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/prod_wages_link_delete/{record_id}")
def prod_wages_link_delete(
    record_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft-delete (active = 0), matching the list filter."""
    try:
        existing = db.query(LinkProdQualityWageQuality).filter(
            LinkProdQualityWageQuality.link_prod_wages_id == record_id,
        ).first()
        if not existing:
            raise HTTPException(status_code=404, detail="Link record not found")
        existing.active = 0
        db.commit()
        return {"message": "Link deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

"""Wastage Report Mapping master CRUD (jute_waste_report_map) — TKT-000183.

The daily Wastage Report bands every waste item into a group through this
table: which (waste item [, department]) lands in which report group, and which
production figure the group's day and monthly-total percentages divide by.
Until this master existed rows could only be added with SQL, so a new waste
item silently fell into the report's "Other / Unmapped" band with no %.

Row semantics (schema: dbqueries/migrations/20260825_jute_waste_report_map.sql):
  * dept_id NULL = the item maps to the group from ANY department; an exact
    (item, dept) row beats the item's NULL-dept row in the report.
  * group_name / day_denom / total_denom / display_order are GROUP-level but
    stored on every row; the report takes the first active row by
    (display_order, id) as the group definition. Create/update therefore
    propagate those four fields to every other active row of the same
    (co_id, group_code) in the SAME transaction, so the group stays consistent.
  * At most one ACTIVE row per (co_id, item_id, dept_id), NULL-safe on dept_id.

Endpoints (prefix /api/wasteReportMap):
  GET  /setup?co_id=   waste items, departments, denominator options, groups
  GET  /list?co_id=    active rows with item / department / branch names
  POST /create         body WasteReportMapCreate
  POST /update         body WasteReportMapUpdate (by waste_report_map_id)
  POST /deactivate     body WasteReportMapDeactivate (active = 0)

Portal persona, same shape as winding_target_map.py: get_tenant_db +
get_current_user_with_refresh, {"data": ...} responses, _require_co_id for GETs,
GET try/except (HTTPException: raise) / (Exception: 500), POST
rollback-then-raise. updated_by = the token's user_id.
"""

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    JUTE_WASTAGE_ITEM_TYPE_ID,
    WASTE_REPORT_DENOM_LABELS,
    WASTE_REPORT_DENOMS,
    WASTE_REPORT_OTHER_GROUP,
)
from src.juteProduction.waste_report_map_query import (
    deactivate_waste_report_map_query,
    find_duplicate_map_row_query,
    get_company_departments_query,
    get_existing_groups_query,
    get_waste_items_for_map_query,
    get_waste_report_map_list_query,
    get_waste_report_map_row_query,
    insert_waste_report_map_query,
    propagate_group_fields_query,
    update_waste_report_map_query,
    verify_map_dept_query,
    verify_map_item_query,
)


router = APIRouter()

GROUP_CODE_RE = re.compile(r"^[A-Z0-9_]{1,40}$")
GROUP_NAME_MAX = 100
COL_LABEL_MAX = 100
DISPLAY_ORDER_MAX = 99999

MAP_ROW_NOT_FOUND_MSG = "Wastage report mapping row not found"


# =============================================================================
# Pydantic models
# =============================================================================
#
# String fields are Optional so a missing value reaches _clean_payload and gets
# a 400 with a clear message rather than FastAPI's generic 422.


class WasteReportMapCreate(BaseModel):
    co_id: int
    group_code: Optional[str] = None
    group_name: Optional[str] = None
    day_denom: Optional[str] = None
    total_denom: Optional[str] = None
    item_id: Optional[int] = None
    # None = the item maps from any department
    dept_id: Optional[int] = None
    col_label: Optional[str] = None
    display_order: int = 0


class WasteReportMapUpdate(WasteReportMapCreate):
    waste_report_map_id: int


class WasteReportMapDeactivate(BaseModel):
    co_id: int
    waste_report_map_id: int


# =============================================================================
# Helpers
# =============================================================================


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id")


def denominator_options() -> list:
    """Denominator picker options in WASTE_REPORT_DENOMS order."""
    return [
        {"code": code, "label": WASTE_REPORT_DENOM_LABELS.get(code, code)}
        for code in WASTE_REPORT_DENOMS
    ]


def _denom_label(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    return WASTE_REPORT_DENOM_LABELS.get(code, code)


def normalize_group_code(raw: Optional[str]) -> str:
    """Trim + uppercase a group code and validate it; 400 on a bad code."""
    code = (raw or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Group code is required")
    if not GROUP_CODE_RE.match(code):
        raise HTTPException(
            status_code=400,
            detail=(
                "Group code may only contain letters A-Z, digits and underscore "
                "(1 to 40 characters)"
            ),
        )
    if code == WASTE_REPORT_OTHER_GROUP:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Group code '{WASTE_REPORT_OTHER_GROUP}' is reserved for the "
                "report's unmapped band"
            ),
        )
    return code


def _validate_denom(raw: Optional[str], field_label: str) -> str:
    code = (raw or "").strip().upper()
    if code not in WASTE_REPORT_DENOMS:
        allowed = ", ".join(WASTE_REPORT_DENOMS)
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} must be one of: {allowed}",
        )
    return code


def clean_payload(body: WasteReportMapCreate) -> dict:
    """Shape-level validation shared by create and update (no DB access).

    Returns the bind params for the insert/update statements (minus id /
    updated_by). Raises 400 with a clear message on the first bad field.
    """
    group_code = normalize_group_code(body.group_code)

    group_name = (body.group_name or "").strip()
    if not group_name:
        raise HTTPException(status_code=400, detail="Group name is required")
    if len(group_name) > GROUP_NAME_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Group name cannot exceed {GROUP_NAME_MAX} characters",
        )

    day_denom = _validate_denom(body.day_denom, "Daily % base (day_denom)")
    total_denom = _validate_denom(body.total_denom, "Monthly % base (total_denom)")

    if body.item_id is None:
        raise HTTPException(status_code=400, detail="Waste item is required")

    col_label = (body.col_label or "").strip() or None
    if col_label is not None and len(col_label) > COL_LABEL_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Column label cannot exceed {COL_LABEL_MAX} characters",
        )

    display_order = int(body.display_order)
    if display_order < 0 or display_order > DISPLAY_ORDER_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Display order must be between 0 and {DISPLAY_ORDER_MAX}",
        )

    return {
        "co_id": int(body.co_id),
        "group_code": group_code,
        "group_name": group_name,
        "day_denom": day_denom,
        "total_denom": total_denom,
        "item_id": int(body.item_id),
        "dept_id": int(body.dept_id) if body.dept_id is not None else None,
        "col_label": col_label,
        "display_order": display_order,
    }


def _check_references(db: Session, params: dict, exclude_id: Optional[int]) -> None:
    """DB-level guards shared by create and update: item belongs to the company
    and is a waste item, department (if any) belongs to a branch of the company,
    and no other active row maps the same (item, department)."""
    item = db.execute(
        verify_map_item_query(),
        {
            "item_id": params["item_id"],
            "co_id": params["co_id"],
            "item_type_id": JUTE_WASTAGE_ITEM_TYPE_ID,
        },
    ).fetchone()
    if not item:
        raise HTTPException(
            status_code=400,
            detail="Waste item not found for this company (must be a jute wastage item)",
        )

    if params["dept_id"] is not None:
        dept = db.execute(
            verify_map_dept_query(),
            {"dept_id": params["dept_id"], "co_id": params["co_id"]},
        ).fetchone()
        if not dept:
            raise HTTPException(
                status_code=400,
                detail="Department not found in any branch of this company",
            )

    dup = db.execute(
        find_duplicate_map_row_query(),
        {
            "co_id": params["co_id"],
            "item_id": params["item_id"],
            "dept_id": params["dept_id"],
            "exclude_id": exclude_id,
        },
    ).fetchone()
    if dup:
        m = dup._mapping
        scope = "this department" if params["dept_id"] is not None else "any department"
        raise HTTPException(
            status_code=400,
            detail=(
                f"This waste item is already mapped for {scope} "
                f"(group {m.get('group_name') or m.get('group_code')}). "
                "Edit that row instead."
            ),
        )


def _propagate(db: Session, params: dict, row_id: int, user_id) -> int:
    """Copy group-level fields onto the group's other active rows; returns the
    number of rows changed."""
    result = db.execute(
        propagate_group_fields_query(),
        {
            "co_id": params["co_id"],
            "group_code": params["group_code"],
            "group_name": params["group_name"],
            "day_denom": params["day_denom"],
            "total_denom": params["total_denom"],
            "display_order": params["display_order"],
            "updated_by": user_id,
            "id": row_id,
        },
    )
    rowcount = getattr(result, "rowcount", 0)
    return rowcount if isinstance(rowcount, int) and rowcount > 0 else 0


def _load_active_row(db: Session, row_id: int, co_id: int):
    existing = db.execute(get_waste_report_map_row_query(), {"id": row_id}).fetchone()
    if not existing or int(existing._mapping["co_id"]) != int(co_id):
        raise HTTPException(status_code=404, detail=MAP_ROW_NOT_FOUND_MSG)
    return existing


# =============================================================================
# Setup
# =============================================================================


@router.get("/setup")
def waste_report_map_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    try:
        waste_items = [
            dict(r._mapping)
            for r in db.execute(
                get_waste_items_for_map_query(),
                {"co_id": co_id, "item_type_id": JUTE_WASTAGE_ITEM_TYPE_ID},
            ).fetchall()
        ]
        departments = [
            dict(r._mapping)
            for r in db.execute(get_company_departments_query(), {"co_id": co_id}).fetchall()
        ]
        groups = []
        for r in db.execute(get_existing_groups_query(), {"co_id": co_id}).fetchall():
            m = dict(r._mapping)
            groups.append(
                {
                    "group_code": m["group_code"],
                    "group_name": m["group_name"],
                    "day_denom": m["day_denom"],
                    "total_denom": m["total_denom"],
                    "display_order": int(m.get("display_order") or 0),
                    "item_count": int(m.get("item_count") or 0),
                }
            )
        return {
            "data": {
                "waste_items": waste_items,
                "departments": departments,
                "denominators": denominator_options(),
                "groups": groups,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# List
# =============================================================================


@router.get("/list")
def waste_report_map_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    try:
        rows = db.execute(get_waste_report_map_list_query(), {"co_id": co_id}).fetchall()
        data = []
        for r in rows:
            m = dict(r._mapping)
            updated = m.get("updated_date_time")
            data.append(
                {
                    "waste_report_map_id": m["waste_report_map_id"],
                    "co_id": m["co_id"],
                    "group_code": m["group_code"],
                    "group_name": m["group_name"],
                    "day_denom": m["day_denom"],
                    "day_denom_label": _denom_label(m.get("day_denom")),
                    "total_denom": m["total_denom"],
                    "total_denom_label": _denom_label(m.get("total_denom")),
                    "item_id": m["item_id"],
                    "item_code": m.get("item_code"),
                    "item_name": m.get("item_name"),
                    "dept_id": m.get("dept_id"),
                    "dept_desc": m.get("dept_desc"),
                    "branch_id": m.get("branch_id"),
                    "branch_name": m.get("branch_name"),
                    "col_label": m.get("col_label"),
                    "display_order": int(m.get("display_order") or 0),
                    "active": int(m.get("active") or 0),
                    "updated_by": m.get("updated_by"),
                    "updated_date_time": str(updated) if updated is not None else None,
                }
            )
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Create
# =============================================================================


@router.post("/create")
def waste_report_map_create(
    body: WasteReportMapCreate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        params = clean_payload(body)
        _check_references(db, params, exclude_id=None)
        user_id = token_data.get("user_id")

        result = db.execute(
            insert_waste_report_map_query(), {**params, "updated_by": user_id}
        )
        new_id = result.lastrowid
        propagated = _propagate(db, params, new_id, user_id)
        db.commit()
        return {
            "data": {
                "waste_report_map_id": new_id,
                "group_code": params["group_code"],
                "group_rows_updated": propagated,
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Update
# =============================================================================


@router.post("/update")
def waste_report_map_update(
    body: WasteReportMapUpdate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        row_id = int(body.waste_report_map_id)
        params = clean_payload(body)
        _load_active_row(db, row_id, params["co_id"])
        _check_references(db, params, exclude_id=row_id)
        user_id = token_data.get("user_id")

        db.execute(
            update_waste_report_map_query(),
            {**params, "id": row_id, "updated_by": user_id},
        )
        propagated = _propagate(db, params, row_id, user_id)
        db.commit()
        return {
            "data": {
                "waste_report_map_id": row_id,
                "group_code": params["group_code"],
                "group_rows_updated": propagated,
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Deactivate (soft delete)
# =============================================================================


@router.post("/deactivate")
def waste_report_map_deactivate(
    body: WasteReportMapDeactivate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        row_id = int(body.waste_report_map_id)
        _load_active_row(db, row_id, int(body.co_id))
        db.execute(
            deactivate_waste_report_map_query(),
            {"id": row_id, "updated_by": token_data.get("user_id")},
        )
        db.commit()
        return {"data": {"waste_report_map_id": row_id, "active": 0}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

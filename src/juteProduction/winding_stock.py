"""Winding stock — daily counted yarn snapshot (/api/windingStock).

The mill counts every package of yarn on the floor each morning. This router
stores that count, one row per (bucket, yarn item, package label, area,
location, department), and the Winding & Weaving Difference report reconciles
the total against book stock.

Location (2026-09-25, purchased-yarn spec §3.3): each line is counted either in
the GODOWN or on the FLOOR; a FLOOR line may name the department it sits in, a
GODOWN line never does. Both fields are optional on the wire — a request that
omits them is a legacy FLOOR line with no department, so the live sls page
keeps working unchanged. The report sums every location, so totals do not move.

Shape follows waste_entry.py: literal {"data": ...} returns, Pydantic body
params, arg-less query builders, soft delete, and validators that answer with a
400 rather than letting MySQL raise a 500.

MT is never stored — it is no_of_samples * wt_per_sample / 1000, computed on
read, so correcting a weight fixes history.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    WINDING_STOCK_BUCKETS,
    WINDING_STOCK_LOC_FLOOR,
    WINDING_STOCK_LOC_GODOWN,
    WINDING_STOCK_LOCATIONS,
    WINDING_STOCK_SAMPLES_MAX,
    WINDING_STOCK_WT_MAX,
)
from src.juteProduction.winding_reports import stock_mt_of
from src.juteProduction.winding_stock_lock import (
    is_day_locked,
    require_edit_if_locked,
)
from src.juteProduction.winding_stock_query import (
    clear_winding_stock_lock_query,
    copy_winding_stock_previous_day_query,
    get_winding_stock_active_by_id_query,
    get_winding_stock_areas_query,
    get_winding_stock_by_date_query,
    get_winding_stock_departments_query,
    get_winding_stock_duplicate_query,
    get_winding_stock_lock_row_query,
    get_winding_stock_prev_entry_date_query,
    get_winding_stock_wt_default_query,
    get_winding_stock_yarn_items_query,
    insert_winding_stock_lock_query,
    insert_winding_stock_row_query,
    soft_delete_winding_stock_row_query,
    update_winding_stock_lock_query,
    update_winding_stock_row_query,
    verify_winding_stock_area_query,
    verify_winding_stock_dept_query,
    verify_winding_stock_item_query,
)

router = APIRouter()

NOT_FOUND_MSG = "Winding stock line not found"
DUPLICATE_MSG = (
    "A line for this bucket, yarn, package, area, location and department "
    "already exists on this date"
)
FUTURE_DATE_MSG = "tran_date cannot be in the future"
BAD_BUCKET_MSG = f"bucket must be one of {', '.join(WINDING_STOCK_BUCKETS)}"
BAD_ITEM_MSG = "item_id must be an active yarn item (item type 4) for this company"
BAD_AREA_MSG = "wastage_area_id must be an active area of this company and branch"
BAD_LOCATION_MSG = f"stock_loc must be one of {', '.join(WINDING_STOCK_LOCATIONS)}"
BAD_DEPT_MSG = "dept_id must be a department of this branch"
PACKAGE_REQUIRED_MSG = "package_label is required"
PACKAGE_TOO_LONG_MSG = "package_label cannot exceed 60 characters"
REMARKS_TOO_LONG_MSG = "remarks cannot exceed 255 characters"
SAMPLES_GATE_MSG = (
    f"no_of_samples must be greater than 0 and at most {WINDING_STOCK_SAMPLES_MAX}"
)
WT_GATE_MSG = (
    f"wt_per_sample must be greater than 0 and at most {WINDING_STOCK_WT_MAX}"
)
COPY_TARGET_NOT_EMPTY_MSG = (
    "This date already has counted lines. Delete them before copying a previous day."
)
NO_PREVIOUS_ENTRY_MSG = "No earlier counted day exists to copy from"


# =============================================================================
# Request models
# =============================================================================


class WindingStockCreate(BaseModel):
    co_id: int
    branch_id: int
    tran_date: date
    bucket: str
    item_id: Optional[int] = None  # NULL for lines with no yarn-master row
    package_label: str
    wastage_area_id: Optional[int] = None
    stock_loc: Optional[str] = None  # GODOWN | FLOOR; omitted = FLOOR
    dept_id: Optional[int] = None  # FLOOR only; forced NULL for GODOWN
    no_of_samples: float
    wt_per_sample: float  # kg per piece
    remarks: Optional[str] = None


class WindingStockUpdate(BaseModel):
    """Editable fields. tran_date and branch_id are immutable — delete and
    re-add to move a count to another day (mirrors the waste-entry precedent)."""

    bucket: Optional[str] = None
    item_id: Optional[int] = None
    package_label: Optional[str] = None
    wastage_area_id: Optional[int] = None
    stock_loc: Optional[str] = None
    dept_id: Optional[int] = None
    no_of_samples: Optional[float] = None
    wt_per_sample: Optional[float] = None
    remarks: Optional[str] = None


class WindingStockDayRef(BaseModel):
    """A (co, branch, date) unit — used by copy-previous-day and the lock."""

    co_id: int
    branch_id: int
    tran_date: date


# =============================================================================
# Param + value helpers
# =============================================================================


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id")


def _require_branch_id(request: Request) -> int:
    raw = request.query_params.get("branch_id")
    if not raw:
        raise HTTPException(status_code=400, detail="branch_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid branch_id")


def _optional_int_param(request: Request, name: str) -> Optional[int]:
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _require_tran_date(request: Request) -> date:
    raw = request.query_params.get("tran_date")
    if not raw:
        raise HTTPException(status_code=400, detail="tran_date is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tran_date format")


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _serialize_dates(row: dict, keys=("tran_date",)) -> dict:
    for k in keys:
        if row.get(k) is not None:
            row[k] = str(row[k])
    return row


def _validate_bucket(bucket: str) -> str:
    value = (bucket or "").strip().upper()
    if value not in WINDING_STOCK_BUCKETS:
        raise HTTPException(status_code=400, detail=BAD_BUCKET_MSG)
    return value


def _clean_package_label(label: str) -> str:
    value = (label or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=PACKAGE_REQUIRED_MSG)
    if len(value) > 60:
        raise HTTPException(status_code=400, detail=PACKAGE_TOO_LONG_MSG)
    return value


def _validate_remarks(value: Optional[str]) -> Optional[str]:
    """remarks is optional but, when given, must fit the String(255) column."""
    if value is None:
        return None
    if len(value) > 255:
        raise HTTPException(status_code=400, detail=REMARKS_TOO_LONG_MSG)
    return value


def _validate_samples(value) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=SAMPLES_GATE_MSG)
    if n <= 0 or n > WINDING_STOCK_SAMPLES_MAX:
        raise HTTPException(status_code=400, detail=SAMPLES_GATE_MSG)
    return n


def _validate_wt(value) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=WT_GATE_MSG)
    if n <= 0 or n > WINDING_STOCK_WT_MAX:
        raise HTTPException(status_code=400, detail=WT_GATE_MSG)
    return n


def _validate_tran_date(tran_date: date) -> date:
    if tran_date > date.today():
        raise HTTPException(status_code=400, detail=FUTURE_DATE_MSG)
    return tran_date


def _validate_item(db: Session, item_id: Optional[int], co_id: int) -> Optional[int]:
    """None is legitimate: BEAM ON FLOOR / waste lines carry no yarn item."""
    if item_id is None:
        return None
    row = db.execute(
        verify_winding_stock_item_query(),
        {"item_id": int(item_id), "co_id": int(co_id)},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=BAD_ITEM_MSG)
    return int(item_id)


def _validate_area(
    db: Session, wastage_area_id: Optional[int], co_id: int, branch_id: int
) -> Optional[int]:
    if wastage_area_id is None:
        return None
    row = db.execute(
        verify_winding_stock_area_query(),
        {
            "wastage_area_id": int(wastage_area_id),
            "co_id": int(co_id),
            "branch_id": int(branch_id),
        },
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=BAD_AREA_MSG)
    return int(wastage_area_id)


def _validate_stock_loc(value: Optional[str]) -> str:
    """Absent / blank means FLOOR — every row written before the column existed
    is a floor count, and the live page does not send the field yet."""
    if value is None or not str(value).strip():
        return WINDING_STOCK_LOC_FLOOR
    loc = str(value).strip().upper()
    if loc not in WINDING_STOCK_LOCATIONS:
        raise HTTPException(status_code=400, detail=BAD_LOCATION_MSG)
    return loc


def _validate_dept(db: Session, dept_id: Optional[int], branch_id: int) -> Optional[int]:
    """None is legitimate (legacy FLOOR rows carry no department). dept_mst has
    no co_id and no active column, so branch is the whole scope."""
    if dept_id is None:
        return None
    row = db.execute(
        verify_winding_stock_dept_query(),
        {"dept_id": int(dept_id), "branch_id": int(branch_id)},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=BAD_DEPT_MSG)
    return int(dept_id)


def _dept_for_location(stock_loc: str, dept_id: Optional[int]) -> Optional[int]:
    """A GODOWN line has no department, by rule rather than by error: the
    godown is one place, so a dept sent with it is dropped, not rejected."""
    return None if stock_loc == WINDING_STOCK_LOC_GODOWN else dept_id


def _reject_duplicate(
    db: Session,
    co_id: int,
    branch_id: int,
    tran_date: date,
    bucket: str,
    item_id: Optional[int],
    package_label: str,
    wastage_area_id: Optional[int],
    stock_loc: str,
    dept_id: Optional[int],
    exclude_id: Optional[int],
) -> None:
    """Answer what a UNIQUE key cannot: MySQL treats NULLs as distinct, so the
    constraint would let exactly the duplicates that matter through.

    Location and department are part of the key: the same package in the
    godown and on a floor is two lines, not a duplicate."""
    row = db.execute(
        get_winding_stock_duplicate_query(),
        {
            "co_id": co_id,
            "branch_id": branch_id,
            "tran_date": tran_date,
            "bucket": bucket,
            "item_id": item_id,
            "package_label": package_label,
            "wastage_area_id": wastage_area_id,
            "stock_loc": stock_loc,
            "dept_id": dept_id,
            "exclude_id": exclude_id,
        },
    ).fetchone()
    if row:
        raise HTTPException(status_code=400, detail=DUPLICATE_MSG)


# =============================================================================
# Setup
# =============================================================================


@router.get("/setup")
def setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _optional_int_param(request, "branch_id")
    try:
        yarn_items = [
            dict(r._mapping)
            for r in db.execute(
                get_winding_stock_yarn_items_query(), {"co_id": co_id}
            ).fetchall()
        ]
        for item in yarn_items:
            item["yarn_count"] = (
                None if item.get("yarn_count") is None else _f(item["yarn_count"])
            )
        areas = [
            dict(r._mapping)
            for r in db.execute(
                get_winding_stock_areas_query(),
                {"co_id": co_id, "branch_id": branch_id},
            ).fetchall()
        ]
        # dept_mst is branch-scoped only; without a branch there is nothing
        # sensible to offer, so the list is empty rather than every branch's.
        departments = (
            []
            if branch_id is None
            else [
                dict(r._mapping)
                for r in db.execute(
                    get_winding_stock_departments_query(), {"branch_id": branch_id}
                ).fetchall()
            ]
        )
        return {
            "data": {
                "buckets": list(WINDING_STOCK_BUCKETS),
                "yarn_items": yarn_items,
                "areas": areas,
                "locations": list(WINDING_STOCK_LOCATIONS),
                "departments": departments,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Day read
# =============================================================================


@router.get("/rows_by_date")
def rows_by_date(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _require_branch_id(request)
    d_val = _require_tran_date(request)
    try:
        binds = {"co_id": co_id, "branch_id": branch_id, "tran_date": d_val}

        rows = []
        for r in db.execute(get_winding_stock_by_date_query(), binds).fetchall():
            row = dict(r._mapping)
            row["no_of_samples"] = _f(row.get("no_of_samples"))
            row["wt_per_sample"] = _f(row.get("wt_per_sample"))
            row["yarn_count"] = (
                None if row.get("yarn_count") is None else _f(row["yarn_count"])
            )
            row["stock_mt"] = stock_mt_of(row["no_of_samples"], row["wt_per_sample"])
            _serialize_dates(row)
            rows.append(row)

        prev = db.execute(get_winding_stock_prev_entry_date_query(), binds).fetchone()
        prev_entry_date = (
            str(prev.prev_entry_date)
            if prev is not None and prev.prev_entry_date is not None
            else None
        )

        return {
            "data": {
                "rows": rows,
                "prev_entry_date": prev_entry_date,
                "locked": is_day_locked(db, co_id, branch_id, d_val),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/wt_default")
def wt_default(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The kg-per-piece last saved for this (item, package) key.

    This is the entire weight-per-sample "master" — no separate table exists,
    so overtyping a row makes that value the next default.
    """
    co_id = _require_co_id(request)
    branch_id = _require_branch_id(request)
    package_label = _clean_package_label(request.query_params.get("package_label", ""))
    item_id = _optional_int_param(request, "item_id")
    try:
        row = db.execute(
            get_winding_stock_wt_default_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "item_id": item_id,
                "package_label": package_label,
            },
        ).fetchone()
        return {
            "data": {
                "wt_per_sample": None if row is None else _f(row.wt_per_sample)
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Mutations
# =============================================================================


@router.post("/row_create")
def row_create(
    body: WindingStockCreate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        tran_date = _validate_tran_date(body.tran_date)
        bucket = _validate_bucket(body.bucket)
        package_label = _clean_package_label(body.package_label)
        no_of_samples = _validate_samples(body.no_of_samples)
        wt_per_sample = _validate_wt(body.wt_per_sample)
        remarks = _validate_remarks(body.remarks)

        require_edit_if_locked(db, token_data, body.co_id, body.branch_id, tran_date)

        item_id = _validate_item(db, body.item_id, body.co_id)
        wastage_area_id = _validate_area(
            db, body.wastage_area_id, body.co_id, body.branch_id
        )
        stock_loc = _validate_stock_loc(body.stock_loc)
        dept_id = _dept_for_location(
            stock_loc, _validate_dept(db, body.dept_id, body.branch_id)
        )
        _reject_duplicate(
            db, int(body.co_id), int(body.branch_id), tran_date, bucket,
            item_id, package_label, wastage_area_id, stock_loc, dept_id, None,
        )

        result = db.execute(
            insert_winding_stock_row_query(),
            {
                "co_id": int(body.co_id),
                "branch_id": int(body.branch_id),
                "tran_date": tran_date,
                "bucket": bucket,
                "item_id": item_id,
                "package_label": package_label,
                "wastage_area_id": wastage_area_id,
                "stock_loc": stock_loc,
                "dept_id": dept_id,
                "no_of_samples": no_of_samples,
                "wt_per_sample": wt_per_sample,
                "remarks": remarks,
                "updated_by": token_data.get("user_id"),
            },
        )
        db.commit()
        return {"data": {"winding_stock_id": result.lastrowid}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/row_edit/{winding_stock_id}")
def row_edit(
    winding_stock_id: int,
    body: WindingStockUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        co_id = _require_co_id(request)
        existing = db.execute(
            get_winding_stock_active_by_id_query(),
            {"id": winding_stock_id, "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        require_edit_if_locked(
            db, token_data, co_id, existing.branch_id, existing.tran_date
        )

        bucket = (
            _validate_bucket(body.bucket) if body.bucket is not None else existing.bucket
        )
        package_label = (
            _clean_package_label(body.package_label)
            if body.package_label is not None
            else existing.package_label
        )
        no_of_samples = (
            _validate_samples(body.no_of_samples)
            if body.no_of_samples is not None
            else _f(existing.no_of_samples)
        )
        wt_per_sample = (
            _validate_wt(body.wt_per_sample)
            if body.wt_per_sample is not None
            else _f(existing.wt_per_sample)
        )
        item_id = (
            _validate_item(db, body.item_id, co_id)
            if body.item_id is not None
            else existing.item_id
        )
        wastage_area_id = (
            _validate_area(db, body.wastage_area_id, co_id, existing.branch_id)
            if body.wastage_area_id is not None
            else existing.wastage_area_id
        )
        remarks = (
            _validate_remarks(body.remarks)
            if body.remarks is not None
            else existing.remarks
        )
        # Rows written before the column existed read back FLOOR / NULL (the
        # column is NOT NULL DEFAULT 'FLOOR'); a body that omits the fields
        # keeps whatever the row already has, like every other field here.
        stock_loc = (
            _validate_stock_loc(body.stock_loc)
            if body.stock_loc is not None
            else existing.stock_loc
        )
        # dept_id is the one field where an explicit null means "clear": the
        # page sends null to take a floor line off its department. Only an
        # ABSENT key keeps the row's current department.
        if "dept_id" in body.model_fields_set:
            new_dept = (
                _validate_dept(db, body.dept_id, existing.branch_id)
                if body.dept_id is not None
                else None
            )
        else:
            new_dept = existing.dept_id
        dept_id = _dept_for_location(stock_loc, new_dept)

        _reject_duplicate(
            db, co_id, int(existing.branch_id), existing.tran_date, bucket,
            item_id, package_label, wastage_area_id, stock_loc, dept_id,
            winding_stock_id,
        )

        db.execute(
            update_winding_stock_row_query(),
            {
                "id": winding_stock_id,
                "co_id": co_id,
                "bucket": bucket,
                "item_id": item_id,
                "package_label": package_label,
                "wastage_area_id": wastage_area_id,
                "stock_loc": stock_loc,
                "dept_id": dept_id,
                "no_of_samples": no_of_samples,
                "wt_per_sample": wt_per_sample,
                "remarks": remarks,
                "updated_by": token_data.get("user_id"),
            },
        )
        db.commit()
        return {"data": {"winding_stock_id": winding_stock_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/row_delete/{winding_stock_id}")
def row_delete(
    winding_stock_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        co_id = _require_co_id(request)
        existing = db.execute(
            get_winding_stock_active_by_id_query(),
            {"id": winding_stock_id, "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        require_edit_if_locked(
            db, token_data, co_id, existing.branch_id, existing.tran_date
        )

        db.execute(
            soft_delete_winding_stock_row_query(),
            {
                "id": winding_stock_id,
                "co_id": co_id,
                "updated_by": token_data.get("user_id"),
            },
        )
        db.commit()
        return {"data": {"winding_stock_id": winding_stock_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/copy_previous_day")
def copy_previous_day(
    body: WindingStockDayRef,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Clone the previous counted day's lines into tran_date.

    The operator adds lines manually by design, but the sheet repeats ~30
    near-identical rows daily; retyping them is the biggest source of error.
    Refuses when the target day already has rows, so it can never silently
    double a count.
    """
    try:
        tran_date = _validate_tran_date(body.tran_date)
        require_edit_if_locked(db, token_data, body.co_id, body.branch_id, tran_date)

        binds = {
            "co_id": int(body.co_id),
            "branch_id": int(body.branch_id),
            "tran_date": tran_date,
        }
        existing = db.execute(get_winding_stock_by_date_query(), binds).fetchall()
        if existing:
            raise HTTPException(status_code=400, detail=COPY_TARGET_NOT_EMPTY_MSG)

        prev = db.execute(get_winding_stock_prev_entry_date_query(), binds).fetchone()
        from_date = None if prev is None else prev.prev_entry_date
        if from_date is None:
            raise HTTPException(status_code=404, detail=NO_PREVIOUS_ENTRY_MSG)

        result = db.execute(
            copy_winding_stock_previous_day_query(),
            {**binds, "from_date": from_date, "updated_by": token_data.get("user_id")},
        )
        db.commit()
        return {"data": {"copied": result.rowcount, "from_date": str(from_date)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Lock
# =============================================================================


@router.post("/lock")
def lock(
    body: WindingStockDayRef,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Freeze the day once its report has been taken.

    An explicit action, never a side effect of running the report: a GET must
    not mutate. A locked day is still editable by a user holding Edit on the
    Winding Stock menu (see winding_stock_lock.require_edit_if_locked).
    """
    try:
        user_id = token_data.get("user_id")
        binds = {
            "co_id": int(body.co_id),
            "branch_id": int(body.branch_id),
            "tran_date": body.tran_date,
        }
        existing = db.execute(get_winding_stock_lock_row_query(), binds).fetchone()
        if existing:
            db.execute(
                update_winding_stock_lock_query(),
                {
                    "id": existing.winding_stock_lock_id,
                    "co_id": binds["co_id"],
                    "processed_by": user_id,
                },
            )
        else:
            db.execute(
                insert_winding_stock_lock_query(), {**binds, "processed_by": user_id}
            )
        db.commit()
        return {"data": {"locked": True}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/unlock")
def unlock(
    body: WindingStockDayRef,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Unlocking is itself a locked-day mutation.

    Without this gate, a Write-only user blocked by require_edit_if_locked on
    every other mutation could simply unlock the day, edit freely, and
    re-lock — making the lock decorative. When there IS a lock to clear the
    day is locked by definition, so this always evaluates the permission
    check on the path that matters and is a no-op otherwise.
    """
    try:
        require_edit_if_locked(db, token_data, body.co_id, body.branch_id, body.tran_date)
        binds = {
            "co_id": int(body.co_id),
            "branch_id": int(body.branch_id),
            "tran_date": body.tran_date,
        }
        existing = db.execute(get_winding_stock_lock_row_query(), binds).fetchone()
        if existing:
            db.execute(
                clear_winding_stock_lock_query(),
                {
                    "id": existing.winding_stock_lock_id,
                    "co_id": binds["co_id"],
                    "updated_by": token_data.get("user_id"),
                },
            )
            db.commit()
        return {"data": {"locked": False}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

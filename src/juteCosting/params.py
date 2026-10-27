"""Costing parameters master (jute_cost_param) — prefix /api/juteCostParam (ITM-019).

Effective-dated EAV of the rates and norms the Daily P&L prices with
(realisation per tonne by product, wage per hand-day, dust sale value, ...).
A change going forward is a NEW row with a later effective date and the old
rows stay for history — so an edit that moves the effective date files a new
row (cost_param_edit, created_new = true) instead of rewriting the old one.

Resolution (``resolve_params``, pure — also imported by daily_pl.py): among
active rows with effective_date <= the date asked for, grouped by
(param_code, product), the winner is the max of
(branch-specific?, product-specific?, effective_date, cost_param_id): a
branch-specific row beats an all-branch row even when the all-branch row is
newer (the hands-norms "branch beats co-wide" rule); within the same scope
the latest effective_date wins, and on the same date the newest id.
``param_value`` tries (code, product) and then falls back to (code, None).

Portal persona: get_tenant_db + get_current_user_with_refresh, {"data": ...}
responses, _require_co_id / _optional_branch_id / _f / _i / _require_date copied
verbatim from spng_target_map.py, GET try/except (HTTPException: raise) /
(ValueError: 400) / (Exception: 500), and POST/PUT/DELETE rollback-then-raise.
"""

from datetime import date
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import now_ist
from src.config.db import get_tenant_db
from src.juteCosting.params_query import (
    check_cost_param_duplicate_query,
    get_cost_param_list_query,
    get_cost_param_row_query,
    get_cost_param_rows_for_resolution_query,
    insert_cost_param_query,
    soft_delete_cost_param_query,
    update_cost_param_query,
)
from src.juteProduction.doff_analysis import _verify_pair


router = APIRouter()

# Ordered catalogue of parameter codes: the master's dropdown, the default uom
# of a new row and the "not keyed" roll-call of the report all read this list.
PARAM_CODES: List[dict] = [
    {"code": "REALISATION_PER_T", "label": "Realisation per tonne", "uom": "₹/t", "product_scoped": True},
    {"code": "JUTE_RATE_PER_KG", "label": "Jute rate per kg", "uom": "₹/kg", "product_scoped": False},
    {"code": "JUTE_PER_KG_CLOTH", "label": "Jute per kg of cloth", "uom": "kg/kg", "product_scoped": False},
    {"code": "WAGE_PER_HAND_DAY", "label": "Wage per hand-day", "uom": "₹/hand-day", "product_scoped": False},
    {"code": "POWER_RATE_PER_KWH", "label": "Power rate per kWh", "uom": "₹/kWh", "product_scoped": False},
    {"code": "COST_OF_CAPITAL_PCT", "label": "Cost of working capital", "uom": "%", "product_scoped": False},
    {"code": "DUST_SALE_PER_KG", "label": "Dust sale value per kg", "uom": "₹/kg", "product_scoped": False},
    {"code": "RESTART_CAPEX_PER_FRAME", "label": "Restart capex per idle frame", "uom": "₹/frame", "product_scoped": False},
    {"code": "PAYMENT_DAYS", "label": "Payment days after despatch", "uom": "days", "product_scoped": False},
    # uom to confirm with the owner (kg per bale vs % of bale weight)
    {"code": "BALE_WEIGHT_TOLERANCE", "label": "Bale weight tolerance", "uom": "kg", "product_scoped": False},
    {"code": "FIXED_COST_PER_DAY", "label": "Overheads: total per day (when sub-lines not keyed)", "uom": "₹/day", "product_scoped": False},
    # ITM-027 — five-head P&L: power by meter type, overhead sub-lines, capital, benchmarks.
    {"code": "DG_RATE_PER_KWH", "label": "DG power cost per kWh", "uom": "₹/kWh", "product_scoped": False},
    {"code": "SOLAR_RATE_PER_KWH", "label": "Solar power cost per kWh", "uom": "₹/kWh", "product_scoped": False},
    {"code": "OVERHEAD_STAFF_PER_DAY", "label": "Overheads: staff salaries per day", "uom": "₹/day", "product_scoped": False},
    {"code": "OVERHEAD_ADMIN_PER_DAY", "label": "Overheads: admin, insurance, rates per day", "uom": "₹/day", "product_scoped": False},
    {"code": "OVERHEAD_OTHER_PER_DAY", "label": "Overheads: other fixed per day", "uom": "₹/day", "product_scoped": False},
    {"code": "WORKING_CAPITAL_OTHER", "label": "Other blocked capital (receivables, advances)", "uom": "₹", "product_scoped": False},
    {"code": "BENCH_STORES_PER_T", "label": "Benchmark: stores per tonne", "uom": "₹/t", "product_scoped": False},
    {"code": "BENCH_POWER_PER_T", "label": "Benchmark: electricity per tonne", "uom": "₹/t", "product_scoped": False},
    {"code": "BENCH_OVERHEAD_PER_T", "label": "Benchmark: overheads per tonne", "uom": "₹/t", "product_scoped": False},
    # Keyed per product (SACKING / HESSIAN): overrides the bale-register derivation
    # of finished-goods stock in the capital line while despatches are not keyed (ITM-021).
    {"code": "FG_STOCK_T", "label": "Finished goods stock (keyed)", "uom": "t", "product_scoped": True},
]
# Conversion-cost benchmarks used by the Daily P&L statement when the BENCH_*
# parameter above is not keyed (₹ per tonne of cloth).
BENCHMARK_DEFAULTS = {"stores": 4000, "power": 4000, "overheads": 2000}
BENCHMARK_SOURCE = "industry rule of thumb (owner, 2026-10-20)"
PARAM_CODE_SET = {p["code"] for p in PARAM_CODES}
PARAM_LABELS = {p["code"]: p["label"] for p in PARAM_CODES}
PARAM_UOMS = {p["code"]: p["uom"] for p in PARAM_CODES}
PRODUCT_SCOPED_CODES = {p["code"] for p in PARAM_CODES if p["product_scoped"]}
PRODUCTS = ["SACKING", "HESSIAN", "YARN"]

COST_PARAM_NOT_FOUND_MSG = "Cost parameter not found"
DUPLICATE_MSG = "A parameter with this code, product, scope and effective date already exists"
EDIT_KEY_CHANGE_MSG = "Create a new parameter instead"
INACTIVE_BASE_MSG = "This row is inactive — reactivate it first or add a new parameter"
ACTIVE_WITH_NEW_DATE_MSG = (
    "A moved effective date files a new active row — change the status of the existing row separately"
)


# =============================================================================
# Pydantic models
# =============================================================================


class CostParamCreate(BaseModel):
    co_id: int
    branch_id: Optional[int] = None
    effective_date: date
    param_code: str
    param_value: float = Field(ge=0)
    uom: Optional[str] = None
    product: Optional[str] = None
    source: Optional[str] = None
    remarks: Optional[str] = None


class CostParamUpdate(BaseModel):
    effective_date: Optional[date] = None
    param_value: Optional[float] = Field(default=None, ge=0)
    uom: Optional[str] = None
    product: Optional[str] = None
    source: Optional[str] = None
    remarks: Optional[str] = None
    active: Optional[int] = None
    # Accepted only so a changed code can be refused with a clear 400 (never applied).
    param_code: Optional[str] = None


# =============================================================================
# Helpers
# =============================================================================
#
# _require_co_id / _optional_branch_id / _f / _i / _require_date are mirrored
# EXACTLY from spng_target_map.py.


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


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _i(v, default: Optional[int] = None) -> Optional[int]:
    return default if v is None else int(v)


def _require_date(request: Request, key: str) -> date:
    raw = request.query_params.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Invalid {key} (expected YYYY-MM-DD)"
        )


def today_ist() -> date:
    """Today on the mill's clock (IST). The server runs on UTC, so between
    00:00 and 05:30 IST date.today() is still yesterday and a row effective
    today would read as not in force while the page caption says it is."""
    return now_ist().date()


def _optional_str(request: Request, key: str) -> Optional[str]:
    raw = request.query_params.get(key)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _optional_active(request: Request) -> Optional[int]:
    raw = _optional_str(request, "active")
    if raw is None:
        return None
    if raw not in ("0", "1"):
        raise HTTPException(status_code=400, detail="Invalid active")
    return int(raw)


def _validate_code(code: str) -> str:
    if code not in PARAM_CODE_SET:
        raise HTTPException(status_code=400, detail=f"Invalid param_code '{code}'")
    return code


def _validate_product(product: Optional[str]) -> Optional[str]:
    """None (all products) is allowed; anything else must be a known product."""
    if product is None or product == "":
        return None
    if product not in PRODUCTS:
        raise HTTPException(status_code=400, detail=f"Invalid product '{product}'")
    return product


def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _serialize_row(m: dict) -> dict:
    branch_id = m.get("branch_id")
    ed = _as_date(m.get("effective_date"))
    return {
        "cost_param_id": m["cost_param_id"],
        "co_id": m["co_id"],
        "branch_id": branch_id,
        "branch_name": m.get("branch_name"),
        "scope": "branch" if branch_id is not None else "all",
        "effective_date": ed.isoformat() if ed is not None else None,
        "param_code": m["param_code"],
        "param_label": PARAM_LABELS.get(m["param_code"], m["param_code"]),
        "param_value": _f(m.get("param_value")),
        "uom": m.get("uom"),
        "product": m.get("product"),
        "source": m.get("source"),
        "remarks": m.get("remarks"),
        "active": _i(m.get("active"), 1),
    }


# =============================================================================
# Pure resolution (no DB) — imported by daily_pl.py
# =============================================================================


def pick_key(row: dict) -> Tuple[bool, bool, Optional[date], int]:
    """Precedence tuple: branch-specific, product-specific, newest date, newest id."""
    return (
        row.get("branch_id") is not None,
        row.get("product") is not None,
        _as_date(row.get("effective_date")),
        int(row.get("cost_param_id") or 0),
    )


def _pick(row: dict) -> dict:
    return {
        "param_code": row.get("param_code"),
        "value": _f(row.get("param_value")),
        "uom": row.get("uom"),
        "effective_date": _as_date(row.get("effective_date")),
        "branch_id": row.get("branch_id"),
        "product": row.get("product"),
        "source": row.get("source"),
        "cost_param_id": row.get("cost_param_id"),
    }


def resolve_params(rows: List[dict], on_date: date) -> Dict[Tuple[str, Optional[str]], dict]:
    """The row in force on ``on_date`` per (param_code, product).

    Considers rows with active == 1 (a missing ``active`` counts as active) and
    effective_date <= on_date, and keeps the max ``pick_key`` per group. Returns
    {(param_code, product_or_None): pick} where pick carries value (float), uom,
    effective_date (date), branch_id, product, source, cost_param_id, param_code.
    """
    best: Dict[Tuple[str, Optional[str]], dict] = {}
    for r in rows or []:
        active = r.get("active")
        if active is not None and int(active) != 1:
            continue
        ed = _as_date(r.get("effective_date"))
        if ed is None or ed > on_date:
            continue
        key = (r.get("param_code"), r.get("product"))
        cur = best.get(key)
        if cur is None or pick_key(r) > pick_key(cur):
            best[key] = r
    return {k: _pick(v) for k, v in best.items()}


def param_value(resolved: dict, code: str, product: Optional[str] = None) -> Optional[dict]:
    """The pick for (code, product), falling back to the all-product pick (code, None)."""
    if product is not None:
        pick = resolved.get((code, product))
        if pick is not None:
            return pick
    return resolved.get((code, None))


def not_keyed_codes(resolved: dict) -> List[str]:
    """Codes of PARAM_CODES with no pick at all (a product-scoped code counts as
    keyed when any product pick or an all-product pick exists)."""
    keyed = {k[0] for k in resolved}
    return [p["code"] for p in PARAM_CODES if p["code"] not in keyed]


def _in_force_ids(rows: List[dict], on_date: date, branch_filter: Optional[int]) -> set:
    """cost_param_ids in force on ``on_date`` among ``rows`` (active ones only).

    With a branch filter the rows are one branch's plus the all-branch rows, so
    one resolution over them is the branch's view. Without a filter the rows
    span every branch: the all-branch rows are resolved on their own (the
    company-wide baseline) and then once per branch together with that branch's
    rows, so every branch's winner is flagged.
    """
    active = [r for r in rows if _i(r.get("active"), 1) == 1]
    if branch_filter is not None:
        return {p["cost_param_id"] for p in resolve_params(active, on_date).values()}
    common = [r for r in active if r.get("branch_id") is None]
    ids = {p["cost_param_id"] for p in resolve_params(common, on_date).values()}
    for b in {r["branch_id"] for r in active if r.get("branch_id") is not None}:
        scoped = common + [r for r in active if r.get("branch_id") == b]
        ids |= {p["cost_param_id"] for p in resolve_params(scoped, on_date).values()}
    return ids


def _scope(branch_id) -> str:
    return "branch" if branch_id is not None else "all"


# =============================================================================
# Setup
# =============================================================================


@router.get("/cost_param_setup")
def cost_param_setup(
    request: Request,
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Catalogue for the master form — no DB call."""
    _require_co_id(request)
    return {
        "data": {
            "param_codes": [dict(p) for p in PARAM_CODES],
            "products": list(PRODUCTS),
        }
    }


# =============================================================================
# List
# =============================================================================


@router.get("/cost_param_list")
def cost_param_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    param_code = _optional_str(request, "param_code")
    if param_code is not None:
        _validate_code(param_code)
    product = _validate_product(_optional_str(request, "product"))
    active = _optional_active(request)
    try:
        rows = [
            dict(r._mapping)
            for r in db.execute(
                get_cost_param_list_query(),
                {
                    "co_id": co_id,
                    "branch_id": branch_id,
                    "param_code": param_code,
                    "product": product,
                    "active": active,
                },
            ).fetchall()
        ]
        in_force = _in_force_ids(rows, today_ist(), branch_id)
        data = []
        for m in rows:
            row = _serialize_row(m)
            row["in_force"] = row["cost_param_id"] in in_force
            data.append(row)
        return {"data": data}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query parameter")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# By id
# =============================================================================


@router.get("/cost_param_by_id/{cost_param_id}")
def cost_param_by_id(
    cost_param_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    try:
        existing = db.execute(
            get_cost_param_row_query(), {"id": cost_param_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=COST_PARAM_NOT_FOUND_MSG)
        m = dict(existing._mapping)
        row = _serialize_row(m)
        # In force = this row is the pick for its own scope today (IST).
        today = today_ist()
        candidates = [
            dict(r._mapping)
            for r in db.execute(
                get_cost_param_rows_for_resolution_query(),
                {"co_id": co_id, "branch_id": m.get("branch_id"), "on_date": today},
            ).fetchall()
        ]
        pick = param_value(resolve_params(candidates, today), m["param_code"], m.get("product"))
        row["in_force"] = bool(pick and pick["cost_param_id"] == row["cost_param_id"])
        return {"data": row}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query parameter")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Create
# =============================================================================


def _duplicate_exists(db: Session, binds: dict) -> bool:
    row = db.execute(check_cost_param_duplicate_query(), binds).fetchone()
    return bool(row is not None and int(row.cnt or 0) > 0)


@router.post("/cost_param_create")
def cost_param_create(
    body: CostParamCreate,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        code = _validate_code(body.param_code)
        product = _validate_product(body.product)
        uom = (body.uom or "").strip() or PARAM_UOMS[code]
        user_id = token_data.get("user_id")
        co_id = int(body.co_id)
        # A branch of ANOTHER company of the tenant must not be attached to
        # this co_id (the row would never resolve for the branch view and
        # would list a foreign branch name) — same guard as the report.
        if body.branch_id is not None:
            _verify_pair(db, co_id, body.branch_id)
        if _duplicate_exists(
            db,
            {
                "co_id": co_id,
                "branch_id": body.branch_id,
                "effective_date": body.effective_date,
                "param_code": code,
                "product": product,
                "id": None,
            },
        ):
            raise HTTPException(status_code=400, detail=DUPLICATE_MSG)
        result = db.execute(
            insert_cost_param_query(),
            {
                "co_id": co_id,
                "branch_id": body.branch_id,
                "effective_date": body.effective_date,
                "param_code": code,
                "param_value": float(body.param_value),
                "uom": uom,
                "product": product,
                "source": body.source,
                "remarks": body.remarks,
                "updated_by": user_id,
            },
        )
        db.commit()
        return {"data": {"cost_param_id": result.lastrowid}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Edit (a moved effective date files a NEW row; same date edits in place)
# =============================================================================


@router.put("/cost_param_edit/{cost_param_id}")
def cost_param_edit(
    cost_param_id: int,
    body: CostParamUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    try:
        existing = db.execute(
            get_cost_param_row_query(), {"id": cost_param_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=COST_PARAM_NOT_FOUND_MSG)
        ex = dict(existing._mapping)
        given = body.model_fields_set

        # The key of a parameter is fixed: a new code or product is a new parameter.
        if "param_code" in given and body.param_code != ex["param_code"]:
            raise HTTPException(status_code=400, detail=EDIT_KEY_CHANGE_MSG)
        if "product" in given and _validate_product(body.product) != ex.get("product"):
            raise HTTPException(status_code=400, detail=EDIT_KEY_CHANGE_MSG)

        user_id = token_data.get("user_id")
        new_date = body.effective_date
        if new_date is not None and new_date != _as_date(ex["effective_date"]):
            # Acceptance 1: a different effective date files a new effective-dated
            # row with the existing values overlaid by the body; the old row stays.
            # The new row is always active, so an inactive base (reactivate it
            # first) or a body asking for an inactive row is refused outright
            # rather than silently filing an active row.
            if _i(ex.get("active"), 1) != 1:
                raise HTTPException(status_code=400, detail=INACTIVE_BASE_MSG)
            if body.active is not None and int(body.active) != 1:
                raise HTTPException(status_code=400, detail=ACTIVE_WITH_NEW_DATE_MSG)
            if _duplicate_exists(
                db,
                {
                    "co_id": co_id,
                    "branch_id": ex.get("branch_id"),
                    "effective_date": new_date,
                    "param_code": ex["param_code"],
                    "product": ex.get("product"),
                    "id": None,
                },
            ):
                raise HTTPException(status_code=400, detail=DUPLICATE_MSG)
            result = db.execute(
                insert_cost_param_query(),
                {
                    "co_id": co_id,
                    "branch_id": ex.get("branch_id"),
                    "effective_date": new_date,
                    "param_code": ex["param_code"],
                    "param_value": float(body.param_value)
                    if body.param_value is not None
                    else _f(ex.get("param_value")),
                    "uom": body.uom if "uom" in given else ex.get("uom"),
                    "product": ex.get("product"),
                    "source": body.source if "source" in given else ex.get("source"),
                    "remarks": body.remarks if "remarks" in given else ex.get("remarks"),
                    "updated_by": user_id,
                },
            )
            db.commit()
            return {
                "data": {
                    "cost_param_id": result.lastrowid,
                    "superseded_id": cost_param_id,
                    "created_new": True,
                }
            }

        sets: List[str] = []
        params: dict = {"id": cost_param_id, "co_id": co_id, "updated_by": user_id}
        if body.param_value is not None:
            sets.append("param_value = :param_value")
            params["param_value"] = float(body.param_value)
        if "uom" in given:
            sets.append("uom = :uom")
            params["uom"] = body.uom
        if "source" in given:
            sets.append("source = :source")
            params["source"] = body.source
        if "remarks" in given:
            sets.append("remarks = :remarks")
            params["remarks"] = body.remarks
        if body.active is not None:
            # Reactivating runs the same duplicate guard as an insert: a second
            # active row at the key would make the resolution pick by id silently.
            if int(body.active) == 1 and _i(ex.get("active"), 1) != 1 and _duplicate_exists(
                db,
                {
                    "co_id": co_id,
                    "branch_id": ex.get("branch_id"),
                    "effective_date": _as_date(ex["effective_date"]),
                    "param_code": ex["param_code"],
                    "product": ex.get("product"),
                    "id": cost_param_id,
                },
            ):
                raise HTTPException(status_code=400, detail=DUPLICATE_MSG)
            sets.append("active = :active")
            params["active"] = int(body.active)
        if sets:
            db.execute(update_cost_param_query(sets), params)
            db.commit()
        return {"data": {"cost_param_id": cost_param_id, "created_new": False}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Delete (soft)
# =============================================================================


@router.delete("/cost_param_delete/{cost_param_id}")
def cost_param_delete(
    cost_param_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    try:
        user_id = token_data.get("user_id")
        result = db.execute(
            soft_delete_cost_param_query(),
            {"id": cost_param_id, "co_id": co_id, "updated_by": user_id},
        )
        if not result.rowcount:
            raise HTTPException(status_code=404, detail=COST_PARAM_NOT_FOUND_MSG)
        db.commit()
        return {"data": {"message": "Deleted"}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Resolve — what is in force on a date
# =============================================================================


@router.get("/cost_param_resolve")
def cost_param_resolve(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    on_date = (
        _require_date(request, "on_date")
        if request.query_params.get("on_date")
        else today_ist()
    )
    try:
        rows = [
            dict(r._mapping)
            for r in db.execute(
                get_cost_param_rows_for_resolution_query(),
                {"co_id": co_id, "branch_id": branch_id, "on_date": on_date},
            ).fetchall()
        ]
        resolved = resolve_params(rows, on_date)
        params = {}
        for (code, product), pick in sorted(
            resolved.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
        ):
            key = f"{code}:{product}" if product else code
            params[key] = {
                "value": pick["value"],
                "uom": pick["uom"],
                "effective_date": pick["effective_date"].isoformat()
                if pick["effective_date"] is not None
                else None,
                "branch_id": pick["branch_id"],
                "scope": _scope(pick["branch_id"]),
                "product": pick["product"],
                "source": pick["source"],
                "cost_param_id": pick["cost_param_id"],
            }
        return {
            "data": {
                "on_date": on_date.isoformat(),
                "params": params,
                "not_keyed": not_keyed_codes(resolved),
            }
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query parameter")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

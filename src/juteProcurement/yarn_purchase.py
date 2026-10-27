"""Yarn Purchase — receipt of purchased yarn at the mill (supplier -> GODOWN).

Design: docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-design.md §3.1
Tables: jute_yarn_purchase (header) / jute_yarn_purchase_li (one row per yarn
quality). Kilograms are the only unit; there is no UOM column and MTR is never
accepted. Weight is stored in whole kg; ``amount = round(weight_kg * rate_per_kg, 2)`` when a rate is
given (rate is per kilogram).

Lifecycle (src/juteProcurement/constants.py):
    21 draft -> /open -> 1 open -> /send_for_approval -> 20 pending
    -> /approve -> 3 approved | /reject -> 4 rejected
    21 -> /cancel -> 6 cancelled; /reopen: 6 -> 21, 4 -> 1.
Only approved (3) documents count as godown stock (phase 3 picker).

Mounted in src/main.py at /api/yarnPurchase.
"""

import logging
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.approval_utils import (
    calculate_approval_permissions,
    process_approval,
    process_rejection,
)
from src.common.utils import now_ist, parse_json_body
from src.config.db import get_tenant_db
from src.juteProcurement.constants import (
    YARN_DOC_STATUS_CANCELLED,
    YARN_DOC_STATUS_DRAFT,
    YARN_DOC_STATUS_OPEN,
    YARN_DOC_STATUS_PENDING,
    YARN_DOC_STATUS_REJECTED,
    YARN_PACKAGE_TYPES,
    YARN_PURCHASE_MENU_PATH,
    YARN_SUPPLIER_PARTY_TYPE_NAME,
)
from src.juteProcurement.yarn_purchase_query import (
    get_next_yarn_purchase_no_for_fy,
    get_yarn_purchase_active_line_ids_query,
    get_yarn_purchase_header_query,
    get_yarn_purchase_line_totals_query,
    get_yarn_purchase_lines_query,
    get_yarn_purchase_list_query,
    get_yarn_purchase_menu_id_query,
    get_yarn_purchase_parties_query,
    get_yarn_purchase_with_approval_info,
    insert_yarn_purchase_header_query,
    insert_yarn_purchase_line_query,
    soft_delete_yarn_purchase_line_query,
    update_yarn_purchase_header_query,
    update_yarn_purchase_line_query,
    update_yarn_purchase_status,
    verify_yarn_item_query,
)
from src.juteProduction.winding_stock_query import get_winding_stock_yarn_items_query

logger = logging.getLogger(__name__)

router = APIRouter()

DOC_NAME = "Yarn Purchase"
NOT_FOUND_MSG = "Yarn Purchase not found"

# Statuses in which the header and lines may still be edited.
EDITABLE_STATUSES = (YARN_DOC_STATUS_DRAFT, YARN_DOC_STATUS_OPEN)

# Shape returned by calculate_approval_permissions, all False.
EMPTY_PERMISSIONS = {
    "canApprove": False,
    "canReject": False,
    "canOpen": False,
    "canSendForApproval": False,
    "canCancelDraft": False,
    "canReopen": False,
    "canViewApprovalLog": False,
    "canClone": False,
    "canSave": False,
}


# =============================================================================
# Small helpers
# =============================================================================


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _opt_f(v) -> Optional[float]:
    return None if v is None else float(v)


def _serialize(row: dict) -> dict:
    """Make a DB row JSON-friendly: dates -> ISO strings, Decimals -> floats."""
    out = {}
    for k, v in row.items():
        if isinstance(v, date) or hasattr(v, "isoformat"):
            out[k] = v.isoformat() if v is not None else None
        elif type(v).__name__ == "Decimal":
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _require_query_int(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _optional_query_int(request: Request, name: str) -> Optional[int]:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _optional_query_date(request: Request, name: str) -> Optional[date]:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    return _parse_date(raw, name)


def _parse_date(raw: Any, name: str) -> date:
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {name} (expected YYYY-MM-DD)")


def _required_int(payload: dict, name: str) -> int:
    val = payload.get(name)
    if val in (None, ""):
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return int(val)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _optional_int(payload: dict, name: str) -> Optional[int]:
    val = payload.get(name)
    if val in (None, ""):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _round2(value: Optional[float]) -> Optional[float]:
    """Every figure on this document is kept at 2 dp (owner ruling 2026-09-26)."""
    return None if value is None else round(float(value), 2)


def _optional_float(payload: dict, name: str, label: str) -> Optional[float]:
    val = payload.get(name)
    if val in (None, ""):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {name} on {label}")


def _optional_text(payload: dict, name: str, max_len: int) -> Optional[str]:
    val = payload.get(name)
    if val in (None, ""):
        return None
    val = str(val).strip()
    if not val:
        return None
    if len(val) > max_len:
        raise HTTPException(status_code=400, detail=f"{name} is longer than {max_len} characters")
    return val


def _resolve_menu_id(db: Session, client_menu_id: Optional[int] = None) -> Optional[int]:
    """Canonical menu_id for the Yarn Purchase page (menu_mst.menu_path); the
    caller's menu_id is only a fallback when the row is missing."""
    row = db.execute(get_yarn_purchase_menu_id_query(), {"menu_path": YARN_PURCHASE_MENU_PATH}).fetchone()
    if row:
        return int(dict(row._mapping)["menu_id"])
    return client_menu_id


def _fetch_doc(db: Session, yarn_purchase_id: int) -> dict:
    row = db.execute(get_yarn_purchase_with_approval_info(), {"yarn_purchase_id": yarn_purchase_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
    return dict(row._mapping)


def _verify_yarn_item(db: Session, co_id: int, item_id: int, label: str) -> None:
    row = db.execute(verify_yarn_item_query(), {"item_id": item_id, "co_id": co_id}).fetchone()
    if not row:
        raise HTTPException(
            status_code=400,
            detail=f"item_id {item_id} on {label} is not an active yarn item of company {co_id}",
        )


def _parse_header(payload: dict) -> dict:
    """Validate the header part of a /create or /update body."""
    co_id = _required_int(payload, "co_id")
    branch_id = _required_int(payload, "branch_id")
    if payload.get("tran_date") in (None, ""):
        raise HTTPException(status_code=400, detail="tran_date is required")
    tran_date = _parse_date(payload.get("tran_date"), "tran_date")
    challan_date = None
    if payload.get("challan_date") not in (None, ""):
        challan_date = _parse_date(payload.get("challan_date"), "challan_date")
    return {
        "co_id": co_id,
        "branch_id": branch_id,
        "tran_date": tran_date,
        "party_id": _optional_int(payload, "party_id"),
        "challan_no": _optional_text(payload, "challan_no", 50),
        "challan_date": challan_date,
        "vehicle_no": _optional_text(payload, "vehicle_no", 30),
        "inward_id": _optional_int(payload, "inward_id"),
        "remarks": _optional_text(payload, "remarks", 255),
    }


def compute_line_amount(weight_kg: float, rate_per_kg: Optional[float]) -> Optional[float]:
    """Rate is per kilogram: amount = kg * rate, 2 dp. NULL when
    no rate is given."""
    if rate_per_kg is None:
        return None
    return round(weight_kg * rate_per_kg, 2)


def _parse_lines(payload: dict) -> list[dict]:
    """Validate the ``lines`` array. Weight in kg is mandatory and positive on
    every line; package fields are counting aids only."""
    lines = payload.get("lines")
    if not isinstance(lines, list) or len(lines) == 0:
        raise HTTPException(status_code=400, detail="At least one line is required")

    parsed = []
    for idx, raw in enumerate(lines, start=1):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"Line {idx} is not an object")
        label = f"line {idx}"

        item_id = raw.get("item_id")
        if item_id in (None, ""):
            raise HTTPException(status_code=400, detail=f"item_id is required on {label}")
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid item_id on {label}")

        if raw.get("weight_kg") in (None, ""):
            raise HTTPException(status_code=400, detail=f"weight_kg is required on {label}")
        try:
            weight_kg = float(raw.get("weight_kg"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid weight_kg on {label}")
        if weight_kg <= 0:
            raise HTTPException(status_code=400, detail=f"weight_kg must be greater than 0 on {label}")

        package_type = raw.get("package_type")
        if package_type in (None, ""):
            package_type = None
        else:
            package_type = str(package_type).strip().upper()
            if package_type not in YARN_PACKAGE_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=f"package_type on {label} must be one of {', '.join(YARN_PACKAGE_TYPES)}",
                )

        no_of_packages = _round2(_optional_float(raw, "no_of_packages", label))
        if no_of_packages is not None and no_of_packages < 0:
            raise HTTPException(status_code=400, detail=f"no_of_packages cannot be negative on {label}")
        wt_per_package = _round2(_optional_float(raw, "wt_per_package", label))
        if wt_per_package is not None and wt_per_package < 0:
            raise HTTPException(status_code=400, detail=f"wt_per_package cannot be negative on {label}")
        rate_per_kg = _round2(_optional_float(raw, "rate_per_kg", label))
        if rate_per_kg is not None and rate_per_kg < 0:
            raise HTTPException(status_code=400, detail=f"rate_per_kg cannot be negative on {label}")

        li_id = raw.get("yarn_purchase_li_id")
        if li_id in (None, "", 0):
            li_id = None
        else:
            try:
                li_id = int(li_id)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"Invalid yarn_purchase_li_id on {label}")

        parsed.append(
            {
                "yarn_purchase_li_id": li_id,
                "item_id": item_id,
                "package_type": package_type,
                "no_of_packages": no_of_packages,
                "wt_per_package": wt_per_package,
                "weight_kg": float(round(weight_kg)),
                "rate_per_kg": rate_per_kg,
                "amount": compute_line_amount(weight_kg, rate_per_kg),
                "remarks": _optional_text(raw, "remarks", 255),
            }
        )
    return parsed


def _line_params(line: dict, yarn_purchase_id: int, user_id: int, ts) -> dict:
    return {
        "yarn_purchase_id": yarn_purchase_id,
        "item_id": line["item_id"],
        "package_type": line["package_type"],
        "no_of_packages": line["no_of_packages"],
        "wt_per_package": line["wt_per_package"],
        "weight_kg": line["weight_kg"],
        "rate_per_kg": line["rate_per_kg"],
        "amount": line["amount"],
        "remarks": line["remarks"],
        "updated_by": user_id,
        "updated_date_time": ts,
    }


def _parse_action_payload(payload: dict, *, id_fields=("branch_id", "menu_id")) -> tuple[int, dict]:
    """Common parsing for the workflow endpoints: yarn_purchase_id plus the
    listed integer fields, all required (indent.py convention)."""
    yarn_purchase_id = _required_int(payload, "yarn_purchase_id")
    extra = {name: _required_int(payload, name) for name in id_fields}
    return yarn_purchase_id, extra


def _set_status(db: Session, yarn_purchase_id: int, status_id: int, approval_level: Optional[int],
                user_id: int, purchase_no: Optional[int] = None) -> None:
    db.execute(
        update_yarn_purchase_status(),
        {
            "yarn_purchase_id": yarn_purchase_id,
            "status_id": status_id,
            "approval_level": approval_level,
            "updated_by": user_id,
            "updated_date_time": now_ist(),
            "purchase_no": purchase_no,
        },
    )


# =============================================================================
# Setup / list / get
# =============================================================================


@router.get("/setup")
def setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Pickers for the page: menu_id (for approval checks), yarn items of the
    company, active parties, package types."""
    co_id = _require_query_int(request, "co_id")
    _optional_query_int(request, "branch_id")  # accepted for symmetry; pickers are company-scoped
    try:
        menu_id = _resolve_menu_id(db)
        yarn_items = [
            dict(r._mapping)
            for r in db.execute(get_winding_stock_yarn_items_query(), {"co_id": co_id}).fetchall()
        ]
        for item in yarn_items:
            item["yarn_count"] = _opt_f(item.get("yarn_count"))
        parties = [
            dict(r._mapping)
            for r in db.execute(
                get_yarn_purchase_parties_query(),
                {"co_id": co_id, "party_type_name": YARN_SUPPLIER_PARTY_TYPE_NAME},
            ).fetchall()
        ]
        return {
            "data": {
                "menu_id": menu_id,
                "yarn_items": yarn_items,
                "parties": parties,
                "package_types": list(YARN_PACKAGE_TYPES),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error loading yarn purchase setup")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
def list_yarn_purchases(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_query_int(request, "co_id")
    branch_id = _require_query_int(request, "branch_id")
    from_date = _optional_query_date(request, "from_date")
    to_date = _optional_query_date(request, "to_date")
    status_id = _optional_query_int(request, "status_id")
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=400, detail="from_date cannot be after to_date")
    try:
        rows = db.execute(
            get_yarn_purchase_list_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "from_date": from_date,
                "to_date": to_date,
                "status_id": status_id,
            },
        ).fetchall()
        data = []
        for r in rows:
            row = _serialize(dict(r._mapping))
            row["line_count"] = int(row.get("line_count") or 0)
            row["total_kg"] = _f(row.get("total_kg"))
            data.append(row)
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error listing yarn purchases")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get/{yarn_purchase_id}")
def get_yarn_purchase(
    yarn_purchase_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Header + lines + the approval permission flags for the caller."""
    client_menu_id = _optional_query_int(request, "menu_id")
    try:
        hdr_row = db.execute(get_yarn_purchase_header_query(), {"yarn_purchase_id": yarn_purchase_id}).fetchone()
        if not hdr_row:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
        header = _serialize(dict(hdr_row._mapping))

        lines = []
        for r in db.execute(get_yarn_purchase_lines_query(), {"yarn_purchase_id": yarn_purchase_id}).fetchall():
            line = _serialize(dict(r._mapping))
            line["weight_kg"] = _f(line.get("weight_kg"))
            for k in ("no_of_packages", "wt_per_package", "rate_per_kg", "amount"):
                line[k] = _opt_f(line.get(k))
            lines.append(line)

        menu_id = _resolve_menu_id(db, client_menu_id)
        user_id = int(token_data.get("user_id"))
        if menu_id is not None:
            permissions = calculate_approval_permissions(
                user_id,
                menu_id,
                int(header["branch_id"]),
                int(header["status_id"]),
                header.get("approval_level"),
                db,
            )
        else:
            # No menu row and no client hint: nothing to evaluate against.
            permissions = dict(EMPTY_PERMISSIONS)
            logger.warning("menu_mst has no row for %s; permissions left empty", YARN_PURCHASE_MENU_PATH)

        return {"data": {"header": header, "lines": lines, "permissions": permissions}}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching yarn purchase %s", yarn_purchase_id)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Create / update
# =============================================================================


@router.post("/create")
def create_yarn_purchase(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """New document in Draft (21) with its lines, one transaction."""
    payload = parse_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    header = _parse_header(payload)
    lines = _parse_lines(payload)
    user_id = int(token_data.get("user_id"))

    try:
        for idx, line in enumerate(lines, start=1):
            _verify_yarn_item(db, header["co_id"], line["item_id"], f"line {idx}")

        ts = now_ist()
        res = db.execute(
            insert_yarn_purchase_header_query(),
            {
                **header,
                "status_id": YARN_DOC_STATUS_DRAFT,
                "updated_by": user_id,
                "updated_date_time": ts,
            },
        )
        yarn_purchase_id = int(res.lastrowid)
        for line in lines:
            db.execute(insert_yarn_purchase_line_query(), _line_params(line, yarn_purchase_id, user_id, ts))
        db.commit()
        return {"data": {"yarn_purchase_id": yarn_purchase_id, "status_id": YARN_DOC_STATUS_DRAFT}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error creating yarn purchase")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/update/{yarn_purchase_id}")
def update_yarn_purchase(
    yarn_purchase_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Replace header fields and lines. Allowed in Draft (21) / Open (1) only.
    Lines carrying an existing ``yarn_purchase_li_id`` are updated, new ones
    inserted, and active lines missing from the body are soft-deleted
    (active = 0) — all in one transaction."""
    payload = parse_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    header = _parse_header(payload)
    lines = _parse_lines(payload)
    user_id = int(token_data.get("user_id"))

    try:
        doc = _fetch_doc(db, yarn_purchase_id)
        status_id = int(doc["status_id"])
        if status_id not in EDITABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{DOC_NAME} with status_id {status_id} cannot be updated. "
                    f"Only Draft (21) or Open (1) documents are editable."
                ),
            )
        if int(doc["co_id"]) != header["co_id"] or int(doc["branch_id"]) != header["branch_id"]:
            raise HTTPException(
                status_code=400,
                detail="co_id / branch_id cannot be changed on an existing Yarn Purchase",
            )

        for idx, line in enumerate(lines, start=1):
            _verify_yarn_item(db, header["co_id"], line["item_id"], f"line {idx}")

        existing_ids = {
            int(dict(r._mapping)["yarn_purchase_li_id"])
            for r in db.execute(get_yarn_purchase_active_line_ids_query(), {"yarn_purchase_id": yarn_purchase_id}).fetchall()
        }
        kept_ids = set()
        for idx, line in enumerate(lines, start=1):
            li_id = line["yarn_purchase_li_id"]
            if li_id is not None:
                if li_id not in existing_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"yarn_purchase_li_id {li_id} on line {idx} does not belong to this Yarn Purchase",
                    )
                if li_id in kept_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"yarn_purchase_li_id {li_id} appears more than once",
                    )
                kept_ids.add(li_id)

        ts = now_ist()
        db.execute(
            update_yarn_purchase_header_query(),
            {
                "yarn_purchase_id": yarn_purchase_id,
                "tran_date": header["tran_date"],
                "party_id": header["party_id"],
                "challan_no": header["challan_no"],
                "challan_date": header["challan_date"],
                "vehicle_no": header["vehicle_no"],
                "inward_id": header["inward_id"],
                "remarks": header["remarks"],
                "updated_by": user_id,
                "updated_date_time": ts,
            },
        )

        for li_id in sorted(existing_ids - kept_ids):
            db.execute(
                soft_delete_yarn_purchase_line_query(),
                {
                    "yarn_purchase_li_id": li_id,
                    "yarn_purchase_id": yarn_purchase_id,
                    "updated_by": user_id,
                    "updated_date_time": ts,
                },
            )

        for line in lines:
            params = _line_params(line, yarn_purchase_id, user_id, ts)
            if line["yarn_purchase_li_id"] is not None:
                params["yarn_purchase_li_id"] = line["yarn_purchase_li_id"]
                db.execute(update_yarn_purchase_line_query(), params)
            else:
                db.execute(insert_yarn_purchase_line_query(), params)

        db.commit()
        return {"data": {"yarn_purchase_id": yarn_purchase_id, "status_id": status_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error updating yarn purchase %s", yarn_purchase_id)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Workflow: open / cancel / send_for_approval / approve / reject / reopen
# =============================================================================


@router.post("/open")
def open_yarn_purchase(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """21 -> 1. Assigns purchase_no (per branch, per financial year of
    tran_date) when it is still NULL."""
    yarn_purchase_id, _ = _parse_action_payload(payload)
    user_id = int(token_data.get("user_id"))
    try:
        doc = _fetch_doc(db, yarn_purchase_id)
        status_id = int(doc["status_id"])
        if status_id != YARN_DOC_STATUS_DRAFT:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot open {DOC_NAME} with status_id {status_id}. Expected status 21 (Drafted).",
            )
        purchase_no = doc.get("purchase_no")
        if purchase_no is None or int(purchase_no) == 0:
            tran_date = doc.get("tran_date")
            if tran_date is None:
                raise HTTPException(status_code=400, detail="tran_date is required to generate the purchase number")
            purchase_no = get_next_yarn_purchase_no_for_fy(db, int(doc["branch_id"]), _parse_date(tran_date, "tran_date"))
        else:
            purchase_no = int(purchase_no)

        _set_status(db, yarn_purchase_id, YARN_DOC_STATUS_OPEN, None, user_id, purchase_no=purchase_no)
        db.commit()
        return {
            "data": {
                "yarn_purchase_id": yarn_purchase_id,
                "status_id": YARN_DOC_STATUS_OPEN,
                "purchase_no": purchase_no,
            },
            "status": "success",
            "new_status_id": YARN_DOC_STATUS_OPEN,
            "purchase_no": purchase_no,
            "message": f"{DOC_NAME} opened successfully.",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error opening yarn purchase %s", yarn_purchase_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cancel")
def cancel_yarn_purchase(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """21 -> 6 (cancel a draft)."""
    yarn_purchase_id, _ = _parse_action_payload(payload)
    user_id = int(token_data.get("user_id"))
    try:
        doc = _fetch_doc(db, yarn_purchase_id)
        status_id = int(doc["status_id"])
        if status_id != YARN_DOC_STATUS_DRAFT:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot cancel {DOC_NAME} with status_id {status_id}. Expected status 21 (Drafted).",
            )
        _set_status(db, yarn_purchase_id, YARN_DOC_STATUS_CANCELLED, None, user_id)
        db.commit()
        return {
            "data": {"yarn_purchase_id": yarn_purchase_id, "status_id": YARN_DOC_STATUS_CANCELLED},
            "status": "success",
            "new_status_id": YARN_DOC_STATUS_CANCELLED,
            "message": "Draft cancelled successfully.",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error cancelling yarn purchase %s", yarn_purchase_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/send_for_approval")
def send_yarn_purchase_for_approval(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """1 -> 20 at approval level 1. Refused when the document has no active
    line or any line has weight_kg <= 0."""
    yarn_purchase_id, _ = _parse_action_payload(payload)
    user_id = int(token_data.get("user_id"))
    try:
        doc = _fetch_doc(db, yarn_purchase_id)
        status_id = int(doc["status_id"])
        if status_id != YARN_DOC_STATUS_OPEN:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot send {DOC_NAME} with status_id {status_id} for approval. Expected status 1 (Open).",
            )
        totals_row = db.execute(get_yarn_purchase_line_totals_query(), {"yarn_purchase_id": yarn_purchase_id}).fetchone()
        totals = dict(totals_row._mapping) if totals_row else {}
        line_count = int(totals.get("line_count") or 0)
        if line_count == 0:
            raise HTTPException(status_code=400, detail=f"{DOC_NAME} has no lines; add at least one line before sending for approval")
        if _f(totals.get("min_kg")) <= 0:
            raise HTTPException(status_code=400, detail="Every line must have weight_kg greater than 0 before sending for approval")

        _set_status(db, yarn_purchase_id, YARN_DOC_STATUS_PENDING, 1, user_id)
        db.commit()
        return {
            "data": {"yarn_purchase_id": yarn_purchase_id, "status_id": YARN_DOC_STATUS_PENDING, "approval_level": 1},
            "status": "success",
            "new_status_id": YARN_DOC_STATUS_PENDING,
            "new_approval_level": 1,
            "message": f"{DOC_NAME} sent for approval successfully.",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error sending yarn purchase %s for approval", yarn_purchase_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/approve")
def approve_yarn_purchase(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Level-based approval through the shared engine: 20 -> 20 (next level)
    or 20 -> 3. Quantity-only document, so no value checks."""
    yarn_purchase_id, ids = _parse_action_payload(payload)
    user_id = int(token_data.get("user_id"))
    try:
        result = process_approval(
            doc_id=yarn_purchase_id,
            user_id=user_id,
            menu_id=ids["menu_id"],
            db=db,
            get_doc_fn=get_yarn_purchase_with_approval_info,
            update_status_fn=update_yarn_purchase_status,
            id_param_name="yarn_purchase_id",
            doc_name=DOC_NAME,
            extra_update_params={"purchase_no": None},
        )
        result["data"] = {
            "yarn_purchase_id": yarn_purchase_id,
            "status_id": result.get("new_status_id"),
            "approval_level": result.get("new_approval_level"),
        }
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error approving yarn purchase %s", yarn_purchase_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reject")
def reject_yarn_purchase(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """20 -> 4 through the shared engine. Body: yarn_purchase_id, co_id,
    menu_id, reason."""
    yarn_purchase_id, ids = _parse_action_payload(payload, id_fields=("co_id",))
    menu_id = _optional_int(payload, "menu_id")
    reason = _optional_text(payload, "reason", 1000)
    user_id = int(token_data.get("user_id"))
    try:
        result = process_rejection(
            doc_id=yarn_purchase_id,
            user_id=user_id,
            menu_id=menu_id,
            db=db,
            get_doc_fn=get_yarn_purchase_with_approval_info,
            update_status_fn=update_yarn_purchase_status,
            id_param_name="yarn_purchase_id",
            doc_name=DOC_NAME,
            reason=reason,
            extra_update_params={"purchase_no": None},
        )
        result["data"] = {
            "yarn_purchase_id": yarn_purchase_id,
            "status_id": YARN_DOC_STATUS_REJECTED,
            "approval_level": None,
        }
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error rejecting yarn purchase %s", yarn_purchase_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reopen")
def reopen_yarn_purchase(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """6 -> 21 (cancelled back to draft), 4 -> 1 (rejected back to open);
    approval_level cleared. purchase_no is kept."""
    yarn_purchase_id, _ = _parse_action_payload(payload)
    user_id = int(token_data.get("user_id"))
    try:
        doc = _fetch_doc(db, yarn_purchase_id)
        status_id = int(doc["status_id"])
        if status_id == YARN_DOC_STATUS_CANCELLED:
            new_status_id = YARN_DOC_STATUS_DRAFT
        elif status_id == YARN_DOC_STATUS_REJECTED:
            new_status_id = YARN_DOC_STATUS_OPEN
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot reopen {DOC_NAME} with status_id {status_id}. "
                    f"Only Cancelled (6) or Rejected (4) can be reopened."
                ),
            )
        _set_status(db, yarn_purchase_id, new_status_id, None, user_id)
        db.commit()
        return {
            "data": {"yarn_purchase_id": yarn_purchase_id, "status_id": new_status_id, "approval_level": None},
            "status": "success",
            "new_status_id": new_status_id,
            "message": f"{DOC_NAME} reopened successfully (status: {new_status_id}).",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error reopening yarn purchase %s", yarn_purchase_id)
        raise HTTPException(status_code=500, detail=str(e))

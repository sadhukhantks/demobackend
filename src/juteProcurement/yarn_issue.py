"""Yarn Issue / Return — purchased yarn between the godown and a department floor.

Design: docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-design.md §3.2
Tables: jute_yarn_issue (header) / jute_yarn_issue_li (one row per lot drawn
or returned). Kilograms are the only unit. ``issue_type`` is ISSUE (godown ->
department floor) or RETURN (department floor -> godown); the lot
(``yarn_purchase_li_id``, a line of an approved Yarn Purchase) is mandatory on
every line (owner ruling 5) and the picker lists lots oldest first.

Guards on save (owner rulings, design §3.2):
    ISSUE  - per lot, this document's kg may not exceed the godown balance
             (hard block, 400). Skipping an older lot of the same item that
             still has stock is allowed but reported in ``warnings``.
    RETURN - per lot, kg above the department's floor book balance is a
             warning, not a block (counts will correct it).

Lifecycle (src/juteProcurement/constants.py):
    21 draft -> /open -> 1 open -> /send_for_approval -> 20 pending
    -> /approve -> 3 approved | /reject -> 4 rejected
    21 -> /cancel -> 6 cancelled; /reopen: 6 -> 21, 4 -> 1.
issue_no is per branch, per financial year of tran_date, one series per
issue_type. Every non-draft / non-rejected / non-cancelled document moves
stock in vw_yarn_godown_outstanding.

Mounted in src/main.py at /api/yarnIssue.
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
    YARN_ISSUE_MENU_PATH,
    YARN_ISSUE_TYPE_ISSUE,
    YARN_ISSUE_TYPES,
    YARN_PURCHASE_ISSUABLE_STATUSES,
)
from src.juteProcurement.yarn_issue_query import (
    get_dept_lots_query,
    get_godown_lots_query,
    get_next_yarn_issue_no_for_fy,
    get_yarn_issue_active_line_ids_query,
    get_yarn_issue_departments_query,
    get_yarn_issue_header_query,
    get_yarn_issue_line_totals_query,
    get_yarn_issue_lines_query,
    get_yarn_issue_list_query,
    get_yarn_issue_with_approval_info,
    get_yarn_lot_query,
    insert_yarn_issue_header_query,
    insert_yarn_issue_line_query,
    soft_delete_yarn_issue_line_query,
    update_yarn_issue_header_query,
    update_yarn_issue_line_query,
    update_yarn_issue_status,
    verify_department_query,
)
from src.juteProcurement.yarn_purchase_query import get_yarn_purchase_menu_id_query
from src.juteProduction.winding_stock_query import get_winding_stock_yarn_items_query

logger = logging.getLogger(__name__)

router = APIRouter()

DOC_NAME = "Yarn Issue"
NOT_FOUND_MSG = "Yarn Issue not found"

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

# Weights are DECIMAL(12,3); compare at that precision.
KG_TOLERANCE = 0.0005


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


def _parse_issue_type(raw: Any, *, required: bool = True) -> Optional[str]:
    """Normalise to one of YARN_ISSUE_TYPES; 400 otherwise."""
    if raw in (None, ""):
        if required:
            raise HTTPException(status_code=400, detail="issue_type is required")
        return None
    issue_type = str(raw).strip().upper()
    if issue_type not in YARN_ISSUE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"issue_type must be one of {', '.join(YARN_ISSUE_TYPES)}",
        )
    return issue_type


def _fmt_kg(v: float) -> str:
    """Kg for messages: no trailing zeros ('500', '12.5', '0.125')."""
    return f"{float(v):.3f}".rstrip("0").rstrip(".")


def _lot_label(lot: dict) -> str:
    """'PUR-<purchase_no>'; an unnumbered purchase falls back to the line id."""
    purchase_no = lot.get("purchase_no")
    if purchase_no is not None:
        return f"PUR-{int(purchase_no)}"
    return f"lot #{int(lot['yarn_purchase_li_id'])}"


def _resolve_menu_id(db: Session, client_menu_id: Optional[int] = None) -> Optional[int]:
    """Canonical menu_id for the Yarn Issue page (menu_mst.menu_path); the
    caller's menu_id is only a fallback when the row is missing."""
    row = db.execute(get_yarn_purchase_menu_id_query(), {"menu_path": YARN_ISSUE_MENU_PATH}).fetchone()
    if row:
        return int(dict(row._mapping)["menu_id"])
    return client_menu_id


def _fetch_doc(db: Session, yarn_issue_id: int) -> dict:
    row = db.execute(get_yarn_issue_with_approval_info(), {"yarn_issue_id": yarn_issue_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
    return dict(row._mapping)


def _verify_department(db: Session, branch_id: int, dept_id: int) -> None:
    row = db.execute(verify_department_query(), {"dept_id": dept_id, "branch_id": branch_id}).fetchone()
    if not row:
        raise HTTPException(
            status_code=400,
            detail=f"dept_id {dept_id} is not a department of branch {branch_id}",
        )


def _parse_header(payload: dict) -> dict:
    """Validate the header part of a /create or /update body."""
    co_id = _required_int(payload, "co_id")
    branch_id = _required_int(payload, "branch_id")
    if payload.get("tran_date") in (None, ""):
        raise HTTPException(status_code=400, detail="tran_date is required")
    tran_date = _parse_date(payload.get("tran_date"), "tran_date")
    issue_type = _parse_issue_type(payload.get("issue_type"))
    dept_id = _required_int(payload, "dept_id")
    return {
        "co_id": co_id,
        "branch_id": branch_id,
        "tran_date": tran_date,
        "issue_type": issue_type,
        "dept_id": dept_id,
        "remarks": _optional_text(payload, "remarks", 255),
    }


def _parse_lines(payload: dict) -> list[dict]:
    """Validate the ``lines`` array. The lot and a positive weight in kg are
    mandatory on every line; packages are a counting aid only."""
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

        lot_id = raw.get("yarn_purchase_li_id")
        if lot_id in (None, "", 0):
            raise HTTPException(status_code=400, detail=f"yarn_purchase_li_id (lot) is required on {label}")
        try:
            lot_id = int(lot_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid yarn_purchase_li_id on {label}")

        if raw.get("weight_kg") in (None, ""):
            raise HTTPException(status_code=400, detail=f"weight_kg is required on {label}")
        try:
            weight_kg = float(raw.get("weight_kg"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid weight_kg on {label}")
        if weight_kg <= 0:
            raise HTTPException(status_code=400, detail=f"weight_kg must be greater than 0 on {label}")

        no_of_packages = _optional_float(raw, "no_of_packages", label)


        no_of_packages = None if no_of_packages is None else round(no_of_packages, 2)
        if no_of_packages is not None and no_of_packages < 0:
            raise HTTPException(status_code=400, detail=f"no_of_packages cannot be negative on {label}")

        li_id = raw.get("yarn_issue_li_id")
        if li_id in (None, "", 0):
            li_id = None
        else:
            try:
                li_id = int(li_id)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"Invalid yarn_issue_li_id on {label}")

        parsed.append(
            {
                "yarn_issue_li_id": li_id,
                "item_id": item_id,
                "yarn_purchase_li_id": lot_id,
                "no_of_packages": no_of_packages,
                "weight_kg": float(round(weight_kg)),
                "remarks": _optional_text(raw, "remarks", 255),
            }
        )
    return parsed


def _line_params(line: dict, yarn_issue_id: int, user_id: int, ts) -> dict:
    return {
        "yarn_issue_id": yarn_issue_id,
        "item_id": line["item_id"],
        "yarn_purchase_li_id": line["yarn_purchase_li_id"],
        "no_of_packages": line["no_of_packages"],
        "weight_kg": line["weight_kg"],
        "remarks": line["remarks"],
        "updated_by": user_id,
        "updated_date_time": ts,
    }


def _parse_action_payload(payload: dict, *, id_fields=("branch_id", "menu_id")) -> tuple[int, dict]:
    """Common parsing for the workflow endpoints: yarn_issue_id plus the
    listed integer fields, all required (indent.py convention)."""
    yarn_issue_id = _required_int(payload, "yarn_issue_id")
    extra = {name: _required_int(payload, name) for name in id_fields}
    return yarn_issue_id, extra


def _set_status(db: Session, yarn_issue_id: int, status_id: int, approval_level: Optional[int],
                user_id: int, issue_no: Optional[int] = None) -> None:
    db.execute(
        update_yarn_issue_status(),
        {
            "yarn_issue_id": yarn_issue_id,
            "status_id": status_id,
            "approval_level": approval_level,
            "updated_by": user_id,
            "updated_date_time": now_ist(),
            "issue_no": issue_no,
        },
    )


# =============================================================================
# Lot guards (shared by /create and /update)
# =============================================================================


def _fetch_lots(db: Session, lines: list[dict], exclude_issue_id: Optional[int]) -> dict[int, dict]:
    """The distinct lots the lines reference, keyed by yarn_purchase_li_id.
    A lot the godown view does not know (draft / rejected / cancelled
    purchase, or no such line) is a 400 naming the first line that uses it."""
    lots: dict[int, dict] = {}
    for idx, line in enumerate(lines, start=1):
        lot_id = line["yarn_purchase_li_id"]
        if lot_id in lots:
            continue
        row = db.execute(
            get_yarn_lot_query(),
            {"yarn_purchase_li_id": lot_id, "exclude_issue_id": exclude_issue_id},
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=400,
                detail=f"yarn_purchase_li_id {lot_id} on line {idx} is not a godown lot of an active Yarn Purchase",
            )
        lots[lot_id] = dict(row._mapping)
    return lots


def _check_lines_against_lots(header: dict, lines: list[dict], lots: dict[int, dict]) -> None:
    """Every line's lot must be of the same company / branch and carry the
    line's item (400 naming the line)."""
    for idx, line in enumerate(lines, start=1):
        lot = lots[line["yarn_purchase_li_id"]]
        if int(lot["co_id"]) != header["co_id"] or int(lot["branch_id"]) != header["branch_id"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Lot {_lot_label(lot)} on line {idx} belongs to company {int(lot['co_id'])} / "
                    f"branch {int(lot['branch_id'])}, not company {header['co_id']} / branch {header['branch_id']}"
                ),
            )
        if int(lot["item_id"]) != line["item_id"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Lot {_lot_label(lot)} on line {idx} is {lot.get('item_name') or 'item ' + str(int(lot['item_id']))} "
                    f"(item_id {int(lot['item_id'])}), not item_id {line['item_id']}"
                ),
            )


def _kg_per_lot(lines: list[dict]) -> dict[int, float]:
    totals: dict[int, float] = {}
    for line in lines:
        totals[line["yarn_purchase_li_id"]] = round(totals.get(line["yarn_purchase_li_id"], 0.0) + line["weight_kg"], 3)
    return totals


def _guard_issue(db: Session, header: dict, lines: list[dict], lots: dict[int, dict],
                 exclude_issue_id: Optional[int]) -> list[str]:
    """ISSUE: lot must be issuable and the document's kg per lot may not
    exceed the godown balance (hard block). Returns the FIFO warnings."""
    for lot_id, kg in _kg_per_lot(lines).items():
        lot = lots[lot_id]
        if int(lot["purchase_status_id"]) not in YARN_PURCHASE_ISSUABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Lot {_lot_label(lot)} cannot be issued: its Yarn Purchase has status_id "
                    f"{int(lot['purchase_status_id'])} (expected {', '.join(str(s) for s in YARN_PURCHASE_ISSUABLE_STATUSES)})"
                ),
            )
        bal_kg = _f(lot.get("bal_kg"))
        if kg > bal_kg + KG_TOLERANCE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Issue of {_fmt_kg(kg)} kg on lot {_lot_label(lot)} ({lot.get('item_name') or int(lot['item_id'])}) "
                    f"exceeds the godown balance of {_fmt_kg(bal_kg)} kg"
                ),
            )
    return _fifo_warnings(db, header, lines, lots, exclude_issue_id)


def _fifo_warnings(db: Session, header: dict, lines: list[dict], lots: dict[int, dict],
                   exclude_issue_id: Optional[int]) -> list[str]:
    """Owner ruling 5: an older lot (earlier tran_date, or same date and lower
    id) of the same item that still has stock and that this document does
    not draw from at all is allowed but flagged. Never blocks."""
    drawn_by_item: dict[int, set[int]] = {}
    for line in lines:
        drawn_by_item.setdefault(line["item_id"], set()).add(line["yarn_purchase_li_id"])

    warnings: list[str] = []
    for item_id in sorted(drawn_by_item):
        drawn = drawn_by_item[item_id]
        newest_drawn = max(
            (lots[lot_id]["tran_date"], int(lot_id)) for lot_id in drawn
        )
        rows = db.execute(
            get_godown_lots_query(),
            {
                "co_id": header["co_id"],
                "branch_id": header["branch_id"],
                "item_id": item_id,
                "exclude_issue_id": exclude_issue_id,
            },
        ).fetchall()
        for r in rows:
            lot = dict(r._mapping)
            lot_id = int(lot["yarn_purchase_li_id"])
            if lot_id in drawn:
                continue
            if (lot["tran_date"], lot_id) >= newest_drawn:
                break  # rows are FIFO-ordered: nothing after this is older
            bal_kg = _f(lot.get("bal_kg"))
            if bal_kg <= 0:
                continue
            item_name = lot.get("item_name") or f"item {item_id}"
            lot_date = lot["tran_date"].isoformat() if hasattr(lot["tran_date"], "isoformat") else str(lot["tran_date"])
            warnings.append(
                f"Older lot {_lot_label(lot)} ({lot_date}, {_fmt_kg(bal_kg)} kg) skipped for {item_name}"
            )
    return warnings


def _guard_return(db: Session, header: dict, lines: list[dict], lots: dict[int, dict],
                  exclude_issue_id: Optional[int]) -> list[str]:
    """RETURN: kg per lot above the department's floor book balance is a
    warning, not a block (design §3.2: counts will correct it)."""
    rows = db.execute(
        get_dept_lots_query(),
        {
            "co_id": header["co_id"],
            "branch_id": header["branch_id"],
            "dept_id": header["dept_id"],
            "item_id": None,
            "exclude_issue_id": exclude_issue_id,
        },
    ).fetchall()
    floor: dict[int, float] = {}
    for r in rows:
        m = dict(r._mapping)
        floor[int(m["yarn_purchase_li_id"])] = _f(m.get("floor_bal_kg"))

    warnings: list[str] = []
    for lot_id, kg in _kg_per_lot(lines).items():
        floor_kg = floor.get(lot_id, 0.0)
        if kg > floor_kg + KG_TOLERANCE:
            warnings.append(
                f"Return of {_fmt_kg(kg)} kg on lot {_lot_label(lots[lot_id])} exceeds the department's "
                f"floor balance of {_fmt_kg(floor_kg)} kg"
            )
    return warnings


def _validate_lots(db: Session, header: dict, lines: list[dict], exclude_issue_id: Optional[int]) -> list[str]:
    """All lot guards for a /create or /update body. Raises 400 on a hard
    block; returns the warnings to carry in the response."""
    lots = _fetch_lots(db, lines, exclude_issue_id)
    _check_lines_against_lots(header, lines, lots)
    if header["issue_type"] == YARN_ISSUE_TYPE_ISSUE:
        return _guard_issue(db, header, lines, lots, exclude_issue_id)
    return _guard_return(db, header, lines, lots, exclude_issue_id)


def _lot_row(row) -> dict:
    lot = _serialize(dict(row._mapping))
    for k in ("received_kg", "bal_kg", "bal_pkgs", "floor_bal_kg", "floor_bal_pkgs"):
        if k in lot:
            lot[k] = _f(lot.get(k))
    return lot


# =============================================================================
# Setup / pickers / list / get
# =============================================================================


@router.get("/setup")
def setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Pickers for the page: menu_id (for approval checks), departments of
    the branch, issue types, yarn items of the company."""
    co_id = _require_query_int(request, "co_id")
    branch_id = _require_query_int(request, "branch_id")
    try:
        menu_id = _resolve_menu_id(db)
        departments = [
            dict(r._mapping)
            for r in db.execute(get_yarn_issue_departments_query(), {"branch_id": branch_id}).fetchall()
        ]
        yarn_items = [
            dict(r._mapping)
            for r in db.execute(get_winding_stock_yarn_items_query(), {"co_id": co_id}).fetchall()
        ]
        for item in yarn_items:
            item["yarn_count"] = _opt_f(item.get("yarn_count"))
        return {
            "data": {
                "menu_id": menu_id,
                "departments": departments,
                "issue_types": list(YARN_ISSUE_TYPES),
                "yarn_items": yarn_items,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error loading yarn issue setup")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/godown_lots")
def godown_lots(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Lots an ISSUE may draw from, oldest first. ``exclude_issue_id`` (the
    document being edited) adds its own saved lines back into the balance."""
    co_id = _require_query_int(request, "co_id")
    branch_id = _require_query_int(request, "branch_id")
    item_id = _optional_query_int(request, "item_id")
    exclude_issue_id = _optional_query_int(request, "exclude_issue_id")
    try:
        rows = db.execute(
            get_godown_lots_query(),
            {"co_id": co_id, "branch_id": branch_id, "item_id": item_id, "exclude_issue_id": exclude_issue_id},
        ).fetchall()
        return {"data": [_lot_row(r) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error listing godown lots")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dept_lots")
def dept_lots(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Lots a RETURN may give back: the department's floor book balance per
    lot (ISSUE kg - RETURN kg), oldest first."""
    co_id = _require_query_int(request, "co_id")
    branch_id = _require_query_int(request, "branch_id")
    dept_id = _require_query_int(request, "dept_id")
    item_id = _optional_query_int(request, "item_id")
    exclude_issue_id = _optional_query_int(request, "exclude_issue_id")
    try:
        rows = db.execute(
            get_dept_lots_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "dept_id": dept_id,
                "item_id": item_id,
                "exclude_issue_id": exclude_issue_id,
            },
        ).fetchall()
        return {"data": [_lot_row(r) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error listing department lots")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
def list_yarn_issues(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_query_int(request, "co_id")
    branch_id = _require_query_int(request, "branch_id")
    from_date = _optional_query_date(request, "from_date")
    to_date = _optional_query_date(request, "to_date")
    status_id = _optional_query_int(request, "status_id")
    issue_type = _parse_issue_type(request.query_params.get("issue_type"), required=False)
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=400, detail="from_date cannot be after to_date")
    try:
        rows = db.execute(
            get_yarn_issue_list_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "from_date": from_date,
                "to_date": to_date,
                "status_id": status_id,
                "issue_type": issue_type,
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
        logger.exception("Error listing yarn issues")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get/{yarn_issue_id}")
def get_yarn_issue(
    yarn_issue_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Header + lines (with the lot's purchase number, date and current
    godown balance) + the approval permission flags for the caller."""
    client_menu_id = _optional_query_int(request, "menu_id")
    try:
        hdr_row = db.execute(get_yarn_issue_header_query(), {"yarn_issue_id": yarn_issue_id}).fetchone()
        if not hdr_row:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
        header = _serialize(dict(hdr_row._mapping))

        lines = []
        for r in db.execute(get_yarn_issue_lines_query(), {"yarn_issue_id": yarn_issue_id}).fetchall():
            line = _serialize(dict(r._mapping))
            line["weight_kg"] = _f(line.get("weight_kg"))
            for k in ("no_of_packages", "lot_bal_kg"):
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
            logger.warning("menu_mst has no row for %s; permissions left empty", YARN_ISSUE_MENU_PATH)

        return {"data": {"header": header, "lines": lines, "permissions": permissions}}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching yarn issue %s", yarn_issue_id)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Create / update
# =============================================================================


@router.post("/create")
def create_yarn_issue(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """New document in Draft (21) with its lines, one transaction. The lot
    guards run before anything is written; ``warnings`` carries the FIFO
    skips (ISSUE) or floor-balance overruns (RETURN)."""
    payload = parse_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    header = _parse_header(payload)
    lines = _parse_lines(payload)
    user_id = int(token_data.get("user_id"))

    try:
        _verify_department(db, header["branch_id"], header["dept_id"])
        warnings = _validate_lots(db, header, lines, exclude_issue_id=None)

        ts = now_ist()
        res = db.execute(
            insert_yarn_issue_header_query(),
            {
                **header,
                "status_id": YARN_DOC_STATUS_DRAFT,
                "updated_by": user_id,
                "updated_date_time": ts,
            },
        )
        yarn_issue_id = int(res.lastrowid)
        for line in lines:
            db.execute(insert_yarn_issue_line_query(), _line_params(line, yarn_issue_id, user_id, ts))
        db.commit()
        return {"data": {"yarn_issue_id": yarn_issue_id, "status_id": YARN_DOC_STATUS_DRAFT, "warnings": warnings}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error creating yarn issue")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/update/{yarn_issue_id}")
def update_yarn_issue(
    yarn_issue_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Replace header fields and lines. Allowed in Draft (21) / Open (1) only.
    Lines carrying an existing ``yarn_issue_li_id`` are updated, new ones
    inserted, and active lines missing from the body are soft-deleted
    (active = 0) — all in one transaction. The lot guards see the godown
    balance with this document's own saved lines added back."""
    payload = parse_json_body(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    header = _parse_header(payload)
    lines = _parse_lines(payload)
    user_id = int(token_data.get("user_id"))

    try:
        doc = _fetch_doc(db, yarn_issue_id)
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
                detail="co_id / branch_id cannot be changed on an existing Yarn Issue",
            )
        if doc.get("issue_no") is not None and str(doc.get("issue_type") or "").upper() != header["issue_type"]:
            # issue_no is one series per type; a numbered document keeps its type
            raise HTTPException(
                status_code=400,
                detail=f"issue_type cannot be changed once {DOC_NAME} has been numbered (issue_no {int(doc['issue_no'])})",
            )

        _verify_department(db, header["branch_id"], header["dept_id"])

        existing_ids = {
            int(dict(r._mapping)["yarn_issue_li_id"])
            for r in db.execute(get_yarn_issue_active_line_ids_query(), {"yarn_issue_id": yarn_issue_id}).fetchall()
        }
        kept_ids = set()
        for idx, line in enumerate(lines, start=1):
            li_id = line["yarn_issue_li_id"]
            if li_id is not None:
                if li_id not in existing_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"yarn_issue_li_id {li_id} on line {idx} does not belong to this Yarn Issue",
                    )
                if li_id in kept_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"yarn_issue_li_id {li_id} appears more than once",
                    )
                kept_ids.add(li_id)

        warnings = _validate_lots(db, header, lines, exclude_issue_id=yarn_issue_id)

        ts = now_ist()
        db.execute(
            update_yarn_issue_header_query(),
            {
                "yarn_issue_id": yarn_issue_id,
                "tran_date": header["tran_date"],
                "issue_type": header["issue_type"],
                "dept_id": header["dept_id"],
                "remarks": header["remarks"],
                "updated_by": user_id,
                "updated_date_time": ts,
            },
        )

        for li_id in sorted(existing_ids - kept_ids):
            db.execute(
                soft_delete_yarn_issue_line_query(),
                {
                    "yarn_issue_li_id": li_id,
                    "yarn_issue_id": yarn_issue_id,
                    "updated_by": user_id,
                    "updated_date_time": ts,
                },
            )

        for line in lines:
            params = _line_params(line, yarn_issue_id, user_id, ts)
            if line["yarn_issue_li_id"] is not None:
                params["yarn_issue_li_id"] = line["yarn_issue_li_id"]
                db.execute(update_yarn_issue_line_query(), params)
            else:
                db.execute(insert_yarn_issue_line_query(), params)

        db.commit()
        return {"data": {"yarn_issue_id": yarn_issue_id, "status_id": status_id, "warnings": warnings}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error updating yarn issue %s", yarn_issue_id)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Workflow: open / cancel / send_for_approval / approve / reject / reopen
# =============================================================================


@router.post("/open")
def open_yarn_issue(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """21 -> 1. Assigns issue_no (per branch, per financial year of
    tran_date, one series per issue_type) when it is still NULL."""
    yarn_issue_id, _ = _parse_action_payload(payload)
    user_id = int(token_data.get("user_id"))
    try:
        doc = _fetch_doc(db, yarn_issue_id)
        status_id = int(doc["status_id"])
        if status_id != YARN_DOC_STATUS_DRAFT:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot open {DOC_NAME} with status_id {status_id}. Expected status 21 (Drafted).",
            )
        issue_no = doc.get("issue_no")
        if issue_no is None or int(issue_no) == 0:
            tran_date = doc.get("tran_date")
            if tran_date is None:
                raise HTTPException(status_code=400, detail="tran_date is required to generate the issue number")
            issue_type = _parse_issue_type(doc.get("issue_type"))
            issue_no = get_next_yarn_issue_no_for_fy(
                db, int(doc["branch_id"]), _parse_date(tran_date, "tran_date"), issue_type
            )
        else:
            issue_no = int(issue_no)

        _set_status(db, yarn_issue_id, YARN_DOC_STATUS_OPEN, None, user_id, issue_no=issue_no)
        db.commit()
        return {
            "data": {
                "yarn_issue_id": yarn_issue_id,
                "status_id": YARN_DOC_STATUS_OPEN,
                "issue_no": issue_no,
            },
            "status": "success",
            "new_status_id": YARN_DOC_STATUS_OPEN,
            "issue_no": issue_no,
            "message": f"{DOC_NAME} opened successfully.",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error opening yarn issue %s", yarn_issue_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cancel")
def cancel_yarn_issue(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """21 -> 6 (cancel a draft)."""
    yarn_issue_id, _ = _parse_action_payload(payload)
    user_id = int(token_data.get("user_id"))
    try:
        doc = _fetch_doc(db, yarn_issue_id)
        status_id = int(doc["status_id"])
        if status_id != YARN_DOC_STATUS_DRAFT:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot cancel {DOC_NAME} with status_id {status_id}. Expected status 21 (Drafted).",
            )
        _set_status(db, yarn_issue_id, YARN_DOC_STATUS_CANCELLED, None, user_id)
        db.commit()
        return {
            "data": {"yarn_issue_id": yarn_issue_id, "status_id": YARN_DOC_STATUS_CANCELLED},
            "status": "success",
            "new_status_id": YARN_DOC_STATUS_CANCELLED,
            "message": "Draft cancelled successfully.",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error cancelling yarn issue %s", yarn_issue_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/send_for_approval")
def send_yarn_issue_for_approval(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """1 -> 20 at approval level 1. Refused when the document has no active
    line or any line has weight_kg <= 0."""
    yarn_issue_id, _ = _parse_action_payload(payload)
    user_id = int(token_data.get("user_id"))
    try:
        doc = _fetch_doc(db, yarn_issue_id)
        status_id = int(doc["status_id"])
        if status_id != YARN_DOC_STATUS_OPEN:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot send {DOC_NAME} with status_id {status_id} for approval. Expected status 1 (Open).",
            )
        totals_row = db.execute(get_yarn_issue_line_totals_query(), {"yarn_issue_id": yarn_issue_id}).fetchone()
        totals = dict(totals_row._mapping) if totals_row else {}
        line_count = int(totals.get("line_count") or 0)
        if line_count == 0:
            raise HTTPException(status_code=400, detail=f"{DOC_NAME} has no lines; add at least one line before sending for approval")
        if _f(totals.get("min_kg")) <= 0:
            raise HTTPException(status_code=400, detail="Every line must have weight_kg greater than 0 before sending for approval")

        _set_status(db, yarn_issue_id, YARN_DOC_STATUS_PENDING, 1, user_id)
        db.commit()
        return {
            "data": {"yarn_issue_id": yarn_issue_id, "status_id": YARN_DOC_STATUS_PENDING, "approval_level": 1},
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
        logger.exception("Error sending yarn issue %s for approval", yarn_issue_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/approve")
def approve_yarn_issue(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Level-based approval through the shared engine: 20 -> 20 (next level)
    or 20 -> 3. Quantity-only document, so no value checks."""
    yarn_issue_id, ids = _parse_action_payload(payload)
    user_id = int(token_data.get("user_id"))
    try:
        result = process_approval(
            doc_id=yarn_issue_id,
            user_id=user_id,
            menu_id=ids["menu_id"],
            db=db,
            get_doc_fn=get_yarn_issue_with_approval_info,
            update_status_fn=update_yarn_issue_status,
            id_param_name="yarn_issue_id",
            doc_name=DOC_NAME,
            extra_update_params={"issue_no": None},
        )
        result["data"] = {
            "yarn_issue_id": yarn_issue_id,
            "status_id": result.get("new_status_id"),
            "approval_level": result.get("new_approval_level"),
        }
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error approving yarn issue %s", yarn_issue_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reject")
def reject_yarn_issue(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """20 -> 4 through the shared engine. Body: yarn_issue_id, co_id,
    menu_id, reason."""
    yarn_issue_id, ids = _parse_action_payload(payload, id_fields=("co_id",))
    menu_id = _optional_int(payload, "menu_id")
    reason = _optional_text(payload, "reason", 1000)
    user_id = int(token_data.get("user_id"))
    try:
        result = process_rejection(
            doc_id=yarn_issue_id,
            user_id=user_id,
            menu_id=menu_id,
            db=db,
            get_doc_fn=get_yarn_issue_with_approval_info,
            update_status_fn=update_yarn_issue_status,
            id_param_name="yarn_issue_id",
            doc_name=DOC_NAME,
            reason=reason,
            extra_update_params={"issue_no": None},
        )
        result["data"] = {
            "yarn_issue_id": yarn_issue_id,
            "status_id": YARN_DOC_STATUS_REJECTED,
            "approval_level": None,
        }
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error rejecting yarn issue %s", yarn_issue_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reopen")
def reopen_yarn_issue(
    payload: dict,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """6 -> 21 (cancelled back to draft), 4 -> 1 (rejected back to open);
    approval_level cleared. issue_no is kept."""
    yarn_issue_id, _ = _parse_action_payload(payload)
    user_id = int(token_data.get("user_id"))
    try:
        doc = _fetch_doc(db, yarn_issue_id)
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
        _set_status(db, yarn_issue_id, new_status_id, None, user_id)
        db.commit()
        return {
            "data": {"yarn_issue_id": yarn_issue_id, "status_id": new_status_id, "approval_level": None},
            "status": "success",
            "new_status_id": new_status_id,
            "message": f"{DOC_NAME} reopened successfully (status: {new_status_id}).",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error reopening yarn issue %s", yarn_issue_id)
        raise HTTPException(status_code=500, detail=str(e))

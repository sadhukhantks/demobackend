from fastapi import Depends, Request, HTTPException, APIRouter
from pydantic import BaseModel
from typing import List, Optional
from datetime import date, datetime
from src.common.utils import now_ist, parse_json_body
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProcurement.query import (
    get_jute_po_table_query,
    get_jute_po_table_count_query,
    get_jute_po_by_id_query,
    get_jute_po_line_items_query,
    get_mukam_list_query,
    get_vehicle_types_query,
    get_jute_items_query,
    get_jute_qualities_by_item_query,
    get_suppliers_by_mukam_query,
    get_parties_by_supplier_query,
    get_branches_query,
    get_all_suppliers_query,
    get_jute_po_with_approval_info,
    update_jute_po_status,
)
from src.common.approval_utils import process_approval, process_rejection
from src.juteProcurement.constants import JUTE_PO_CLOSED_STATUS
from src.juteProcurement.formatters import format_jute_po_number
from src.juteProcurement.po_numbering import (
    PoNumberLock,
    commit_with_po_no,
    po_number_key_changed,
)
from src.juteProcurement.po_settlement import (
    DEFAULT_PO_MENU_PATH,
    edit_access_check,
    CLOSE_TYPE_BULK,
    CLOSE_TYPE_SHORT,
    MISSING_SCHEMA_MSG,
    REMARK_MAX_LEN,
    SOURCE_BULK,
    SOURCE_MANUAL,
    STALE_DAYS_MAX,
    STALE_DAYS_MIN,
    TOLERANCE_FRACTION_MAX,
    TOLERANCE_FRACTION_MIN,
    SettleRule,
    get_po_close_info,
    get_status_log,
    is_missing_settlement_schema,
    load_rule,
    manual_close_po,
    recompute_branch,
    reopen_closed_po,
    save_rule,
)
from src.juteProcurement._excel_utils import (
    parse_iso_date,
    build_excel_response,
    enforce_row_cap,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# WEIGHT CALCULATION CONSTANTS
# =============================================================================

# Weight per bale in kg
WEIGHT_PER_BALE_KG = 150
# Weight per loose unit in kg
WEIGHT_PER_LOOSE_KG = 48
# Allowed variance percentage for vehicle weight validation
VEHICLE_WEIGHT_TOLERANCE_PERCENT = 5


def calculate_line_item_weight(quantity: float, jute_unit: str) -> float:
    """
    Calculate weight for a line item based on unit type.
    
    Args:
        quantity: Number of bales or loose units
        jute_unit: "BALE" or "LOOSE"
    
    Returns:
        Weight in kg
    """
    if jute_unit == "BALE":
        return WEIGHT_PER_BALE_KG * quantity
    else:
        # LOOSE
        return WEIGHT_PER_LOOSE_KG * quantity


def derive_from_percentage(
    percentage: float,
    vehicle_capacity_qtl: float,
    vehicle_quantity: int,
    jute_unit: str,
) -> tuple[float, float]:
    """
    From a line-item percentage and header-level vehicle/unit context, derive
    (weight_kg, quantity_units) for DB persistence. quantity_units is rounded
    to the nearest whole number of bales/drums.
    """
    total_vehicle_weight_kg = vehicle_capacity_qtl * vehicle_quantity * 100  # qtl -> kg
    weight_kg = (percentage / 100.0) * total_vehicle_weight_kg
    weight_per_unit = WEIGHT_PER_BALE_KG if jute_unit == "BALE" else WEIGHT_PER_LOOSE_KG
    raw_qty = weight_kg / weight_per_unit if weight_per_unit else 0.0
    quantity_units = float(round(raw_qty))
    return weight_kg, quantity_units


def validate_percentage_sum(line_items: list, epsilon: float = 0.01) -> None:
    """
    Raise HTTPException(400) unless all items have percentage set and the sum is 100 (+/- epsilon).
    """
    total = 0.0
    for li in line_items:
        if li.percentage is None:
            raise HTTPException(
                status_code=400,
                detail="All line items must have a percentage when using percentage-based entry",
            )
        total += float(li.percentage)
    if abs(total - 100.0) > epsilon:
        raise HTTPException(
            status_code=400,
            detail=f"Line item percentages must sum to 100 (got {total:.2f})",
        )


def validate_vehicle_weight(
    total_weight_kg: float,
    vehicle_capacity_qtl: float,
    vehicle_quantity: int
) -> tuple[bool, str]:
    """
    Validate that total line item weight is within ±5% of vehicle capacity.
    
    Args:
        total_weight_kg: Total weight of all line items in kg
        vehicle_capacity_qtl: Weight capacity of one vehicle in quintals
        vehicle_quantity: Number of vehicles
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    # Convert total weight to quintals for comparison
    total_weight_qtl = total_weight_kg / 100
    expected_vehicle_weight_qtl = vehicle_capacity_qtl * vehicle_quantity
    
    if expected_vehicle_weight_qtl <= 0:
        return False, "Vehicle capacity not configured"
    
    if total_weight_qtl <= 0:
        return False, "No line items with weight"
    
    variance = total_weight_qtl - expected_vehicle_weight_qtl
    variance_percent = (variance / expected_vehicle_weight_qtl) * 100
    abs_variance_percent = abs(variance_percent)
    
    if abs_variance_percent > VEHICLE_WEIGHT_TOLERANCE_PERCENT:
        direction = "exceeds" if variance_percent > 0 else "is below"
        return False, (
            f"Total weight ({total_weight_qtl:.2f} Qtl) {direction} "
            f"vehicle capacity ({expected_vehicle_weight_qtl:.2f} Qtl) by {abs_variance_percent:.1f}%. "
            f"Must be within ±{VEHICLE_WEIGHT_TOLERANCE_PERCENT}%."
        )
    
    return True, ""


JUTE_PO_DOWNLOAD_COLUMNS = [
    ("PO No", "po_num"),
    ("PO Date", "po_date"),
    ("Branch", "branch_id"),
    ("Supplier", "supplier_name"),
    ("Party", "party_name"),
    ("Mukam", "mukam"),
    ("Vehicle Type", "vehicle_type"),
    ("Vehicle Qty", "vehicle_qty"),
    ("Lorries Received", "received_lorries"),
    ("Lorries Pending", "pending_lorries"),
    ("Qty Received (Qtl)", "received_qty_qtl"),
    ("Qty Pending (Qtl)", "pending_qty_qtl"),
    ("Weight", "weight"),
    ("PO Value", "po_val_wt_tax"),
    ("Status", "status"),
]


def _build_jute_po_params(
    co_id: int,
    q_search: str,
    date_from,
    date_to,
    status_id: int = None,
):
    """Shared bind-dict builder for jute PO list/count/download queries."""
    search_param = f"%{q_search}%" if q_search else None
    base = {
        "co_id": co_id,
        "date_from": date_from,
        "date_to": date_to,
        "status_id": status_id,
    }
    if search_param:
        base["search"] = search_param
    return base, search_param


@router.get("/get_jute_po_table")
def get_jute_po_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get paginated list of Jute Purchase Orders.

    Query params:
    - co_id: Company ID (required)
    - page: Page number (default: 1)
    - limit: Records per page (default: 10)
    - search: Search term for po_num, supplier_name, broker_name, or mukam
    - from_date / to_date: Optional ISO dates (YYYY-MM-DD) bounding po_date
    """
    try:
        q_co_id = request.query_params.get("co_id")
        q_page = request.query_params.get("page", "1")
        q_limit = request.query_params.get("limit", "10")
        q_search = request.query_params.get("search", "").strip()
        q_from_date = (request.query_params.get("from_date") or "").strip()
        q_to_date = (request.query_params.get("to_date") or "").strip()

        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        try:
            co_id = int(q_co_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid co_id")

        date_from = parse_iso_date(q_from_date, "from_date")
        date_to = parse_iso_date(q_to_date, "to_date")

        try:
            page = max(1, int(q_page))
            limit = min(100, max(1, int(q_limit)))
        except ValueError:
            page = 1
            limit = 10

        offset = (page - 1) * limit

        q_status_id = (request.query_params.get("status_id") or "").strip()
        try:
            status_id = int(q_status_id) if q_status_id else None
        except ValueError:
            status_id = None

        base_params, _ = _build_jute_po_params(co_id, q_search, date_from, date_to, status_id)

        count_query = get_jute_po_table_count_query(co_id=co_id, search=q_search if q_search else None)
        count_result = db.execute(count_query, base_params).fetchone()
        total = count_result.total if count_result else 0

        data_query = get_jute_po_table_query(co_id=co_id, search=q_search if q_search else None)
        data_params = {**base_params, "limit": limit, "offset": offset}

        result = db.execute(data_query, data_params).fetchall()
        rows = [dict(r._mapping) for r in result]

        for row in rows:
            if row.get("po_date"):
                row["po_date"] = str(row["po_date"])
            if row.get("updated_date_time"):
                row["updated_date_time"] = str(row["updated_date_time"])
            row["received_lorries"] = int(row.get("received_lorries") or 0)
            row["pending_lorries"] = int(row.get("pending_lorries") or 0)
            row["received_qty_qtl"] = float(row.get("received_qty_qtl") or 0)
            row["pending_qty_qtl"] = float(row.get("pending_qty_qtl") or 0)

        return {
            "data": rows,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit if limit > 0 else 0,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching Jute PO table")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download_po_table")
def download_jute_po_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Download the Jute PO list as an .xlsx workbook with same filters as /get_jute_po_table."""
    try:
        q_co_id = request.query_params.get("co_id")
        q_search = request.query_params.get("search", "").strip()
        q_from_date = (request.query_params.get("from_date") or "").strip()
        q_to_date = (request.query_params.get("to_date") or "").strip()

        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        try:
            co_id = int(q_co_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid co_id")

        date_from = parse_iso_date(q_from_date, "from_date")
        date_to = parse_iso_date(q_to_date, "to_date")

        q_status_id = (request.query_params.get("status_id") or "").strip()
        try:
            status_id = int(q_status_id) if q_status_id else None
        except ValueError:
            status_id = None

        base_params, _ = _build_jute_po_params(co_id, q_search, date_from, date_to, status_id)

        count_query = get_jute_po_table_count_query(co_id=co_id, search=q_search if q_search else None)
        count_result = db.execute(count_query, base_params).fetchone()
        total = int(count_result.total) if count_result and count_result.total else 0
        enforce_row_cap(total)

        rows = []
        if total > 0:
            data_query = get_jute_po_table_query(co_id=co_id, search=q_search if q_search else None)
            data_params = {**base_params, "limit": total, "offset": 0}
            result = db.execute(data_query, data_params).fetchall()
            rows = [dict(r._mapping) for r in result]

        return build_excel_response(
            rows=rows,
            columns=JUTE_PO_DOWNLOAD_COLUMNS,
            sheet_title="Jute PO",
            filename_prefix="jute_po",
            from_date_str=q_from_date,
            to_date_str=q_to_date,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error downloading Jute PO table")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_jute_po_by_id/{jute_po_id}")
def get_jute_po_by_id(
    jute_po_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get a single Jute PO by ID.
    
    Path params:
    - jute_po_id: Jute PO ID
    
    Query params:
    - co_id: Company ID (required)
    """
    try:
        q_co_id = request.query_params.get("co_id")

        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        try:
            co_id = int(q_co_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid co_id")

        query = get_jute_po_by_id_query()
        result = db.execute(query, {"jute_po_id": jute_po_id, "co_id": co_id}).fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Jute PO not found")

        row = dict(result._mapping)

        # Closed panel (po-settlement-spec.md section 7): closed_date, closed_by,
        # closed_by_name, close_type, close_remark — None on an un-migrated tenant.
        row.update(get_po_close_info(db, jute_po_id))

        # Format dates for JSON serialization
        if row.get("po_date"):
            row["po_date"] = str(row["po_date"])
        if row.get("updated_date_time"):
            row["updated_date_time"] = str(row["updated_date_time"])
        if row.get("closed_date"):
            row["closed_date"] = str(row["closed_date"])
        if row.get("mod_on"):
            row["mod_on"] = str(row["mod_on"])
        if row.get("contract_date"):
            row["contract_date"] = str(row["contract_date"])

        # Fetch line items and include in response
        line_items_query = get_jute_po_line_items_query()
        line_items_result = db.execute(line_items_query, {"jute_po_id": jute_po_id}).fetchall()
        row["line_items"] = [dict(r._mapping) for r in line_items_result]

        return row

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching Jute PO by ID")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_jute_po_line_items/{jute_po_id}")
def get_jute_po_line_items(
    jute_po_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Get line items for a Jute PO."""
    try:
        query = get_jute_po_line_items_query()
        result = db.execute(query, {"jute_po_id": jute_po_id}).fetchall()
        rows = [dict(r._mapping) for r in result]
        return {"line_items": rows}
    except Exception as e:
        logger.exception("Error fetching Jute PO line items")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/jute_po_create_setup")
def jute_po_create_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get setup data for creating a Jute PO.
    Returns branches, mukams, vehicle types, and jute items.
    """
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        
        try:
            co_id = int(q_co_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid co_id")

        # Get branches
        branches_result = db.execute(get_branches_query(), {"co_id": co_id}).fetchall()
        branches = [dict(r._mapping) for r in branches_result]

        # Get mukams
        mukams_result = db.execute(get_mukam_list_query(), {"co_id": co_id}).fetchall()
        mukams = [dict(r._mapping) for r in mukams_result]

        # Get vehicle types
        vehicle_types_result = db.execute(get_vehicle_types_query(), {"co_id": co_id}).fetchall()
        vehicle_types = [dict(r._mapping) for r in vehicle_types_result]

        # Get jute groups: raw-jute (type 2) subgroups AND jute-waste (type 3) roots
        jute_groups_result = db.execute(get_jute_items_query(), {"co_id": co_id}).fetchall()
        jute_groups = [dict(r._mapping) for r in jute_groups_result]

        # Get all jute suppliers for the company (mandatory field on PO)
        suppliers_result = db.execute(get_all_suppliers_query(), {"co_id": co_id}).fetchall()
        suppliers = [dict(r._mapping) for r in suppliers_result]

        # Static options
        channel_options = [
            {"value": "DOMESTIC", "label": "Domestic"},
            {"value": "IMPORT", "label": "Import"},
            {"value": "JCI", "label": "JCI"},
            {"value": "PTF", "label": "PTF"},
        ]

        unit_options = [
            {"value": "LOOSE", "label": "Loose"},
            {"value": "BALE", "label": "Bale"},
        ]

        # Generate crop year options (current year -1 to +2)
        current_year = now_ist().year % 100  # Get last 2 digits
        crop_year_options = []
        for i in range(-1, 3):
            start_year = current_year + i - 1
            end_year = current_year + i
            crop_year_options.append({
                "value": f"{start_year:02d}-{end_year:02d}",
                "label": f"{start_year:02d}-{end_year:02d}",
            })

        return {
            "branches": branches,
            "mukams": mukams,
            "vehicle_types": vehicle_types,
            "jute_groups": jute_groups,
            "suppliers": suppliers,
            "channel_options": channel_options,
            "unit_options": unit_options,
            "crop_year_options": crop_year_options,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching Jute PO create setup")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_suppliers_by_mukam/{mukam_id}")
def get_suppliers_by_mukam(
    mukam_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Get jute suppliers filtered by mukam."""
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        
        co_id = int(q_co_id)
        
        query = get_suppliers_by_mukam_query()
        result = db.execute(query, {"mukam_id": mukam_id, "co_id": co_id}).fetchall()
        suppliers = [dict(r._mapping) for r in result]
        
        return {"suppliers": suppliers}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching suppliers by mukam")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_parties_by_supplier/{supplier_id}")
def get_parties_by_supplier(
    supplier_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Get parties mapped to a jute supplier, scoped to the caller's company."""
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        try:
            co_id = int(q_co_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid co_id")

        query = get_parties_by_supplier_query()
        result = db.execute(
            query, {"supplier_id": supplier_id, "co_id": co_id}
        ).fetchall()
        parties = [dict(r._mapping) for r in result]

        return {"parties": parties}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching parties by supplier")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_qualities_by_item/{item_grp_id}")
def get_qualities_by_item(
    item_grp_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Get items (formerly qualities) for a specific jute subgroup."""
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        
        co_id = int(q_co_id)
        
        query = get_jute_qualities_by_item_query()
        result = db.execute(query, {"item_grp_id": item_grp_id}).fetchall()
        qualities = [dict(r._mapping) for r in result]
        
        return {"qualities": qualities}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching qualities by item")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# JUTE PO CREATE / UPDATE MODELS
# =============================================================================

class JutePOLineItemCreate(BaseModel):
    """Schema for a Jute PO line item."""
    item_id: Optional[int] = None
    crop_year: Optional[str] = None
    marka: Optional[str] = None
    quantity: Optional[float] = None
    percentage: Optional[float] = None
    rate: float
    allowable_moisture: Optional[float] = None
    jute_unit: str = "BALE"  # "LOOSE" or "BALE"


class JutePOCreate(BaseModel):
    """Schema for creating a Jute PO."""
    co_id: int
    branch_id: int
    po_date: date
    mukam_id: int
    jute_unit: str = "BALE"  # Header-level unit used for all line item weight calculations
    supplier_id: int
    party_id: Optional[int] = None
    vehicle_type_id: int
    vehicle_quantity: int
    channel_code: str
    credit_term: Optional[int] = None
    delivery_timeline: Optional[int] = None
    freight_charge: Optional[float] = None
    remarks: Optional[str] = None
    line_items: List[JutePOLineItemCreate]


class JutePOUpdate(BaseModel):
    """Schema for updating a Jute PO."""
    branch_id: Optional[int] = None
    po_date: Optional[date] = None
    mukam_id: Optional[int] = None
    jute_unit: Optional[str] = None
    supplier_id: Optional[int] = None
    party_id: Optional[int] = None
    vehicle_type_id: Optional[int] = None
    vehicle_quantity: Optional[int] = None
    channel_code: Optional[str] = None
    credit_term: Optional[int] = None
    delivery_timeline: Optional[int] = None
    freight_charge: Optional[float] = None
    remarks: Optional[str] = None
    line_items: Optional[List[JutePOLineItemCreate]] = None


# =============================================================================
# JUTE PO CREATE / UPDATE ENDPOINTS
# =============================================================================


def _validate_branch_belongs_to_co(db: Session, branch_id: int, co_id: int) -> None:
    """Raise 400 unless branch_id belongs to co_id in branch_mst."""
    row = db.execute(
        text("SELECT co_id FROM branch_mst WHERE branch_id = :branch_id"),
        {"branch_id": branch_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="Invalid branch_id")
    if row.co_id != co_id:
        raise HTTPException(
            status_code=400,
            detail="branch_id does not belong to the specified company",
        )


def _validate_supplier_party_for_co(
    db: Session, supplier_id: int, party_id: Optional[int], co_id: int
) -> None:
    """Raise 400 unless (supplier_id, party_id) is mapped for this co_id.

    If party_id is None, only verifies the supplier has at least one mapping
    for this company (so it's a usable supplier in this company's PO at all).
    """
    if party_id is None:
        row = db.execute(
            text(
                """
                SELECT 1 FROM jute_supp_party_map
                WHERE jute_supplier_id = :supplier_id AND co_id = :co_id
                LIMIT 1
                """
            ),
            {"supplier_id": supplier_id, "co_id": co_id},
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=400,
                detail="supplier_id is not available for this company",
            )
        return

    row = db.execute(
        text(
            """
            SELECT 1 FROM jute_supp_party_map
            WHERE jute_supplier_id = :supplier_id
              AND party_id = :party_id
              AND co_id = :co_id
            LIMIT 1
            """
        ),
        {"supplier_id": supplier_id, "party_id": party_id, "co_id": co_id},
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=400,
            detail="party_id is not mapped to this supplier for the specified company",
        )


@router.post("/jute_po_create")
def jute_po_create(
    payload: JutePOCreate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create a new Jute PO."""
    try:
        from src.models.jute import JutePo, JutePoLi

        user_id = token_data.get("user_id")

        # Defense in depth: enforce company scoping at the persistence layer.
        # The dropdown queries are now co_id-filtered, but a stale client cache
        # or a direct API caller could still POST a cross-company party_id.
        _validate_branch_belongs_to_co(db, payload.branch_id, payload.co_id)
        _validate_supplier_party_for_co(
            db, payload.supplier_id, payload.party_id, payload.co_id
        )

        # Get vehicle weight for validation
        vehicle_result = db.execute(
            text("SELECT weight FROM jute_lorry_mst WHERE jute_lorry_type_id = :vehicle_type_id"),
            {"vehicle_type_id": payload.vehicle_type_id}
        ).fetchone()
        vehicle_capacity_qtl = vehicle_result.weight if vehicle_result else 0
        
        # Calculate total weight and value from line items FIRST for validation
        total_weight_kg = 0.0
        total_value = 0.0
        line_items_data = []

        # Use header-level jute_unit for all line items
        header_jute_unit = payload.jute_unit or "BALE"

        # Detect percentage-based path: if any line has percentage set, validate and use it
        uses_percentage = any(li.percentage is not None for li in payload.line_items)

        if uses_percentage:
            validate_percentage_sum(payload.line_items)
            for li in payload.line_items:
                weight_kg, derived_qty = derive_from_percentage(
                    float(li.percentage),
                    vehicle_capacity_qtl,
                    payload.vehicle_quantity,
                    header_jute_unit,
                )
                weight_in_quintals = weight_kg / 100
                amount = weight_in_quintals * li.rate
                total_weight_kg += weight_kg
                total_value += amount

                line_items_data.append({
                    "li": li,
                    "weight_kg": weight_kg,
                    "amount": amount,
                    "derived_quantity": derived_qty,
                })
        else:
            for li in payload.line_items:
                # Calculate weight based on header's unit type (fixed weights)
                weight_kg = calculate_line_item_weight(li.quantity, header_jute_unit)

                # Rate is per quintal (100 kg), so convert weight to quintals
                weight_in_quintals = weight_kg / 100
                amount = weight_in_quintals * li.rate
                total_weight_kg += weight_kg
                total_value += amount

                line_items_data.append({
                    "li": li,
                    "weight_kg": weight_kg,
                    "amount": amount,
                    "derived_quantity": li.quantity,
                })

            # Validate vehicle weight tolerance (legacy path only; % path is tight by construction)
            is_valid, error_message = validate_vehicle_weight(
                total_weight_kg, vehicle_capacity_qtl, payload.vehicle_quantity
            )
            if not is_valid:
                raise HTTPException(status_code=400, detail=error_message)
        
        # PO number: MAX(po_no) + 1 per branch within the April-March financial
        # year of po_date, read and inserted under the branch's numbering lock
        # (po_numbering.py: the transfer app takes the same lock). The unique key
        # (branch_id, fy_start_year, po_no) is the backstop for writers that skip
        # the lock: on its duplicate error we take the next number once more.
        def _build_header_and_lines(next_po_no: int) -> JutePo:
            jute_po = JutePo(
                branch_id=payload.branch_id,
                po_no=next_po_no,
                po_date=payload.po_date,
                jute_mukam_id=payload.mukam_id,
                jute_uom=header_jute_unit,
                supplier_id=payload.supplier_id,
                party_id=payload.party_id,
                vehicle_type_id=payload.vehicle_type_id,
                vehicle_quantity=payload.vehicle_quantity,
                channel_code=payload.channel_code,
                credit_term=payload.credit_term,
                delivery_days=payload.delivery_timeline,
                frieght_charge=payload.freight_charge,
                remarks=payload.remarks,
                weight=total_weight_kg,
                jute_po_value=total_value,
                status_id=21,  # Draft status
                updated_by=user_id,
            )
            db.add(jute_po)
            db.flush()  # Get the jute_po_id (and the duplicate-key error, if any)

            for item_data in line_items_data:
                li = item_data["li"]
                line_item = JutePoLi(
                    jute_po_id=jute_po.jute_po_id,
                    item_id=li.item_id,
                    crop_year=int(li.crop_year.split("-")[0]) if li.crop_year else None,
                    marka=li.marka,
                    quantity=item_data["derived_quantity"],
                    percentage=li.percentage,
                    rate=li.rate,
                    allowable_moisture=li.allowable_moisture,
                    jute_uom=header_jute_unit,
                    value=item_data["amount"],
                    status_id=21,  # Draft status (same as header)
                )
                db.add(line_item)
            return jute_po

        with PoNumberLock(db, payload.branch_id) as lock:
            jute_po, next_po_no = commit_with_po_no(
                db, lock, payload.po_date, _build_header_and_lines
            )

        # Fetch company and branch prefix for formatted PO number
        prefix_result = db.execute(
            text("""
                SELECT cm.co_prefix, bm.branch_prefix
                FROM branch_mst bm
                INNER JOIN co_mst cm ON cm.co_id = bm.co_id
                WHERE bm.branch_id = :branch_id
            """),
            {"branch_id": payload.branch_id}
        ).fetchone()
        
        co_prefix = prefix_result.co_prefix if prefix_result else None
        branch_prefix = prefix_result.branch_prefix if prefix_result else None
        
        # Generate formatted po_num string for response
        po_num = format_jute_po_number(next_po_no, payload.po_date, co_prefix, branch_prefix)
        
        return {
            "success": True,
            "jute_po_id": jute_po.jute_po_id,
            "po_num": po_num,
            "message": "Jute PO created successfully",
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error creating Jute PO")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/jute_po_update/{jute_po_id}")
def jute_po_update(
    jute_po_id: int,
    payload: JutePOUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Update an existing Jute PO."""
    try:
        from src.models.jute import JutePo, JutePoLi
        
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        co_id = int(q_co_id)
        
        user_id = token_data.get("user_id")
        
        # Get existing PO - verify it belongs to a branch under this co_id
        # First get valid branch IDs for this company
        valid_branches_result = db.execute(
            text("SELECT branch_id FROM branch_mst WHERE co_id = :co_id"),
            {"co_id": co_id}
        ).fetchall()
        valid_branch_ids = [r.branch_id for r in valid_branches_result]
        
        if not valid_branch_ids:
            raise HTTPException(status_code=404, detail="No branches found for this company")
        
        jute_po = db.query(JutePo).filter(
            JutePo.jute_po_id == jute_po_id,
            JutePo.branch_id.in_(valid_branch_ids)
        ).first()
        
        if not jute_po:
            raise HTTPException(status_code=404, detail="Jute PO not found")
        
        # Check if PO is editable (Draft or Open status)
        if jute_po.status_id not in [21, 1]:
            raise HTTPException(status_code=400, detail="Jute PO cannot be edited in current status")
        
        # Defense in depth: re-validate branch + supplier/party against co_id
        # using the effective values that will be saved (payload overrides
        # existing). Prevents a stale client or direct API call from
        # introducing a cross-company party_id on update.
        effective_branch_id = (
            payload.branch_id if payload.branch_id is not None else jute_po.branch_id
        )
        effective_supplier_id = (
            payload.supplier_id
            if payload.supplier_id is not None
            else jute_po.supplier_id
        )
        # party_id is nullable; treat "not in payload" as "keep existing".
        # Pydantic doesn't distinguish missing vs explicit-None here, so we
        # only validate when supplier or party is being touched.
        effective_party_id = (
            payload.party_id if payload.party_id is not None else jute_po.party_id
        )
        if effective_branch_id is not None:
            _validate_branch_belongs_to_co(db, effective_branch_id, co_id)
        if effective_supplier_id is not None:
            _validate_supplier_party_for_co(
                db, effective_supplier_id, effective_party_id, co_id
            )

        # The number belongs to the (branch, financial year) sequence it was
        # drawn from. An edit that moves the PO to another branch or across the
        # March/April boundary (po_date is editable while Draft/Open) must draw
        # a fresh number there -- keeping the old one would print a number that
        # another PO of that FY already carries (and the unique key would
        # reject the UPDATE). Allocated under the same lock as on create.
        effective_po_date = (
            payload.po_date if payload.po_date is not None else jute_po.po_date
        )
        renumber = effective_po_date is not None and po_number_key_changed(
            jute_po.branch_id, jute_po.po_date, effective_branch_id, effective_po_date
        )

        def _apply_update(next_po_no: Optional[int]) -> None:
            """Stage every change of this update on the session. Re-runnable:
            after a rollback the PO reloads its stored values and the payload is
            applied again."""
            if next_po_no is not None:
                jute_po.po_no = next_po_no

            # Update header fields
            if payload.branch_id is not None:
                jute_po.branch_id = payload.branch_id
            if payload.po_date is not None:
                jute_po.po_date = payload.po_date
            if payload.mukam_id is not None:
                jute_po.jute_mukam_id = payload.mukam_id
            if payload.jute_unit is not None:
                jute_po.jute_uom = payload.jute_unit
            if payload.supplier_id is not None:
                jute_po.supplier_id = payload.supplier_id
            if payload.party_id is not None:
                jute_po.party_id = payload.party_id
            if payload.vehicle_type_id is not None:
                jute_po.vehicle_type_id = payload.vehicle_type_id
            if payload.vehicle_quantity is not None:
                jute_po.vehicle_quantity = payload.vehicle_quantity
            if payload.channel_code is not None:
                jute_po.channel_code = payload.channel_code
            if payload.credit_term is not None:
                jute_po.credit_term = payload.credit_term
            if payload.delivery_timeline is not None:
                jute_po.delivery_days = payload.delivery_timeline
            if payload.freight_charge is not None:
                jute_po.frieght_charge = payload.freight_charge
            if payload.remarks is not None:
                jute_po.remarks = payload.remarks

            jute_po.updated_by = user_id
            jute_po.updated_date_time = now_ist()

            # Update line items if provided
            if payload.line_items is None:
                return

            # Get vehicle weight for validation
            vehicle_result = db.execute(
                text("SELECT weight FROM jute_lorry_mst WHERE jute_lorry_type_id = :vehicle_type_id"),
                {"vehicle_type_id": jute_po.vehicle_type_id}
            ).fetchone()
            vehicle_capacity_qtl = vehicle_result.weight if vehicle_result else 0

            # Use header-level jute_unit for all line items
            # Prefer payload value, fall back to existing PO value, then default
            header_jute_unit = payload.jute_unit or jute_po.jute_uom or "BALE"

            # Calculate total weight and value FIRST for validation
            total_weight_kg = 0.0
            total_value = 0.0
            line_items_data = []

            # Detect percentage-based path: if any line has percentage set, validate and use it
            uses_percentage = any(li.percentage is not None for li in payload.line_items)

            if uses_percentage:
                validate_percentage_sum(payload.line_items)
                for li in payload.line_items:
                    weight_kg, derived_qty = derive_from_percentage(
                        float(li.percentage),
                        vehicle_capacity_qtl,
                        jute_po.vehicle_quantity or 0,
                        header_jute_unit,
                    )
                    weight_in_quintals = weight_kg / 100
                    amount = weight_in_quintals * li.rate
                    total_weight_kg += weight_kg
                    total_value += amount

                    line_items_data.append({
                        "li": li,
                        "weight_kg": weight_kg,
                        "amount": amount,
                        "derived_quantity": derived_qty,
                    })
            else:
                for li in payload.line_items:
                    # Calculate weight based on header's unit type (fixed weights)
                    weight_kg = calculate_line_item_weight(li.quantity, header_jute_unit)

                    # Rate is per quintal (100 kg), so convert weight to quintals
                    weight_in_quintals = weight_kg / 100
                    amount = weight_in_quintals * li.rate
                    total_weight_kg += weight_kg
                    total_value += amount

                    line_items_data.append({
                        "li": li,
                        "weight_kg": weight_kg,
                        "amount": amount,
                        "derived_quantity": li.quantity,
                    })

                # Validate vehicle weight tolerance (legacy path only; % path is tight by construction)
                is_valid, error_message = validate_vehicle_weight(
                    total_weight_kg, vehicle_capacity_qtl, jute_po.vehicle_quantity or 0
                )
                if not is_valid:
                    raise HTTPException(status_code=400, detail=error_message)

            # Delete existing line items (triggers will log the deletions)
            db.query(JutePoLi).filter(JutePoLi.jute_po_id == jute_po_id).delete()

            # Create new line items
            for item_data in line_items_data:
                li = item_data["li"]
                line_item = JutePoLi(
                    jute_po_id=jute_po_id,
                    item_id=li.item_id,
                    crop_year=int(li.crop_year.split("-")[0]) if li.crop_year else None,
                    marka=li.marka,
                    quantity=item_data["derived_quantity"],
                    percentage=li.percentage,
                    rate=li.rate,
                    allowable_moisture=li.allowable_moisture,
                    jute_uom=header_jute_unit,
                    value=item_data["amount"],
                    status_id=jute_po.status_id,  # Same status as header
                )
                db.add(line_item)

            # Update totals
            jute_po.weight = total_weight_kg
            jute_po.jute_po_value = total_value

        if not renumber:
            _apply_update(None)
            db.commit()
            return {
                "success": True,
                "jute_po_id": jute_po_id,
                "message": "Jute PO updated successfully",
            }

        with PoNumberLock(db, effective_branch_id) as lock:
            _, new_po_no = commit_with_po_no(db, lock, effective_po_date, _apply_update)

        prefix_result = db.execute(
            text("""
                SELECT cm.co_prefix, bm.branch_prefix
                FROM branch_mst bm
                INNER JOIN co_mst cm ON cm.co_id = bm.co_id
                WHERE bm.branch_id = :branch_id
            """),
            {"branch_id": effective_branch_id}
        ).fetchone()
        po_num = format_jute_po_number(
            new_po_no,
            effective_po_date,
            prefix_result.co_prefix if prefix_result else None,
            prefix_result.branch_prefix if prefix_result else None,
        )
        return {
            "success": True,
            "jute_po_id": jute_po_id,
            "po_num": po_num,
            "po_no": new_po_no,
            "renumbered": True,
            "message": (
                f"Jute PO updated successfully. It moved to another financial "
                f"year/branch sequence and is now {po_num}"
            ),
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error updating Jute PO")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# JUTE PO APPROVAL SCHEMAS
# =============================================================================

class ApprovePayload(BaseModel):
    menu_id: Optional[int] = None  # For multi-level approval via shared utility


class RejectPayload(BaseModel):
    reason: Optional[str] = None
    menu_id: Optional[int] = None  # For multi-level rejection via shared utility


# =============================================================================
# JUTE PO APPROVAL ENDPOINTS
# =============================================================================

@router.post("/open_jute_po/{jute_po_id}")
def open_jute_po(
    jute_po_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Open a Jute PO (change status from Draft to Open)."""
    try:
        from src.models.jute import JutePo
        from src.models.mst import BranchMst
        
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        co_id = int(q_co_id)
        
        # Join with branch_mst to verify co_id since JutePo doesn't have co_id column
        jute_po = db.query(JutePo).join(
            BranchMst, JutePo.branch_id == BranchMst.branch_id
        ).filter(
            JutePo.jute_po_id == jute_po_id,
            BranchMst.co_id == co_id
        ).first()
        
        if not jute_po:
            raise HTTPException(status_code=404, detail="Jute PO not found")
        
        if jute_po.status_id != 21:  # Must be Draft
            raise HTTPException(status_code=400, detail="Only Draft POs can be opened")
        
        jute_po.status_id = 1  # Open status
        jute_po.updated_by = token_data.get("user_id")
        jute_po.updated_date_time = now_ist()
        
        db.commit()
        
        return {"success": True, "message": "Jute PO opened successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error opening Jute PO")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/approve_jute_po/{jute_po_id}")
def approve_jute_po(
    jute_po_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
    payload: Optional[ApprovePayload] = None,
):
    """Approve a Jute PO.

    When menu_id is provided (via body or query param), uses the shared approval
    utility for multi-level approval hierarchy support. When omitted, falls back
    to direct approval (backward compatible).
    """
    try:
        from src.models.jute import JutePo
        from src.models.mst import BranchMst

        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        co_id = int(q_co_id)

        user_id = token_data.get("user_id")

        # Determine menu_id from payload body or query param
        menu_id = None
        if payload and payload.menu_id is not None:
            menu_id = payload.menu_id
        elif request.query_params.get("menu_id"):
            menu_id = int(request.query_params.get("menu_id"))

        if menu_id is not None:
            # Use shared approval utility for multi-level approval
            approval_result = process_approval(
                doc_id=jute_po_id,
                user_id=user_id,
                menu_id=menu_id,
                db=db,
                get_doc_fn=get_jute_po_with_approval_info,
                update_status_fn=update_jute_po_status,
                id_param_name="jute_po_id",
                doc_name="Jute PO",
                document_amount=None,  # Jute never checks values
            )
            new_status = approval_result["new_status_id"]
            is_final = new_status == 3
            message = "Jute PO approved successfully" if is_final else approval_result["message"]

            return {"success": True, "message": message, "status_id": new_status}
        else:
            # Backward compatible: direct approval without hierarchy checks
            jute_po = db.query(JutePo).join(
                BranchMst, JutePo.branch_id == BranchMst.branch_id
            ).filter(
                JutePo.jute_po_id == jute_po_id,
                BranchMst.co_id == co_id,
            ).first()

            if not jute_po:
                raise HTTPException(status_code=404, detail="Jute PO not found")

            if jute_po.status_id not in [1, 20]:
                raise HTTPException(
                    status_code=400,
                    detail="Jute PO cannot be approved in current status",
                )

            jute_po.status_id = 3  # Approved status
            jute_po.updated_by = user_id
            jute_po.updated_date_time = now_ist()

            db.commit()

            return {"success": True, "message": "Jute PO approved successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error approving Jute PO")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reject_jute_po/{jute_po_id}")
def reject_jute_po(
    jute_po_id: int,
    payload: RejectPayload,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Reject a Jute PO.

    When menu_id is provided, uses the shared rejection utility for
    level-based permission checks. When omitted, falls back to direct
    rejection (backward compatible).
    """
    try:
        from src.models.jute import JutePo
        from src.models.mst import BranchMst

        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        co_id = int(q_co_id)

        user_id = token_data.get("user_id")
        menu_id = payload.menu_id

        if menu_id is not None:
            # Use shared rejection utility for level-based permission checks
            process_rejection(
                doc_id=jute_po_id,
                user_id=user_id,
                menu_id=menu_id,
                db=db,
                get_doc_fn=get_jute_po_with_approval_info,
                update_status_fn=update_jute_po_status,
                id_param_name="jute_po_id",
                doc_name="Jute PO",
                reason=payload.reason,
            )
        else:
            # Backward compatible: direct rejection without hierarchy checks
            jute_po = db.query(JutePo).join(
                BranchMst, JutePo.branch_id == BranchMst.branch_id
            ).filter(
                JutePo.jute_po_id == jute_po_id,
                BranchMst.co_id == co_id,
            ).first()

            if not jute_po:
                raise HTTPException(status_code=404, detail="Jute PO not found")

            if jute_po.status_id not in [1, 20]:
                raise HTTPException(
                    status_code=400,
                    detail="Jute PO cannot be rejected in current status",
                )

            jute_po.status_id = 4  # Rejected status
            jute_po.approval_level = None
            jute_po.updated_by = user_id
            jute_po.updated_date_time = now_ist()

            db.commit()

        # Store rejection reason in internal_note (both paths)
        if payload.reason:
            note_query = text("""
                UPDATE jute_po
                SET internal_note = CONCAT(
                    COALESCE(internal_note, ''),
                    CASE WHEN internal_note IS NOT NULL AND internal_note != '' THEN ' | ' ELSE '' END,
                    'Rejected: ', :reason
                )
                WHERE jute_po_id = :jute_po_id
            """)
            db.execute(note_query, {"jute_po_id": jute_po_id, "reason": payload.reason})
            db.commit()

        return {"success": True, "message": "Jute PO rejected successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error rejecting Jute PO")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cancel_draft_jute_po/{jute_po_id}")
def cancel_draft_jute_po(
    jute_po_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Cancel a Draft Jute PO."""
    try:
        from src.models.jute import JutePo
        from src.models.mst import BranchMst
        
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        co_id = int(q_co_id)
        
        # Join with branch_mst to verify co_id since JutePo doesn't have co_id column
        jute_po = db.query(JutePo).join(
            BranchMst, JutePo.branch_id == BranchMst.branch_id
        ).filter(
            JutePo.jute_po_id == jute_po_id,
            BranchMst.co_id == co_id
        ).first()
        
        if not jute_po:
            raise HTTPException(status_code=404, detail="Jute PO not found")
        
        if jute_po.status_id != 21:  # Must be Draft
            raise HTTPException(status_code=400, detail="Only Draft POs can be cancelled")
        
        jute_po.status_id = 6  # Cancelled status
        jute_po.updated_by = token_data.get("user_id")
        jute_po.updated_date_time = now_ist()
        
        db.commit()
        
        return {"success": True, "message": "Jute PO cancelled successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error cancelling Jute PO")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reopen_jute_po/{jute_po_id}")
def reopen_jute_po(
    jute_po_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Reopen a rejected or cancelled Jute PO (back to Draft), or a CLOSED one.

    A CLOSED PO (any close_type: AUTO / SHORT / BULK / MIGRATED) goes back to the
    status it had before the close, taken from jute_po_status_log (fallback
    APPROVED), the four close columns are cleared and the change is logged
    with source MANUAL (po-settlement-spec.md section 4).
    """
    try:
        from src.models.jute import JutePo
        from src.models.mst import BranchMst
        
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        co_id = int(q_co_id)
        user_id = token_data.get("user_id")
        
        # Join with branch_mst to verify co_id since JutePo doesn't have co_id column
        jute_po = db.query(JutePo).join(
            BranchMst, JutePo.branch_id == BranchMst.branch_id
        ).filter(
            JutePo.jute_po_id == jute_po_id,
            BranchMst.co_id == co_id
        ).first()
        
        if not jute_po:
            raise HTTPException(status_code=404, detail="Jute PO not found")

        if jute_po.status_id == JUTE_PO_CLOSED_STATUS:
            _require_edit_access(db, request, user_id, [jute_po.branch_id])
            jute_po.updated_by = user_id
            jute_po.updated_date_time = now_ist()
            restored = reopen_closed_po(
                db, jute_po_id, user_id=user_id, trigger_ref=f"reopen_jute_po by user {user_id}"
            )
            db.commit()
            return {
                "success": True,
                "message": "Jute PO reopened successfully",
                "status_id": restored,
            }
        
        if jute_po.status_id not in [4, 6]:  # Must be Rejected or Cancelled
            raise HTTPException(status_code=400, detail="Only Rejected, Cancelled or Closed POs can be reopened")
        
        jute_po.status_id = 21  # Back to Draft
        jute_po.updated_by = user_id
        jute_po.updated_date_time = now_ist()
        
        db.commit()
        
        return {"success": True, "message": "Jute PO reopened successfully", "status_id": 21}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        if is_missing_settlement_schema(e):
            raise HTTPException(status_code=409, detail=MISSING_SCHEMA_MSG)
        logger.exception("Error reopening Jute PO")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# JUTE PO SETTLEMENT / CLOSING
# Spec: ../vowerp3ui/docs/claude/modules/jute-purchase/po-settlement-spec.md (section 4).
# All status writes go through src/juteProcurement/po_settlement.py; the
# handlers here only validate, scope by company and commit.
# =============================================================================


def _require_int_query(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _json_body(request: Request) -> dict:
    try:
        body = parse_json_body(request)
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return body


def _require_remark(body: dict) -> str:
    remark = body.get("remark")
    if not isinstance(remark, str) or not remark.strip():
        raise HTTPException(status_code=400, detail="remark is required")
    remark = remark.strip()
    if len(remark) > REMARK_MAX_LEN:
        raise HTTPException(
            status_code=400, detail=f"remark must be at most {REMARK_MAX_LEN} characters"
        )
    return remark


def _po_in_company(db: Session, jute_po_id: int, co_id: int):
    """jute_po row scoped through branch_mst (jute_po has no co_id), or None."""
    return db.execute(
        text("""
            SELECT jp.jute_po_id, jp.status_id
            FROM jute_po jp
            INNER JOIN branch_mst bm ON bm.branch_id = jp.branch_id
            WHERE jp.jute_po_id = :jute_po_id AND bm.co_id = :co_id
        """),
        {"jute_po_id": int(jute_po_id), "co_id": int(co_id)},
    ).fetchone()


def _close_result(result: dict) -> dict:
    return {k: result.get(k) for k in ("jute_po_id", "status_id", "close_type", "closed_date")}


NO_EDIT_ACCESS_MSG = "This action needs edit access on the Jute PO menu"


def _require_edit_access(db: Session, request: Request, user_id, branch_ids) -> None:
    """403 unless the user has edit access on the page they act from (?menu_path=,
    default the Jute PO menu) for one of the branches. Server-side half of the
    frontend's hasMenuAccess(path, "edit") gate."""
    menu_path = request.query_params.get("menu_path") or DEFAULT_PO_MENU_PATH
    if not edit_access_check(db, user_id, list(branch_ids), menu_path):
        raise HTTPException(status_code=403, detail=NO_EDIT_ACCESS_MSG)


@router.post("/close_jute_po/{jute_po_id}")
def close_jute_po(
    jute_po_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Short-close a live (Open / Approved) Jute PO that will not be fulfilled.

    ?co_id= ; body {"remark": str (required, <= 500)}. Sets status CLOSED with
    close_type SHORT, logs source MANUAL. 400 for any other status.
    """
    co_id = _require_int_query(request, "co_id")
    remark = _require_remark(_json_body(request))
    user_id = token_data.get("user_id")
    try:
        po_row = db.execute(
            text("SELECT branch_id FROM jute_po WHERE jute_po_id = :id"), {"id": jute_po_id}
        ).fetchone()
        if po_row is not None:
            _require_edit_access(db, request, user_id, [po_row._mapping["branch_id"]])
        result = manual_close_po(
            db, jute_po_id, co_id=co_id, user_id=user_id, remark=remark,
            close_type=CLOSE_TYPE_SHORT, source=SOURCE_MANUAL,
            trigger_ref=f"close_jute_po by user {user_id}",
        )
        if not result["ok"]:
            raise HTTPException(
                status_code=404 if result.get("not_found") else 400, detail=result["reason"]
            )
        db.commit()
        return {"data": _close_result(result)}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        if is_missing_settlement_schema(e):
            raise HTTPException(status_code=409, detail=MISSING_SCHEMA_MSG)
        logger.exception("Error closing Jute PO")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bulk_close_jute_po")
def bulk_close_jute_po(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Short-close several live POs of a branch at once (from the Outstanding PO
    Position report). ?co_id=&branch_id= ; body {"jute_po_ids": [int], "remark": str}.
    close_type BULK, source BULK. Non-live or foreign ids are skipped and reported.
    """
    co_id = _require_int_query(request, "co_id")
    branch_id = _require_int_query(request, "branch_id")
    body = _json_body(request)
    raw_ids = body.get("jute_po_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(status_code=400, detail="jute_po_ids must be a non-empty list")
    try:
        ids = [int(i) for i in raw_ids]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="jute_po_ids must be integers")
    remark = _require_remark(body)
    user_id = token_data.get("user_id")
    try:
        _require_edit_access(db, request, user_id, [branch_id])
        closed, skipped, seen = [], [], set()
        for jute_po_id in ids:
            if jute_po_id in seen:
                continue
            seen.add(jute_po_id)
            result = manual_close_po(
                db, jute_po_id, co_id=co_id, branch_id=branch_id, user_id=user_id, remark=remark,
                close_type=CLOSE_TYPE_BULK, source=SOURCE_BULK,
                trigger_ref=f"bulk_close_jute_po by user {user_id}",
            )
            if result["ok"]:
                closed.append(_close_result(result))
            else:
                skipped.append({"jute_po_id": jute_po_id, "reason": result["reason"]})
        db.commit()
        return {"data": {"closed": closed, "skipped": skipped}}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        if is_missing_settlement_schema(e):
            raise HTTPException(status_code=409, detail=MISSING_SCHEMA_MSG)
        logger.exception("Error bulk-closing Jute POs")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/recompute_settlement")
def recompute_settlement(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Re-evaluate every live and AUTO-closed PO of a branch against its
    receipts (?co_id=&branch_id=). Run this after any direct database change
    to jute_mr / jute_po. Returns {"data": {"closed": n, "reopened": n, "unchanged": n}}.
    """
    co_id = _require_int_query(request, "co_id")
    branch_id = _require_int_query(request, "branch_id")
    user_id = token_data.get("user_id")
    try:
        _require_edit_access(db, request, user_id, [branch_id])
        counts = recompute_branch(
            db, co_id, branch_id, user_id=user_id,
            trigger_ref=f"recompute co={co_id} br={branch_id} by user {user_id}",
        )
        db.commit()
        return {"data": counts}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        if is_missing_settlement_schema(e):
            raise HTTPException(status_code=409, detail=MISSING_SCHEMA_MSG)
        logger.exception("Error recomputing Jute PO settlement")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/settle_rule")
def get_settle_rule(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The company's settlement rule (?co_id=), or the defaults with is_default=true."""
    co_id = _require_int_query(request, "co_id")
    try:
        return {"data": load_rule(db, co_id).as_dict()}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error loading Jute PO settle rule")
        raise HTTPException(status_code=500, detail=str(e))


def _parse_rule_body(body: dict) -> SettleRule:
    enabled = body.get("lorry_rule_enabled")
    if isinstance(enabled, bool):
        lorry = enabled
    elif enabled in (0, 1, "0", "1", "true", "false"):
        lorry = str(enabled).lower() in ("1", "true")
    else:
        raise HTTPException(status_code=400, detail="lorry_rule_enabled must be true or false")
    try:
        tol = float(body.get("tolerance_lorry_fraction"))
        stale = int(body.get("stale_days"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="tolerance_lorry_fraction and stale_days are required numbers"
        )
    if not (TOLERANCE_FRACTION_MIN <= tol <= TOLERANCE_FRACTION_MAX):
        raise HTTPException(
            status_code=400,
            detail=f"tolerance_lorry_fraction must be between {TOLERANCE_FRACTION_MIN:g} and {TOLERANCE_FRACTION_MAX:g} (fraction of one lorry)",
        )
    if not (STALE_DAYS_MIN <= stale <= STALE_DAYS_MAX):
        raise HTTPException(
            status_code=400, detail=f"stale_days must be between {STALE_DAYS_MIN} and {STALE_DAYS_MAX}"
        )
    return SettleRule(lorry_rule_enabled=lorry, tolerance_lorry_fraction=round(tol, 2), stale_days=stale, is_default=False)


@router.put("/settle_rule")
def put_settle_rule(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Upsert the company's rule (?co_id= ; body {lorry_rule_enabled,
    tolerance_lorry_fraction 0-1, stale_days 1-365}) and recompute every branch of
    the company so the change takes effect at once. Returns the rule plus
    "counts": {closed, reopened, unchanged} summed over the branches.
    """
    co_id = _require_int_query(request, "co_id")
    rule = _parse_rule_body(_json_body(request))
    user_id = token_data.get("user_id")
    try:
        branches = db.execute(
            text("SELECT branch_id FROM branch_mst WHERE co_id = :co_id ORDER BY branch_id"),
            {"co_id": co_id},
        ).fetchall()
        _require_edit_access(db, request, user_id, [int(b._mapping["branch_id"]) for b in branches])
        saved = save_rule(db, co_id, rule, user_id)
        counts = {"closed": 0, "reopened": 0, "unchanged": 0}
        for b in branches:
            branch_id = int(b._mapping["branch_id"])
            part = recompute_branch(
                db, co_id, branch_id, user_id=user_id,
                trigger_ref=f"settle_rule co={co_id} br={branch_id} by user {user_id}",
            )
            for k in counts:
                counts[k] += part.get(k, 0)
        db.commit()
        return {"data": {**saved.as_dict(), "counts": counts}}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        if is_missing_settlement_schema(e):
            raise HTTPException(status_code=409, detail=MISSING_SCHEMA_MSG)
        logger.exception("Error saving Jute PO settle rule")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status_log/{jute_po_id}")
def jute_po_status_log(
    jute_po_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Status history of a PO (?co_id=), newest first, with status and user names."""
    co_id = _require_int_query(request, "co_id")
    try:
        if not _po_in_company(db, jute_po_id, co_id):
            raise HTTPException(status_code=404, detail="Jute PO not found")
        return {"data": get_status_log(db, jute_po_id)}
    except HTTPException:
        raise
    except Exception as e:
        if is_missing_settlement_schema(e):
            raise HTTPException(status_code=409, detail=MISSING_SCHEMA_MSG)
        logger.exception("Error loading Jute PO status log")
        raise HTTPException(status_code=500, detail=str(e))

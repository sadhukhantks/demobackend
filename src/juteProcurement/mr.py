"""
Jute Material Receipt (MR) API endpoints.
Provides endpoints for viewing and managing jute MR records.
"""

from io import BytesIO
from fastapi import Depends, Request, HTTPException, APIRouter
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, date
from decimal import ROUND_HALF_UP, Decimal
from src.common.utils import now_ist
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProcurement._jute_scope_guards import (
    assert_issued_lines_intact,
    snapshot_issued_lines,
    validate_party_branch,
    validate_warehouse,
)
from src.juteProcurement.query import (
    get_jute_mr_table_query,
    get_jute_mr_table_count_query,
    get_jute_mr_by_id_query,
    get_jute_mr_line_items_query,
    get_all_active_company_branches_query,
    get_agent_map_options_query,
    get_warehouse_options_query,
    get_mr_with_approval_info,
    update_mr_status,
)
from src.common.approval_utils import process_approval, process_rejection, calculate_approval_permissions, _user_has_edit_access
from src.common.approval_query import get_max_approval_level
from src.juteProcurement.totals import compute_jute_totals, round_kg, sync_mr_weight_from_lines
from src.juteProcurement.po_settlement import settle_after_mr_change
from src.juteProcurement.formatters import (
    get_financial_year_sql_expression,
    format_jute_mr_number,
    format_jute_bill_pass_number,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class MRLineItemUpdate(BaseModel):
    """Model for updating a jute_mr_li record."""
    jute_mr_li_id: int
    actual_item_id: Optional[int] = None
    actual_quality: Optional[int] = None
    actual_qty: Optional[float] = None
    actual_weight: Optional[float] = None
    allowable_moisture: Optional[float] = None
    actual_moisture: Optional[str] = None
    claim_dust: Optional[float] = None
    shortage_kgs: Optional[int] = None
    accepted_weight: Optional[float] = None
    rate: Optional[float] = None
    claim_rate: Optional[float] = None
    claim_quality: Optional[str] = None
    water_damage_amount: Optional[float] = None
    premium_amount: Optional[float] = None
    remarks: Optional[str] = None
    warehouse_id: Optional[int] = None


class MRUpdateRequest(BaseModel):
    """Request model for updating MR header and line items."""
    mr_weight: Optional[float] = None
    party_id: Optional[str] = None
    party_branch_id: Optional[int] = None
    remarks: Optional[str] = None
    line_items: List[MRLineItemUpdate] = []


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.get("/get_mr_table")
def get_jute_mr_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get paginated list of jute MR records.
    
    Query params:
    - co_id: Company ID (required)
    - page: Page number (default: 1)
    - limit: Records per page (default: 10)
    - search: Search term
    - from_date / to_date: Optional ISO dates (YYYY-MM-DD) bounding jute_mr_date
    - ge_from_date / ge_to_date: Optional ISO dates bounding jute_gate_entry_date (TKT-000226)
    """
    try:
        # Get query parameters
        q_co_id = request.query_params.get("co_id")
        q_page = request.query_params.get("page", "1")
        q_limit = request.query_params.get("limit", "10")
        q_search = request.query_params.get("search", "").strip()
        q_from_date = (request.query_params.get("from_date") or "").strip()
        q_to_date = (request.query_params.get("to_date") or "").strip()
        # TKT-000226: optional gate-entry-date bounds, combinable with the MR-date ones
        q_ge_from_date = (request.query_params.get("ge_from_date") or "").strip()
        q_ge_to_date = (request.query_params.get("ge_to_date") or "").strip()
        q_status_id = (request.query_params.get("status_id") or "").strip()

        # Validate co_id
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        try:
            co_id = int(q_co_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid co_id")

        def _parse_iso_date(value: str, field_name: str):
            if not value:
                return None
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid {field_name}; expected YYYY-MM-DD",
                )

        date_from = _parse_iso_date(q_from_date, "from_date")
        date_to = _parse_iso_date(q_to_date, "to_date")
        ge_date_from = _parse_iso_date(q_ge_from_date, "ge_from_date")
        ge_date_to = _parse_iso_date(q_ge_to_date, "ge_to_date")

        status_id = None
        if q_status_id:
            try:
                status_id = int(q_status_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid status_id")

        # Parse pagination
        try:
            page = max(1, int(q_page))
            limit = min(100, max(1, int(q_limit)))
        except ValueError:
            page = 1
            limit = 10

        offset = (page - 1) * limit

        # Build search parameter
        search_param = f"%{q_search}%" if q_search else None

        # Get total count
        count_query = get_jute_mr_table_count_query(co_id=co_id, search=q_search if q_search else None)
        count_params = {
            "co_id": co_id,
            "date_from": date_from,
            "date_to": date_to,
            "ge_date_from": ge_date_from,
            "ge_date_to": ge_date_to,
            "status_id": status_id,
        }
        if search_param:
            count_params["search"] = search_param

        count_result = db.execute(count_query, count_params).fetchone()
        total = count_result.total if count_result else 0

        # Get paginated data
        data_query = get_jute_mr_table_query(co_id=co_id, search=q_search if q_search else None)
        data_params = {
            "co_id": co_id,
            "limit": limit,
            "offset": offset,
            "date_from": date_from,
            "date_to": date_to,
            "ge_date_from": ge_date_from,
            "ge_date_to": ge_date_to,
            "status_id": status_id,
        }
        if search_param:
            data_params["search"] = search_param

        result = db.execute(data_query, data_params).fetchall()
        rows = [dict(r._mapping) for r in result]

        # Format dates for JSON serialization
        for row in rows:
            for field in ["jute_mr_date", "challan_date", "po_date", "jute_gate_entry_date", "updated_date_time"]:
                if row.get(field):
                    dt = row[field]
                    if isinstance(dt, (datetime, date)):
                        row[field] = str(dt) if isinstance(dt, date) else dt.isoformat()

        return {
            "data": rows,
            "total": total,
            "page": page,
            "page_size": limit,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching jute MR table")
        raise HTTPException(status_code=500, detail=str(e))


MR_DOWNLOAD_MAX_ROWS = 50000

MR_DOWNLOAD_COLUMNS = [
    ("MR No", "mr_num"),
    ("MR Date", "jute_mr_date"),
    ("Branch", "branch_name"),
    ("Supplier", "supplier_name"),
    ("Party", "party_name"),
    ("Challan No", "challan_no"),
    ("Gate Entry No", "gate_entry_no"),
    ("Gate Entry Date", "jute_gate_entry_date"),
    ("Mukam", "mukam"),
    ("Vehicle No", "vehicle_no"),
    ("MR Weight", "mr_weight"),
    ("Status", "status"),
]


def mr_download_date_suffix(from_date: str, to_date: str, ge_from_date: str = "", ge_to_date: str = "") -> str:
    """File-name tag for the MR Excel: the MR-date range, else the gate-entry
    range prefixed ``ge_`` (TKT-000226), else ``all``. Both ranges given → both."""
    parts = [p for p in (from_date, to_date) if p]
    ge_parts = [p for p in (ge_from_date, ge_to_date) if p]
    if ge_parts:
        parts.append("ge_" + "_".join(ge_parts))
    return "_".join(parts) if parts else "all"


@router.get("/download_mr_table")
def download_jute_mr_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Download the jute MR list as an .xlsx workbook, applying the same filters
    (search, from_date, to_date, ge_from_date, ge_to_date, status_id) as
    /get_mr_table. No pagination — exports the entire filtered set up to
    MR_DOWNLOAD_MAX_ROWS.

    Query params:
    - co_id: Company ID (required)
    - search: Optional search term
    - from_date / to_date: Optional ISO dates (YYYY-MM-DD) bounding jute_mr_date
    - ge_from_date / ge_to_date: Optional ISO dates bounding jute_gate_entry_date (TKT-000226)
    """
    try:
        q_co_id = request.query_params.get("co_id")
        q_search = request.query_params.get("search", "").strip()
        q_from_date = (request.query_params.get("from_date") or "").strip()
        q_to_date = (request.query_params.get("to_date") or "").strip()
        # TKT-000226: optional gate-entry-date bounds, combinable with the MR-date ones
        q_ge_from_date = (request.query_params.get("ge_from_date") or "").strip()
        q_ge_to_date = (request.query_params.get("ge_to_date") or "").strip()
        q_status_id = (request.query_params.get("status_id") or "").strip()

        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        try:
            co_id = int(q_co_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid co_id")

        def _parse_iso_date(value: str, field_name: str):
            if not value:
                return None
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid {field_name}; expected YYYY-MM-DD",
                )

        date_from = _parse_iso_date(q_from_date, "from_date")
        date_to = _parse_iso_date(q_to_date, "to_date")
        ge_date_from = _parse_iso_date(q_ge_from_date, "ge_from_date")
        ge_date_to = _parse_iso_date(q_ge_to_date, "ge_to_date")

        status_id = None
        if q_status_id:
            try:
                status_id = int(q_status_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid status_id")

        search_param = f"%{q_search}%" if q_search else None

        count_query = get_jute_mr_table_count_query(co_id=co_id, search=q_search if q_search else None)
        count_params = {
            "co_id": co_id,
            "date_from": date_from,
            "date_to": date_to,
            "ge_date_from": ge_date_from,
            "ge_date_to": ge_date_to,
            "status_id": status_id,
        }
        if search_param:
            count_params["search"] = search_param

        count_result = db.execute(count_query, count_params).fetchone()
        total = int(count_result.total) if count_result and count_result.total else 0

        if total > MR_DOWNLOAD_MAX_ROWS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Result set too large — max {MR_DOWNLOAD_MAX_ROWS} rows, "
                    f"got {total}. Narrow the date range or search term."
                ),
            )

        rows = []
        if total > 0:
            data_query = get_jute_mr_table_query(co_id=co_id, search=q_search if q_search else None)
            data_params = {
                "co_id": co_id,
                "limit": total,
                "offset": 0,
                "date_from": date_from,
                "date_to": date_to,
                "ge_date_from": ge_date_from,
                "ge_date_to": ge_date_to,
                "status_id": status_id,
            }
            if search_param:
                data_params["search"] = search_param

            result = db.execute(data_query, data_params).fetchall()
            rows = [dict(r._mapping) for r in result]

        wb = Workbook()
        ws = wb.active
        ws.title = "Jute MR"

        for col_idx, (header, _) in enumerate(MR_DOWNLOAD_COLUMNS, start=1):
            ws.cell(row=1, column=col_idx, value=header)

        for row_idx, row in enumerate(rows, start=2):
            mr_no = row.get("mr_num") or row.get("branch_mr_no")
            mr_date = row.get("jute_mr_date")
            if isinstance(mr_date, (datetime, date)):
                mr_date = str(mr_date) if isinstance(mr_date, date) else mr_date.isoformat()
            ge_date = row.get("jute_gate_entry_date")
            if isinstance(ge_date, (datetime, date)):
                ge_date = str(ge_date) if isinstance(ge_date, date) else ge_date.isoformat()

            values = {
                "mr_num": mr_no,
                "jute_mr_date": mr_date,
                "branch_name": row.get("branch_name"),
                "supplier_name": row.get("supplier_name"),
                "party_name": row.get("party_name"),
                "challan_no": row.get("challan_no"),
                "gate_entry_no": row.get("gate_entry_no"),
                "jute_gate_entry_date": ge_date,
                "mukam": row.get("mukam"),
                "vehicle_no": row.get("vehicle_no"),
                "mr_weight": row.get("mr_weight"),
                "status": row.get("status") or "Open",
            }
            for col_idx, (_, key) in enumerate(MR_DOWNLOAD_COLUMNS, start=1):
                ws.cell(row=row_idx, column=col_idx, value=values[key])

        bio = BytesIO()
        wb.save(bio)
        bio.seek(0)
        xlsx_bytes = bio.getvalue()

        filename = f"jute_mr_{mr_download_date_suffix(q_from_date, q_to_date, q_ge_from_date, q_ge_to_date)}.xlsx"

        return StreamingResponse(
            iter([xlsx_bytes]),
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error downloading jute MR table")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_agent_options")
def get_agent_options(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get agent options from jute_agent_map table.
    Returns agent branches that have been mapped for the current company.
    """
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        
        try:
            co_id = int(q_co_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid co_id")

        query = get_agent_map_options_query()
        result = db.execute(query, {"co_id": co_id}).fetchall()
        branches = [dict(r._mapping) for r in result]

        return {
            "branches": branches,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching agent options")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_warehouse_options")
def get_warehouse_options(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get warehouse options for a specific branch.
    Returns warehouses with hierarchical path (e.g., 'Main-Section A-Bin 1').
    """
    try:
        q_branch_id = request.query_params.get("branch_id")
        if not q_branch_id:
            raise HTTPException(status_code=400, detail="branch_id is required")
        
        try:
            branch_id = int(q_branch_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid branch_id")

        query = get_warehouse_options_query()
        result = db.execute(query, {"branch_id": branch_id}).fetchall()
        warehouses = [dict(r._mapping) for r in result]

        return {
            "warehouses": warehouses,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching warehouse options")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_party_branches")
def get_party_branches(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get party branches for a specific party.
    Returns party branches with address info for the dropdown.
    Used in MR edit to select party branch (mandatory field).
    
    Query params:
    - party_id: Party ID (required)
    """
    try:
        q_party_id = request.query_params.get("party_id")
        if not q_party_id:
            raise HTTPException(status_code=400, detail="party_id is required")
        
        try:
            party_id = int(q_party_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid party_id")

        query = text("""
            SELECT 
                pbm.party_mst_branch_id,
                pbm.party_id,
                pbm.address,
                pbm.gst_no,
                pm.supp_name AS party_name,
                CONCAT(COALESCE(pm.supp_name, ''), ' - ', COALESCE(pbm.address, '')) AS display
            FROM party_branch_mst pbm
            LEFT JOIN party_mst pm ON pm.party_id = pbm.party_id
            WHERE pbm.party_id = :party_id
                AND pbm.active = 1
            ORDER BY pbm.address
        """)
        result = db.execute(query, {"party_id": party_id}).fetchall()
        branches = [dict(r._mapping) for r in result]

        return {
            "branches": branches,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching party branches")
        raise HTTPException(status_code=500, detail=str(e))


def _build_mr_permissions(
    db: Session,
    menu_id: Optional[int],
    branch_id: Optional[int],
    status_id: Optional[int],
    approval_level: Optional[int],
    user_id: Optional[int],
) -> tuple[dict, Optional[int]]:
    """Compute the current user's approval permissions + the configured max level.

    Drives the MR action buttons so they follow the approval hierarchy:
      - Approve/Reject show only for the approver whose level matches the MR's
        current approval level (level-aware, via calculate_approval_permissions).
        Reject only surfaces once the MR is in Pending Approval (status 20) —
        matching the shared approval bar and the other transaction modules.
      - The frontend additionally shows Pending only at the highest level
        (canApprove AND approval_level == max_approval_level).

    Returns (permissions_dict, max_approval_level). When no menu context or no
    hierarchy is available, returns ({}, None) — the frontend then falls back to
    its status-based defaults (which do NOT grant approve, i.e. fail closed).
    """
    if not menu_id or not branch_id or not user_id:
        return {}, None
    try:
        permissions = calculate_approval_permissions(
            user_id=user_id,
            menu_id=menu_id,
            branch_id=branch_id,
            status_id=status_id,
            current_approval_level=approval_level,
            db=db,
        )

        max_row = db.execute(
            get_max_approval_level(),
            {"menu_id": menu_id, "branch_id": branch_id},
        ).fetchone()
        max_level = dict(max_row._mapping).get("max_level") if max_row else None
        return permissions, max_level
    except Exception:
        logger.exception("Error building MR approval permissions")
        return {}, None


@router.get("/get_mr_by_id")
def get_jute_mr_by_id(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get a single jute MR with line items.
    
    Query params:
    - id: MR ID (required)
    """
    try:
        q_id = request.query_params.get("id")
        if not q_id:
            raise HTTPException(status_code=400, detail="id is required")
        
        try:
            mr_id = int(q_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid id")

        # Get header data
        header_query = get_jute_mr_by_id_query()
        header_result = db.execute(header_query, {"mr_id": mr_id}).fetchone()
        
        if not header_result:
            raise HTTPException(status_code=404, detail="MR not found")

        header = dict(header_result._mapping)

        # Format dates
        for field in ["jute_mr_date", "challan_date", "po_date", "jute_gate_entry_date", "updated_date_time"]:
            if header.get(field):
                dt = header[field]
                if isinstance(dt, (datetime, date)):
                    header[field] = str(dt) if isinstance(dt, date) else dt.isoformat()

        # Get line items
        line_items_query = get_jute_mr_line_items_query()
        line_items_result = db.execute(line_items_query, {"mr_id": mr_id}).fetchall()
        line_items = [dict(r._mapping) for r in line_items_result]

        # Level-aware approval permissions so the UI action buttons follow the
        # approval hierarchy (Approve/Reject only for the current level's
        # approver; Pending only at the highest level). menu_id is optional —
        # when absent the frontend falls back to status-based defaults.
        menu_id_param = request.query_params.get("menu_id")
        menu_id_val = None
        if menu_id_param:
            try:
                menu_id_val = int(menu_id_param)
            except ValueError:
                menu_id_val = None
        permissions, max_approval_level = _build_mr_permissions(
            db,
            menu_id_val,
            header.get("branch_id"),
            header.get("status_id"),
            header.get("approval_level"),
            token_data.get("user_id"),
        )
        header["permissions"] = permissions
        header["max_approval_level"] = max_approval_level

        return {
            "header": header,
            "line_items": line_items,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching jute MR by ID")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/update_mr/{mr_id}")
def update_jute_mr(
    mr_id: int,
    request: Request,
    body: MRUpdateRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Update jute MR header and line items.
    Recalculates accepted_weight for each line item and updates mr_weight as sum of accepted weights.
    """
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        
        try:
            co_id = int(q_co_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid co_id")

        # Verify MR exists
        header_query = get_jute_mr_by_id_query()
        header_result = db.execute(header_query, {"mr_id": mr_id}).fetchone()
        
        if not header_result:
            raise HTTPException(status_code=404, detail="MR not found")

        header = dict(header_result._mapping)

        assert_mr_editable(db, mr_id, header.get("status_id"))

        # Same ownership rules the QC page is held to — a guard on one writer
        # only would leave the other open
        effective_party_id = body.party_id if body.party_id is not None else header.get("party_id")
        validate_party_branch(db, body.party_branch_id, effective_party_id)
        for item in body.line_items:
            validate_warehouse(db, item.warehouse_id, header.get("branch_id"))

        # Get user info for audit
        user_id = token_data.get("user_id")
        now = now_ist()

        issued_before = snapshot_issued_lines(db, mr_id)

        # Update line items and calculate accepted weights
        total_accepted_weight = 0.0
        
        for item in body.line_items:
            # Calculate shortage_kgs and accepted_weight
            actual_weight = float(item.actual_weight or 0.0)
            allowable_moisture = float(item.allowable_moisture) if item.allowable_moisture is not None else None
            actual_moisture_str = item.actual_moisture or "0"
            try:
                actual_moisture = float(actual_moisture_str)
            except (ValueError, TypeError):
                actual_moisture = 0.0
            
            claim_dust = float(item.claim_dust) if item.claim_dust is not None else 0.0
            
            # Calculate shortage_kgs first — all weights rounded to 0 decimals (whole kg)
            shortage_kgs = 0.0
            rounded_weight = round_kg(actual_weight)
            accepted_weight = rounded_weight
            
            if actual_weight > 0:
                moisture_diff = 0.0
                if allowable_moisture is not None and actual_moisture > allowable_moisture:
                    moisture_diff = actual_moisture - allowable_moisture
                
                deduction_percentage = moisture_diff + claim_dust
                if deduction_percentage > 0:
                    # Formula: shortage_kgs = actual_weight * (moisture diff % + claim_dust%)
                    # Round to 0 decimals to match Integer column type
                    shortage_kgs = round_kg(rounded_weight * deduction_percentage / 100.0)
                    # Formula: accepted_weight = actual_weight - shortage_kgs (both integers)
                    accepted_weight = max(0, rounded_weight - int(shortage_kgs))
                
                total_accepted_weight += accepted_weight
            
            # Update line item
            # Note: actual_rate is set to the same value as rate automatically
            update_query = text("""
                UPDATE jute_mr_li
                SET actual_item_id = :actual_item_id,
                    actual_quality = :actual_quality,
                    actual_qty = :actual_qty,
                    actual_weight = :actual_weight,
                    actual_rate = :rate,
                    allowable_moisture = :allowable_moisture,
                    actual_moisture = :actual_moisture,
                    claim_dust = :claim_dust,
                    shortage_kgs = :shortage_kgs,
                    accepted_weight = :accepted_weight,
                    total_price = ROUND((COALESCE(:accepted_weight, :actual_weight, 0) / 100) * COALESCE(:rate, 0), 2),
                    rate = :rate,
                    -- claim_rate is NOT NULL DEFAULT 0: an empty claim cell arrives as
                    -- null and would raise a raw IntegrityError back at the user. Blank
                    -- means "no claim", which is 0.
                    claim_rate = COALESCE(:claim_rate, 0),
                    claim_quality = :claim_quality,
                    water_damage_amount = :water_damage_amount,
                    premium_amount = :premium_amount,
                    remarks = :remarks,
                    warehouse_id = :warehouse_id,
                    updated_date_time = :updated_date_time
                WHERE jute_mr_li_id = :jute_mr_li_id
            """)
            
            db.execute(update_query, {
                "jute_mr_li_id": item.jute_mr_li_id,
                "actual_item_id": item.actual_item_id,
                "actual_quality": item.actual_quality,
                "actual_qty": item.actual_qty,
                # Received weight is stored in whole kg -- the same number the
                # shortage/accepted maths above ran on.
                "actual_weight": rounded_weight if item.actual_weight is not None else None,
                "allowable_moisture": item.allowable_moisture,
                "actual_moisture": item.actual_moisture,
                "claim_dust": item.claim_dust,
                "shortage_kgs": int(round(shortage_kgs)),
                "accepted_weight": int(round(accepted_weight)),
                "rate": item.rate,
                "claim_rate": item.claim_rate,
                "claim_quality": item.claim_quality,
                "water_damage_amount": item.water_damage_amount,
                "premium_amount": item.premium_amount,
                "remarks": item.remarks,
                "warehouse_id": item.warehouse_id,
                "updated_date_time": now,
            })

        # Update MR header with calculated weight
        mr_weight = round_kg(body.mr_weight) if body.mr_weight is not None else round_kg(total_accepted_weight)
        # A caller-sent 0 must not blank the MR Weight when the lines just saved
        # carry weight (TKT-000207) -- the lines are the source of truth.
        if not mr_weight and total_accepted_weight:
            mr_weight = round_kg(total_accepted_weight)
        
        header_sets = [
            "mr_weight = :mr_weight",
            "party_branch_id = :party_branch_id",
            "remarks = :remarks",
            "updated_by = :updated_by",
            "updated_date_time = :updated_date_time",
        ]
        header_params = {
            "mr_id": mr_id,
            "mr_weight": mr_weight,
            "party_branch_id": body.party_branch_id,
            "remarks": body.remarks,
            "updated_by": user_id,
            "updated_date_time": now,
        }
        # party_id only when the caller sent it — a legacy caller omitting it must
        # not blank the party (party_branch_id above is always sent by the MR page)
        if body.party_id is not None:
            header_sets.insert(1, "party_id = :party_id")
            header_params["party_id"] = body.party_id

        update_header_query = text(
            f"UPDATE jute_mr SET {', '.join(header_sets)} WHERE jute_mr_id = :mr_id"
        )

        db.execute(update_header_query, header_params)

        # Hard stop: a line cut below its issued quantity, or re-itemed under live issues.
        assert_issued_lines_intact(db, mr_id, issued_before)

        # Read back inside the same transaction so the check sees the weights
        # just written. Warning only -- the save is never refused (TKT-000196).
        warning = over_issued_lines_warning(db, mr_id)

        # PO settlement: the edited weights may settle (or un-settle) the PO.
        settle_after_mr_change(db, mr_id, user_id=user_id, trigger_ref=f"MR:{mr_id} update_mr")

        db.commit()

        return {
            "success": True,
            "message": "MR updated successfully",
            "mr_id": mr_id,
            "mr_weight": mr_weight,
            "warning": warning,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error updating jute MR")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# STATUS CHANGE SCHEMA
# =============================================================================

class MRStatusChangeRequest(BaseModel):
    """Request model for changing MR status."""
    new_status_id: int
    remarks: Optional[str] = None


# =============================================================================
# STATUS CHANGE ENDPOINT
# =============================================================================

@router.put("/change_status/{mr_id}")
def change_mr_status(
    mr_id: int,
    request: Request,
    body: MRStatusChangeRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Change the status of a jute MR.
    
    Validates that party_branch_id is set before allowing status changes from draft.
    Status IDs:
    - 21: Draft
    - 1: Open
    - 3: Approved
    - 4: Rejected
    - 5: Closed
    """
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        
        try:
            co_id = int(q_co_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid co_id")

        user_id = token_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="User not authenticated")

        # Get current MR details
        check_query = text("""
            SELECT jute_mr_id, status_id, party_id, party_branch_id
            FROM jute_mr
            WHERE jute_mr_id = :mr_id
        """)
        mr_row = db.execute(check_query, {"mr_id": mr_id}).fetchone()
        
        if not mr_row:
            raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")

        mr = dict(mr_row._mapping)
        current_status_id = mr.get("status_id")
        party_id = mr.get("party_id")
        party_branch_id = mr.get("party_branch_id")

        # Validate party and party_branch before allowing status change from draft,
        # and before Pending/Approved regardless of the current status (decision D4).
        if body.new_status_id in (MR_STATUS_APPROVED, MR_STATUS_PENDING) or (
            current_status_id == MR_STATUS_DRAFT and body.new_status_id != MR_STATUS_DRAFT
        ):
            require_mr_party_complete(party_id, party_branch_id)

        now = now_ist()

        # Update the status
        update_query = text("""
            UPDATE jute_mr
            SET status_id = :new_status_id,
                updated_by = :updated_by,
                updated_date_time = :updated_date_time
            WHERE jute_mr_id = :mr_id
        """)
        
        db.execute(update_query, {
            "mr_id": mr_id,
            "new_status_id": body.new_status_id,
            "updated_by": user_id,
            "updated_date_time": now,
        })

        # PO settlement: the MR's status decides whether it counts as a receipt.
        settle_after_mr_change(db, mr_id, user_id=user_id, trigger_ref=f"MR:{mr_id} change_status")

        db.commit()

        # Get status name
        status_query = text("SELECT status_name FROM status_mst WHERE status_id = :status_id")
        status_row = db.execute(status_query, {"status_id": body.new_status_id}).fetchone()
        status_name = status_row[0] if status_row else str(body.new_status_id)

        return {
            "success": True,
            "message": f"MR status changed to {status_name}",
            "mr_id": mr_id,
            "status_id": body.new_status_id,
            "status_name": status_name,
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error changing MR status")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# APPROVAL WORKFLOW SCHEMAS
# =============================================================================

class MRApprovalRequest(BaseModel):
    """Request model for MR approval actions."""
    mr_id: str
    branch_id: str
    reason: Optional[str] = None  # For reject action
    party_branch_id: Optional[int] = None  # For approve action - use if provided
    mr_date: Optional[str] = None  # For approve action - YYYY-MM-DD format, mandatory for final approval
    menu_id: Optional[int] = None  # For multi-level approval via shared utility


class MRReopenRequest(BaseModel):
    mr_id: str
    branch_id: str
    menu_id: int
    target: str = "open"  # "open" = MR editable again; "qc" = also redo QC (qc_check = 0)


# =============================================================================
# MR STATUS CONSTANTS
# =============================================================================

MR_STATUS_DRAFT = 21
MR_STATUS_OPEN = 1
MR_STATUS_PENDING = 13
MR_STATUS_PENDING_APPROVAL = 20
MR_STATUS_APPROVED = 3
MR_STATUS_REJECTED = 4
MR_STATUS_CANCELLED = 6
MR_STATUS_RETURNED = 48  # legacy status, converted to the is_returned flag by 20261003_jute_mr_is_returned_flag.sql


def require_mr_party_complete(party_id, party_branch_id) -> None:
    """Pending (13) and Approved (3) both need party AND party branch (decision D4).

    One helper, three call sites: pending_mr, approve_mr, change_mr_status.
    Do not re-implement this check inline anywhere else.
    """
    if not party_id:
        raise HTTPException(
            status_code=400,
            detail="Party must be selected before the MR can be set to Pending or Approved.",
        )
    if not party_branch_id:
        raise HTTPException(
            status_code=400,
            detail="Party branch must be selected before the MR can be set to Pending or Approved. "
                   "Add a branch for this party in Party Master if none exist.",
        )


MR_FROZEN_STATUSES = (MR_STATUS_PENDING, MR_STATUS_APPROVED)

_CHAIN_STATE_SQL = text("""
    SELECT CASE WHEN m.src_jute_mr_id IS NOT NULL THEN 1 ELSE 0 END AS is_hop,
           (SELECT COUNT(*) FROM jute_mr c
             WHERE c.src_jute_mr_id = m.jute_mr_id AND c.jute_mr_id <> m.jute_mr_id) AS child_count
    FROM jute_mr m
    WHERE m.jute_mr_id = :mr_id
""")


def mr_chain_state(db: Session, mr_id: int) -> dict:
    """juteTransfer linkage of an MR. Tolerant of tenants that lack src_jute_mr_id
    (sls and dev3 have it; others may not): an SQL error means 'no chain feature'."""
    try:
        row = db.execute(_CHAIN_STATE_SQL, {"mr_id": int(mr_id)}).fetchone()
    except Exception:  # ponytail: unknown-column on tenants without the transfer schema
        db.rollback()
        return {"is_hop": False, "child_count": 0}
    if not row:
        return {"is_hop": False, "child_count": 0}
    mapping = row._mapping
    return {"is_hop": bool(mapping.get("is_hop")), "child_count": int(mapping.get("child_count") or 0)}


def assert_mr_editable(db: Session, mr_id: int, status_id=None) -> None:
    """Decision D2: once Pending/Approved, or once a vertical chain exists on it,
    nothing on the MR may change from the ERP side except through reopen_mr."""
    if status_id in MR_FROZEN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="MR is Pending/Approved and locked. Use Reopen (edit access) to change it.",
        )
    chain = mr_chain_state(db, mr_id)
    if chain["is_hop"] or chain["child_count"]:
        raise HTTPException(
            status_code=409,
            detail="MR is part of a vertical transfer chain. Roll it back in juteTransfer first.",
        )


_LIVE_ISSUES_SQL = text("""
    SELECT COUNT(*) AS cnt, COALESCE(SUM(ji.weight), 0) AS wt
    FROM jute_issue ji
    JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id
    WHERE li.jute_mr_id = :mr_id AND ji.status_id <> 4
""")


def live_issue_summary(db: Session, mr_id: int) -> dict:
    row = db.execute(_LIVE_ISSUES_SQL, {"mr_id": int(mr_id)}).fetchone()
    if not row:
        return {"count": 0, "weight": 0.0}
    mapping = row._mapping
    return {"count": int(mapping.get("cnt") or 0), "weight": float(mapping.get("wt") or 0)}


# TKT-000196: an MR edit that lowers a line's received weight below what the
# floor has already drawn from it turns that line's stock negative (sls MR 28256:
# lines re-saved at 1,121 / 1,693 kg with 3,600 / 5,440 kg of draft issues, and
# the Jute Stock report read -2,479 / -3,747 for D TD-5 / D TD-6). reopen_mr warns
# once, before the edit; the save itself said nothing. Rejected issues (4) do not
# hold stock, matching vw_jute_stock_outstanding.
_OVER_ISSUED_LINES_SQL = text("""
    SELECT li.jute_mr_li_id,
           COALESCE(im.item_name, li.actual_quality) AS item_name,
           COALESCE(li.actual_weight, 0) AS actual_weight,
           COALESCE(SUM(ji.weight), 0) AS issued
    FROM jute_mr_li li
    JOIN jute_issue ji ON ji.jute_mr_li_id = li.jute_mr_li_id AND ji.status_id <> 4
    LEFT JOIN item_mst im ON im.item_id = li.actual_item_id
    WHERE li.jute_mr_id = :mr_id AND li.active = 1
    GROUP BY li.jute_mr_li_id, im.item_name, li.actual_quality, li.actual_weight
    HAVING COALESCE(SUM(ji.weight), 0) > COALESCE(li.actual_weight, 0) + 0.5
    ORDER BY li.jute_mr_li_id
""")


def over_issued_lines_warning(db: Session, mr_id: int):
    """Lines of one MR whose live issues exceed their received weight, as one
    user-facing sentence, or None. Never blocks (owner ruling 2026-09-03: an MR
    correction is followed by 'Sync issued weights', not refused)."""
    rows = db.execute(_OVER_ISSUED_LINES_SQL, {"mr_id": int(mr_id)}).fetchall()
    if not rows:
        return None
    parts = []
    for r in rows:
        m = r._mapping
        name = m.get("item_name") or f"line {m.get('jute_mr_li_id')}"
        parts.append(
            f"{name}: {float(m.get('issued') or 0):.0f} kg already issued against "
            f"{float(m.get('actual_weight') or 0):.0f} kg received"
        )
    return (
        "; ".join(parts)
        + ". Stock on these lines is now negative. Correct the received weight, or run "
        "'Sync issued weights' on the Jute MR Wise Issue Detail report so the issues "
        "fit the received weight."
    )


def assert_no_downstream(db: Session, mr_id: int) -> None:
    """Reject / Cancel / Return must not orphan issues or chain hops."""
    issues = live_issue_summary(db, mr_id)
    if issues["count"]:
        raise HTTPException(
            status_code=409,
            detail=f"{issues['count']} issue entries ({issues['weight']:.0f} kg) are drawn against this MR. "
                   "Reject/delete those issues first.",
        )
    chain = mr_chain_state(db, mr_id)
    if chain["is_hop"] or chain["child_count"]:
        raise HTTPException(
            status_code=409,
            detail="MR is part of a vertical transfer chain. Roll it back in juteTransfer first.",
        )


# =============================================================================
# APPROVAL WORKFLOW ENDPOINTS
# =============================================================================

def get_next_branch_mr_no(db: Session, branch_id: int) -> int:
    """Get the next branch_mr_no for the given branch (simple increment, no FY)."""
    query = text("""
        SELECT COALESCE(MAX(branch_mr_no), 0) + 1 AS next_no
        FROM jute_mr
        WHERE branch_id = :branch_id
    """)
    result = db.execute(query, {"branch_id": branch_id}).fetchone()
    return result[0] if result else 1


def get_next_mr_no_for_fy(db: Session, branch_id: int, mr_date: date) -> int:
    """
    Get the next MR number for the given branch and financial year.
    
    MR numbering is per branch per financial year.
    Financial year: April 1 to March 31.
    
    Args:
        db: Database session
        branch_id: Branch ID
        mr_date: MR date to determine the financial year
    
    Returns:
        Next MR number (1-based)
    """
    # Build the financial year SQL expression for jute_mr_date
    fy_expr = get_financial_year_sql_expression("jute_mr_date")
    
    # Determine the current financial year from mr_date
    if mr_date.month >= 4:  # April onwards
        fy_start_year = mr_date.year
    else:  # January to March
        fy_start_year = mr_date.year - 1
    
    fy_string = f"{fy_start_year % 100:02d}-{(fy_start_year + 1) % 100:02d}"
    
    query = text(f"""
        SELECT COALESCE(MAX(branch_mr_no), 0) + 1 AS next_no
        FROM jute_mr
        WHERE branch_id = :branch_id
          AND jute_mr_date IS NOT NULL
          AND {fy_expr} = :fy_string
    """)
    
    result = db.execute(query, {"branch_id": branch_id, "fy_string": fy_string}).fetchone()
    return result[0] if result else 1


def get_next_bill_pass_no_for_fy(db: Session, branch_id: int, bill_pass_date: date) -> int:
    """
    Get the next Bill Pass number for the given branch and financial year.
    
    Bill Pass numbering is per branch per financial year.
    Financial year: April 1 to March 31.
    
    Args:
        db: Database session
        branch_id: Branch ID
        bill_pass_date: Bill Pass date to determine the financial year
    
    Returns:
        Next Bill Pass number (1-based)
    """
    # Build the financial year SQL expression for bill_pass_date
    fy_expr = get_financial_year_sql_expression("bill_pass_date")
    
    # Determine the current financial year from bill_pass_date
    if bill_pass_date.month >= 4:  # April onwards
        fy_start_year = bill_pass_date.year
    else:  # January to March
        fy_start_year = bill_pass_date.year - 1
    
    fy_string = f"{fy_start_year % 100:02d}-{(fy_start_year + 1) % 100:02d}"
    
    query = text(f"""
        SELECT COALESCE(MAX(bill_pass_no), 0) + 1 AS next_no
        FROM jute_mr
        WHERE branch_id = :branch_id
          AND bill_pass_date IS NOT NULL
          AND {fy_expr} = :fy_string
    """)
    
    result = db.execute(query, {"branch_id": branch_id, "fy_string": fy_string}).fetchone()
    return result[0] if result else 1


# =============================================================================
# TDS CALCULATION HELPERS
# =============================================================================

# TDS threshold: Rs. 50 lacs (50,00,000)
TDS_THRESHOLD = 5000000.0

# TDS rate: 0.1% (0.001)
TDS_RATE = 0.001


# A preview for an MR that has no jute_mr_id yet (exclude_mr_id None) sorts AFTER
# every MR that ties on date and number (MySQL INT max; the column is INT).
MR_ORDER_KEY_UNASSIGNED = 2147483647

# The ERP's natural MR sequence, as a SQL predicate on a jute_mr row: TRUE when the
# row comes BEFORE the MR described by :mr_date / :mr_no / :mr_id (see
# get_cumulative_mr_value_for_party_in_fy for the rule and its justification).
MR_BEFORE_SQL = """(
              jute_mr_date < :mr_date
              OR (jute_mr_date = :mr_date AND COALESCE(branch_mr_no, 0) < :mr_no)
              OR (jute_mr_date = :mr_date AND COALESCE(branch_mr_no, 0) = :mr_no AND jute_mr_id < :mr_id)
          )"""


def get_cumulative_mr_value_for_party_in_fy(
    db: Session,
    party_id: str,
    mr_date: date,
    exclude_mr_id: int = None,
    branch_mr_no: int = None,
) -> float:
    """Sum of total_amount of the party's APPROVED MRs that come BEFORE the given MR in
    the financial year of mr_date — the 194Q cumulative ("cumulative_previous").

    Owner ruling (2026-10-03): TDS counts only the party's MRs that come before this
    one, at approve and at bill pass alike. Until then the bill-pass recompute summed
    every other approved MR of the FY, so once later MRs were approved an early MR lost
    its share of the Rs 50 lakh exemption and was charged TDS on its full amount.

    "Before" is the ERP's natural approval sequence, read from the columns final
    approval writes, so it is the same at approve time and at every later bill pass:

        (jute_mr_date, COALESCE(branch_mr_no, 0), jute_mr_id) ascending

    - jute_mr_date first. The MR date is the purchase/credit date in the books
      (approve sets bill_pass_date = jute_mr_date) and 194Q aggregates the year's purchases in that order. A
      back-dated MR therefore takes its place by its date, not by the day it was
      approved — and the stored TDS of already-approved MRs dated after it is stale
      until their next bill pass save recomputes it.
    - branch_mr_no next. Final approval assigns it as MAX+1 per branch and FY of the
      MR date, so for same-day MRs of one branch it is the order they were approved
      in. Reopen keeps it (P2-2), so a reopened and re-approved MR keeps its place and
      gets the same TDS back (if its lines did not change).
    - jute_mr_id last. Tie-break for same-day MRs of the party at two branches of the
      company (the number sequences are per branch, so the numbers are incomparable —
      party_mst is per company, so one party_id already spans only one buyer company)
      and for legacy rows without a number (COALESCE(branch_mr_no, 0)). It is the
      gate-entry order.

    Only status 3 (Approved) counts. Pending 13 is the hand-off state — it has no MR
    date or number, and in a transfer chain the root is finalised later under another
    party — so it never counts; Open/Rejected/Cancelled/Returned never count. The MR
    itself is excluded by id: at approve time process_approval may already have
    written status 3 while the row still carries its old date/number (re-approve).

    Args:
        db: Database session
        party_id: Party ID (jute_mr.party_id is a string column)
        mr_date: the MR's jute_mr_date — fixes the FY and is the first sort key
        exclude_mr_id: the MR's jute_mr_id — excluded, and the last sort key. None =
            a preview for an MR that does not exist yet: nothing is excluded and every
            MR tying on date and number counts as before (MR_ORDER_KEY_UNASSIGNED).
        branch_mr_no: the MR's branch_mr_no (at approve: the number being assigned, at
            bill pass: the stored one). None is treated exactly like a NULL column
            (COALESCE -> 0), so an un-numbered MR sorts first on its day — the same
            place it holds when it is one of the OTHER rows; the order must stay a total
            order or two MRs could each count the other as "before". A preview of an
            unapproved MR should pass the number approve will assign
            (get_next_mr_no_for_fy).

    Returns:
        Total value (total_amount column) of the approved MRs before this one in the FY
    """
    # Build the financial year SQL expression for jute_mr_date
    fy_expr = get_financial_year_sql_expression("jute_mr_date")

    # Determine the current financial year from mr_date
    if mr_date.month >= 4:  # April onwards
        fy_start_year = mr_date.year
    else:  # January to March
        fy_start_year = mr_date.year - 1

    fy_string = f"{fy_start_year % 100:02d}-{(fy_start_year + 1) % 100:02d}"

    exclude_clause = ""
    params = {
        "party_id": party_id,
        "fy_string": fy_string,
        "status_approved": MR_STATUS_APPROVED,
        "mr_date": mr_date,
        "mr_no": int(branch_mr_no) if branch_mr_no is not None else 0,
        "mr_id": int(exclude_mr_id) if exclude_mr_id else MR_ORDER_KEY_UNASSIGNED,
    }

    if exclude_mr_id:
        exclude_clause = "AND jute_mr_id != :exclude_mr_id"
        params["exclude_mr_id"] = exclude_mr_id

    query = text(f"""
        SELECT COALESCE(SUM(COALESCE(total_amount, 0)), 0) AS cumulative_value
        FROM jute_mr
        WHERE party_id = :party_id
          AND status_id = :status_approved
          AND jute_mr_date IS NOT NULL
          AND {fy_expr} = :fy_string
          {exclude_clause}
          AND {MR_BEFORE_SQL}
    """)

    result = db.execute(query, params).fetchone()
    return float(result[0]) if result and result[0] else 0.0


def calculate_mr_amounts(db: Session, mr_id: int) -> dict:
    """
    Calculate total_amount, claim_amount for an MR from its line items.
    
    Formulas:
    - total_amount = sum of (accepted_weight / 100) * rate  [weight in KG, rate per quintal]
    - claim_amount = sum of (accepted_weight / 100) * claim_rate
    
    Returns:
        dict with total_amount and claim_amount
    """
    query = text("""
        SELECT 
            COALESCE(SUM((COALESCE(accepted_weight, actual_weight, 0) / 100) * COALESCE(rate, 0)), 0) AS total_amount,
            COALESCE(SUM((COALESCE(accepted_weight, actual_weight, 0) / 100) * COALESCE(claim_rate, 0)), 0) AS claim_amount
        FROM jute_mr_li
        WHERE jute_mr_id = :mr_id
          AND (active = 1 OR active IS NULL)
    """)
    
    result = db.execute(query, {"mr_id": mr_id}).fetchone()

    if result:
        return {
            "total_amount": float(result.total_amount) if result.total_amount else 0.0,
            "claim_amount": float(result.claim_amount) if result.claim_amount else 0.0,
        }
    return {"total_amount": 0.0, "claim_amount": 0.0}


def calculate_tds_amount(cumulative_previous: float, current_total: float) -> tuple:
    """
    Calculate TDS amount based on cumulative MR value and threshold.

    TDS Logic:
    - TDS is 0.1% of the gross total_amount (194Q incremental logic; reviewed 2026-09-03)
    - TDS applies only if cumulative MR value for the party in FY exceeds Rs. 50 lacs
    - If crossing threshold with current bill, TDS applies only on amount exceeding threshold
    
    Args:
        cumulative_previous: Sum of the party's approved MRs that come BEFORE this one
            in the FY (get_cumulative_mr_value_for_party_in_fy)
        current_total: Total amount of the current MR
    
    Returns:
        tuple: (tds_amount, tds_applicable_amount, is_tds_applicable)
    """
    cumulative_after = cumulative_previous + current_total
    
    # If cumulative (including current) is below threshold, no TDS
    if cumulative_after <= TDS_THRESHOLD:
        return (0.0, 0.0, False)
    
    # Calculate the amount on which TDS applies
    if cumulative_previous >= TDS_THRESHOLD:
        # Already crossed threshold - TDS on full current amount
        tds_applicable_amount = current_total
    else:
        # Crossing threshold with this MR - TDS only on amount exceeding threshold
        tds_applicable_amount = cumulative_after - TDS_THRESHOLD
    
    # Whole rupees, 50 paise and up rounds up (owner ruling 2026-10-09). Decimal so a
    # .5 never lands on the wrong side through float noise.
    tds_amount = float(
        (Decimal(str(tds_applicable_amount)) * Decimal(str(TDS_RATE))).quantize(Decimal("1"), ROUND_HALF_UP)
    )
    
    return (tds_amount, tds_applicable_amount, True)


def derive_mr_tds(db: Session, *, party_id, mr_date, mr_id: int, branch_mr_no, total_amount: float) -> dict:
    """194Q TDS for one MR, derived the one way approve and bill pass share: from the
    party's approved MRs that come BEFORE this MR in the FY (see
    get_cumulative_mr_value_for_party_in_fy for the order), then calculate_tds_amount.

    mr_date / branch_mr_no are the MR's own keys — at approve the ones being written,
    at bill pass the stored ones — so both paths place the MR at the same spot and a
    bill pass save gives back the TDS approve computed (same lines, same earlier MRs).
    No party or no MR date (nothing to place the MR by) = no TDS.

    Returns a dict: tds_amount, tds_applicable_amount, is_tds_applicable,
    cumulative_previous, cumulative_after.
    """
    total_amount = float(total_amount or 0)
    tds_amount, tds_applicable_amount, is_tds_applicable, cumulative_previous = 0.0, 0.0, False, 0.0
    if party_id and mr_date:
        cumulative_previous = get_cumulative_mr_value_for_party_in_fy(
            db, party_id, mr_date, exclude_mr_id=mr_id, branch_mr_no=branch_mr_no
        )
        tds_amount, tds_applicable_amount, is_tds_applicable = calculate_tds_amount(
            cumulative_previous, total_amount
        )
    return {
        "tds_amount": tds_amount,
        "tds_applicable_amount": tds_applicable_amount,
        "is_tds_applicable": is_tds_applicable,
        "cumulative_previous": cumulative_previous,
        "cumulative_after": cumulative_previous + total_amount,
    }


_LINE_TOTAL_PRICE_SQL = text("""
    UPDATE jute_mr_li
    SET total_price = ROUND((COALESCE(accepted_weight, actual_weight, 0) / 100) * COALESCE(rate, 0), 2)
    WHERE jute_mr_id = :mr_id AND (active = 1 OR active IS NULL)
""")


def recompute_mr_money(db: Session, mr_id: int, tds_amount=None) -> dict:
    """One writer for the approved-weight money basis (decision D5/P5-1/P5-2):
    line total_price + header total_amount/claim_amount/roundoff/net_total.
    tds_amount None = keep the stored TDS (pending_mr); a float = store it (approve/bill pass)."""
    db.execute(_LINE_TOTAL_PRICE_SQL, {"mr_id": int(mr_id)})
    amounts = calculate_mr_amounts(db, mr_id)
    total_amount = round(amounts["total_amount"], 2)
    claim_amount = round(amounts["claim_amount"], 2)
    if tds_amount is None:
        row = db.execute(text("SELECT COALESCE(tds_amount, 0) AS t FROM jute_mr WHERE jute_mr_id = :mr_id"), {"mr_id": int(mr_id)}).fetchone()
        tds_amount = float(row._mapping.get("t") or 0) if row else 0.0
    roundoff, net_total = compute_jute_totals(total_amount, claim_amount, tds_amount)
    db.execute(text("""
        UPDATE jute_mr
        SET total_amount = :total_amount, claim_amount = :claim_amount,
            tds_amount = :tds_amount, roundoff = :roundoff, net_total = :net_total
        WHERE jute_mr_id = :mr_id
    """), {"mr_id": int(mr_id), "total_amount": total_amount, "claim_amount": claim_amount,
           "tds_amount": round(float(tds_amount), 2), "roundoff": roundoff, "net_total": net_total})
    # Pending / Approve / bill pass reach here with or without an MR Save in between;
    # an MR that was never saved still has a NULL/0 header MR Weight (TKT-000207).
    sync_mr_weight_from_lines(db, mr_id, only_if_blank=True)
    return {"total_amount": total_amount, "claim_amount": claim_amount,
            "tds_amount": round(float(tds_amount), 2), "roundoff": roundoff, "net_total": net_total}


def calculate_roundoff(amount: float) -> float:
    """
    Calculate roundoff to nearest rupee.
    
    Returns the roundoff adjustment (can be positive or negative).
    """
    rounded = round(amount)
    return round(rounded - amount, 2)


@router.post("/open_mr")
def open_mr(
    request: Request,
    body: MRApprovalRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Open MR - Changes status from Draft (21) to Open (1).
    Validates that party_id and party_branch_id are set.
    """
    try:
        mr_id = int(body.mr_id)
        user_id = token_data.get("user_id")

        if not user_id:
            raise HTTPException(status_code=401, detail="User not authenticated")

        # Get current MR details
        check_query = text("""
            SELECT jute_mr_id, status_id, party_id, party_branch_id
            FROM jute_mr
            WHERE jute_mr_id = :mr_id
        """)
        mr_row = db.execute(check_query, {"mr_id": mr_id}).fetchone()

        if not mr_row:
            raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")

        mr = dict(mr_row._mapping)
        current_status = mr.get("status_id")
        party_id = mr.get("party_id")
        party_branch_id = mr.get("party_branch_id")

        # Validate current status - can only open from Draft
        if current_status != MR_STATUS_DRAFT:
            raise HTTPException(status_code=400, detail="MR can only be opened from Draft status")

        # Validate party_id is set
        if not party_id:
            raise HTTPException(
                status_code=400,
                detail="Cannot open MR: Party must be selected before opening."
            )

        # Validate party_branch_id is set
        if not party_branch_id:
            raise HTTPException(
                status_code=400,
                detail="Cannot open MR: Party branch must be selected before opening. Please add a branch for this party in Party Master if none exist."
            )

        now = now_ist()

        # Update status to Open
        update_query = text("""
            UPDATE jute_mr
            SET status_id = :new_status,
                updated_by = :updated_by,
                updated_date_time = :updated_date_time
            WHERE jute_mr_id = :mr_id
        """)

        db.execute(update_query, {
            "mr_id": mr_id,
            "new_status": MR_STATUS_OPEN,
            "updated_by": user_id,
            "updated_date_time": now,
        })

        # PO settlement: an opened MR is a receipt against its PO.
        settle_after_mr_change(db, mr_id, user_id=user_id, trigger_ref=f"MR:{mr_id} open_mr")

        db.commit()

        return {
            "success": True,
            "message": "MR opened successfully",
            "mr_id": mr_id,
            "status_id": MR_STATUS_OPEN,
            "status_name": "Open",
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error opening MR")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pending_mr")
def pending_mr(
    request: Request,
    body: MRApprovalRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Set MR to Pending status (13).
    This is a terminal state on this screen - handled by external system.
    Can only be done from Open status.
    """
    try:
        mr_id = int(body.mr_id)
        user_id = token_data.get("user_id")

        if not user_id:
            raise HTTPException(status_code=401, detail="User not authenticated")

        # Get current MR details
        check_query = text("""
            SELECT jute_mr_id, status_id, party_id, party_branch_id
            FROM jute_mr
            WHERE jute_mr_id = :mr_id
        """)
        mr_row = db.execute(check_query, {"mr_id": mr_id}).fetchone()

        if not mr_row:
            raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")

        mr = dict(mr_row._mapping)
        current_status = mr.get("status_id")

        # Validate current status. Pending is a top-of-hierarchy action shown
        # by the UI only to the highest-level approver, so the MR may be Open
        # (single-level) or already walking the approval levels (Pending Approval).
        if current_status not in (MR_STATUS_OPEN, MR_STATUS_PENDING_APPROVAL):
            raise HTTPException(
                status_code=400,
                detail="MR can only be set to Pending from Open or Pending Approval status",
            )
        require_mr_party_complete(mr.get("party_id"), mr.get("party_branch_id"))

        recompute_mr_money(db, mr_id)  # Pending is the hand-off snapshot: carry real totals (P5-2)

        now = now_ist()

        # Update status to Pending
        update_query = text("""
            UPDATE jute_mr
            SET status_id = :new_status,
                updated_by = :updated_by,
                updated_date_time = :updated_date_time
            WHERE jute_mr_id = :mr_id
        """)
        
        db.execute(update_query, {
            "mr_id": mr_id,
            "new_status": MR_STATUS_PENDING,
            "updated_by": user_id,
            "updated_date_time": now,
        })

        # PO settlement: keep the PO in step with the MR's status.
        settle_after_mr_change(db, mr_id, user_id=user_id, trigger_ref=f"MR:{mr_id} pending_mr")

        db.commit()

        return {
            "success": True,
            "message": "MR set to Pending",
            "mr_id": mr_id,
            "status_id": MR_STATUS_PENDING,
            "status_name": "Pending",
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error setting MR to pending")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/approve_mr")
def approve_mr(
    request: Request,
    body: MRApprovalRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Approve MR - Changes status to Approved (3) or advances approval level.
    Can only be done from Open (1) or Pending Approval (20) status.

    If party_branch_id is provided in the request, it will be used to update the MR
    before approval (handles case where branch was selected but not saved).

    For final approval:
    - mr_date is MANDATORY (YYYY-MM-DD format)
    - branch_mr_no will be auto-generated based on branch + financial year

    When menu_id is provided, uses the shared approval utility for multi-level
    approval hierarchy support. When menu_id is omitted, falls back to direct
    approval (backward compatible).
    """
    try:
        mr_id = int(body.mr_id)
        branch_id = int(body.branch_id)
        user_id = token_data.get("user_id")
        request_party_branch_id = body.party_branch_id
        request_mr_date = body.mr_date
        menu_id = body.menu_id

        if not user_id:
            raise HTTPException(status_code=401, detail="User not authenticated")

        # Get current MR details
        check_query = text("""
            SELECT jute_mr_id, status_id, party_id, party_branch_id, branch_id,
                   branch_mr_no, jute_mr_date, bill_pass_no
            FROM jute_mr
            WHERE jute_mr_id = :mr_id
        """)
        mr_row = db.execute(check_query, {"mr_id": mr_id}).fetchone()

        if not mr_row:
            raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")

        mr = dict(mr_row._mapping)
        current_status = mr.get("status_id")
        party_id = mr.get("party_id")
        db_party_branch_id = mr.get("party_branch_id")
        db_branch_id = mr.get("branch_id")
        db_branch_mr_no = mr.get("branch_mr_no")
        db_bill_pass_no = mr.get("bill_pass_no")

        # Use request party_branch_id if provided, otherwise use database value
        effective_party_branch_id = (
            request_party_branch_id
            if request_party_branch_id is not None
            else db_party_branch_id
        )

        # Validate current status
        if current_status not in [MR_STATUS_OPEN, MR_STATUS_PENDING_APPROVAL]:
            raise HTTPException(
                status_code=400,
                detail="MR can only be approved from Open or Pending Approval status",
            )

        # Validate party AND party_branch (decision D4) using the effective value
        require_mr_party_complete(party_id, effective_party_branch_id)

        now = now_ist()

        # Determine approval status via shared utility or direct approval
        if menu_id is not None:
            # Use shared approval utility for multi-level approval
            approval_result = process_approval(
                doc_id=mr_id,
                user_id=user_id,
                menu_id=menu_id,
                db=db,
                get_doc_fn=get_mr_with_approval_info,
                update_status_fn=update_mr_status,
                id_param_name="jute_mr_id",
                doc_name="MR",
                document_amount=None,  # Jute never checks values
                # Defer the commit: on final approval this endpoint then
                # generates branch_mr_no / bill_pass_no / amounts and commits
                # once, so the status change and those writes stay atomic. If
                # that second phase raises, the whole approval rolls back.
                commit=False,
            )
            new_status = approval_result["new_status_id"]
            is_final_approval = new_status == MR_STATUS_APPROVED
        else:
            # Backward compatible: direct approval (no approval hierarchy)
            is_final_approval = True
            new_status = MR_STATUS_APPROVED

        # For final approval, mr_date is MANDATORY
        if is_final_approval:
            if not request_mr_date:
                raise HTTPException(
                    status_code=400,
                    detail="MR Date is required for final approval. Please enter the MR date.",
                )

            # Parse and validate mr_date
            try:
                parsed_mr_date = datetime.strptime(request_mr_date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid MR date format. Please use YYYY-MM-DD format.",
                )

            # Generate MR number based on branch + financial year if not already set
            if not db_branch_mr_no:
                new_branch_mr_no = get_next_mr_no_for_fy(db, db_branch_id, parsed_mr_date)
            else:
                new_branch_mr_no = db_branch_mr_no
        else:
            # Not final approval - mr_date is optional
            parsed_mr_date = None
            new_branch_mr_no = db_branch_mr_no
            if request_mr_date:
                try:
                    parsed_mr_date = datetime.strptime(request_mr_date, "%Y-%m-%d").date()
                except ValueError:
                    pass  # Ignore invalid date for non-final approval

        # Build update query for business data fields
        # Note: status_id and approval_level are already set by process_approval()
        # when menu_id is provided. For backward compat (no menu_id), we set status here.
        update_fields = ["updated_by = :updated_by", "updated_date_time = :updated_date_time"]
        update_params = {
            "mr_id": mr_id,
            "updated_by": user_id,
            "updated_date_time": now,
        }

        # When menu_id is not provided, we must set the status ourselves
        if menu_id is None:
            update_fields.append("status_id = :new_status")
            update_params["new_status"] = new_status

        # Add party_branch_id if changed
        if request_party_branch_id is not None and request_party_branch_id != db_party_branch_id:
            update_fields.append("party_branch_id = :party_branch_id")
            update_params["party_branch_id"] = request_party_branch_id

        # Add mr_date if provided for final approval
        if is_final_approval and parsed_mr_date:
            update_fields.append("jute_mr_date = :jute_mr_date")
            update_params["jute_mr_date"] = parsed_mr_date

        # Add branch_mr_no if generated
        if is_final_approval and new_branch_mr_no and new_branch_mr_no != db_branch_mr_no:
            update_fields.append("branch_mr_no = :branch_mr_no")
            update_params["branch_mr_no"] = new_branch_mr_no

        # For final approval, also set bill_pass_date = mr_date and generate bill_pass_no
        new_bill_pass_no = None
        calculated_amounts = {}
        if is_final_approval and parsed_mr_date:
            # bill_pass_date = jute_mr_date (same date)
            update_fields.append("bill_pass_date = :bill_pass_date")
            update_params["bill_pass_date"] = parsed_mr_date

            # Generate bill_pass_no based on branch + financial year — unless
            # this MR already has one (re-approval after Reopen keeps the number, P2-2)
            if not db_bill_pass_no:
                new_bill_pass_no = get_next_bill_pass_no_for_fy(db, db_branch_id, parsed_mr_date)
            else:
                new_bill_pass_no = db_bill_pass_no
            update_fields.append("bill_pass_no = :bill_pass_no")
            update_params["bill_pass_no"] = new_bill_pass_no

            # Calculate amounts from line items (for the TDS-cumulative call below)
            mr_amounts = calculate_mr_amounts(db, mr_id)
            total_amount = mr_amounts["total_amount"]

            # 194Q TDS from the party's approved MRs that come BEFORE this one in the
            # FY — placed by the MR date and MR number this approval writes, so a bill
            # pass save later derives the same TDS (owner ruling 2026-10-03).
            tds = derive_mr_tds(
                db, party_id=party_id, mr_date=parsed_mr_date, mr_id=mr_id,
                branch_mr_no=new_branch_mr_no, total_amount=total_amount,
            )

            # Single writer for total_amount/claim_amount/roundoff/net_total/tds_amount
            # (also writes jute_mr_li.total_price) — status_id/other header fields are
            # written by the UPDATE below, in the same transaction.
            money = recompute_mr_money(db, mr_id, tds_amount=tds["tds_amount"])

            calculated_amounts = {
                "total_amount": money["total_amount"],
                "claim_amount": money["claim_amount"],
                "roundoff": money["roundoff"],
                "net_total": money["net_total"],
                "tds_amount": money["tds_amount"],
                "tds_applicable_amount": tds["tds_applicable_amount"],
                "is_tds_applicable": tds["is_tds_applicable"],
                "cumulative_previous": tds["cumulative_previous"],
                "cumulative_after": tds["cumulative_after"],
                "tds_threshold": TDS_THRESHOLD,
            }

        update_query = text(f"""
            UPDATE jute_mr
            SET {', '.join(update_fields)}
            WHERE jute_mr_id = :mr_id
        """)

        db.execute(update_query, update_params)
        # PO settlement: an approved MR may complete the PO (po-settlement-spec.md §4).
        settle_after_mr_change(db, mr_id, user_id=user_id, trigger_ref=f"MR:{mr_id} approve_mr")
        db.commit()

        # Fetch company and branch prefix for formatted document numbers
        mr_num = None
        bill_pass_num = None
        prefix_result = db.execute(
            text("""
                SELECT cm.co_prefix, bm.branch_prefix
                FROM branch_mst bm
                INNER JOIN co_mst cm ON cm.co_id = bm.co_id
                WHERE bm.branch_id = :branch_id
            """),
            {"branch_id": db_branch_id},
        ).fetchone()

        co_prefix = prefix_result.co_prefix if prefix_result else None
        branch_prefix = prefix_result.branch_prefix if prefix_result else None

        if new_branch_mr_no and parsed_mr_date:
            mr_num = format_jute_mr_number(new_branch_mr_no, parsed_mr_date, co_prefix, branch_prefix)
        if new_bill_pass_no and parsed_mr_date:
            bill_pass_num = format_jute_bill_pass_number(
                new_bill_pass_no, parsed_mr_date, co_prefix, branch_prefix
            )

        response = {
            "success": True,
            "message": "MR approved successfully" if is_final_approval else "MR moved to next approval level",
            "mr_id": mr_id,
            "status_id": new_status,
            "status_name": "Approved" if is_final_approval else "Pending Approval",
            "branch_mr_no": new_branch_mr_no,
            "mr_num": mr_num,
            "jute_mr_date": str(parsed_mr_date) if parsed_mr_date else None,
            "bill_pass_no": new_bill_pass_no,
            "bill_pass_num": bill_pass_num,
            "bill_pass_date": str(parsed_mr_date) if parsed_mr_date else None,
            "calculated_amounts": calculated_amounts,
        }

        return response

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error approving MR")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reject_mr")
def reject_mr(
    request: Request,
    body: MRApprovalRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Reject MR - Changes status to Rejected (4).
    Can only be done from Open (1) or Pending Approval (20) status.

    When menu_id is provided, uses the shared rejection utility for
    level-based permission checks. When omitted, falls back to direct
    rejection (backward compatible).
    """
    try:
        mr_id = int(body.mr_id)
        user_id = token_data.get("user_id")
        reason = body.reason or ""
        menu_id = body.menu_id

        if not user_id:
            raise HTTPException(status_code=401, detail="User not authenticated")

        pre = db.execute(text("SELECT jute_mr_id, status_id FROM jute_mr WHERE jute_mr_id = :mr_id"), {"mr_id": mr_id}).fetchone()
        if not pre:
            raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")
        assert_no_downstream(db, mr_id)

        if menu_id is not None:
            # Use shared rejection utility for level-based permission checks
            process_rejection(
                doc_id=mr_id,
                user_id=user_id,
                menu_id=menu_id,
                db=db,
                get_doc_fn=get_mr_with_approval_info,
                update_status_fn=update_mr_status,
                id_param_name="jute_mr_id",
                doc_name="MR",
                reason=reason,
            )
        else:
            # Backward compatible: direct rejection without hierarchy checks
            check_query = text("""
                SELECT jute_mr_id, status_id
                FROM jute_mr
                WHERE jute_mr_id = :mr_id
            """)
            mr_row = db.execute(check_query, {"mr_id": mr_id}).fetchone()

            if not mr_row:
                raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")

            mr = dict(mr_row._mapping)
            current_status = mr.get("status_id")

            if current_status not in [MR_STATUS_OPEN, MR_STATUS_PENDING_APPROVAL]:
                raise HTTPException(
                    status_code=400,
                    detail="MR can only be rejected from Open or Pending Approval status",
                )

            now = now_ist()
            update_query = text("""
                UPDATE jute_mr
                SET status_id = :new_status,
                    approval_level = NULL,
                    updated_by = :updated_by,
                    updated_date_time = :updated_date_time
                WHERE jute_mr_id = :mr_id
            """)
            db.execute(update_query, {
                "mr_id": mr_id,
                "new_status": MR_STATUS_REJECTED,
                "updated_by": user_id,
                "updated_date_time": now,
            })
            db.commit()

        # PO settlement: a rejected MR is no longer a receipt against its PO (both paths).
        settle_after_mr_change(db, mr_id, user_id=user_id, trigger_ref=f"MR:{mr_id} reject_mr")
        db.commit()

        # Append rejection reason to remarks (both paths)
        if reason:
            remarks_query = text("""
                UPDATE jute_mr
                SET remarks = CONCAT(COALESCE(remarks, ''), ' [Rejected: ', :reason, ']')
                WHERE jute_mr_id = :mr_id
            """)
            db.execute(remarks_query, {"mr_id": mr_id, "reason": reason})
            db.commit()

        return {
            "success": True,
            "message": "MR rejected",
            "mr_id": mr_id,
            "status_id": MR_STATUS_REJECTED,
            "status_name": "Rejected",
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error rejecting MR")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cancel_mr")
def cancel_mr(
    request: Request,
    body: MRApprovalRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Cancel MR - Changes status to Cancelled (6).
    Can only be done from Open (1) status.
    """
    try:
        mr_id = int(body.mr_id)
        user_id = token_data.get("user_id")

        if not user_id:
            raise HTTPException(status_code=401, detail="User not authenticated")

        # Get current MR details
        check_query = text("""
            SELECT jute_mr_id, status_id
            FROM jute_mr
            WHERE jute_mr_id = :mr_id
        """)
        mr_row = db.execute(check_query, {"mr_id": mr_id}).fetchone()
        
        if not mr_row:
            raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")

        mr = dict(mr_row._mapping)
        current_status = mr.get("status_id")

        # Validate current status - can only cancel from Open
        if current_status != MR_STATUS_OPEN:
            raise HTTPException(status_code=400, detail="Only Open MRs can be cancelled")

        assert_no_downstream(db, mr_id)

        now = now_ist()

        # Update status to Cancelled
        update_query = text("""
            UPDATE jute_mr
            SET status_id = :new_status,
                updated_by = :updated_by,
                updated_date_time = :updated_date_time
            WHERE jute_mr_id = :mr_id
        """)
        
        db.execute(update_query, {
            "mr_id": mr_id,
            "new_status": MR_STATUS_CANCELLED,
            "updated_by": user_id,
            "updated_date_time": now,
        })

        # PO settlement: a cancelled MR is no longer a receipt against its PO.
        settle_after_mr_change(db, mr_id, user_id=user_id, trigger_ref=f"MR:{mr_id} cancel_mr")

        db.commit()

        return {
            "success": True,
            "message": "MR cancelled",
            "mr_id": mr_id,
            "status_id": MR_STATUS_CANCELLED,
            "status_name": "Cancelled",
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error cancelling MR")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reopen_mr")
def reopen_mr(
    request: Request,
    body: MRReopenRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Decision D2: role-gated rollback of a Pending (13) / Approved (3) MR to Open (1).

    - edit access (access_type_id >= 4) on the MR menu is required
    - refused if the MR is a chain hop or has chain children (roll back in juteTransfer first)
    - clears what approval created (bill_pass_no/date, totals, TDS, approval_level); branch_mr_no is kept
    - target 'qc' also resets qc_check so Material Inspection can be redone
    - never blocks on issues: returns a warning so the user runs 'Sync issued weights' afterwards

    TKT-000229 (2026-10-16): target 'qc' is also accepted on an OPEN (1) MR whose QC is
    done (qc_check = 1). That is a pure QC rollback: qc_check -> 0, the MR stays Open and
    is listed again in Jute Quality Check; nothing else is cleared and the PO settlement
    is not touched (the MR is still a receipt). 'open' from Open is refused (400).
    """
    try:
        mr_id = int(body.mr_id)
        branch_id = int(body.branch_id)
        user_id = token_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="User not authenticated")
        if body.target not in ("open", "qc"):
            raise HTTPException(status_code=400, detail="target must be 'open' or 'qc'")

        row = db.execute(text("SELECT jute_mr_id, status_id, branch_id, qc_check FROM jute_mr WHERE jute_mr_id = :mr_id"), {"mr_id": mr_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")
        qc_rollback_from_open = row.status_id == MR_STATUS_OPEN and body.target == "qc"
        if qc_rollback_from_open:
            if int(row._mapping.get("qc_check") or 0) != 1:
                raise HTTPException(status_code=400, detail="QC has not been completed on this MR, so there is nothing to roll back to")
        elif row.status_id not in MR_FROZEN_STATUSES:
            if row.status_id == MR_STATUS_OPEN:
                raise HTTPException(status_code=400, detail="MR is already Open; use 'Roll back to QC' to redo the inspection")
            raise HTTPException(status_code=400, detail="Only Pending or Approved MRs can be reopened")
        if not _user_has_edit_access(user_id, branch_id, body.menu_id, db):
            raise HTTPException(status_code=403, detail="Reopen needs edit access on the Jute MR menu")

        chain = mr_chain_state(db, mr_id)
        if chain["is_hop"] or chain["child_count"]:
            raise HTTPException(status_code=409, detail="MR has been transferred in a vertical chain. Roll back from juteTransfer first.")

        issues = live_issue_summary(db, mr_id)
        if qc_rollback_from_open:
            # Already Open: only the QC flag goes back; money / approval fields were never set.
            sets = ["qc_check = 0", "updated_by = :updated_by", "updated_date_time = :now"]
        else:
            sets = [
                "status_id = :open", "approval_level = NULL",
                "bill_pass_no = NULL", "bill_pass_date = NULL",
                "total_amount = NULL", "claim_amount = NULL", "tds_amount = NULL", "roundoff = NULL", "net_total = NULL",
                "updated_by = :updated_by", "updated_date_time = :now",
            ]
            if body.target == "qc":
                sets.append("qc_check = 0")
        db.execute(text(f"UPDATE jute_mr SET {', '.join(sets)} WHERE jute_mr_id = :mr_id"),
                   {"mr_id": mr_id, "open": MR_STATUS_OPEN, "updated_by": user_id, "now": now_ist()})
        if not qc_rollback_from_open:
            # PO settlement: a reopened MR is a receipt again (an Open MR already was one).
            settle_after_mr_change(db, mr_id, user_id=user_id, trigger_ref=f"MR:{mr_id} reopen_mr")
        db.commit()

        warning = None
        if issues["count"]:
            warning = (f"{issues['count']} issue entries ({issues['weight']:.0f} kg) already drawn against this MR. "
                       "After you re-finalise, run 'Sync issued weights' on the Jute MR Wise Issue Detail report "
                       "so issued bales carry the updated weights.")
        return {"success": True, "mr_id": mr_id, "status_id": MR_STATUS_OPEN, "target": body.target,
                "from_status_id": row.status_id, "issues": issues, "warning": warning}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error reopening MR")
        raise HTTPException(status_code=500, detail=str(e))


def _set_mr_returned(db: Session, mr_id: int, user_id, returned: bool, reason: str = "") -> dict:
    """Shared body of return_mr / unreturn_mr: flips jute_mr.is_returned only.

    Returned is a FLAG (2026-10-03), not a status: the MR keeps its normal status,
    so it still goes through approval, bill pass, issue and transfer. Allowed while
    Open (1) or Pending (13)."""
    row = db.execute(text("SELECT jute_mr_id, status_id, is_returned FROM jute_mr WHERE jute_mr_id = :mr_id"),
                     {"mr_id": mr_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"MR with id {mr_id} not found")
    if row.status_id not in (MR_STATUS_OPEN, MR_STATUS_PENDING):
        raise HTTPException(status_code=400, detail="Only Open or Pending MRs can be marked Returned")
    if bool(row.is_returned) == returned:
        raise HTTPException(status_code=400, detail="MR is already marked Returned" if returned else "MR is not marked Returned")
    db.execute(text("""
        UPDATE jute_mr
        SET is_returned = :flag,
            remarks = CASE WHEN :reason = '' THEN remarks
                           ELSE CONCAT(COALESCE(remarks, ''), :tag, :reason, ']') END,
            updated_by = :updated_by, updated_date_time = :now
        WHERE jute_mr_id = :mr_id
    """), {"mr_id": mr_id, "flag": 1 if returned else 0, "reason": reason,
           "tag": " [Returned: " if returned else " [Return undone: ",
           "updated_by": user_id, "now": now_ist()})
    # PO settlement: a returned lorry's receipt weight is its retained line weight.
    settle_after_mr_change(db, mr_id, user_id=user_id,
                           trigger_ref=f"MR:{mr_id} {'return_mr' if returned else 'unreturn_mr'}")
    db.commit()
    return {"success": True, "mr_id": mr_id, "status_id": row.status_id, "is_returned": returned}


@router.post("/return_mr")
def return_mr(
    request: Request,
    body: MRApprovalRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Lorry came in but was sent back to the supplier → is_returned = 1 (status unchanged).
    The retained material (the MR lines) is still stock, payable and transferable."""
    try:
        user_id = token_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="User not authenticated")
        return _set_mr_returned(db, int(body.mr_id), user_id, True, (body.reason or "").strip())
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/unreturn_mr")
def unreturn_mr(
    request: Request,
    body: MRApprovalRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Clear the Returned flag (Open or Pending MRs only)."""
    try:
        user_id = token_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="User not authenticated")
        return _set_mr_returned(db, int(body.mr_id), user_id, False, (body.reason or "").strip())
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

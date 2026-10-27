"""
Jute Issue API endpoints.
Provides endpoints for viewing and managing jute issue records.
Jute issue records track yarn issued against MR line items.
"""

from fastapi import Depends, Request, HTTPException, APIRouter
from typing import Optional, List
from pydantic import BaseModel
from datetime import date, datetime
from src.common.utils import now_ist
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProcurement.query import (
    get_jute_issue_table_query,
    get_jute_issue_table_count_query,
    get_jute_issue_create_setup_query,
    get_jute_stock_outstanding_query,
    get_jute_stock_outstanding_by_item_query,
    get_jute_waste_stock_outstanding_query,
    get_jute_waste_entry_for_issue_query,
    get_jute_waste_entries_for_allocation_query,
    get_jute_mr_line_for_issue_query,
    get_jute_issues_by_date_query,
    get_yarn_types_query,
    get_jute_mr_lines_and_issues_for_sync_query,
)
from src.juteProcurement.totals import round_kg
from src.juteProcurement._excel_utils import (
    parse_iso_date,
    build_excel_response,
    enforce_row_cap,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class JuteIssueCreate(BaseModel):
    """Model for creating a jute issue line item.

    A line draws on EXACTLY ONE stock source:
      * `jute_mr_li_id` — purchased jute, one MR line;
      * `waste_item_id` — process waste picked as ONE grouped item balance
        (TKT-000147); the endpoint fans it out FIFO across that item's open
        Waste Entry bookings and writes one row per booking consumed;
      * `waste_entry_id` — one specific Waste Entry booking. The picker no
        longer offers these, but the field stays so existing lines (and any
        integration built against the per-entry API) keep working.

    All three are optional at the schema level and the endpoint rejects
    zero-or-many — a Pydantic validator cannot produce the project's standard
    400 shape, and the check is one `if`.
    """
    branch_id: int
    issue_date: date
    jute_mr_li_id: Optional[int] = None
    waste_entry_id: Optional[int] = None
    waste_item_id: Optional[int] = None
    yarn_type_id: int
    item_id: int
    quantity: float
    weight: float
    unit_conversion: Optional[str] = None
    issue_value: Optional[float] = None


class JuteIssueUpdate(BaseModel):
    """Model for updating a jute issue line item."""
    yarn_type_id: Optional[int] = None
    item_id: Optional[int] = None
    quantity: Optional[float] = None
    weight: Optional[float] = None
    unit_conversion: Optional[str] = None
    issue_value: Optional[float] = None


class JuteIssueBulkCreate(BaseModel):
    """Model for creating multiple jute issue line items."""
    branch_id: int
    issue_date: date
    items: List[JuteIssueCreate]


class JuteIssueStatusUpdate(BaseModel):
    """Model for updating issue status (open, approve, reject)."""
    branch_id: Optional[int] = None
    issue_date: Optional[date] = None
    status_id: int
    issue_ids: Optional[List[int]] = None  # Specific issue IDs to update (takes precedence)


# =============================================================================
# ENDPOINTS
# =============================================================================

JUTE_ISSUE_DOWNLOAD_COLUMNS = [
    ("Issue Date", "issue_date"),
    ("Branch", "branch_name"),
    ("Total Entries", "total_entries"),
    ("Total Weight", "total_weight"),
    ("Approved Count", "approved_count"),
    ("Status", "status"),
]


# Both stock ledgers are FLOAT columns fed from DECIMAL sources, so an exact
# full-balance issue can land a hair over its own balance. Same slack the waste
# path has always used.
_STOCK_TOLERANCE = 0.001


def _resolve_waste_rate_for_issue(
    db: Session,
    waste_entry_id: int,
    co_id: int,
    branch_id: int,
    issue_date,
    weight: float,
    exclude_issue_id: Optional[int] = None,
) -> float:
    """Validate a waste line against its source booking and return its rate.

    One round trip covers every guard the MR path gets from the picker plus the
    ones only the server can enforce:
      * the entry exists, is not soft-deleted, and belongs to this company;
      * it belongs to the branch being issued from (no cross-branch draw);
      * it was booked on or before the issue date (no issuing tomorrow's waste);
      * the weight asked for still fits the outstanding balance.

    `exclude_issue_id` adds back the weight already booked by the line being
    edited, so re-saving the same line at the same weight is not read as a
    double draw against its own quantity.

    The rate itself comes from jute_waste_rate_mst via the same builder the
    stock picker uses, so the value previewed and the value stored agree. A
    waste item with no rate on file resolves to 0 — deliberately issuable, since
    the physical movement matters more than the valuation.
    """
    row = db.execute(
        get_jute_waste_entry_for_issue_query(),
        {
            "waste_entry_id": int(waste_entry_id),
            "co_id": int(co_id),
            "issue_date": issue_date,
        },
    ).fetchone()

    if not row:
        raise HTTPException(status_code=400, detail="Invalid waste entry ID")
    if row.branch_id is not None and int(row.branch_id) != int(branch_id):
        raise HTTPException(
            status_code=400, detail="Waste entry belongs to a different branch"
        )
    if row.inward_date and str(row.inward_date) > str(issue_date):
        raise HTTPException(
            status_code=400, detail="Waste entry was booked after the issue date"
        )

    available = float(row.bal_weight or 0)
    if exclude_issue_id is not None:
        prior = db.execute(
            text(
                "SELECT weight FROM jute_issue "
                "WHERE jute_issue_id = :id AND (status_id IS NULL OR status_id <> 4)"
            ),
            {"id": exclude_issue_id},
        ).fetchone()
        available += float(prior.weight or 0) if prior else 0.0

    # 0.001 tolerance: waste_qty is DECIMAL(10,3) but jute_issue.weight is a
    # FLOAT, so an exact full-balance issue can round a hair over.
    if weight and float(weight) > available + _STOCK_TOLERANCE:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot issue more than the available waste balance ({available:.3f} kg)",
        )

    return float(row.actual_rate or 0)


# One row-to-write of a grouped waste pick. `source` is 'PROD' (a Waste Entry
# booking, key = waste_entry_id) or 'MR' (a purchased-waste MR line, key =
# jute_mr_li_id); an issue row carries exactly one of the two keys.
WasteSlice = tuple[str, int, float, float]  # (source, key_id, weight, rate)

WASTE_SOURCE_PROD = "PROD"
WASTE_SOURCE_MR = "MR"


def _row_flag(row, name: str) -> bool:
    """True when a 0/1 SQL flag column on `row` is set.

    Tolerates rows that predate the column (they read as unset) and never
    trusts a non-scalar stand-in, so a mocked row without the attribute does
    not auto-vivify into a truthy flag.
    """
    return getattr(row, name, 0) in (1, True, "1")


def _allocate_waste_fifo(
    db: Session,
    waste_item_id: int,
    co_id: int,
    branch_id: int,
    issue_date,
    weight: float,
) -> tuple[list[WasteSlice], list[str]]:
    """Split one grouped waste pick across its open bookings, oldest first.

    The picker now offers waste as a single balance per item (TKT-000147), so
    the server is what turns "Dust, 200 kg" back into the individual bookings
    it actually comes out of. Oldest booking is drained first — waste is
    undated stock in practice, and FIFO keeps the pile turning over instead of
    leaving an old 6 kg remnant sitting open forever.

    The pool has TWO legs since purchased waste joined it (owner ruling
    2026-09-25, D3/D4) and the allocator walks both in one date order:
      * 'PROD' — a Waste Entry booking, keyed by waste_entry_id, priced from
                 jute_waste_rate_mst (one rate for every PROD row: the master
                 resolves per co/item/branch/date, never per entry);
      * 'MR'   — a jute_mr_li line of a type-3 item, keyed by jute_mr_li_id,
                 guarded and priced through _resolve_mr_stock_for_issue — the
                 same helper a direct MR issue uses — so it is valued at the MR
                 line's rate (actual_rate, else the QC-written rate) and, like
                 every MR-keyed issue, written in WHOLE kg (owner rule), with
                 ONE exception: a slice that DRAINS its line takes the exact
                 fractional balance. Without that, the 2021-2025 sub-kg and
                 fractional stubs that sit at the head of FIFO on most sls
                 branches (2.9 kg, 0.9 kg, ...) could never be drained — a
                 whole-kg cap would skip or short them forever while the
                 picker kept showing their balance. quantity=0 is passed on
                 purpose: waste has no bale count, so the guard judges the
                 weight ledger alone.

    Returns the (source, key_id, weight, rate) slices to write plus any
    warnings for the caller's response. Raises 400 when the item has no
    issuable stock at this branch/date or the ask overruns the summed balance
    — the same two failures the per-entry guard reports, phrased against the
    grouped total the operator was shown. Because a draining MR slice is exact,
    every kg the picker shows is drawable, so an ask within the balance is
    never silently short-written.

    PROD slices are rounded to 2dp to match the column the caller writes and
    the LAST slice absorbs the rounding remainder, so the written rows sum to
    exactly the weight that was asked for — unless the last slice is a
    non-draining MR slice, which holds its whole-kg value: a fractional ask
    that lands on purchased waste then rounds half-up exactly as a direct MR
    issue would, and the difference is reported back as a warning rather than
    hidden.
    """
    rows = db.execute(
        get_jute_waste_entries_for_allocation_query(),
        {
            "co_id": int(co_id),
            "branch_id": int(branch_id),
            "item_id": int(waste_item_id),
            "issue_date": issue_date,
        },
    ).fetchall()

    if not rows:
        raise HTTPException(
            status_code=400,
            detail="No waste stock available for this item on the issue date",
        )

    available = sum(float(r.bal_weight or 0) for r in rows)
    want = round(float(weight or 0), 2)
    if want <= 0:
        raise HTTPException(status_code=400, detail="Weight must be greater than 0")
    # Same 0.001 slack the per-entry guard uses: waste_qty is DECIMAL(10,3) but
    # jute_issue.weight is a FLOAT, so an exact full-balance issue can round a
    # hair over its own total.
    if want > available + _STOCK_TOLERANCE:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot issue more than the available waste balance ({available:.3f} kg)",
        )

    slices: list[WasteSlice] = []
    warnings: list[str] = []
    remaining = want
    for r in rows:
        if remaining <= 0:
            break
        bal = float(r.bal_weight or 0)
        take = round(min(remaining, bal), 2)
        if take <= 0:
            continue
        if getattr(r, "source_type", None) == WASTE_SOURCE_MR:
            if take >= bal - _STOCK_TOLERANCE:
                # Drains the line: take its exact balance, fraction and all
                # (2.9 kg stays 2.9), so the line closes at zero. The guard
                # recognises the same closing draw and keeps it exact.
                take = bal
            else:
                # Whole kg on the MR ledger, half-up like a direct MR issue.
                # A 0.4 kg remainder rounds to nothing and moves on; an ask
                # that rounds past the line (2.5 against 2.9) drains it.
                take = float(round_kg(take))
                if take <= 0:
                    continue
                if take > bal:
                    take = bal
            mr_rate, _qty_warnings, take = _resolve_mr_stock_for_issue(
                db, int(r.jute_mr_li_id), co_id, branch_id, 0, take
            )
            if take <= 0:
                continue
            slices.append((WASTE_SOURCE_MR, int(r.jute_mr_li_id), float(take), mr_rate))
        else:
            slices.append(
                (WASTE_SOURCE_PROD, int(r.waste_entry_id), take, float(r.actual_rate or 0))
            )
        remaining = round(remaining - take, 2)

    if not slices:
        raise HTTPException(
            status_code=400,
            detail="No waste stock available for this item on the issue date",
        )

    # Push any residue (the tolerance case, or 2dp rounding) onto the last slice
    # so sum(slices) == the weight the operator asked for, exactly. Only a PROD
    # slice can take a fraction; an MR slice keeps its whole-kg value.
    residue = round(want - sum(s[2] for s in slices), 2)
    if residue and slices[-1][0] == WASTE_SOURCE_PROD:
        source, key_id, take, rate = slices[-1]
        slices[-1] = (source, key_id, round(take + residue, 2), rate)

    # Whole-kg rounding on a trailing MR slice is the ONLY way the rows can
    # differ from the ask (every line is fully drawable, so a shortfall is
    # never a stock problem). Say so instead of leaving the operator to spot
    # 199.6 kg written against a 200 kg entry.
    written = round(sum(s[2] for s in slices), 2)
    if abs(written - want) > _STOCK_TOLERANCE:
        warnings.append(
            f"Purchased waste is issued in whole kg: {written:g} kg written "
            f"for {want:g} kg asked"
        )

    return slices, warnings


def _resolve_mr_stock_for_issue(
    db: Session,
    jute_mr_li_id: int,
    co_id: int,
    branch_id: int,
    quantity: float,
    weight: float,
    exclude_issue_id: Optional[int] = None,
) -> tuple[float, list[str]]:
    """Validate an MR line against its outstanding stock and return its rate.

    The waste path has had a write guard since waste became issuable; the MR
    path had none — it looked up `actual_rate` and wrote the row, so purchased
    jute could be issued straight into the negative. This is the twin guard, in
    one round trip:
      * the line exists and belongs to this company;
      * it belongs to the branch being issued from (no cross-branch draw);
      * its MR is eligible stock at all (bal_qty NULL = not open/pending/approved);
      * the WEIGHT asked for still fits the outstanding balance.

    The two ledgers are judged INDEPENDENTLY — every real over-issue found on
    sls over-drew quantity while weight stayed in credit, so a weight-only check
    catches none of them — but they are not treated alike:

      * weight over-draw   -> hard 400, nothing is written;
      * quantity over-draw -> WARNING only, returned to the caller and logged.

    Quantity warns rather than blocks because ~581 branch-87 MR lines carry junk
    `actual_qty` (line 45014: qty 1 against 106.5 weight, ratios to 149.6). A
    hard qty block would reject physically legitimate weight draws on those
    lines. That data is being repaired separately; until it is, weight is the
    trustworthy ledger.

    PURCHASED WASTE (owner ruling 2026-09-25) — a line whose item sits in a
    type-3 group (`row.is_waste`) is kg-only and differs in three ways:
      * quantity is IGNORED: the line was bought in bales/loose units
        (actual_qty) but is issued in kg slices from the grouped waste pool,
        so the bale ledger says nothing about it — no bale warning, no
        empties-line snap on quantity;
      * a draw that closes the line's WEIGHT takes the exact fractional
        balance (2.9 kg stays 2.9), so sub-kg and fractional stubs can be
        drained; every other draw is whole kg like any MR issue;
      * it is priced at actual_rate, else jml.rate — the QC insert that
        creates the line writes `rate` only, and 314 of the 330 open sls
        waste lines have no actual_rate, so pricing from actual_rate alone
        valued the first pooled issues at 0 while stock value never left.
    A raw-jute (type-2) line is untouched by all three.

    Returns `(rate, warnings, weight)` — `weight` is the value to store.
    """
    row = db.execute(
        get_jute_mr_line_for_issue_query(),
        {"jute_mr_li_id": int(jute_mr_li_id), "co_id": int(co_id)},
    ).fetchone()

    if not row:
        raise HTTPException(status_code=400, detail="Invalid MR line item ID")
    if row.branch_id is not None and int(row.branch_id) != int(branch_id):
        raise HTTPException(
            status_code=400, detail="MR line belongs to a different branch"
        )
    # LEFT JOIN: the line resolved but the stock view has nothing issuable for
    # it, i.e. its MR is not in an issuable status. Distinct from "no such line".
    #
    # This does NOT rely on the view being narrow — it deliberately is not.
    # vw_jute_stock_outstanding carries in-flight MRs so downstream reports can
    # see them, so it is BROADER than what may be issued, and the guard's LEFT
    # JOIN re-narrows it to JUTE_MR_ISSUABLE_STATUSES (open / pending /
    # approved). The issue picker narrows it the same way. Those two must always
    # carry the SAME tuple: relax it here and the picker's restriction becomes
    # cosmetic, because a direct POST never goes near the picker.
    if row.bal_qty is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Material Receipt is not open, pending or approved — "
                "its stock is not available for issue"
            ),
        )

    avail_qty = float(row.bal_qty or 0)
    avail_weight = float(row.bal_weight or 0)

    is_waste = _row_flag(row, "is_waste")
    if is_waste:
        quantity = 0

    if exclude_issue_id is not None:
        # The view has already netted this row off the balance, so comparing an
        # edit against it would read a re-save as a second draw. Predicate is a
        # bare `status_id <> 4` to match the MR view's own issued-total
        # subquery — NOT the waste helper's `(status_id IS NULL OR ...)`, which
        # would credit back a NULL-status row the view never subtracted.
        prior = db.execute(
            text(
                "SELECT quantity, weight FROM jute_issue "
                "WHERE jute_issue_id = :id AND status_id <> 4"
            ),
            {"id": exclude_issue_id},
        ).fetchone()
        if prior:
            avail_qty += float(prior.quantity or 0)
            avail_weight += float(prior.weight or 0)

    # Qty unit varies by tenant (BALE / DRUM / …) and weight is kg on some
    # tenants and quintal on others, so never hardcode a unit here.
    uom = row.unit_conversion or ""

    warnings: list[str] = []

    # ONLY the positive direction is guarded, on BOTH ledgers. A correction row
    # is posted as a NEGATIVE quantity/weight and RETURNS stock; comparing it to
    # a balance would read -5 > -49 as an over-draw and reject the one entry
    # that repairs an already-negative line. Do not "simplify" this away.
    if float(quantity or 0) > 0 and float(quantity) > avail_qty + _STOCK_TOLERANCE:
        qty_label = f"{avail_qty:.2f} {uom}".strip()
        msg = f"Issued quantity exceeds available balance quantity ({qty_label})"
        logger.warning(
            "Jute issue over-draws quantity on MR line %s: asked %s, available %s",
            jute_mr_li_id,
            quantity,
            avail_qty,
        )
        warnings.append(msg)

    # Stock is kept in WHOLE KG. A draw that empties the line's quantity takes
    # the exact remaining weight, so the line closes at zero even when the
    # caller's unit-weight math drifted by a fraction; every other draw rounds.
    # A gap of a kg or more is not rounding -- it falls through to the guard.
    weight = float(weight or 0)
    empties_line = (
        float(quantity or 0) > 0
        and avail_qty > 0
        and float(quantity) >= avail_qty - _STOCK_TOLERANCE
    )
    # Purchased waste has no bale ledger to empty; its closing draw is the one
    # that takes the whole remaining WEIGHT, and it is kept exact.
    closes_waste_line = (
        is_waste and avail_weight > 0 and abs(weight - avail_weight) <= _STOCK_TOLERANCE
    )
    if (empties_line and abs(weight - avail_weight) < 1) or closes_waste_line:
        weight = avail_weight
    else:
        weight = round_kg(weight)

    if weight > 0 and weight > avail_weight + _STOCK_TOLERANCE:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot issue more than the available balance weight ({avail_weight:.3f})",
        )

    rate = float(row.actual_rate or 0)
    if is_waste and not rate:
        rate = float(getattr(row, "rate", None) or 0)

    return rate, warnings, weight


def _build_issue_params(co_id: int, q_search: str, date_from, date_to):
    search_param = f"%{q_search}%" if q_search else None
    base = {
        "co_id": co_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if search_param:
        base["search"] = search_param
    return base


@router.get("/get_issue_table")
def get_jute_issue_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get paginated list of jute issue records.

    Query params:
    - co_id: Company ID (required)
    - page: Page number (default: 1)
    - limit: Records per page (default: 10)
    - search: Search term (optional)
    - from_date / to_date: Optional ISO dates (YYYY-MM-DD) bounding issue_date
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
            limit = max(1, min(100, int(q_limit)))
        except ValueError:
            page = 1
            limit = 10

        offset = (page - 1) * limit
        base_params = _build_issue_params(co_id, q_search, date_from, date_to)

        count_query = get_jute_issue_table_count_query(co_id, q_search)
        count_result = db.execute(count_query, base_params).fetchone()
        total = count_result.total if count_result else 0

        data_query = get_jute_issue_table_query(co_id, q_search)
        params = {**base_params, "limit": limit, "offset": offset}

        rows = db.execute(data_query, params).fetchall()
        data = [dict(r._mapping) for r in rows]

        return {
            "data": data,
            "total": total,
            "page": page,
            "page_size": limit,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching jute issue table: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching jute issue records: {str(e)}")


@router.get("/download_issue_table")
def download_jute_issue_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Download the Jute Issue list (aggregated by date+branch) as an .xlsx workbook."""
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

        base_params = _build_issue_params(co_id, q_search, date_from, date_to)

        count_query = get_jute_issue_table_count_query(co_id, q_search)
        count_result = db.execute(count_query, base_params).fetchone()
        total = int(count_result.total) if count_result and count_result.total else 0
        enforce_row_cap(total)

        rows = []
        if total > 0:
            data_query = get_jute_issue_table_query(co_id, q_search)
            data_params = {**base_params, "limit": total, "offset": 0}
            result = db.execute(data_query, data_params).fetchall()
            rows = [dict(r._mapping) for r in result]

        return build_excel_response(
            rows=rows,
            columns=JUTE_ISSUE_DOWNLOAD_COLUMNS,
            sheet_title="Jute Issue",
            filename_prefix="jute_issue",
            from_date_str=q_from_date,
            to_date_str=q_to_date,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error downloading Jute Issue table")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get_issue_by_id/{issue_id}")
def get_jute_issue_by_id(
    issue_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get a single jute issue record by ID.
    
    Path params:
    - issue_id: The jute issue ID
    
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

        sql = text("""
            SELECT
                ji.jute_issue_id,
                ji.branch_id,
                bm.branch_name,
                ji.issue_date,
                ji.status_id,
                COALESCE(sm.status_name, 'Draft') AS status,
                ji.issue_value,
                COALESCE(ji.item_id, mrli.actual_item_id) AS item_id,
                COALESCE(im.item_name, im2.item_name) AS item_name,
                ji.jute_mr_li_id,
                mrli.jute_mr_id,
                jm.jute_gate_entry_no,
                ji.yarn_type_id,
                COALESCE(yim.item_name, jym.jute_yarn_name) AS yarn_type_name,
                ji.quantity,
                ji.weight,
                ji.unit_conversion,
                ji.updated_by,
                ji.update_date_time
            FROM jute_issue ji
            INNER JOIN branch_mst bm ON bm.branch_id = ji.branch_id
            LEFT JOIN status_mst sm ON sm.status_id = ji.status_id
            LEFT JOIN jute_mr_li mrli ON mrli.jute_mr_li_id = ji.jute_mr_li_id
            LEFT JOIN jute_mr jm ON jm.jute_mr_id = mrli.jute_mr_id
            LEFT JOIN item_mst im ON im.item_id = ji.item_id
            LEFT JOIN item_mst im2 ON im2.item_id = mrli.actual_item_id
            LEFT JOIN jute_yarn_mst jym ON jym.jute_yarn_id = ji.yarn_type_id
            LEFT JOIN item_mst yim ON yim.item_id = jym.item_id
            WHERE ji.jute_issue_id = :issue_id
            AND bm.co_id = :co_id
        """)

        result = db.execute(sql, {"issue_id": issue_id, "co_id": co_id}).fetchone()
        
        if not result:
            raise HTTPException(status_code=404, detail="Jute issue record not found")

        return {"data": dict(result._mapping)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching jute issue by ID: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching jute issue record: {str(e)}")


@router.get("/get_issue_create_setup")
def get_issue_create_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get setup data for creating jute issue.
    Returns: jute items (item_grp_id 2 or 3), yarn types, and branches.
    """
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        
        co_id = int(q_co_id)

        # Get jute groups (subgroups from item_grp_mst)
        jute_items_query = get_jute_issue_create_setup_query()
        jute_items_rows = db.execute(jute_items_query, {"co_id": co_id}).fetchall()
        jute_groups = [dict(r._mapping) for r in jute_items_rows]

        # Get yarn types
        yarn_types_query = get_yarn_types_query()
        yarn_types_rows = db.execute(yarn_types_query, {"co_id": co_id}).fetchall()
        yarn_types = [dict(r._mapping) for r in yarn_types_rows]

        # Get branches
        branches_sql = text("""
            SELECT branch_id, branch_name 
            FROM branch_mst 
            WHERE co_id = :co_id 
            AND (active = 1 OR active IS NULL)
            ORDER BY branch_name
        """)
        branches_rows = db.execute(branches_sql, {"co_id": co_id}).fetchall()
        branches = [dict(r._mapping) for r in branches_rows]

        return {
            "jute_groups": jute_groups,
            "yarn_types": yarn_types,
            "branches": branches,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching issue create setup: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching setup data: {str(e)}")


@router.get("/get_stock_outstanding")
def get_stock_outstanding(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get available jute stock for issue from BOTH stock sources, as one list:

      * purchased jute  — vw_jute_stock_outstanding, one row per MR line of a
                          RAW JUTE (type-2) item;
      * waste           — vw_jute_waste_stock_outstanding, GROUPED to one row
                          per waste ITEM (kg-only). Since the owner ruling of
                          2026-09-25 this pool holds BOTH floor waste (Waste
                          Entry bookings) and PURCHASED waste (MR lines whose
                          item sits in a type-3 group); the MR arm above
                          excludes type-3 lines so a purchased lot is offered
                          once, inside its item's 'WI-<item_id>' row.

    Waste is grouped, not listed per booking (TKT-000147): the operator picks a
    whole item balance and create_issue fans it back out FIFO across that
    item's open Waste Entry bookings. So a waste row here carries no
    waste_entry_id, department or area — it stands for several bookings.

    Both queries emit the same columns, so the caller sees a uniform list and
    tells the two apart by `source` ('MR' / 'WASTE'). Rows are keyed on
    `stock_key` ('MR-<mr line>' / 'WI-<item>'), not jute_mr_li_id, because a
    waste row has no MR line.

    Filters by branch_id (required) and optionally by item_id and issue_date.
    """
    try:
        q_co_id = request.query_params.get("co_id")
        q_branch_id = request.query_params.get("branch_id")
        q_item_id = request.query_params.get("item_id")
        q_issue_date = request.query_params.get("issue_date")

        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not q_branch_id:
            raise HTTPException(status_code=400, detail="branch_id is required")

        co_id = int(q_co_id)
        branch_id = int(q_branch_id)
        item_id = int(q_item_id) if q_item_id else None

        # Both sources gate on `inward_date <= :issue_date`, and the waste rate
        # lookup resolves as-of the same date. Falling back to today keeps that
        # comparison against a real date — the previous '' fallback compared a
        # DATE against an empty string, which MySQL evaluates to NULL and so
        # silently returned NO stock at all when the caller omitted the date.
        issue_date = q_issue_date if q_issue_date else date.today().isoformat()

        if item_id is not None:
            mr_query = get_jute_stock_outstanding_by_item_query()
            mr_params = {"branch_id": branch_id, "item_id": item_id}
        else:
            mr_query = get_jute_stock_outstanding_query()
            mr_params = {"branch_id": branch_id}
        mr_params["issue_date"] = issue_date

        mr_rows = db.execute(mr_query, mr_params).fetchall()
        waste_rows = db.execute(
            get_jute_waste_stock_outstanding_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "item_id": item_id,
                "issue_date": issue_date,
            },
        ).fetchall()

        data = [dict(r._mapping) for r in mr_rows] + [
            dict(r._mapping) for r in waste_rows
        ]
        return {"data": data}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching stock outstanding: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching stock data: {str(e)}")


@router.get("/get_issues_by_date")
def get_issues_by_date(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get all jute issue line items for a specific branch and date.
    Used for the issue detail/edit page.
    """
    try:
        q_co_id = request.query_params.get("co_id")
        q_branch_id = request.query_params.get("branch_id")
        q_issue_date = request.query_params.get("issue_date")

        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not q_branch_id:
            raise HTTPException(status_code=400, detail="branch_id is required")
        if not q_issue_date:
            raise HTTPException(status_code=400, detail="issue_date is required")

        co_id = int(q_co_id)
        branch_id = int(q_branch_id)

        query = get_jute_issues_by_date_query()
        rows = db.execute(query, {
            "co_id": co_id,
            "branch_id": branch_id,
            "issue_date": q_issue_date,
        }).fetchall()

        data = [dict(r._mapping) for r in rows]

        # Rejected (4) lines stay listed for the audit trail but never count:
        # a rejected issue gives its stock back, so it is not part of the day's
        # weight/value or of its approval state (TKT-000160).
        live = [row for row in data if row.get("status_id") != 4]

        # Calculate totals
        total_weight = sum(row.get("weight", 0) or 0 for row in live)
        total_value = sum(row.get("issue_value", 0) or 0 for row in live)
        total_qty = sum(row.get("quantity", 0) or 0 for row in live)

        # Determine aggregated status
        if not live:
            status = "Draft"
            status_id = 21
        else:
            approved_count = sum(1 for row in live if row.get("status_id") == 3)
            if approved_count == len(live):
                status = "Approved"
                status_id = 3
            elif approved_count > 0:
                status = "Partial Approved"
                status_id = 20
            else:
                # Check if any are open (status_id = 1)
                open_count = sum(1 for row in live if row.get("status_id") == 1)
                if open_count == len(live):
                    status = "Open"
                    status_id = 1
                elif open_count > 0:
                    status = "Partial Open"
                    status_id = 20
                else:
                    status = "Draft"
                    status_id = 21

        return {
            "data": data,
            "summary": {
                "total_entries": len(data),
                "total_weight": round(total_weight, 2),
                "total_value": round(total_value, 2),
                "total_qty": round(total_qty, 2),
                "status": status,
                "status_id": status_id,
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching issues by date: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching issues: {str(e)}")


@router.get("/get_max_issue_date")
def get_max_issue_date(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Get the maximum issue date for a branch.
    Used to set default date for new issues (max_date + 1).
    
    Query params:
    - co_id: Company ID (required for tenant resolution)
    - branch_id: Branch ID (required)
    """
    try:
        q_co_id = request.query_params.get("co_id")
        q_branch_id = request.query_params.get("branch_id")

        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if not q_branch_id:
            raise HTTPException(status_code=400, detail="branch_id is required")

        branch_id = int(q_branch_id)

        query = text("""
            SELECT MAX(issue_date) as max_date
            FROM jute_issue
            WHERE branch_id = :branch_id
        """)
        
        result = db.execute(query, {"branch_id": branch_id}).fetchone()
        max_date = result.max_date if result else None

        return {
            "max_date": max_date.isoformat() if max_date else None
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching max issue date: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching max issue date: {str(e)}")


@router.post("/create_issue")
def create_issue(
    body: JuteIssueCreate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Create a new jute issue line item from any stock source.
    Issue starts as Draft (status_id = 21).

    A grouped waste pick (`waste_item_id`) fans out into ONE ROW PER BOOKING
    consumed, oldest first — so a single POST can return several ids. A
    booking is a Waste Entry (row keyed by waste_entry_id) or, since purchased
    waste joined the pool (owner ruling 2026-09-25), an MR line of a type-3
    item (row keyed by jute_mr_li_id); each row carries exactly one key and is
    priced from its own source. Every other source still writes exactly one
    row.
    """
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        co_id = int(q_co_id)

        sources = [
            bool(body.jute_mr_li_id),
            bool(body.waste_entry_id),
            bool(body.waste_item_id),
        ]
        if sum(sources) != 1:
            raise HTTPException(
                status_code=400,
                detail="Provide exactly one of jute_mr_li_id, waste_entry_id or waste_item_id",
            )

        # Rate is per quintal (100 kg) in EVERY source, so the value formula is
        # shared; only where the rate comes from differs. Each lookup doubles as
        # the write guard for its own stock source.
        #
        # `slices` is what actually gets written: (source, key_id, weight,
        # rate) per row — source 'PROD' keys the row by waste_entry_id, 'MR'
        # by jute_mr_li_id. The two single-row paths build a one-element list
        # carrying the weight as sent, so all three sources leave through the
        # same INSERT.
        warnings: list[str] = []
        if body.waste_item_id:
            slices, warnings = _allocate_waste_fifo(
                db, body.waste_item_id, co_id, body.branch_id, body.issue_date, body.weight
            )
        elif body.waste_entry_id:
            actual_rate = _resolve_waste_rate_for_issue(
                db, body.waste_entry_id, co_id, body.branch_id, body.issue_date, body.weight
            )
            slices = [
                (WASTE_SOURCE_PROD, body.waste_entry_id, round(body.weight or 0, 2), actual_rate)
            ]
        else:
            actual_rate, warnings, mr_weight = _resolve_mr_stock_for_issue(
                db, body.jute_mr_li_id, co_id, body.branch_id, body.quantity, body.weight
            )
            slices = [(WASTE_SOURCE_MR, body.jute_mr_li_id, mr_weight, actual_rate)]

        sql = text("""
            INSERT INTO jute_issue (
                branch_id, issue_date, jute_mr_li_id, waste_entry_id, yarn_type_id, item_id,
                quantity, weight, unit_conversion, issue_value, status_id,
                updated_by, update_date_time
            ) VALUES (
                :branch_id, :issue_date, :jute_mr_li_id, :waste_entry_id, :yarn_type_id, :item_id,
                :quantity, :weight, :unit_conversion, :issue_value, :status_id,
                :updated_by, :update_date_time
            )
        """)

        username = token_data.get("sub", "system")
        now = now_ist()

        # Quantity rides along in the SAME proportion as weight. For waste the
        # two are the same number by construction (the view reports waste_qty as
        # both), but splitting proportionally rather than assuming that keeps the
        # header totals right if they ever diverge. The last slice takes the
        # remainder, so the written rows sum back to exactly what was sent.
        total_weight = sum(s[2] for s in slices)
        sent_qty = round(body.quantity, 2) if body.quantity else 0

        new_ids: list[int] = []
        issue_value_total = 0.0
        qty_left = sent_qty

        for idx, (source, key_id, slice_weight, slice_rate) in enumerate(slices):
            is_last = idx == len(slices) - 1
            if is_last or not total_weight:
                slice_qty = round(qty_left, 2)
            else:
                slice_qty = round(sent_qty * slice_weight / total_weight, 2)
            qty_left = round(qty_left - slice_qty, 2)

            # Each slice is priced at ITS OWN source's rate: a grouped waste
            # pick can mix floor sweeps (waste rate master) with purchased lots
            # (MR line rate) in one POST.
            issue_value = round((slice_weight / 100) * slice_rate, 2) if slice_weight else 0
            issue_value_total = round(issue_value_total + issue_value, 2)

            db.execute(sql, {
                "branch_id": body.branch_id,
                "issue_date": body.issue_date,
                "jute_mr_li_id": key_id if source == WASTE_SOURCE_MR else None,
                "waste_entry_id": key_id if source == WASTE_SOURCE_PROD else None,
                "yarn_type_id": body.yarn_type_id,
                "item_id": body.item_id,
                "quantity": slice_qty,
                "weight": slice_weight,
                "unit_conversion": body.unit_conversion,
                "issue_value": issue_value,
                "status_id": 21,  # Draft
                "updated_by": username,
                "update_date_time": now,
            })
            row = db.execute(text("SELECT LAST_INSERT_ID() AS id")).fetchone()
            if row:
                new_ids.append(int(row.id))

        db.commit()

        return {
            "message": "Jute issue created successfully",
            # First id, for callers that only ever expected one row.
            "jute_issue_id": new_ids[0] if new_ids else None,
            # Every id written — a grouped waste pick produces more than one.
            "jute_issue_ids": new_ids,
            "issue_value": issue_value_total,
            # Additive: always present, empty when the line is clean. Existing
            # keys are untouched so the current FE keeps working as-is.
            "warnings": warnings,
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating jute issue: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error creating jute issue: {str(e)}")


@router.put("/update_issue/{issue_id}")
def update_issue(
    issue_id: int,
    body: JuteIssueUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Update an existing jute issue line item.
    Only allowed if status is Draft (21).
    """
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        co_id = int(q_co_id)

        # Check if issue exists and is in draft status. quantity/weight come
        # along because a partial edit has to be guarded on EFFECTIVE values —
        # the client may send only one of the two.
        check_sql = text("""
            SELECT ji.jute_issue_id, ji.status_id, ji.jute_mr_li_id,
                   ji.waste_entry_id, ji.branch_id, ji.issue_date,
                   ji.quantity, ji.weight
            FROM jute_issue ji
            INNER JOIN branch_mst bm ON bm.branch_id = ji.branch_id
            WHERE ji.jute_issue_id = :issue_id AND bm.co_id = :co_id
        """)
        existing = db.execute(check_sql, {"issue_id": issue_id, "co_id": co_id}).fetchone()
        
        if not existing:
            raise HTTPException(status_code=404, detail="Jute issue not found")
        if existing.status_id not in (21, None):  # Allow update only for Draft
            raise HTTPException(status_code=400, detail="Can only update issues in Draft status")

        # Build update fields dynamically
        update_fields = []
        update_params = {"issue_id": issue_id}

        if body.yarn_type_id is not None:
            update_fields.append("yarn_type_id = :yarn_type_id")
            update_params["yarn_type_id"] = body.yarn_type_id

        if body.item_id is not None:
            update_fields.append("item_id = :item_id")
            update_params["item_id"] = body.item_id

        if body.quantity is not None:
            update_fields.append("quantity = :quantity")
            update_params["quantity"] = round(body.quantity, 2)

        if body.weight is not None:
            update_fields.append("weight = :weight")
            update_params["weight"] = round(body.weight, 2)

        # Guard + reprice whenever EITHER ledger moves. This used to hang off
        # `weight is not None` alone, so a quantity-only edit slipped past the
        # stock guard entirely — the one edit most likely to over-draw.
        warnings: list[str] = []
        if body.quantity is not None or body.weight is not None:
            eff_quantity = (
                body.quantity if body.quantity is not None else float(existing.quantity or 0)
            )
            eff_weight = (
                body.weight if body.weight is not None else float(existing.weight or 0)
            )

            # Price from the line's OWN stock source. A waste line has no MR
            # line to read a rate off, so pricing it via jute_mr_li would
            # silently value it at zero.
            if existing.waste_entry_id:
                actual_rate = _resolve_waste_rate_for_issue(
                    db,
                    existing.waste_entry_id,
                    co_id,
                    existing.branch_id,
                    existing.issue_date,
                    eff_weight,
                    exclude_issue_id=issue_id,
                )
            else:
                actual_rate, warnings, eff_weight = _resolve_mr_stock_for_issue(
                    db,
                    existing.jute_mr_li_id,
                    co_id,
                    existing.branch_id,
                    eff_quantity,
                    eff_weight,
                    exclude_issue_id=issue_id,
                )
                # The guard returns the weight to store (whole kg, or the exact
                # closing balance) -- write it even on a quantity-only edit.
                update_params["weight"] = eff_weight
                if "weight = :weight" not in update_fields:
                    update_fields.append("weight = :weight")
            issue_value = round((eff_weight / 100) * actual_rate, 2) if eff_weight else 0
            update_fields.append("issue_value = :issue_value")
            update_params["issue_value"] = issue_value

        if body.unit_conversion is not None:
            update_fields.append("unit_conversion = :unit_conversion")
            update_params["unit_conversion"] = body.unit_conversion

        if not update_fields:
            return {"message": "No fields to update", "warnings": warnings}

        # Add audit fields
        username = token_data.get("sub", "system")
        update_fields.append("updated_by = :updated_by")
        update_fields.append("update_date_time = :update_date_time")
        update_params["updated_by"] = username
        update_params["update_date_time"] = now_ist()

        sql = text(f"""
            UPDATE jute_issue SET {", ".join(update_fields)}
            WHERE jute_issue_id = :issue_id
        """)
        db.execute(sql, update_params)
        db.commit()

        return {"message": "Jute issue updated successfully", "warnings": warnings}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating jute issue: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error updating jute issue: {str(e)}")


@router.delete("/delete_issue/{issue_id}")
def delete_issue(
    issue_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Delete a jute issue line item.
    Only allowed if status is Draft (21).
    """
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        # Check if issue exists and is in draft status
        check_sql = text("""
            SELECT ji.jute_issue_id, ji.status_id
            FROM jute_issue ji
            INNER JOIN branch_mst bm ON bm.branch_id = ji.branch_id
            WHERE ji.jute_issue_id = :issue_id AND bm.co_id = :co_id
        """)
        existing = db.execute(check_sql, {"issue_id": issue_id, "co_id": int(q_co_id)}).fetchone()
        
        if not existing:
            raise HTTPException(status_code=404, detail="Jute issue not found")
        if existing.status_id not in (21, None):
            raise HTTPException(status_code=400, detail="Can only delete issues in Draft status")

        sql = text("DELETE FROM jute_issue WHERE jute_issue_id = :issue_id")
        db.execute(sql, {"issue_id": issue_id})
        db.commit()

        return {"message": "Jute issue deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting jute issue: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error deleting jute issue: {str(e)}")


@router.post("/open_issues")
def open_issues(
    body: JuteIssueStatusUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Open jute issues. Changes status from Draft (21) to Open (1).
    
    Can operate in two modes:
    - Specific IDs: If issue_ids is provided, only those issues are updated
    - Bulk by date: If branch_id and issue_date are provided, all matching issues are updated
    """
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        username = token_data.get("sub", "system")
        update_params = {
            "co_id": int(q_co_id),
            "updated_by": username,
            "update_date_time": now_ist(),
        }

        # Mode 1: Specific issue IDs
        if body.issue_ids and len(body.issue_ids) > 0:
            placeholders = ", ".join([f":id_{i}" for i in range(len(body.issue_ids))])
            sql = text(f"""
                UPDATE jute_issue ji
                INNER JOIN branch_mst bm ON bm.branch_id = ji.branch_id
                SET ji.status_id = 1, ji.updated_by = :updated_by, ji.update_date_time = :update_date_time
                WHERE ji.jute_issue_id IN ({placeholders})
                AND ji.status_id = 21
                AND bm.co_id = :co_id
            """)
            for i, issue_id in enumerate(body.issue_ids):
                update_params[f"id_{i}"] = issue_id
        # Mode 2: Bulk by branch and date
        elif body.branch_id and body.issue_date:
            sql = text("""
                UPDATE jute_issue ji
                INNER JOIN branch_mst bm ON bm.branch_id = ji.branch_id
                SET ji.status_id = 1, ji.updated_by = :updated_by, ji.update_date_time = :update_date_time
                WHERE ji.branch_id = :branch_id
                AND ji.issue_date = :issue_date
                AND ji.status_id = 21
                AND bm.co_id = :co_id
            """)
            update_params["branch_id"] = body.branch_id
            update_params["issue_date"] = body.issue_date
        else:
            raise HTTPException(status_code=400, detail="Either issue_ids or (branch_id and issue_date) is required")
        
        result = db.execute(sql, update_params)
        db.commit()

        return {"message": f"Opened {result.rowcount} issue(s) successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error opening issues: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error opening issues: {str(e)}")


@router.post("/approve_issues")
def approve_issues(
    body: JuteIssueStatusUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Approve jute issues. Changes status from Open (1) to Approved (3).
    
    Can operate in two modes:
    - Specific IDs: If issue_ids is provided, only those issues are updated
    - Bulk by date: If branch_id and issue_date are provided, all matching issues are updated
    """
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        username = token_data.get("sub", "system")
        update_params = {
            "co_id": int(q_co_id),
            "updated_by": username,
            "update_date_time": now_ist(),
        }

        # Mode 1: Specific issue IDs
        if body.issue_ids and len(body.issue_ids) > 0:
            placeholders = ", ".join([f":id_{i}" for i in range(len(body.issue_ids))])
            sql = text(f"""
                UPDATE jute_issue ji
                INNER JOIN branch_mst bm ON bm.branch_id = ji.branch_id
                SET ji.status_id = 3, ji.updated_by = :updated_by, ji.update_date_time = :update_date_time
                WHERE ji.jute_issue_id IN ({placeholders})
                AND ji.status_id = 1
                AND bm.co_id = :co_id
            """)
            for i, issue_id in enumerate(body.issue_ids):
                update_params[f"id_{i}"] = issue_id
        # Mode 2: Bulk by branch and date
        elif body.branch_id and body.issue_date:
            sql = text("""
                UPDATE jute_issue ji
                INNER JOIN branch_mst bm ON bm.branch_id = ji.branch_id
                SET ji.status_id = 3, ji.updated_by = :updated_by, ji.update_date_time = :update_date_time
                WHERE ji.branch_id = :branch_id
                AND ji.issue_date = :issue_date
                AND ji.status_id = 1
                AND bm.co_id = :co_id
            """)
            update_params["branch_id"] = body.branch_id
            update_params["issue_date"] = body.issue_date
        else:
            raise HTTPException(status_code=400, detail="Either issue_ids or (branch_id and issue_date) is required")
        
        result = db.execute(sql, update_params)
        db.commit()

        return {"message": f"Approved {result.rowcount} issue(s) successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error approving issues: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error approving issues: {str(e)}")


@router.post("/reject_issues")
def reject_issues(
    body: JuteIssueStatusUpdate,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """
    Reject jute issues. Changes status from Open (1) to Rejected (4).
    
    Can operate in two modes:
    - Specific IDs: If issue_ids is provided, only those issues are updated
    - Bulk by date: If branch_id and issue_date are provided, all matching issues are updated
    """
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")

        username = token_data.get("sub", "system")
        update_params = {
            "co_id": int(q_co_id),
            "updated_by": username,
            "update_date_time": now_ist(),
        }

        # Mode 1: Specific issue IDs
        if body.issue_ids and len(body.issue_ids) > 0:
            placeholders = ", ".join([f":id_{i}" for i in range(len(body.issue_ids))])
            sql = text(f"""
                UPDATE jute_issue ji
                INNER JOIN branch_mst bm ON bm.branch_id = ji.branch_id
                SET ji.status_id = 4, ji.updated_by = :updated_by, ji.update_date_time = :update_date_time
                WHERE ji.jute_issue_id IN ({placeholders})
                AND ji.status_id = 1
                AND bm.co_id = :co_id
            """)
            for i, issue_id in enumerate(body.issue_ids):
                update_params[f"id_{i}"] = issue_id
        # Mode 2: Bulk by branch and date
        elif body.branch_id and body.issue_date:
            sql = text("""
                UPDATE jute_issue ji
                INNER JOIN branch_mst bm ON bm.branch_id = ji.branch_id
                SET ji.status_id = 4, ji.updated_by = :updated_by, ji.update_date_time = :update_date_time
                WHERE ji.branch_id = :branch_id
                AND ji.issue_date = :issue_date
                AND ji.status_id = 1
                AND bm.co_id = :co_id
            """)
            update_params["branch_id"] = body.branch_id
            update_params["issue_date"] = body.issue_date
        else:
            raise HTTPException(status_code=400, detail="Either issue_ids or (branch_id and issue_date) is required")
        
        result = db.execute(sql, update_params)
        db.commit()

        return {"message": f"Rejected {result.rowcount} issue(s) successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error rejecting issues: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error rejecting issues: {str(e)}")


class JuteIssueSyncWeights(BaseModel):
    jute_mr_id: int
    branch_id: int


@router.post("/sync_issue_weights")
def sync_issue_weights(
    body: JuteIssueSyncWeights,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Re-derive weight/issue_value of every non-rejected issue on one MR from the line's
    CURRENT actual_weight/actual_qty (bale unit weight). Deliberately updates Approved issues
    too -- this is the explicit, user-triggered sync after an MR correction (owner ruling 2026-09-03).

    PURCHASED WASTE lines (type-3 item, owner ruling 2026-09-25) are kg-only: bought in
    bales/loose units (actual_qty) but issued as kg slices from the grouped waste pool, so a
    slice's quantity IS its kg and the bale unit-weight math above does not apply. On sls not
    one of the 486 waste lines has actual_qty == actual_weight (line 45659: 108 units against
    10,042 kg), so applying it would inflate a 500 kg slice to 46,500 kg and then "close" the
    line by rewriting the last slice to the whole line weight. Such lines are REPRICED ONLY:
    weight and quantity stay as written, issue_value = weight/100 * the line's rate
    (actual_rate, else the QC-written rate)."""
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        co_id = int(q_co_id)
        rows = db.execute(get_jute_mr_lines_and_issues_for_sync_query(),
                          {"co_id": co_id, "jute_mr_id": body.jute_mr_id, "branch_id": body.branch_id}).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="MR not found for this branch/company")
        lines: dict = {}
        for r in rows:
            if r.jute_mr_li_id not in lines:
                is_waste = _row_flag(r, "is_waste")
                rate = float(r.actual_rate or 0)
                if is_waste and not rate:
                    rate = float(getattr(r, "rate", None) or 0)
                lines[r.jute_mr_li_id] = {
                    "aq": float(r.actual_qty or 0), "aw": float(r.actual_weight or 0),
                    "rate": rate, "is_waste": is_waste, "issues": []}
            line = lines[r.jute_mr_li_id]
            if r.jute_issue_id is not None:
                line["issues"].append(r)
        now = now_ist()
        # jute_issue.updated_by is String(255) (see src.models.jute.JuteIssueDev3) -- bind the
        # user's login/sub string, not the numeric user_id.
        updated_by = token_data.get("sub") or str(token_data.get("user_id", "system"))
        upd = text("""UPDATE jute_issue SET weight = :w, quantity = :q, issue_value = :v,
                             updated_by = :u, update_date_time = :t
                      WHERE jute_issue_id = :id""")
        results, changed = [], 0
        for li_id, line in lines.items():
            if not line["issues"]:
                continue
            # A waste line has no bale unit weight and is never rescaled (see docstring).
            unit_wt = line["aw"] / line["aq"] if line["aq"] > 0 and not line["is_waste"] else None
            sum_w = sum(float(i.weight or 0) for i in line["issues"])
            sum_q = sum(float(i.quantity or 0) for i in line["issues"])
            new_ws: list[float] = []
            for i in line["issues"]:
                qty = float(i.quantity or 0)
                before_w = float(i.weight or 0)
                if line["is_waste"]:
                    new_ws.append(before_w)  # a purchased-waste slice IS its kg
                elif unit_wt is not None and qty > 0:
                    new_ws.append(round_kg(qty * unit_wt))
                elif sum_w > line["aw"] > 0:
                    new_ws.append(round_kg(before_w * line["aw"] / sum_w))  # no bale ratio: scale down to fit
                else:
                    new_ws.append(before_w)
            # Once the line's bales are all gone (or over-drawn) the LAST row
            # closes it: it takes the whole-kg rounding residue, and on a bale
            # over-issue it is cut back so the line ends at exactly zero
            # (owner ruling 2026-09-05: "reduce the last issue to make it 0").
            new_qs = [float(i.quantity or 0) for i in line["issues"]]
            fully_issued = (
                not line["is_waste"] and line["aq"] > 0 and sum_q >= line["aq"] - _STOCK_TOLERANCE
            )
            scaled_to_fit = not line["is_waste"] and unit_wt is None and sum_w > line["aw"] > 0
            if (fully_issued or scaled_to_fit) and new_ws:
                new_ws[-1] = round(new_ws[-1] + line["aw"] - sum(new_ws), 3)
                if fully_issued and sum_q > line["aq"] + _STOCK_TOLERANCE:
                    new_qs[-1] = round(new_qs[-1] - (sum_q - line["aq"]), 3)
            for i, new_w, new_q in zip(line["issues"], new_ws, new_qs):
                qty = float(i.quantity or 0)
                before_w, before_v = float(i.weight or 0), float(i.issue_value or 0)
                new_v = round(new_w / 100 * line["rate"], 2)
                if new_w != before_w or new_v != before_v or new_q != qty:
                    db.execute(upd, {"w": new_w, "q": new_q, "v": new_v, "u": updated_by, "t": now,
                                     "id": i.jute_issue_id})
                    changed += 1
                results.append({"jute_issue_id": i.jute_issue_id, "jute_mr_li_id": li_id, "quantity": qty,
                                "quantity_after": new_q,
                                "status_id": i.status_id, "weight_before": before_w, "weight_after": new_w,
                                "issue_value_before": before_v, "issue_value_after": new_v})
        db.commit()
        return {"data": results, "totals": {
            "lines_processed": len(lines), "issues_processed": len(results), "issues_changed": changed,
            "weight_before": round(sum(x["weight_before"] for x in results), 2),
            "weight_after": round(sum(x["weight_after"] for x in results), 2)}}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error syncing issue weights: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error syncing issue weights: {e}")

# ---------------------------------------------------------------------------
# Re-evaluate issue values (owner request 2026-10-08, Batch Cost report)
#
# An issue's value is stamped at save time as weight/100 x its MR line's rate.
# When the MR line has no rate yet (rates arrive later, at QC / approval) the
# issue is stored at 0 and never catches up — on sls 357 issues since Sep.
# This re-prices every non-rejected MR-sourced issue of a branch in a date range
# from the line's CURRENT rate (actual_rate, else the QC-written rate — the same
# rate sync_issue_weights uses). Weights and quantities are never touched.
# Floor-waste issues (no MR line) have no rate source and are left alone; lines
# that still have no rate are reported, not zeroed.
# ---------------------------------------------------------------------------

_REPRICE_CANDIDATES_SQL = text("""
    SELECT ji.jute_issue_id, ji.weight, COALESCE(ji.issue_value, 0) AS issue_value,
           li.jute_mr_id,
           COALESCE(NULLIF(li.actual_rate, 0), li.rate, 0) AS line_rate
    FROM jute_issue ji
    JOIN jute_mr_li li ON li.jute_mr_li_id = ji.jute_mr_li_id
    JOIN branch_mst bm ON bm.branch_id = ji.branch_id
    WHERE ji.branch_id = :branch_id
      AND bm.co_id = :co_id
      AND ji.issue_date BETWEEN :date_from AND :date_to
      AND (ji.status_id IS NULL OR ji.status_id <> 4)
""")


class JuteIssueReprice(BaseModel):
    branch_id: int
    date_from: date
    date_to: date


def plan_issue_reprice(rows) -> dict:
    """Pure: which issues get a new value. Returns {"updates": [(id, new_value)],
    "no_rate_mr_ids": [...], "value_before": x, "value_after": y}."""
    updates, no_rate, before, after = [], set(), 0.0, 0.0
    for r in rows:
        old_v = round(float(r.issue_value or 0), 2)
        rate = float(r.line_rate or 0)
        before += old_v
        if rate <= 0:
            if old_v == 0 and float(r.weight or 0) > 0:
                no_rate.add(int(r.jute_mr_id))
            after += old_v
            continue
        new_v = round(float(r.weight or 0) / 100 * rate, 2)
        after += new_v
        if abs(new_v - old_v) >= 0.01:
            updates.append((int(r.jute_issue_id), new_v))
    return {"updates": updates, "no_rate_mr_ids": sorted(no_rate),
            "value_before": round(before, 2), "value_after": round(after, 2)}


@router.post("/reprice_issues")
def reprice_issues(
    body: JuteIssueReprice,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Re-evaluate: re-price a branch's issues in [date_from, date_to] from their MR
    line's current rate (see the block comment above). Approved issues included, as
    with sync_issue_weights — this is an explicit, user-triggered sync."""
    try:
        q_co_id = request.query_params.get("co_id")
        if not q_co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        if body.date_to < body.date_from:
            raise HTTPException(status_code=400, detail="date_to is before date_from")
        if (body.date_to - body.date_from).days > 92:
            raise HTTPException(status_code=400, detail="Re-evaluate at most about three months at a time")
        rows = db.execute(_REPRICE_CANDIDATES_SQL, {
            "co_id": int(q_co_id), "branch_id": body.branch_id,
            "date_from": body.date_from.isoformat(), "date_to": body.date_to.isoformat(),
        }).fetchall()
        plan = plan_issue_reprice(rows)
        updated_by = token_data.get("sub") or str(token_data.get("user_id", "system"))
        now = now_ist()
        upd = text("""UPDATE jute_issue SET issue_value = :v, updated_by = :u, update_date_time = :t
                      WHERE jute_issue_id = :id""")
        for issue_id, new_v in plan["updates"]:
            db.execute(upd, {"v": new_v, "u": updated_by, "t": now, "id": issue_id})
        db.commit()
        return {"data": {
            "issues_checked": len(rows),
            "issues_changed": len(plan["updates"]),
            "value_before": plan["value_before"],
            "value_after": plan["value_after"],
            "no_rate_mr_ids": plan["no_rate_mr_ids"],
        }}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error re-pricing issues: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error re-pricing issues: {e}")

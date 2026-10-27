"""Bale Stock & Inspection — bale-level stock derived from Bale Production,
inspection marking, bale despatch (against a sales invoice, or without one and
linked later) (/api/baleStock).

Spec: docs/superpowers/specs/2026-10-12-bale-stock-inspection-design.md.

Nothing here stores a stock figure. One jute_prod_bale_entry row is one physical
bale (D1); its state as on a date is computed by ONE statement
(bale_stock_query.get_bale_stock_rows_query, spec §3) — REPACKED (opened for a
REPACK), DESPATCHED (linked to a live invoice, or sent out without one on a
challan / delivery order / transfer / sample — §7b), INSPECTED, or IN_STOCK.
This router adds the two events the bale row does not carry: an inspection
marking (sacking bales only, D3) and a despatch row (D4 / §7b), both
soft-deleted to undo. A no-invoice despatch is linked to its invoice later by
the same POST /despatch (the row is UPDATEd in place; the despatch date stays).

Persona: Portal (``Depends(get_tenant_db)`` + ``get_current_user_with_refresh``),
plain ``def`` handlers, literal ``{"data": ...}`` returns, Pydantic bodies,
arg-less query builders, co_id on every read guard and write, and validators
that answer 400 (or 404 for an unknown bale / invoice) before MySQL can 500.
Conventions mirror finishing_stock.py.

Endpoints (spec §4):
  GET  /setup?co_id&branch_id            -> {qualities, series, statuses, despatch_reasons, bale_source}
  GET  /bales?co_id&branch_id[&as_on][&status=STOCK][&filters…][&invoice_id]
                                         -> {as_on, rows, truncated}
                    (invoice_id = the bales linked to that invoice, live or void — §7e)
  GET  /suggest?co_id&branch_id&bale_series_id&count&purpose=inspect|despatch
                [&as_on][&finishing_quality_id]
                                         -> {rows, available, purpose, bale_series_id}   (§7e)
  GET  /summary?co_id&branch_id[&as_on]  -> {as_on, bale_source, lines, totals, series_lines}
  GET  /invoices?co_id&branch_id[&as_on][&days=120] -> {rows}
  POST /inspect     {co_id, branch_id, bale_entry_ids, inspection_date, inspection_ref?, inspector?, remarks?}
                                         -> {marked}
  POST /uninspect   {co_id, bale_entry_ids} -> {unmarked}          (404 when none active)
  POST /despatch    {co_id, branch_id, bale_entry_ids, invoice_id?, despatch_date?,
                     despatch_reason?, despatch_ref?, remarks?}
                                         -> {linked, relinked, uninspected_sacking}
                    (no invoice: despatch_date + despatch_reason required; with an
                     invoice, bales already despatched WITHOUT one are relinked)
  POST /undespatch  {co_id, bale_entry_ids} -> {unlinked}          (404 when none active)

Write gates evaluate each bale's CURRENT state (rows query as on today): a bale
that is despatched or repacked today is not inspectable, and a bale with a live
link cannot be linked again (a no-invoice despatch is UPDATEd, never
re-inserted) — so one bale never carries two active rows in either table, which
is what keeps the LEFT JOINs in the derivation from fanning a bale out into two
grid rows. The date rule of D5 (tran_date <= the inspection
/ despatch date) is applied here in Python.

Finishing Stock (finishing_stock.py §5 / §7a) imports build_bale_stock_summary,
_bale_source, _summary_lines and _series_lines from here — this module must never
import finishing_stock.
"""

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.bale_query import get_bale_qualities_query
from src.juteProduction.bale_stock_query import (
    get_bale_stock_invoice_lines_query,
    get_bale_stock_invoice_linked_counts_query,
    get_bale_stock_invoice_row_query,
    get_bale_stock_invoices_query,
    get_bale_stock_rows_query,
    get_bale_stock_series_query,
    get_bale_stock_series_summary_query,
    get_bale_stock_source_query,
    get_bale_stock_summary_query,
    insert_bale_despatch_query,
    insert_bale_inspection_query,
    soft_delete_bale_despatches_query,
    soft_delete_bale_inspections_query,
    update_bale_despatch_invoice_query,
)
from src.juteProduction.constants import (
    BACKDATE_DAYS_MAX,
    BALE_DESPATCH_INVOICE_TYPE_KEYS,
    BALE_DESPATCH_REASON_CODES,
    BALE_DESPATCH_REASON_LABELS,
    BALE_DESPATCH_REASONS,
    BALE_DESPATCH_REF_MAX,
    BALE_INSPECTION_INSPECTOR_MAX,
    BALE_INSPECTION_REF_MAX,
    BALE_STOCK_FILTERS,
    BALE_STOCK_IN_STOCK_STATUSES,
    BALE_STOCK_INVOICE_DAYS_DEFAULT,
    BALE_STOCK_MAX_IDS,
    BALE_STOCK_ROW_CAP,
    BALE_STOCK_SOURCE_BALE_PROD,
    BALE_STOCK_SOURCE_MANUAL,
    BALE_STOCK_STATUS_DESPATCHED,
    BALE_STOCK_STATUS_IN_STOCK,
    BALE_STOCK_STATUS_INSPECTED,
    BALE_STOCK_STATUS_REPACKED,
    FINISHING_QUALITY_TYPE_HESSIAN,
    FINISHING_QUALITY_TYPE_SACKING,
    PLANNING_CLOTH_ITEM_TYPE_ID,
)
from src.sales.constants import INVOICE_TYPE_IDS, SALES_STATUS_IDS

router = APIRouter()

# Invoice header states that void a despatch link (D2b / D4). A BIND, never
# interpolated — see finishing_stock_query.DESPATCH_EXCLUDED_STATUS_IDS.
CANCELLED_INVOICE_STATUS_IDS = (
    SALES_STATUS_IDS["REJECTED"],
    SALES_STATUS_IDS["CANCELLED"],
)
# Invoice types a bale may be despatched against (Regular / Hessian / Govt Sacking).
DESPATCH_INVOICE_TYPE_IDS = tuple(
    INVOICE_TYPE_IDS[key] for key in BALE_DESPATCH_INVOICE_TYPE_KEYS
)
# Grid filter codes with the labels the FE shows (spec §4 /setup statuses).
BALE_STOCK_FILTER_LABELS = (
    ("STOCK", "In stock (all)"),
    ("INSPECTED", "Inspected"),
    ("NOT_INSPECTED", "Not inspected"),
    ("DESPATCHED", "Despatched"),
    ("REPACKED", "Repacked"),
    ("ALL", "All"),
)
REMARKS_MAX = 255
INVOICE_DAYS_MAX = 3660  # ~10 years; the picker is a recent-invoice list, not an archive
QUALITY_TYPE_LABELS = {
    FINISHING_QUALITY_TYPE_HESSIAN: "hessian",
    FINISHING_QUALITY_TYPE_SACKING: "sacking",
}
# §7e suggested pick: purpose -> the grid filter code of its eligible set.
# inspect = bales not yet inspected (status IN_STOCK; sacking only, O3);
# despatch = anything in stock (IN_STOCK + INSPECTED).
SUGGEST_PURPOSE_INSPECT = "inspect"
SUGGEST_PURPOSE_DESPATCH = "despatch"
SUGGEST_PURPOSE_FILTERS = {
    SUGGEST_PURPOSE_INSPECT: "NOT_INSPECTED",
    SUGGEST_PURPOSE_DESPATCH: "STOCK",
}
SUGGEST_PURPOSES = tuple(SUGGEST_PURPOSE_FILTERS)

NOT_FOUND_MSG = "Bale {id} was not found for this company / branch"
FUTURE_DATE_MSG = "{field} cannot be in the future"
BACKDATE_MSG = "{field} cannot be more than {days} days in the past"
BAD_STATUS_MSG = f"status must be one of {', '.join(BALE_STOCK_FILTERS)}"
BAD_DATE_MSG = "Invalid {field} format (expected YYYY-MM-DD)"
EMPTY_IDS_MSG = "bale_entry_ids must contain at least one bale"
TOO_MANY_IDS_MSG = f"bale_entry_ids cannot carry more than {BALE_STOCK_MAX_IDS} bales per call"
REF_TOO_LONG_MSG = f"inspection_ref cannot exceed {BALE_INSPECTION_REF_MAX} characters"
INSPECTOR_TOO_LONG_MSG = f"inspector cannot exceed {BALE_INSPECTION_INSPECTOR_MAX} characters"
REMARKS_TOO_LONG_MSG = f"remarks cannot exceed {REMARKS_MAX} characters"
INSPECT_SACKING_ONLY_MSG = (
    "Only sacking bales are inspected — bale {no} ({series}, {item}) is {type}"
)
INSPECT_BEFORE_PRESS_MSG = (
    "Bale {no} ({series}) was pressed on {pressed}, after the inspection date {on}"
)
DESPATCH_BEFORE_PRESS_MSG = (
    "Bale {no} ({series}) was pressed on {pressed}, after the despatch date {on}"
)
ALREADY_INSPECTED_MSG = "Bale {no} ({series}) is already marked inspected (on {on})"
NOT_IN_STOCK_MSG = "Bale {no} ({series}) is not in stock — it is {status}"
ALREADY_DESPATCHED_MSG = "Bale {no} ({series}) is already linked to invoice {invoice}"
ALREADY_DESPATCHED_NO_INVOICE_MSG = (
    "Bale {no} ({series}) is already despatched without invoice (reason {reason}, on {on})"
)
RELINK_STALE_MSG = (
    "Bale {no} ({series}) could not be linked — its despatch row changed meanwhile; "
    "reload and try again"
)
NO_ACTIVE_INSPECTION_MSG = "None of these bales carries an active inspection marking"
NO_ACTIVE_DESPATCH_MSG = "None of these bales carries an active despatch link"
INVOICE_NOT_FOUND_MSG = "Sales invoice not found"
INVOICE_BRANCH_MSG = "The invoice belongs to a different branch"
INVOICE_TYPE_MSG = (
    "Bales can only be despatched against Regular, Hessian or Govt Sacking invoices"
)
INVOICE_CANCELLED_MSG = "The invoice is rejected or cancelled"
DESPATCH_DATE_REQUIRED_MSG = (
    "despatch_date is required — there is no invoice date to default to"
)
DESPATCH_REASON_MSG = (
    "despatch_reason is required when no invoice is given and must be one of "
    + ", ".join(BALE_DESPATCH_REASON_CODES)
)
DESPATCH_REF_TOO_LONG_MSG = f"despatch_ref cannot exceed {BALE_DESPATCH_REF_MAX} characters"
SUGGEST_COUNT_MSG = f"count must be a whole number between 1 and {BALE_STOCK_MAX_IDS}"
SUGGEST_PURPOSE_MSG = "purpose must be " + " or ".join(SUGGEST_PURPOSES)
SUGGEST_SACKING_ONLY_MSG = "Only sacking bales are inspected — series {name} is hessian"
STATUS_WORDS = {
    BALE_STOCK_STATUS_DESPATCHED: "despatched",
    BALE_STOCK_STATUS_REPACKED: "repacked",
    BALE_STOCK_STATUS_INSPECTED: "inspected",
    BALE_STOCK_STATUS_IN_STOCK: "in stock",
}


# =============================================================================
# Request models
# =============================================================================


class BaleIdsBody(BaseModel):
    """POST /uninspect and /undespatch: the bales whose active row to retire."""

    co_id: int
    bale_entry_ids: List[int]


class BaleInspectBody(BaseModel):
    co_id: int
    branch_id: int
    bale_entry_ids: List[int]
    inspection_date: date
    inspection_ref: Optional[str] = None
    inspector: Optional[str] = None
    remarks: Optional[str] = None


class BaleDespatchBody(BaseModel):
    """POST /despatch. ``invoice_id`` None = despatch WITHOUT invoice (§7b):
    then ``despatch_date`` and ``despatch_reason`` are required. With an
    invoice the date defaults to the invoice date and the reason is optional
    (validated when present). ``despatch_ref`` = challan / DO / gate-pass no."""

    co_id: int
    branch_id: int
    bale_entry_ids: List[int]
    invoice_id: Optional[int] = None
    despatch_date: Optional[date] = None
    despatch_reason: Optional[str] = None
    despatch_ref: Optional[str] = None
    remarks: Optional[str] = None


# =============================================================================
# Param + value helpers
# =============================================================================


def _require_int_param(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _optional_int_param(request: Request, name: str) -> Optional[int]:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _optional_date_param(request: Request, name: str) -> Optional[date]:
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=BAD_DATE_MSG.format(field=name))


def _as_on_param(request: Request) -> date:
    """``as_on`` defaults to today and may not be in the future (400)."""
    value = _optional_date_param(request, "as_on") or date.today()
    # A view date, not an entry date: no backdate floor (spec §7d).
    if value > date.today():
        raise HTTPException(status_code=400, detail=FUTURE_DATE_MSG.format(field="as_on"))
    return value


def _not_future(value: date, field: str) -> date:
    """Entry dates: not in the future and not more than BACKDATE_DAYS_MAX days
    back (spec §7d). The `as_on` view date is checked separately and has no floor."""
    today = date.today()
    if value > today:
        raise HTTPException(status_code=400, detail=FUTURE_DATE_MSG.format(field=field))
    if value < today - timedelta(days=BACKDATE_DAYS_MAX):
        raise HTTPException(
            status_code=400, detail=BACKDATE_MSG.format(field=field, days=BACKDATE_DAYS_MAX)
        )
    return value


def _status_filter_param(request: Request) -> Optional[str]:
    """Grid filter code -> the ``:status_filter`` bind (ALL -> NULL = no filter)."""
    raw = (request.query_params.get("status") or "STOCK").strip().upper()
    if raw not in BALE_STOCK_FILTERS:
        raise HTTPException(status_code=400, detail=BAD_STATUS_MSG)
    return None if raw == "ALL" else raw


def _suggest_count_param(request: Request) -> int:
    """``count`` for /suggest: a whole number 1..BALE_STOCK_MAX_IDS (the same
    cap as a write body, since the pick feeds one). Missing, non-numeric and
    out-of-range all answer the one SUGGEST_COUNT_MSG."""
    raw = request.query_params.get("count")
    try:
        count = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=SUGGEST_COUNT_MSG)
    if count < 1 or count > BALE_STOCK_MAX_IDS:
        raise HTTPException(status_code=400, detail=SUGGEST_COUNT_MSG)
    return count


def _suggest_purpose_param(request: Request) -> str:
    """``purpose`` for /suggest: inspect | despatch (case-insensitive), echoed
    back lower-case."""
    raw = (request.query_params.get("purpose") or "").strip().lower()
    if raw not in SUGGEST_PURPOSE_FILTERS:
        raise HTTPException(status_code=400, detail=SUGGEST_PURPOSE_MSG)
    return raw


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _fn(v) -> Optional[float]:
    return None if v is None else float(v)


def _i(v) -> Optional[int]:
    return None if v is None else int(v)


def _s(v) -> Optional[str]:
    return None if v is None else str(v)


def _mt(kg) -> float:
    """kg -> MT at 3 dp, half-up on whole grams (the finishing_stock rule, kept
    byte-identical here because this module must not import finishing_stock)."""
    grams = Decimal(str(_f(kg))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(grams) / 1000


def _validate_ids(ids: List[int]) -> List[int]:
    """Non-empty, capped, de-duplicated in first-seen order (a duplicate id in
    the body would otherwise insert two rows for one bale)."""
    if not ids:
        raise HTTPException(status_code=400, detail=EMPTY_IDS_MSG)
    if len(ids) > BALE_STOCK_MAX_IDS:
        raise HTTPException(status_code=400, detail=TOO_MANY_IDS_MSG)
    seen: Dict[int, None] = {}
    for raw in ids:
        seen.setdefault(int(raw), None)
    return list(seen)


def _validate_text(value: Optional[str], max_len: int, msg: str) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if len(value) > max_len:
        raise HTTPException(status_code=400, detail=msg)
    return value or None


def _validate_reason(value: Optional[str], required: bool) -> Optional[str]:
    """despatch_reason -> a BALE_DESPATCH_REASONS code (upper-cased) or None.
    Required when the despatch carries no invoice; always validated when given."""
    code = (value or "").strip().upper()
    if not code:
        if required:
            raise HTTPException(status_code=400, detail=DESPATCH_REASON_MSG)
        return None
    if code not in BALE_DESPATCH_REASON_CODES:
        raise HTTPException(status_code=400, detail=DESPATCH_REASON_MSG)
    return code


def _mapping(row) -> dict:
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)


# =============================================================================
# Pure helpers (exported for tests and for Finishing Stock)
# =============================================================================


def derive_bale_source(found: bool) -> str:
    """BALE_PROD when the unit has any bale entry as on the date, else MANUAL (D7)."""
    return BALE_STOCK_SOURCE_BALE_PROD if found else BALE_STOCK_SOURCE_MANUAL


def serialize_bale_row(row) -> dict:
    """Flat derivation row -> the BaleStockRow shape (spec §4).

    ``inspection`` / ``despatch`` / ``repacked_by`` are nested objects or None.
    ``despatch`` is returned whenever a despatch row exists — even when its
    invoice is cancelled and the status therefore reads IN_STOCK / INSPECTED —
    so the FE can show "linked to cancelled invoice" and offer undo / re-link
    (D4). A despatch WITHOUT invoice (§7b) has ``invoice_id`` None and every
    invoice sub-field None; ``despatch_reason_label`` is the constants label
    of the code (the raw code when it is not a known one, None when no code).
    Decimal -> float, date -> 'YYYY-MM-DD', weigh_time -> ISO seconds.
    """
    m = _mapping(row)
    weigh_time = m.get("weigh_time")
    if isinstance(weigh_time, datetime):
        weigh_time = weigh_time.isoformat(sep="T", timespec="seconds")
    elif weigh_time is not None:
        weigh_time = str(weigh_time)

    inspection = None
    if m.get("bale_inspection_id") is not None:
        inspection = {
            "bale_inspection_id": _i(m.get("bale_inspection_id")),
            "inspection_date": _s(m.get("inspection_date")),
            "inspection_ref": m.get("inspection_ref"),
            "inspector": m.get("inspector"),
            "remarks": m.get("inspection_remarks"),
        }

    despatch = None
    if m.get("bale_despatch_id") is not None:
        invoice_id = _i(m.get("invoice_id"))
        has_invoice = invoice_id is not None
        reason = _s(m.get("despatch_reason")) or None
        despatch = {
            "bale_despatch_id": _i(m.get("bale_despatch_id")),
            "invoice_id": invoice_id,
            "invoice_no": _i(m.get("invoice_no")) if has_invoice else None,
            "invoice_date": _s(m.get("invoice_date")) if has_invoice else None,
            "invoice_type": _i(m.get("invoice_type")) if has_invoice else None,
            "invoice_status_id": _i(m.get("invoice_status_id")) if has_invoice else None,
            "party_name": m.get("party_name") if has_invoice else None,
            "despatch_date": _s(m.get("despatch_date")),
            "despatch_reason": reason,
            "despatch_reason_label": (
                BALE_DESPATCH_REASON_LABELS.get(reason, reason) if reason else None
            ),
            "despatch_ref": m.get("despatch_ref"),
            "remarks": m.get("despatch_remarks"),
        }

    repacked_by = None
    if m.get("repacked_by_id") is not None:
        repacked_by = {
            "bale_entry_id": _i(m.get("repacked_by_id")),
            "bale_no": _i(m.get("repacked_by_no")),
            "tran_date": _s(m.get("repacked_on")),
        }

    return {
        "bale_entry_id": _i(m.get("bale_entry_id")),
        "tran_date": _s(m.get("tran_date")),
        "entry_type": m.get("entry_type"),
        "bale_series_id": _i(m.get("bale_series_id")),
        "series_name": m.get("series_name"),
        "bale_no": _i(m.get("bale_no")),
        "original_bale_no": _i(m.get("original_bale_no")),
        "finishing_quality_id": _i(m.get("finishing_quality_id")),
        "item_id": _i(m.get("item_id")),
        "item_name": m.get("item_name"),
        "quality_type": _i(m.get("quality_type")),
        "gross_wt": _fn(m.get("gross_wt")),
        "net_wt": _fn(m.get("net_wt")),
        "weigh_time": weigh_time,
        "status": m.get("status"),
        "inspection": inspection,
        "despatch": despatch,
        "repacked_by": repacked_by,
    }


def suggest_sort_key(purpose: str):
    """Sort key over serialised BaleStockRows for the §7e suggested pick. Pure.

    ``despatch``: INSPECTED bales first, then press date, then bale no — the
    oldest inspected stock leaves first, and an uninspected bale is proposed
    only once the inspected ones run out. ``inspect``: press date, then bale
    no — the oldest unmarked bale is proposed first. bale_entry_id breaks any
    remaining tie so the proposal is stable between two calls. tran_date is
    the 'YYYY-MM-DD' string serialize_bale_row emits, which orders as a date.
    """
    inspected_first = purpose == SUGGEST_PURPOSE_DESPATCH

    def key(row: dict):
        rank = 0
        if inspected_first and row.get("status") != BALE_STOCK_STATUS_INSPECTED:
            rank = 1
        return (
            rank,
            row.get("tran_date") or "",
            row.get("bale_no") or 0,
            row.get("bale_entry_id") or 0,
        )

    return key


def _group_totals(lines: list) -> dict:
    return {
        "bales": sum(line["bales"] for line in lines),
        "inspected_bales": sum(line["inspected_bales"] for line in lines),
        "wt_mt": round(sum(line["wt_mt"] for line in lines), 3),
    }


def _summary_line(m: dict) -> dict:
    """The measures one summary row (per quality OR per series) turns into.

    Weights are the ACTUAL weighed net (D6): per_wt_kg = total_kg / bales at
    3 dp (None when no bale is in stock), wt_mt = kg / 1000 at 3 dp. One
    function for both groupings so a series line can never round differently
    from the quality line it is part of.
    """
    bales = int(_f(m.get("bales")))
    total_kg = round(_f(m.get("total_kg")), 3)
    return {
        "finishing_quality_id": _i(m.get("finishing_quality_id")),
        "item_id": _i(m.get("item_id")),
        "item_name": m.get("item_name"),
        "quality_type": _i(m.get("quality_type")),
        "bales": bales,
        "inspected_bales": int(_f(m.get("inspected_bales"))),
        "total_kg": total_kg,
        "inspected_kg": round(_f(m.get("inspected_kg")), 3),
        "per_wt_kg": round(total_kg / bales, 3) if bales else None,
        "wt_mt": _mt(total_kg),
        "despatched_bales": int(_f(m.get("despatched_bales"))),
    }


def build_bale_stock_series_lines(series_rows: list) -> list:
    """Per-series summary rows -> [SeriesLine] (spec §7a). Pure.

    ``series_rows`` are get_bale_stock_series_summary_query() rows as dicts. A
    SeriesLine is a summary line plus ``bale_series_id`` / ``series_name``.
    Every quality type is returned (consumers filter to sacking). Sorted
    quality_type, series_name, item_name — the statement's ORDER BY, re-applied
    here so the order holds whatever produced the rows.
    """
    lines = []
    for r in series_rows:
        m = _mapping(r)
        line = {
            "bale_series_id": _i(m.get("bale_series_id")),
            "series_name": _s(m.get("series_name")),
        }
        line.update(_summary_line(m))
        lines.append(line)
    lines.sort(
        key=lambda l: (
            l["quality_type"] or 0,
            (l["series_name"] or "").upper(),
            (l["item_name"] or "").upper(),
        )
    )
    return lines


def build_bale_stock_summary(rows: list, series_rows: Optional[list] = None) -> dict:
    """Per-quality summary rows -> {lines, totals, series_lines} (spec §4 / §7a).
    Pure.

    ``rows`` are get_bale_stock_summary_query() rows as dicts; ``series_rows``
    (optional) get_bale_stock_series_summary_query() rows — they become
    ``series_lines`` (``[]`` when not given) and are NOT added into ``totals``:
    a series line is a breakup of a quality line, never extra stock. Totals
    carry Hessian / Sacking groups so the summary table can print the two
    subtotals the sheet has. Finishing Stock (§5) reuses ``lines`` for its
    derived own-company block and ``series_lines`` for the series breakup.
    """
    lines = [_summary_line(_mapping(r)) for r in rows]

    hessian = [l for l in lines if l["quality_type"] == FINISHING_QUALITY_TYPE_HESSIAN]
    sacking = [l for l in lines if l["quality_type"] == FINISHING_QUALITY_TYPE_SACKING]
    totals = _group_totals(lines)
    totals["despatched_bales"] = sum(line["despatched_bales"] for line in lines)
    totals["hessian"] = _group_totals(hessian)
    totals["sacking"] = _group_totals(sacking)
    return {
        "lines": lines,
        "totals": totals,
        "series_lines": build_bale_stock_series_lines(series_rows or []),
    }


# =============================================================================
# DB helpers
# =============================================================================


def _rows_binds(co_id: int, branch_id: int, as_on: date, **filters) -> dict:
    """Every bind get_bale_stock_rows_query() declares, filters defaulted to
    "no filter" — one place, so a missing key can never 500 a read."""
    binds = {
        "co_id": int(co_id),
        "branch_id": int(branch_id),
        "as_on": as_on,
        "cancelled_status": list(CANCELLED_INVOICE_STATUS_IDS),
        "quality_type": None,
        "finishing_quality_id": None,
        "bale_series_id": None,
        "bale_no_from": None,
        "bale_no_to": None,
        "date_from": None,
        "date_to": None,
        "bale_entry_ids_set": 0,
        "bale_entry_ids": [0],
        "invoice_id": None,  # §7e: bales linked to one invoice; NULL = no filter
        "status_filter": None,
        "row_cap": BALE_STOCK_ROW_CAP + 1,
    }
    binds.update(filters)
    return binds


def _fetch_bales_for_ids(
    db: Session, co_id: int, branch_id: int, ids: List[int], as_on: date
) -> List[dict]:
    """The derivation rows for an explicit id list — no status filter, cap =
    len(ids) + 1 so a fan-out (two active links on one bale) would show up as
    an extra row rather than silently truncating."""
    result = db.execute(
        get_bale_stock_rows_query(),
        _rows_binds(
            co_id,
            branch_id,
            as_on,
            bale_entry_ids_set=1,
            bale_entry_ids=[int(i) for i in ids],
            row_cap=len(ids) + 1,
        ),
    ).fetchall()
    return [serialize_bale_row(r) for r in result]


def _bale_source(db: Session, co_id: int, branch_id: Optional[int], as_on: date) -> str:
    row = db.execute(
        get_bale_stock_source_query(),
        {"co_id": int(co_id), "branch_id": branch_id, "as_on": as_on},
    ).fetchone()
    return derive_bale_source(row is not None)


def _summary_binds(co_id: int, branch_id: int, as_on: date) -> dict:
    """The one params dict both summary statements (quality, series) declare."""
    return {
        "co_id": int(co_id),
        "branch_id": int(branch_id),
        "as_on": as_on,
        "cancelled_status": list(CANCELLED_INVOICE_STATUS_IDS),
    }


def _summary_lines(db: Session, co_id: int, branch_id: int, as_on: date) -> dict:
    """Per-quality figures as on the date: build_bale_stock_summary() of the
    quality statement (``series_lines`` is [] here — see _series_lines)."""
    rows = db.execute(
        get_bale_stock_summary_query(), _summary_binds(co_id, branch_id, as_on)
    ).fetchall()
    return build_bale_stock_summary([dict(r._mapping) for r in rows])


def _series_lines(db: Session, co_id: int, branch_id: int, as_on: date) -> List[dict]:
    """Per-series figures as on the date (spec §7a): the series statement run
    with the SAME binds as the quality one, every quality type included."""
    rows = db.execute(
        get_bale_stock_series_summary_query(), _summary_binds(co_id, branch_id, as_on)
    ).fetchall()
    return build_bale_stock_series_lines([dict(r._mapping) for r in rows])


def _bales_by_id_or_404(
    db: Session, co_id: int, branch_id: int, ids: List[int], as_on: date
) -> List[dict]:
    """Rows for every id, in the order asked; 404 naming the first id that does
    not resolve to an active bale of this company / branch."""
    by_id = {r["bale_entry_id"]: r for r in _fetch_bales_for_ids(db, co_id, branch_id, ids, as_on)}
    rows = []
    for bale_id in ids:
        row = by_id.get(bale_id)
        if row is None:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG.format(id=bale_id))
        rows.append(row)
    return rows


def _label(row: dict) -> dict:
    return {"no": row.get("bale_no"), "series": row.get("series_name") or "-"}


# =============================================================================
# Setup
# =============================================================================


@router.get("/setup")
def setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_int_param(request, "co_id")
    branch_id = _optional_int_param(request, "branch_id")
    try:
        qualities = []
        for r in db.execute(
            get_bale_qualities_query(), {"co_id": co_id, "branch_id": branch_id}
        ).fetchall():
            q = dict(r._mapping)
            for key in ("std_bale_weight", "packsheet_wt", "std_mr_pct", "weft_count"):
                if key in q:
                    q[key] = _fn(q.get(key))
            qualities.append(q)

        series = [
            dict(r._mapping)
            for r in db.execute(
                get_bale_stock_series_query(), {"co_id": co_id, "branch_id": branch_id}
            ).fetchall()
        ]

        return {
            "data": {
                "qualities": qualities,
                "series": series,
                "statuses": [
                    {"code": code, "label": label} for code, label in BALE_STOCK_FILTER_LABELS
                ],
                # §7b: why a bale may leave without an invoice (picker order)
                "despatch_reasons": [
                    {"code": code, "label": label} for code, label in BALE_DESPATCH_REASONS
                ],
                "bale_source": _bale_source(db, co_id, branch_id, date.today()),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Reads
# =============================================================================


@router.get("/bales")
def bales(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The bale grid as on a date. Default filter STOCK (both in-stock
    statuses); ``truncated`` is true when more than BALE_STOCK_ROW_CAP rows
    matched — narrow the filters rather than page a stock register.
    ``invoice_id`` (§7e) lists the bales whose active despatch row points at
    that invoice, live or void — the sales-invoice page's read-only list, which
    passes status=ALL so a void link (cancelled invoice, status back to
    IN_STOCK / INSPECTED) still shows."""
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    as_on = _as_on_param(request)
    status_filter = _status_filter_param(request)
    filters = {
        "quality_type": _optional_int_param(request, "quality_type"),
        "finishing_quality_id": _optional_int_param(request, "finishing_quality_id"),
        "bale_series_id": _optional_int_param(request, "bale_series_id"),
        "bale_no_from": _optional_int_param(request, "bale_no_from"),
        "bale_no_to": _optional_int_param(request, "bale_no_to"),
        "date_from": _optional_date_param(request, "date_from"),
        "date_to": _optional_date_param(request, "date_to"),
        "invoice_id": _optional_int_param(request, "invoice_id"),
    }
    try:
        result = db.execute(
            get_bale_stock_rows_query(),
            _rows_binds(
                co_id,
                branch_id,
                as_on,
                status_filter=status_filter,
                row_cap=BALE_STOCK_ROW_CAP + 1,
                **filters,
            ),
        ).fetchall()
        truncated = len(result) > BALE_STOCK_ROW_CAP
        rows = [serialize_bale_row(r) for r in result[:BALE_STOCK_ROW_CAP]]
        return {"data": {"as_on": str(as_on), "rows": rows, "truncated": truncated}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/suggest")
def suggest(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Suggested default pick (spec §7e, owner O1 / O2): the user names the
    state (bale series) and how many bales, the system proposes that many,
    the user adjusts by hand. Nothing is written.

    One statement — the rows query filtered to the series (and optionally
    one quality) with the purpose's grid code: ``inspect`` = NOT_INSPECTED
    (status IN_STOCK), ``despatch`` = STOCK (IN_STOCK + INSPECTED). Then in
    Python: for ``inspect`` every eligible bale must be sacking (O3) — a
    hessian series answers 400 SUGGEST_SACKING_ONLY_MSG; the rows are ordered
    by suggest_sort_key (despatch: inspected first, then oldest, then bale no;
    inspect: oldest, then bale no); ``rows`` = the first ``count`` and
    ``available`` = the eligible total, so the UI can say "only 12 available".
    A series with nothing eligible is 200 with [] / 0, not 404. The fetch is
    capped like the grid (BALE_STOCK_ROW_CAP + 1), so ``available`` saturates
    there."""
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    bale_series_id = _require_int_param(request, "bale_series_id")
    count = _suggest_count_param(request)
    purpose = _suggest_purpose_param(request)
    as_on = _as_on_param(request)
    finishing_quality_id = _optional_int_param(request, "finishing_quality_id")
    try:
        result = db.execute(
            get_bale_stock_rows_query(),
            _rows_binds(
                co_id,
                branch_id,
                as_on,
                bale_series_id=bale_series_id,
                finishing_quality_id=finishing_quality_id,
                status_filter=SUGGEST_PURPOSE_FILTERS[purpose],
                row_cap=BALE_STOCK_ROW_CAP + 1,
            ),
        ).fetchall()
        eligible = [serialize_bale_row(r) for r in result]
        if purpose == SUGGEST_PURPOSE_INSPECT:
            for row in eligible:
                if row.get("quality_type") != FINISHING_QUALITY_TYPE_SACKING:
                    raise HTTPException(
                        status_code=400,
                        detail=SUGGEST_SACKING_ONLY_MSG.format(
                            name=row.get("series_name") or bale_series_id
                        ),
                    )
        eligible.sort(key=suggest_sort_key(purpose))
        return {
            "data": {
                "rows": eligible[:count],
                "available": len(eligible),
                "purpose": purpose,
                "bale_series_id": bale_series_id,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/summary")
def summary(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    as_on = _as_on_param(request)
    try:
        bale_source = _bale_source(db, co_id, branch_id, as_on)
        data = _summary_lines(db, co_id, branch_id, as_on)
        series_lines = _series_lines(db, co_id, branch_id, as_on)
        return {
            "data": {
                "as_on": str(as_on),
                "bale_source": bale_source,
                "lines": data["lines"],
                "totals": data["totals"],
                # §7a: the same figures broken up per bale series (all quality
                # types; the page shows the sacking ones)
                "series_lines": series_lines,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/invoices")
def invoices(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Candidate invoices for a despatch link: the branch's Regular / Hessian /
    Govt Sacking invoices of the last ``days`` days up to as_on, not rejected /
    cancelled, newest first, with their cloth lines and how many bales are
    already linked to each."""
    _require_int_param(request, "co_id")  # persona guard; sales_invoice has no co_id
    branch_id = _require_int_param(request, "branch_id")
    as_on = _as_on_param(request)
    days = _optional_int_param(request, "days")
    if days is None:
        days = BALE_STOCK_INVOICE_DAYS_DEFAULT
    if days < 0 or days > INVOICE_DAYS_MAX:
        raise HTTPException(
            status_code=400, detail=f"days must be between 0 and {INVOICE_DAYS_MAX}"
        )
    try:
        heads = [
            dict(r._mapping)
            for r in db.execute(
                get_bale_stock_invoices_query(),
                {
                    "branch_id": branch_id,
                    "invoice_types": list(DESPATCH_INVOICE_TYPE_IDS),
                    "cancelled_status": list(CANCELLED_INVOICE_STATUS_IDS),
                    "date_from": as_on - timedelta(days=days),
                    "as_on": as_on,
                },
            ).fetchall()
        ]

        lines_by_invoice: Dict[int, list] = {}
        linked_by_invoice: Dict[int, int] = {}
        if heads:
            invoice_ids = [int(h["invoice_id"]) for h in heads]
            for r in db.execute(
                get_bale_stock_invoice_lines_query(),
                {"invoice_ids": invoice_ids, "cloth_item_type_id": PLANNING_CLOTH_ITEM_TYPE_ID},
            ).fetchall():
                m = dict(r._mapping)
                lines_by_invoice.setdefault(int(m["invoice_id"]), []).append(
                    {
                        "invoice_line_item_id": _i(m.get("invoice_line_item_id")),
                        "item_id": _i(m.get("item_id")),
                        "item_name": m.get("item_name"),
                        "quantity": _fn(m.get("quantity")),
                        "uom_name": m.get("uom_name"),
                        "qty_bales": _fn(m.get("qty_bales")),
                    }
                )
            for r in db.execute(
                get_bale_stock_invoice_linked_counts_query(), {"invoice_ids": invoice_ids}
            ).fetchall():
                m = dict(r._mapping)
                linked_by_invoice[int(m["invoice_id"])] = int(_f(m.get("linked_bales")))

        rows = []
        for h in heads:
            invoice_id = int(h["invoice_id"])
            rows.append(
                {
                    "invoice_id": invoice_id,
                    "invoice_no": _i(h.get("invoice_no")),
                    "invoice_date": _s(h.get("invoice_date")),
                    "invoice_type": _i(h.get("invoice_type")),
                    "invoice_type_name": h.get("invoice_type_name"),
                    "party_id": _i(h.get("party_id")),
                    "party_name": h.get("party_name"),
                    "status_id": _i(h.get("status_id")),
                    "lines": lines_by_invoice.get(invoice_id, []),
                    "linked_bales": linked_by_invoice.get(invoice_id, 0),
                }
            )
        return {"data": {"as_on": str(as_on), "days": days, "rows": rows}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Inspection
# =============================================================================


@router.post("/inspect")
def inspect(
    body: BaleInspectBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Mark bales inspected. Gates, in order: body (ids, date, lengths) ->
    every id resolves (404) -> sacking only -> pressed on or before the
    inspection date -> currently IN_STOCK (not already inspected, not
    despatched / repacked). All-or-nothing: one bad bale fails the whole call
    before any INSERT runs."""
    try:
        ids = _validate_ids(body.bale_entry_ids)
        inspection_date = _not_future(body.inspection_date, "inspection_date")
        inspection_ref = _validate_text(body.inspection_ref, BALE_INSPECTION_REF_MAX, REF_TOO_LONG_MSG)
        inspector = _validate_text(body.inspector, BALE_INSPECTION_INSPECTOR_MAX, INSPECTOR_TOO_LONG_MSG)
        remarks = _validate_text(body.remarks, REMARKS_MAX, REMARKS_TOO_LONG_MSG)

        rows = _bales_by_id_or_404(db, body.co_id, body.branch_id, ids, date.today())
        for row in rows:
            label = _label(row)
            if row.get("quality_type") != FINISHING_QUALITY_TYPE_SACKING:
                raise HTTPException(
                    status_code=400,
                    detail=INSPECT_SACKING_ONLY_MSG.format(
                        **label,
                        item=row.get("item_name") or "-",
                        type=QUALITY_TYPE_LABELS.get(row.get("quality_type"), "not sacking"),
                    ),
                )
            pressed = row.get("tran_date")
            if pressed and date.fromisoformat(pressed) > inspection_date:
                raise HTTPException(
                    status_code=400,
                    detail=INSPECT_BEFORE_PRESS_MSG.format(
                        **label, pressed=pressed, on=str(inspection_date)
                    ),
                )
            status = row.get("status")
            if status == BALE_STOCK_STATUS_INSPECTED:
                raise HTTPException(
                    status_code=400,
                    detail=ALREADY_INSPECTED_MSG.format(
                        **label, on=(row.get("inspection") or {}).get("inspection_date")
                    ),
                )
            if status != BALE_STOCK_STATUS_IN_STOCK:
                raise HTTPException(
                    status_code=400,
                    detail=NOT_IN_STOCK_MSG.format(
                        **label, status=STATUS_WORDS.get(status, status)
                    ),
                )

        for row in rows:
            db.execute(
                insert_bale_inspection_query(),
                {
                    "co_id": int(body.co_id),
                    "branch_id": int(body.branch_id),
                    "bale_entry_id": int(row["bale_entry_id"]),
                    "inspection_date": inspection_date,
                    "inspection_ref": inspection_ref,
                    "inspector": inspector,
                    "remarks": remarks,
                    "updated_by": token_data.get("user_id"),
                },
            )
        db.commit()
        return {"data": {"marked": len(rows)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/uninspect")
def uninspect(
    body: BaleIdsBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Undo the inspection marking of the given bales (soft delete). 404 when
    none of them carries an active marking of this company."""
    try:
        ids = _validate_ids(body.bale_entry_ids)
        result = db.execute(
            soft_delete_bale_inspections_query(),
            {
                "bale_entry_ids": ids,
                "co_id": int(body.co_id),
                "updated_by": token_data.get("user_id"),
            },
        )
        if not result.rowcount:
            raise HTTPException(status_code=404, detail=NO_ACTIVE_INSPECTION_MSG)
        db.commit()
        return {"data": {"unmarked": int(result.rowcount)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Despatch link
# =============================================================================


def _invoice_for_despatch(db: Session, invoice_id: int, branch_id: int):
    """The invoice gates (spec §4 /despatch): exists and active (404), same
    branch, allowed type, not rejected / cancelled. Returns the header row."""
    inv = db.execute(get_bale_stock_invoice_row_query(), {"invoice_id": int(invoice_id)}).fetchone()
    if inv is None or int(getattr(inv, "active", 1) or 0) != 1:
        raise HTTPException(status_code=404, detail=INVOICE_NOT_FOUND_MSG)
    if _i(getattr(inv, "branch_id", None)) != int(branch_id):
        raise HTTPException(status_code=400, detail=INVOICE_BRANCH_MSG)
    if _i(getattr(inv, "invoice_type", None)) not in DESPATCH_INVOICE_TYPE_IDS:
        raise HTTPException(status_code=400, detail=INVOICE_TYPE_MSG)
    status_id = _i(getattr(inv, "status_id", None))
    if status_id is not None and status_id in CANCELLED_INVOICE_STATUS_IDS:
        raise HTTPException(status_code=400, detail=INVOICE_CANCELLED_MSG)
    return inv


@router.post("/despatch")
def despatch(
    body: BaleDespatchBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Despatch bales — against a sales invoice, or WITHOUT one (§7b) — and
    link no-invoice despatches to their invoice later.

    Body gates first (ids, lengths, reason code, date not in the future), then
    the invoice gates when an invoice is given (exists / branch / type / not
    cancelled; the date defaults to the invoice date). Without an invoice the
    date AND a reason are required. Then every bale must resolve (404) and, by
    its CURRENT state:
      IN_STOCK / INSPECTED      -> INSERT (pressed on or before the date); a
                                   VOID link (cancelled invoice) on the bale is
                                   retired in the same transaction
      DESPATCHED, no invoice    -> with an invoice in the body: UPDATE that row's
                                   invoice_id ("link later", despatch date kept)
                                   = relinked; without one: 400
      DESPATCHED, live invoice  -> 400
      REPACKED                  -> 400
    All-or-nothing. Despatch without inspection is allowed (D9); the response
    counts the sacking bales (inserted or relinked) that carry no marking so the
    UI can warn."""
    try:
        ids = _validate_ids(body.bale_entry_ids)
        remarks = _validate_text(body.remarks, REMARKS_MAX, REMARKS_TOO_LONG_MSG)
        despatch_ref = _validate_text(
            body.despatch_ref, BALE_DESPATCH_REF_MAX, DESPATCH_REF_TOO_LONG_MSG
        )
        has_invoice = body.invoice_id is not None
        despatch_reason = _validate_reason(body.despatch_reason, required=not has_invoice)
        if body.despatch_date is not None:
            _not_future(body.despatch_date, "despatch_date")

        despatch_date = body.despatch_date
        if has_invoice:
            inv = _invoice_for_despatch(db, body.invoice_id, body.branch_id)
            despatch_date = despatch_date or getattr(inv, "invoice_date", None)
        if despatch_date is None:
            raise HTTPException(status_code=400, detail=DESPATCH_DATE_REQUIRED_MSG)
        if isinstance(despatch_date, datetime):
            despatch_date = despatch_date.date()
        despatch_date = _not_future(despatch_date, "despatch_date")

        rows = _bales_by_id_or_404(db, body.co_id, body.branch_id, ids, date.today())
        insert_rows: List[dict] = []
        relink_rows: List[dict] = []
        void_link_ids: List[int] = []
        uninspected_sacking = 0
        for row in rows:
            label = _label(row)
            status = row.get("status")
            link = row.get("despatch") or {}
            if status == BALE_STOCK_STATUS_DESPATCHED:
                if link.get("invoice_id") is not None:
                    raise HTTPException(
                        status_code=400,
                        detail=ALREADY_DESPATCHED_MSG.format(
                            **label, invoice=link.get("invoice_no") or link.get("invoice_id")
                        ),
                    )
                if not has_invoice:
                    raise HTTPException(
                        status_code=400,
                        detail=ALREADY_DESPATCHED_NO_INVOICE_MSG.format(
                            **label,
                            reason=link.get("despatch_reason_label")
                            or link.get("despatch_reason")
                            or "-",
                            on=link.get("despatch_date"),
                        ),
                    )
                # despatched without invoice + an invoice in the body = link later
                relink_rows.append(row)
            elif status not in BALE_STOCK_IN_STOCK_STATUSES:
                raise HTTPException(
                    status_code=400,
                    detail=NOT_IN_STOCK_MSG.format(
                        **label, status=STATUS_WORDS.get(status, status)
                    ),
                )
            else:
                pressed = row.get("tran_date")
                if pressed and date.fromisoformat(pressed) > despatch_date:
                    raise HTTPException(
                        status_code=400,
                        detail=DESPATCH_BEFORE_PRESS_MSG.format(
                            **label, pressed=pressed, on=str(despatch_date)
                        ),
                    )
                if row.get("despatch") is not None:
                    # in stock yet linked = the link's invoice was cancelled (void)
                    void_link_ids.append(int(row["bale_entry_id"]))
                insert_rows.append(row)
            if (
                row.get("quality_type") == FINISHING_QUALITY_TYPE_SACKING
                and row.get("inspection") is None
            ):
                uninspected_sacking += 1

        user_id = token_data.get("user_id")
        if void_link_ids:
            db.execute(
                soft_delete_bale_despatches_query(),
                {"bale_entry_ids": void_link_ids, "co_id": int(body.co_id), "updated_by": user_id},
            )
        for row in insert_rows:
            db.execute(
                insert_bale_despatch_query(),
                {
                    "co_id": int(body.co_id),
                    "branch_id": int(body.branch_id),
                    "bale_entry_id": int(row["bale_entry_id"]),
                    "invoice_id": int(body.invoice_id) if has_invoice else None,
                    "invoice_line_item_id": None,
                    "despatch_date": despatch_date,
                    "despatch_reason": despatch_reason,
                    "despatch_ref": despatch_ref,
                    "remarks": remarks,
                    "updated_by": user_id,
                },
            )
        for row in relink_rows:
            result = db.execute(
                update_bale_despatch_invoice_query(),
                {
                    "invoice_id": int(body.invoice_id),
                    "despatch_reason": despatch_reason,
                    "despatch_ref": despatch_ref,
                    "remarks": remarks,
                    "updated_by": user_id,
                    "bale_despatch_id": int(row["despatch"]["bale_despatch_id"]),
                    "co_id": int(body.co_id),
                },
            )
            if not result.rowcount:
                # the row was linked or retired between the read and this write
                raise HTTPException(status_code=400, detail=RELINK_STALE_MSG.format(**_label(row)))
        db.commit()
        return {
            "data": {
                "linked": len(insert_rows),
                "relinked": len(relink_rows),
                "uninspected_sacking": uninspected_sacking,
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/undespatch")
def undespatch(
    body: BaleIdsBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Undo the despatch link of the given bales (soft delete) — they are back
    in stock. 404 when none of them carries an active link of this company."""
    try:
        ids = _validate_ids(body.bale_entry_ids)
        result = db.execute(
            soft_delete_bale_despatches_query(),
            {
                "bale_entry_ids": ids,
                "co_id": int(body.co_id),
                "updated_by": token_data.get("user_id"),
            },
        )
        if not result.rowcount:
            raise HTTPException(status_code=404, detail=NO_ACTIVE_DESPATCH_MSG)
        db.commit()
        return {"data": {"unlinked": int(result.rowcount)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

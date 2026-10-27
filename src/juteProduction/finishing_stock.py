"""Finishing stock — daily physical count of the finishing floor
(/api/finishingStock).

Every morning the department counts what is physically standing there: pressed
bales waiting for despatch (per owner company — the mill presses for its group
companies too) and loose WIP at each stage (cuts, bundles, pieces), counted AND
weighed. This router stores that count, one row per lot, and /report renders it
in the shape of the mill's Excel sheet.

Shape follows winding_stock.py: literal {"data": ...} returns, Pydantic bodies,
arg-less query builders, soft delete, and validators that answer with a 400
rather than letting MySQL raise a 500. Handlers are plain `def`.

total_kg is never stored — it is wt_kg (loose, weighed) or qty * wt_per_unit_kg
(bales), computed at read, so correcting a weight fixes history.

No day lock and no duplicate probe, both deliberate: several lines for one
(stage, quality) are separate LOTS, exactly as the shop-floor sheet records them.

Bale source (Bale Stock & Inspection spec 2026-10-12, D7): once the branch has
any Bale Production entry dated on or before the day (``bale_source ==
"BALE_PROD"``) the OWN-company bale figures are derived from those entries
(bale_stock.build_bale_stock_summary) — /rows_by_date returns them as
``derived_bales``, flags manual own-company BALE rows ``superseded``, /report
builds the own block from them (with ``inspected_bales``), and /row_create and
/row_edit refuse a new own-company BALE line with 400. Bales held for OTHER
owners stay hand-counted in both modes. Before the first bale entry
(``"MANUAL"``) nothing changes.

Series breakup (spec §7a): sacking bales are numbered per consignee series
(PUNJAB, BIHAR …), so in BALE_PROD mode /rows_by_date also returns the sacking
series lines as ``derived_series`` and the report's own bale block carries
``series`` + ``series_total`` (bale_stock._series_lines). MANUAL mode and
other-owner blocks carry empty ones and run no extra statement.
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
# Bale Stock & Inspection (spec 2026-10-12 §5): own-company bales are DERIVED from
# Bale Production once the branch has bale entries. Import, do NOT copy — the
# derivation, the source probe and the summary shape live in bale_stock.py.
from src.juteProduction.bale_stock import (
    _bale_source,
    _series_lines,
    _summary_lines,
    build_bale_stock_summary,
)
from src.juteProduction.constants import (
    BACKDATE_DAYS_MAX,
    BALE_STOCK_SOURCE_BALE_PROD,
    BALE_STOCK_SOURCE_MANUAL,
    FINISHING_ID_TYPE_QLTY,
    FINISHING_QUALITY_TYPE_HESSIAN,
    FINISHING_QUALITY_TYPE_SACKING,
    FINISHING_STOCK_BUCKET_BALE,
    FINISHING_STOCK_BUCKETS,
    FINISHING_STOCK_LOOSE_QTY_UOMS,
    FINISHING_STOCK_QTY_MAX,
    FINISHING_STOCK_QTY_UOM_BALE,
    FINISHING_STOCK_STAGE_CODES,
    FINISHING_STOCK_STAGES,
    FINISHING_STOCK_WORK_BY,
    FINISHING_STOCK_WT_MAX,
    PLANNING_CLOTH_ITEM_TYPE_ID,
)
from src.juteProduction.finishing_query import get_finishing_quality_for_entry_query
from src.juteProduction.finishing_stock_query import (
    DESPATCH_EXCLUDED_STATUS_IDS,
    copy_finishing_stock_previous_day_query,
    get_finishing_stock_active_by_id_query,
    get_finishing_stock_by_date_query,
    get_finishing_stock_companies_query,
    get_finishing_stock_cut_weights_query,
    get_finishing_stock_despatch_query,
    get_finishing_stock_pcs_per_bundle_query,
    get_finishing_stock_prev_entry_date_query,
    get_finishing_stock_qualities_query,
    get_finishing_stock_unit_names_query,
    insert_finishing_stock_row_query,
    soft_delete_finishing_stock_row_query,
    update_finishing_stock_row_query,
    verify_finishing_stock_owner_co_query,
)

router = APIRouter()

FINISHING_STOCK_STAGE_LABELS = dict(FINISHING_STOCK_STAGES)

# Spec-sheet coordinates of the pieces-per-bundle standard the bundle weight is
# derived from (spec addendum §6). Bound, never interpolated.
BUNDLE_PROCESS = "sacksewing"
BUNDLE_PARAM = "pcs_per_bundle"
STANDARD_VALUE_ROLE = "standard"  # spec-sheet value_role, as every other target-map caller spells it

NOT_FOUND_MSG = "Finishing stock line not found"
FUTURE_DATE_MSG = "tran_date cannot be in the future"
BACKDATE_MSG = f"tran_date cannot be more than {BACKDATE_DAYS_MAX} days in the past"
BAD_BUCKET_MSG = f"bucket must be one of {', '.join(FINISHING_STOCK_BUCKETS)}"
BAD_QUALITY_MSG = "finishing_quality_id must be an active finishing quality of this company"
BAD_OWNER_MSG = "owner_co_id must be an existing company"
BALE_STAGE_MSG = "a bale line carries no stage"
BALE_WORK_BY_MSG = "work_by applies to loose stock only"
BALE_UOM_MSG = f"a bale line must be counted in {FINISHING_STOCK_QTY_UOM_BALE}"
BALE_WT_KG_MSG = "a bale line carries a per-bale weight, not a weighed total"
BALE_QTY_MSG = f"qty must be greater than 0 and at most {FINISHING_STOCK_QTY_MAX}"
BALE_WT_REQUIRED_MSG = (
    "per-bale weight required — this quality has no standard bale weight, "
    "so enter wt_per_unit_kg on the line"
)
BALE_WT_GATE_MSG = (
    f"wt_per_unit_kg must be greater than 0 and at most {FINISHING_STOCK_WT_MAX}"
)
LOOSE_OWNER_MSG = "owner_co_id applies to bales only"
LOOSE_STAGE_MSG = f"stage must be one of {', '.join(FINISHING_STOCK_STAGE_CODES)}"
LOOSE_WORK_BY_MSG = "work_by must be 0 (Dept) or 1 (Contract)"
LOOSE_UOM_MSG = f"qty_uom must be one of {', '.join(FINISHING_STOCK_LOOSE_QTY_UOMS)}"
LOOSE_QTY_MSG = f"qty must be 0 or more and at most {FINISHING_STOCK_QTY_MAX}"
LOOSE_WT_MSG = (
    f"wt_kg is required on a loose line and must be 0 or more "
    f"and at most {FINISHING_STOCK_WT_MAX}"
)
LOOSE_WT_PER_UNIT_MSG = "wt_per_unit_kg applies to bales only"
REMARKS_TOO_LONG_MSG = "remarks cannot exceed 255 characters"
COPY_TARGET_NOT_EMPTY_MSG = (
    "This date already has counted lines. Delete them before copying a previous day."
)
NO_PREVIOUS_ENTRY_MSG = "No earlier counted day exists to copy from"
# Owner ruling 2026-10-12: once bales are derived, the page takes loose stock
# only — no manual BALE line at all, whoever owns the bales.
OWN_BALE_DERIVED_MSG = (
    "Pressed bales are counted from Bale Production for this date — this page "
    "takes loose finishing stock only"
)
BALE_DERIVED_MSG = OWN_BALE_DERIVED_MSG


# =============================================================================
# Request models
# =============================================================================


class FinishingStockEditBody(BaseModel):
    """Editable fields. co_id / branch_id / tran_date are NOT here: the identity
    of a physical count is (unit, date), so a line is deleted and re-added to
    move it — the same precedent winding stock and waste entry follow."""

    bucket: str
    finishing_quality_id: int
    stage: Optional[str] = None
    work_by: int = 0
    owner_co_id: Optional[int] = None
    qty: float
    qty_uom: str
    wt_per_unit_kg: Optional[float] = None
    wt_kg: Optional[float] = None
    remarks: Optional[str] = None


class FinishingStockCreateBody(FinishingStockEditBody):
    co_id: int
    branch_id: int
    tran_date: date


class FinishingStockDayRef(BaseModel):
    """A (co, branch, date) unit — used by copy-previous-day."""

    co_id: int
    branch_id: int
    tran_date: date


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


def _f_or_none(v) -> Optional[float]:
    return None if v is None else float(v)


def total_kg_of(qty, wt_per_unit_kg, wt_kg) -> float:
    """The one weight rule, in one place: weighed total wins, else count x unit.

    Module-level and shared with the report builder so a line can never total
    one way on the entry grid and another on the report.
    """
    if wt_kg is not None:
        return round(float(wt_kg), 3)
    if wt_per_unit_kg is None:
        return 0.0
    return round(_f(qty) * float(wt_per_unit_kg), 3)


def unit_weights_for(
    quality: dict, cut_wt_by_item: dict, pcs_per_bundle_by_qid: dict
) -> dict:
    """The three derived unit weights of one finishing quality (spec §6).

    Derived at read and never stored — ``wt_kg`` on a counted line stays the
    weighed figure and always wins; these only prefill the form. Any unit that
    does not resolve is None, which the form reads as "leave Weight kg manual",
    so a missing master can never quietly invent a weight.

    Pure (no DB) so the arithmetic is unit-testable on its own.
    """
    std_bale_weight = _f_or_none(quality.get("std_bale_weight"))
    no_of_bags = _f_or_none(quality.get("no_of_bags"))
    bag_wt_kg = (
        round(std_bale_weight / no_of_bags, 3)
        if (std_bale_weight or 0) > 0 and (no_of_bags or 0) > 0
        else None
    )

    pcs_per_bundle = _f_or_none(
        pcs_per_bundle_by_qid.get(quality.get("finishing_quality_id"))
    )
    bundle_wt_kg = (
        round(pcs_per_bundle * bag_wt_kg, 3)
        if bag_wt_kg is not None and (pcs_per_bundle or 0) > 0
        else None
    )

    return {
        "cut_wt_kg": _f_or_none(cut_wt_by_item.get(quality.get("item_id"))),
        "bag_wt_kg": bag_wt_kg,
        "bundle_wt_kg": bundle_wt_kg,
    }


def row_to_dict(row, stage_labels: dict, own_co_name: Optional[str]) -> dict:
    """One counted lot as the API returns it (spec §3 ROW).

    ``own_co_name`` fills owner_co_name for own-company lines, where owner_co_id
    is NULL by design — the report prints a block title for every owner and
    "(none)" would be wrong for the company that owns the mill.
    """
    d = dict(row._mapping)
    qty = _f(d.get("qty"))
    wt_per_unit_kg = _f_or_none(d.get("wt_per_unit_kg"))
    wt_kg = _f_or_none(d.get("wt_kg"))
    stage = d.get("stage")
    tran_date = d.get("tran_date")
    return {
        "finishing_stock_id": d.get("finishing_stock_id"),
        "co_id": d.get("co_id"),
        "branch_id": d.get("branch_id"),
        "tran_date": None if tran_date is None else str(tran_date),
        "bucket": d.get("bucket"),
        "finishing_quality_id": d.get("finishing_quality_id"),
        "item_id": d.get("item_id"),
        "item_code": d.get("item_code"),
        "item_name": d.get("item_name"),
        "quality_type": d.get("quality_type"),
        "stage": stage,
        "stage_label": stage_labels.get(stage) if stage else None,
        "work_by": int(d.get("work_by") or 0),
        "owner_co_id": d.get("owner_co_id"),
        "owner_co_name": d.get("owner_co_name") or own_co_name,
        "qty": qty,
        "qty_uom": d.get("qty_uom"),
        "wt_per_unit_kg": wt_per_unit_kg,
        "wt_kg": wt_kg,
        "total_kg": total_kg_of(qty, wt_per_unit_kg, wt_kg),
        "remarks": d.get("remarks"),
    }


# =============================================================================
# Validation
# =============================================================================


def _validate_bucket(bucket: str) -> str:
    value = (bucket or "").strip().upper()
    if value not in FINISHING_STOCK_BUCKETS:
        raise HTTPException(status_code=400, detail=BAD_BUCKET_MSG)
    return value


def _validate_tran_date(tran_date: date) -> date:
    """Not in the future, and not more than BACKDATE_DAYS_MAX days back (spec §7d)."""
    today = date.today()
    if tran_date > today:
        raise HTTPException(status_code=400, detail=FUTURE_DATE_MSG)
    if tran_date < today - timedelta(days=BACKDATE_DAYS_MAX):
        raise HTTPException(status_code=400, detail=BACKDATE_MSG)
    return tran_date


def _validate_remarks(value: Optional[str]) -> Optional[str]:
    if value is not None and len(value) > 255:
        raise HTTPException(status_code=400, detail=REMARKS_TOO_LONG_MSG)
    return value


def _fetch_quality(db: Session, finishing_quality_id: int, co_id: int):
    row = db.execute(
        get_finishing_quality_for_entry_query(),
        {"finishing_quality_id": int(finishing_quality_id), "co_id": int(co_id)},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=BAD_QUALITY_MSG)
    return row


def _normalise_owner(owner_co_id: Optional[int], co_id: int) -> Optional[int]:
    """"Ours" is ONE value, not two: an owner equal to the entering company is
    stored as NULL, so the report never splits own bales across two blocks."""
    if owner_co_id is None or int(owner_co_id) == int(co_id):
        return None
    return int(owner_co_id)


def _owner_exists(db: Session, owner_co_id: Optional[int]) -> Optional[bool]:
    """None when there is nothing to check — kept distinct from False, which
    means "an owner was named and it does not exist"."""
    if owner_co_id is None:
        return None
    row = db.execute(
        verify_finishing_stock_owner_co_query(), {"owner_co_id": owner_co_id}
    ).fetchone()
    return row is not None


def _validate_line(
    body: FinishingStockEditBody,
    bucket: str,
    quality_row,
    owner_co_id: Optional[int],
    owner_exists: Optional[bool],
) -> dict:
    """Pure: turn a validated body into the column values to store, or 400.

    Shared by create and edit so the two can never drift — an edit that skipped
    a rule would be a hole straight through every rule.
    """
    remarks = _validate_remarks(body.remarks)
    qty = _f(body.qty)

    if bucket == FINISHING_STOCK_BUCKET_BALE:
        if body.stage:
            raise HTTPException(status_code=400, detail=BALE_STAGE_MSG)
        if int(body.work_by or 0) != 0:
            raise HTTPException(status_code=400, detail=BALE_WORK_BY_MSG)
        if (body.qty_uom or "").strip().upper() != FINISHING_STOCK_QTY_UOM_BALE:
            raise HTTPException(status_code=400, detail=BALE_UOM_MSG)
        if body.wt_kg is not None:
            raise HTTPException(status_code=400, detail=BALE_WT_KG_MSG)
        if qty <= 0 or qty > FINISHING_STOCK_QTY_MAX:
            raise HTTPException(status_code=400, detail=BALE_QTY_MSG)
        if owner_co_id is not None and not owner_exists:
            raise HTTPException(status_code=400, detail=BAD_OWNER_MSG)

        wt_per_unit_kg = body.wt_per_unit_kg
        if wt_per_unit_kg is None:
            wt_per_unit_kg = getattr(quality_row, "std_bale_weight", None)
        if wt_per_unit_kg is None:
            raise HTTPException(status_code=400, detail=BALE_WT_REQUIRED_MSG)
        wt_per_unit_kg = float(wt_per_unit_kg)
        if wt_per_unit_kg <= 0 or wt_per_unit_kg > FINISHING_STOCK_WT_MAX:
            raise HTTPException(status_code=400, detail=BALE_WT_GATE_MSG)

        return {
            "bucket": bucket,
            "finishing_quality_id": int(body.finishing_quality_id),
            "stage": None,
            "work_by": 0,
            "owner_co_id": owner_co_id,
            "qty": qty,
            "qty_uom": FINISHING_STOCK_QTY_UOM_BALE,
            "wt_per_unit_kg": wt_per_unit_kg,
            "wt_kg": None,
            "remarks": remarks,
        }

    # LOOSE
    if owner_co_id is not None:
        raise HTTPException(status_code=400, detail=LOOSE_OWNER_MSG)
    stage = (body.stage or "").strip().upper() or None
    if stage is not None and stage not in FINISHING_STOCK_STAGE_CODES:
        raise HTTPException(status_code=400, detail=LOOSE_STAGE_MSG)
    work_by = int(body.work_by or 0)
    if work_by not in FINISHING_STOCK_WORK_BY:
        raise HTTPException(status_code=400, detail=LOOSE_WORK_BY_MSG)
    qty_uom = (body.qty_uom or "").strip().upper()
    if qty_uom not in FINISHING_STOCK_LOOSE_QTY_UOMS:
        raise HTTPException(status_code=400, detail=LOOSE_UOM_MSG)
    if qty < 0 or qty > FINISHING_STOCK_QTY_MAX:
        raise HTTPException(status_code=400, detail=LOOSE_QTY_MSG)
    if body.wt_per_unit_kg is not None:
        raise HTTPException(status_code=400, detail=LOOSE_WT_PER_UNIT_MSG)
    if body.wt_kg is None:
        raise HTTPException(status_code=400, detail=LOOSE_WT_MSG)
    wt_kg = float(body.wt_kg)
    if wt_kg < 0 or wt_kg > FINISHING_STOCK_WT_MAX:
        raise HTTPException(status_code=400, detail=LOOSE_WT_MSG)

    return {
        "bucket": bucket,
        "finishing_quality_id": int(body.finishing_quality_id),
        "stage": stage,
        "work_by": work_by,
        "owner_co_id": None,
        "qty": qty,
        "qty_uom": qty_uom,
        "wt_per_unit_kg": None,
        "wt_kg": wt_kg,
        "remarks": remarks,
    }


def _prepare_line(db: Session, body: FinishingStockEditBody, co_id: int) -> dict:
    """The create/edit body -> storable values pipeline: bucket, quality, owner,
    then the pure rule set."""
    bucket = _validate_bucket(body.bucket)
    quality_row = _fetch_quality(db, body.finishing_quality_id, co_id)
    owner_co_id = _normalise_owner(body.owner_co_id, co_id)
    owner_exists = _owner_exists(db, owner_co_id)
    return _validate_line(body, bucket, quality_row, owner_co_id, owner_exists)


def _refuse_own_bale_when_derived(
    db: Session, values: dict, co_id: int, branch_id: int, tran_date: date
) -> None:
    """D7 + owner ruling 2026-10-12: once the branch has bale entries dated
    <= tran_date, pressed bales come from Bale Production and EVERY manual BALE
    line is refused (400), whoever the owner is — the page then takes loose
    finishing stock only. Runs AFTER the pure rule set so a malformed line gets
    its own message first, and spends the probe only on BALE lines."""
    if (
        values.get("bucket") == FINISHING_STOCK_BUCKET_BALE
        and _bale_source(db, co_id, branch_id, tran_date) == BALE_STOCK_SOURCE_BALE_PROD
    ):
        raise HTTPException(status_code=400, detail=OWN_BALE_DERIVED_MSG)


def _is_own_bale_row(row: dict, co_id: int) -> bool:
    owner = row.get("owner_co_id")
    return row.get("bucket") == FINISHING_STOCK_BUCKET_BALE and (
        owner is None or int(owner) == int(co_id)
    )


def _sacking_only(series_lines: list) -> list:
    """The series lines of sacking qualities (§7a): hessian is not numbered per
    consignee series, so its breakup is never shown."""
    return [
        line for line in series_lines
        if line.get("quality_type") == FINISHING_QUALITY_TYPE_SACKING
    ]


def _own_names(db: Session, co_id: int, branch_id: Optional[int]) -> tuple:
    row = db.execute(
        get_finishing_stock_unit_names_query(),
        {"co_id": co_id, "branch_id": branch_id},
    ).fetchone()
    if row is None:
        return None, None
    return row.co_name, row.branch_name


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
            get_finishing_stock_qualities_query(),
            {"co_id": co_id, "branch_id": branch_id},
        ).fetchall():
            q = dict(r._mapping)
            q["std_bale_weight"] = _f_or_none(q.get("std_bale_weight"))
            q["packsheet_wt"] = _f_or_none(q.get("packsheet_wt"))
            qualities.append(q)

        companies = [
            dict(r._mapping)
            for r in db.execute(get_finishing_stock_companies_query()).fetchall()
        ]

        # Unit weights (§6): two lookups for the whole picker, not one per
        # quality — the form prefills Weight kg from them for CUTS / BDL / PCS.
        cut_wt_by_item = {
            r._mapping["item_id"]: r._mapping["cut_wt_kg"]
            for r in db.execute(
                get_finishing_stock_cut_weights_query(), {"co_id": co_id}
            ).fetchall()
        }
        pcs_per_bundle_by_qid = {
            r._mapping["finishing_quality_id"]: r._mapping["pcs_per_bundle"]
            for r in db.execute(
                get_finishing_stock_pcs_per_bundle_query(),
                {
                    "co_id": co_id,
                    "process": BUNDLE_PROCESS,
                    "id_type": FINISHING_ID_TYPE_QLTY,
                    "value_role": STANDARD_VALUE_ROLE,
                    "param": BUNDLE_PARAM,
                    "as_of": date.today(),
                },
            ).fetchall()
        }
        for q in qualities:
            q.update(unit_weights_for(q, cut_wt_by_item, pcs_per_bundle_by_qid))

        return {
            "data": {
                "qualities": qualities,
                "stages": [
                    {"code": code, "label": label}
                    for code, label in FINISHING_STOCK_STAGES
                ],
                "companies": companies,
                "loose_qty_uoms": list(FINISHING_STOCK_LOOSE_QTY_UOMS),
                # as of today: tells the form whether own-company BALE lines
                # are still keyed by hand (MANUAL) or derived (BALE_PROD, D7)
                "bale_source": _bale_source(db, co_id, branch_id, date.today()),
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
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    d_val = _require_tran_date(request)
    try:
        binds = {"co_id": co_id, "branch_id": branch_id, "tran_date": d_val}
        own_co_name, _ = _own_names(db, co_id, branch_id)

        rows = [
            row_to_dict(r, FINISHING_STOCK_STAGE_LABELS, own_co_name)
            for r in db.execute(get_finishing_stock_by_date_query(), binds).fetchall()
        ]

        prev = db.execute(get_finishing_stock_prev_entry_date_query(), binds).fetchone()
        prev_entry_date = (
            str(prev.prev_entry_date)
            if prev is not None and prev.prev_entry_date is not None
            else None
        )

        # D7: with bale production on the branch the own-company bale figures
        # are derived as on tran_date, and the manual own-company BALE rows are
        # flagged superseded (shown greyed, delete-only, not counted).
        bale_source = _bale_source(db, co_id, branch_id, d_val)
        derived = bale_source == BALE_STOCK_SOURCE_BALE_PROD
        derived_bales = (
            _summary_lines(db, co_id, branch_id, d_val)["lines"] if derived else []
        )
        # §7a: the sacking series breakup under those quality lines (no
        # statement spent in MANUAL mode)
        derived_series = (
            _sacking_only(_series_lines(db, co_id, branch_id, d_val)) if derived else []
        )
        for row in rows:
            row["superseded"] = bool(derived and _is_own_bale_row(row, co_id))

        return {
            "data": {
                "rows": rows,
                "prev_entry_date": prev_entry_date,
                "bale_source": bale_source,
                "derived_bales": derived_bales,
                "derived_series": derived_series,
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
    body: FinishingStockCreateBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        tran_date = _validate_tran_date(body.tran_date)
        values = _prepare_line(db, body, body.co_id)
        _refuse_own_bale_when_derived(db, values, body.co_id, body.branch_id, tran_date)
        result = db.execute(
            insert_finishing_stock_row_query(),
            {
                **values,
                "co_id": int(body.co_id),
                "branch_id": int(body.branch_id),
                "tran_date": tran_date,
                "updated_by": token_data.get("user_id"),
            },
        )
        db.commit()
        return {"data": {"finishing_stock_id": result.lastrowid}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/row_edit/{finishing_stock_id}")
def row_edit(
    finishing_stock_id: int,
    body: FinishingStockEditBody,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        co_id = _require_int_param(request, "co_id")
        existing = db.execute(
            get_finishing_stock_active_by_id_query(),
            {"id": finishing_stock_id, "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        values = _prepare_line(db, body, co_id)
        # the row's own (unit, date) decides — those are immutable on edit
        _refuse_own_bale_when_derived(
            db, values, co_id, existing.branch_id, existing.tran_date
        )
        db.execute(
            update_finishing_stock_row_query(),
            {
                **values,
                "id": finishing_stock_id,
                "co_id": co_id,
                "updated_by": token_data.get("user_id"),
            },
        )
        db.commit()
        return {"data": {"updated": True}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/row_delete/{finishing_stock_id}")
def row_delete(
    finishing_stock_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        co_id = _require_int_param(request, "co_id")
        existing = db.execute(
            get_finishing_stock_active_by_id_query(),
            {"id": finishing_stock_id, "co_id": co_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        db.execute(
            soft_delete_finishing_stock_row_query(),
            {
                "id": finishing_stock_id,
                "co_id": co_id,
                "updated_by": token_data.get("user_id"),
            },
        )
        db.commit()
        return {"data": {"deleted": True}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/copy_previous_day")
def copy_previous_day(
    body: FinishingStockDayRef,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Clone the latest earlier counted day into tran_date.

    The sheet repeats ~30 near-identical rows daily; retyping them is the
    biggest source of error. Refuses when the target day already has rows, so it
    can never silently double a count.
    """
    try:
        tran_date = _validate_tran_date(body.tran_date)
        binds = {
            "co_id": int(body.co_id),
            "branch_id": int(body.branch_id),
            "tran_date": tran_date,
        }
        if db.execute(get_finishing_stock_by_date_query(), binds).fetchall():
            raise HTTPException(status_code=400, detail=COPY_TARGET_NOT_EMPTY_MSG)

        prev = db.execute(get_finishing_stock_prev_entry_date_query(), binds).fetchone()
        from_date = None if prev is None else prev.prev_entry_date
        if from_date is None:
            raise HTTPException(status_code=404, detail=NO_PREVIOUS_ENTRY_MSG)

        result = db.execute(
            copy_finishing_stock_previous_day_query(),
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
# Report
# =============================================================================


def _mt(kg: float) -> float:
    """kg -> MT at 3 dp, rounding half-up on whole grams.

    Matches the entry grid's Math.round-based kgToMt so a lot never prints
    19.420 on screen and 19.419 on the report (Python's float round() is
    half-even and the binary tie lands either side).
    """
    grams = Decimal(str(_f(kg))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(grams) / 1000


def _bale_line(qid, item_id, item_name, lots: list) -> dict:
    """One printed bale line: the lots of a quality collapsed to count + weight.

    per_wt_kg is the WEIGHTED average over the lots (total kg / total bales),
    not any one lot's figure — bales of the same quality genuinely differ, and
    the sheet prints what the stack actually averages.
    """
    bales = sum(_f(r.get("qty")) for r in lots)
    kg = sum(_f(r.get("total_kg")) for r in lots)
    return {
        "finishing_quality_id": qid,
        "item_id": item_id,
        "item_name": item_name,
        "bales": round(bales, 3),
        "per_wt_kg": round(kg / bales, 3) if bales else None,
        "wt_mt": _mt(kg),
        # manual lines carry no inspection figure — only derived bales do
        "inspected_bales": None,
    }


def _derived_bale_line(qid, item_id, item_name, derived: Optional[dict]) -> dict:
    """One printed bale line from Bale Production (D6 / §5): ``bales`` is the
    count of bales in stock, the weight the actual weighed net, and
    ``inspected_bales`` how many of them carry an inspection marking.
    ``derived`` None = the quality has no bales in stock -> zero line."""
    if not derived:
        return {
            "finishing_quality_id": qid,
            "item_id": item_id,
            "item_name": item_name,
            "bales": 0,
            "per_wt_kg": None,
            "wt_mt": 0.0,
            "inspected_bales": 0,
        }
    bales = int(derived.get("bales") or 0)
    kg = _f(derived.get("total_kg"))
    return {
        "finishing_quality_id": qid,
        "item_id": item_id,
        "item_name": item_name,
        "bales": bales,
        "per_wt_kg": round(kg / bales, 3) if bales else None,
        "wt_mt": _mt(kg),
        "inspected_bales": int(derived.get("inspected_bales") or 0),
    }


def _bale_total(lines: list) -> dict:
    """Section total from the PRINTED lines, so the column adds up on paper."""
    return {
        "bales": round(sum(line["bales"] for line in lines), 3),
        "wt_mt": round(sum(line["wt_mt"] for line in lines), 3),
    }


def _derived_series_line(line: dict) -> dict:
    """One printed series line (§7a) from a bale_stock series line: the same
    arithmetic as _derived_bale_line, keyed by series as well as quality."""
    bales = int(line.get("bales") or 0)
    kg = _f(line.get("total_kg"))
    return {
        "bale_series_id": line.get("bale_series_id"),
        "series_name": line.get("series_name"),
        "finishing_quality_id": line.get("finishing_quality_id"),
        "item_name": line.get("item_name"),
        "bales": bales,
        "inspected_bales": int(line.get("inspected_bales") or 0),
        "per_wt_kg": round(kg / bales, 3) if bales else None,
        "wt_mt": _mt(kg),
    }


def _series_lines_for_block(derived_series: Optional[list]) -> list:
    """The "Sacking bales by series" rows of the own block: sacking only, and a
    series with nothing in stock is left out (the Excel never prints an empty
    consignee)."""
    return [
        _derived_series_line(line)
        for line in _sacking_only(derived_series or [])
        if int(line.get("bales") or 0) > 0
    ]


def _series_total(lines: list) -> dict:
    return {
        "bales": sum(line["bales"] for line in lines),
        "inspected_bales": sum(line["inspected_bales"] for line in lines),
        "wt_mt": round(sum(line["wt_mt"] for line in lines), 3),
    }


def _entries_from_lots(lots: dict) -> list:
    """(qid, item_id, item_name, quality_type) per quality present in the rows."""
    entries = [
        (qid, rs[0].get("item_id"), rs[0].get("item_name"), rs[0].get("quality_type"))
        for qid, rs in lots.items()
        if rs
    ]
    return sorted(entries, key=lambda e: (e[3] or 0, e[2] or ""))


def _zero_filled_entries(qualities: list, lots: dict) -> list:
    """Every active quality, plus any quality the rows use that the master no
    longer lists — a quality deactivated after the count must not swallow real
    tonnage out of the report."""
    entries = [
        (
            q.get("finishing_quality_id"),
            q.get("item_id"),
            q.get("item_name"),
            q.get("quality_type"),
        )
        for q in qualities
    ]
    known = {e[0] for e in entries}
    return entries + _entries_from_lots(
        {k: v for k, v in lots.items() if k not in known}
    )


def _bale_block(
    owner_co_id,
    owner_co_name,
    is_own: bool,
    lots: dict,
    qualities,
    derived_by_qid: Optional[dict] = None,
    derived_series: Optional[list] = None,
) -> dict:
    """One owner's bale block, split Hessian / Sacking.

    ``qualities`` is the zero-fill list for the own-company block (every active
    quality — the Excel prints 0.000 rows) and None for other owners, whose
    blocks list only what was actually counted.

    ``derived_by_qid`` (own block in BALE_PROD mode only) maps
    finishing_quality_id -> a build_bale_stock_summary line; the lines then come
    from Bale Production instead of ``lots``. A derived quality the master no
    longer lists is still printed (same rule as a counted one).

    ``derived_series`` (own block in BALE_PROD mode only) are the
    bale_stock series lines; they become ``series`` (sacking, non-zero) with a
    ``series_total`` — a breakup of the sacking lines, not added to ``total``.
    Every other block carries ``series: []`` and a zero total.
    """
    if derived_by_qid is not None:
        # one-element pseudo-lots so _zero_filled_entries can surface derived
        # qualities that left the master
        lots = {qid: [line] for qid, line in derived_by_qid.items()}
    entries = (
        _zero_filled_entries(qualities, lots)
        if qualities is not None
        else _entries_from_lots(lots)
    )

    hessian, sacking = [], []
    for qid, item_id, item_name, quality_type in entries:
        if derived_by_qid is not None:
            line = _derived_bale_line(qid, item_id, item_name, derived_by_qid.get(qid))
        else:
            line = _bale_line(qid, item_id, item_name, lots.get(qid, []))
        if quality_type == FINISHING_QUALITY_TYPE_HESSIAN:
            hessian.append(line)
        elif quality_type == FINISHING_QUALITY_TYPE_SACKING:
            sacking.append(line)

    hessian_total = _bale_total(hessian)
    sacking_total = _bale_total(sacking)
    series = _series_lines_for_block(derived_series) if derived_by_qid is not None else []
    return {
        "owner_co_id": owner_co_id,
        "owner_co_name": owner_co_name,
        "is_own": is_own,
        "hessian": hessian,
        "sacking": sacking,
        "hessian_total": hessian_total,
        "sacking_total": sacking_total,
        "total": {
            "bales": round(hessian_total["bales"] + sacking_total["bales"], 3),
            "wt_mt": round(hessian_total["wt_mt"] + sacking_total["wt_mt"], 3),
        },
        "series": series,
        "series_total": _series_total(series),
    }


def _loose_block(loose_rows: list, qualities: list) -> dict:
    """Loose stock summed per quality — the report's LOOSE STOCK card.

    Stage and Dept/Contract collapse away here; loose_lines keeps that detail.
    """
    lots: dict = {}
    kg_by_qid: dict = {}
    for r in loose_rows:
        qid = r.get("finishing_quality_id")
        lots.setdefault(qid, []).append(r)
        kg_by_qid[qid] = kg_by_qid.get(qid, 0.0) + _f(r.get("total_kg"))

    hessian, sacking = [], []
    for qid, item_id, item_name, quality_type in _zero_filled_entries(qualities, lots):
        line = {
            "finishing_quality_id": qid,
            "item_id": item_id,
            "item_name": item_name,
            "wt_mt": _mt(kg_by_qid.get(qid, 0.0)),
        }
        if quality_type == FINISHING_QUALITY_TYPE_HESSIAN:
            hessian.append(line)
        elif quality_type == FINISHING_QUALITY_TYPE_SACKING:
            sacking.append(line)

    hessian_total_mt = round(sum(line["wt_mt"] for line in hessian), 3)
    sacking_total_mt = round(sum(line["wt_mt"] for line in sacking), 3)
    return {
        "hessian": hessian,
        "sacking": sacking,
        "hessian_total_mt": hessian_total_mt,
        "sacking_total_mt": sacking_total_mt,
        "total_mt": round(hessian_total_mt + sacking_total_mt, 3),
    }


def build_finishing_stock_report(
    rows: list,
    qualities: list,
    despatch_rows: list,
    own_co_id: Optional[int],
    own_co_name: Optional[str],
    *,
    derived_lines: Optional[list] = None,
    bale_source: str = BALE_STOCK_SOURCE_MANUAL,
    derived_series: Optional[list] = None,
) -> dict:
    """The whole report body from already-fetched data — pure, so it is unit
    testable and the handler stays a fetch-and-call.

    ``rows`` are row_to_dict() dicts, so the entry grid and the report can never
    total one line two different ways.

    ``bale_source`` BALE_PROD (D7): the own-company bale block is built from
    ``derived_lines`` (build_bale_stock_summary(...)["lines"] as on the date) —
    bales = count in stock, weight = weighed net, plus ``inspected_bales`` — and
    own-company MANUAL bale rows are dropped (they are superseded, not added).
    Other owners' blocks are unchanged in both modes. Every bale line carries
    ``inspected_bales`` (None on manual lines); the body carries ``bale_source``.

    ``derived_series`` (§7a, BALE_PROD only): bale_stock series lines — the own
    block prints the sacking ones with bales > 0 as ``series`` plus a
    ``series_total``; every other block (and MANUAL mode) has ``series: []``.
    """
    derived = bale_source == BALE_STOCK_SOURCE_BALE_PROD
    bale_rows = [r for r in rows if r.get("bucket") == FINISHING_STOCK_BUCKET_BALE]
    loose_rows = [r for r in rows if r.get("bucket") != FINISHING_STOCK_BUCKET_BALE]

    owners: dict = {}
    for r in bale_rows:
        owner_co_id = r.get("owner_co_id")
        # /row_create stores "ours" as NULL, but a row imported or copied by SQL
        # can still carry the company's own id — fold it in here or the report
        # prints two own blocks.
        if own_co_id is not None and owner_co_id == own_co_id:
            owner_co_id = None
        owner = owners.setdefault(
            owner_co_id, {"name": r.get("owner_co_name"), "lots": {}}
        )
        owner["lots"].setdefault(r.get("finishing_quality_id"), []).append(r)

    own = owners.pop(None, {"name": own_co_name, "lots": {}})
    # Own block first (always present, zero-filled), then the others by name.
    # Every block title is a string: co_mst can fail to resolve (a deleted
    # company, a co_id with no row) and the block header is not nullable.
    derived_by_qid = None
    if derived:
        derived_by_qid = {
            line.get("finishing_quality_id"): line for line in (derived_lines or [])
        }
    blocks = [
        _bale_block(
            None,
            own_co_name or "",
            True,
            own["lots"],
            qualities,
            derived_by_qid,
            derived_series if derived else None,
        )
    ]
    for owner_co_id, owner in sorted(
        owners.items(), key=lambda kv: ((kv[1]["name"] or ""), kv[0] or 0)
    ):
        blocks.append(
            _bale_block(owner_co_id, owner["name"] or "", False, owner["lots"], None)
        )

    loose = _loose_block(loose_rows, qualities)
    # quality_type is Hessian-or-Sacking, never null, on the detail table. The
    # row's own value comes from the LEFT JOIN, so it is NULL only when the
    # quality row is gone from the master entirely; fall back to the qualities
    # list and, failing that, drop the line — an unidentifiable lot is already
    # absent from the typed loose card above, so nothing counted elsewhere is
    # lost by leaving it out of the detail too.
    quality_type_by_id = {
        q.get("finishing_quality_id"): q.get("quality_type") for q in qualities
    }
    loose_lines = []
    for r in loose_rows:
        quality_type = r.get("quality_type")
        if quality_type is None:
            quality_type = quality_type_by_id.get(r.get("finishing_quality_id"))
        if quality_type is None:
            continue
        loose_lines.append(
            {
                "stage": r.get("stage"),
                "stage_label": r.get("stage_label"),
                "work_by": int(r.get("work_by") or 0),
                "quality_type": quality_type,
                "item_name": r.get("item_name"),
                "qty": _f(r.get("qty")),
                "qty_uom": r.get("qty_uom"),
                "wt_mt": _mt(r.get("total_kg")),
            }
        )

    bales_mt = round(sum(b["total"]["wt_mt"] for b in blocks), 3)
    return {
        "bale_source": bale_source,
        "bales": blocks,
        "loose": loose,
        "loose_lines": loose_lines,
        "despatch": [
            {
                "item_id": d.get("item_id"),
                "item_name": d.get("item_name"),
                "bales": _f_or_none(d.get("bales")),
                "qty": _f(d.get("qty")),
                "uom_name": d.get("uom_name"),
            }
            for d in despatch_rows
        ],
        "totals": {
            "bales_mt": bales_mt,
            "loose_mt": loose["total_mt"],
            "stock_mt": round(bales_mt + loose["total_mt"], 3),
        },
    }


@router.get("/report")
def report(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The mill's stock sheet for one day: bale blocks per owner, loose stock by
    quality, the loose detail by stage, and what was despatched on the date."""
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    d_val = _require_tran_date(request)
    try:
        own_co_name, branch_name = _own_names(db, co_id, branch_id)

        qualities = [
            dict(r._mapping)
            for r in db.execute(
                get_finishing_stock_qualities_query(),
                {"co_id": co_id, "branch_id": branch_id},
            ).fetchall()
        ]
        rows = [
            row_to_dict(r, FINISHING_STOCK_STAGE_LABELS, own_co_name)
            for r in db.execute(
                get_finishing_stock_by_date_query(),
                {"co_id": co_id, "branch_id": branch_id, "tran_date": d_val},
            ).fetchall()
        ]
        despatch_rows = [
            dict(r._mapping)
            for r in db.execute(
                get_finishing_stock_despatch_query(),
                {
                    "branch_id": branch_id,
                    "tran_date": d_val,
                    "excluded_status": list(DESPATCH_EXCLUDED_STATUS_IDS),
                    "cloth_item_type_id": PLANNING_CLOTH_ITEM_TYPE_ID,
                },
            ).fetchall()
        ]

        bale_source = _bale_source(db, co_id, branch_id, d_val)
        derived = bale_source == BALE_STOCK_SOURCE_BALE_PROD
        derived_lines = _summary_lines(db, co_id, branch_id, d_val)["lines"] if derived else None
        derived_series = _series_lines(db, co_id, branch_id, d_val) if derived else None
        data = build_finishing_stock_report(
            rows,
            qualities,
            despatch_rows,
            co_id,
            own_co_name,
            derived_lines=derived_lines,
            bale_source=bale_source,
            derived_series=derived_series,
        )
        data.update(
            {
                "tran_date": str(d_val),
                "co_name": own_co_name,
                "branch_name": branch_name,
            }
        )
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

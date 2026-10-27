"""Jute Market Rate — daily market rate entry + its two masters (source, rate difference).

Replaces legacy tbl_jute_daily_jute_rate (one rate per quality per day). Here:
  * quality = raw-jute item (item_grp_mst.item_type_id = 2)
  * each day can carry rates from several SOURCES (jute_market_rate_source_mst); every source
    is optional — a day/source pair only exists once someone saves rates for it
  * jute_rate_diff_mst holds a fixed diff of an item vs a base item, effective-dated:
    rate(item) = rate(base_item) + diff_amount. The entry page auto-fills derived items from
    it; the user may override, and whatever is entered is what is saved.

Co-scoped only. Portal persona: get_tenant_db + get_current_user_with_refresh.
"""

import logging
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.marketRateQueries import (
    get_day_source_counts_query,
    get_rate_diff_as_of_query,
    get_rate_diff_table_query,
    get_rates_for_day_query,
    get_raw_jute_items_query,
    get_sources_query,
)
from src.models.jute import JuteMarketRate, JuteMarketRateSourceMst, JuteRateDiffMst

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# Pydantic models
# =============================================================================


class SourceSaveRequest(BaseModel):
    co_id: int
    source_name: str
    source_id: Optional[int] = None


class SourceDeleteRequest(BaseModel):
    co_id: int
    source_id: int


class RateDiffSaveRequest(BaseModel):
    co_id: int
    item_id: int
    base_item_id: int
    diff_amount: float
    effective_from: date
    rate_diff_id: Optional[int] = None


class RateDiffDeleteRequest(BaseModel):
    co_id: int
    rate_diff_id: int


class RateLine(BaseModel):
    item_id: int
    rate: Optional[float] = None


class RatesSaveRequest(BaseModel):
    co_id: int
    rate_date: date
    source_id: int
    rates: List[RateLine]


# =============================================================================
# Helpers
# =============================================================================


def _require_int(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {name} format")


def _require_date(request: Request, name: str) -> date:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name} format (YYYY-MM-DD)")


def _raw_jute_items(db: Session, co_id: int) -> list:
    return [dict(r._mapping) for r in db.execute(get_raw_jute_items_query(), {"co_id": co_id}).fetchall()]


def _get_source(db: Session, co_id: int, source_id: int) -> JuteMarketRateSourceMst:
    src = db.get(JuteMarketRateSourceMst, source_id)
    if src is None or src.co_id != co_id or src.active != 1:
        raise HTTPException(status_code=404, detail="Market rate source not found")
    return src


def diff_map_as_of(db: Session, co_id: int, as_of: date) -> dict:
    """{item_id: (base_item_id, diff_amount)} for the diff rows in force on as_of."""
    rows = db.execute(get_rate_diff_as_of_query(), {"co_id": co_id, "as_of": as_of}).fetchall()
    return {int(r.item_id): (int(r.base_item_id), float(r.diff_amount)) for r in rows}


def creates_cycle(diffs: dict, item_id: int, base_item_id: int) -> bool:
    """True when making item_id derive from base_item_id loops back to item_id.
    diffs = {item_id: (base_item_id, diff)} as in diff_map_as_of."""
    seen = set()
    cur = base_item_id
    while cur is not None and cur not in seen:
        if cur == item_id:
            return True
        seen.add(cur)
        cur = diffs.get(cur, (None,))[0]
    return False


# =============================================================================
# Setup
# =============================================================================


@router.get("/setup")
def get_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Sources + raw-jute items of the company (pickers for all three pages)."""
    co_id = _require_int(request, "co_id")
    try:
        sources = [dict(r._mapping) for r in db.execute(get_sources_query(), {"co_id": co_id}).fetchall()]
        return {"data": {"sources": sources, "items": _raw_jute_items(db, co_id)}}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching market rate setup")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Source master
# =============================================================================


@router.post("/sources/save")
def save_source(
    body: SourceSaveRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create / rename a source. Re-adding a deleted name revives it."""
    try:
        co_id = int(body.co_id)
        name = (body.source_name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="source_name is required")
        if len(name) > 100:
            raise HTTPException(status_code=400, detail="source_name is too long (max 100)")

        same_name = (
            db.query(JuteMarketRateSourceMst)
            .filter(JuteMarketRateSourceMst.co_id == co_id, JuteMarketRateSourceMst.source_name == name)
            .first()
        )
        if body.source_id:
            record = _get_source(db, co_id, int(body.source_id))
            if same_name is not None and same_name.source_id != record.source_id:
                # a deleted source keeps its old rates, so never drop it to free the name
                detail = "already exists" if same_name.active == 1 else "exists as a deleted source — add it again instead"
                raise HTTPException(status_code=400, detail=f"Source '{name}' {detail}")
            record.source_name = name
        elif same_name is not None:
            if same_name.active == 1:
                raise HTTPException(status_code=400, detail=f"Source '{name}' already exists")
            record = same_name
            record.active = 1
        else:
            record = JuteMarketRateSourceMst(co_id=co_id, source_name=name, active=1)
            db.add(record)
        record.updated_by = token_data.get("user_id")
        db.commit()
        db.refresh(record)
        return {"data": {"source_id": record.source_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error saving market rate source")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sources/delete")
def delete_source(
    body: SourceDeleteRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft-delete. Rates already saved under it are kept."""
    try:
        record = _get_source(db, int(body.co_id), int(body.source_id))
        record.active = 0
        record.updated_by = token_data.get("user_id")
        db.commit()
        return {"data": {"source_id": record.source_id, "active": 0}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error deleting market rate source")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Rate difference master
# =============================================================================


@router.get("/rate_diff")
def get_rate_diff_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """All active diff rows (history included). in_force flags the row applying on as_of
    (default today)."""
    co_id = _require_int(request, "co_id")
    as_of = _require_date(request, "as_of") if request.query_params.get("as_of") else date.today()
    try:
        rows = db.execute(get_rate_diff_table_query(), {"co_id": co_id, "as_of": as_of}).fetchall()
        return {"data": [dict(r._mapping) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching rate diff table")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rate_diff/save")
def save_rate_diff(
    body: RateDiffSaveRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Upsert on (co_id, item_id, effective_from). A new diff from a later date is a NEW row —
    history before that date keeps the old diff."""
    try:
        co_id = int(body.co_id)
        item_id, base_item_id = int(body.item_id), int(body.base_item_id)
        if item_id == base_item_id:
            raise HTTPException(status_code=400, detail="Item and base item must be different")

        valid = {int(i["item_id"]) for i in _raw_jute_items(db, co_id)}
        if item_id not in valid or base_item_id not in valid:
            raise HTTPException(status_code=400, detail="Item and base item must be raw-jute items of this company")

        # ponytail: cycle check uses diffs in force on effective_from only; a future-dated row
        # could still close a loop later — the entry page stops at a loop, so it can't hang.
        diffs = diff_map_as_of(db, co_id, body.effective_from)
        if creates_cycle(diffs, item_id, base_item_id):
            raise HTTPException(
                status_code=400,
                detail="This base item already derives (directly or via others) from this item — circular difference",
            )

        if body.rate_diff_id:
            record = db.get(JuteRateDiffMst, int(body.rate_diff_id))
            if record is None or record.co_id != co_id:
                raise HTTPException(status_code=404, detail="Rate difference not found")
            clash = (
                db.query(JuteRateDiffMst)
                .filter(
                    JuteRateDiffMst.co_id == co_id,
                    JuteRateDiffMst.item_id == item_id,
                    JuteRateDiffMst.effective_from == body.effective_from,
                    JuteRateDiffMst.rate_diff_id != record.rate_diff_id,
                )
                .first()
            )
            if clash is not None:
                if clash.active == 1:
                    raise HTTPException(status_code=400, detail="A difference for this item from this date already exists")
                db.delete(clash)
                db.flush()
        else:
            record = (
                db.query(JuteRateDiffMst)
                .filter(
                    JuteRateDiffMst.co_id == co_id,
                    JuteRateDiffMst.item_id == item_id,
                    JuteRateDiffMst.effective_from == body.effective_from,
                )
                .first()
            )
            if record is None:
                record = JuteRateDiffMst(co_id=co_id)
                db.add(record)

        record.item_id = item_id
        record.base_item_id = base_item_id
        record.diff_amount = float(body.diff_amount)
        record.effective_from = body.effective_from
        record.active = 1
        record.updated_by = token_data.get("user_id")
        db.commit()
        db.refresh(record)
        return {"data": {"rate_diff_id": record.rate_diff_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error saving rate diff")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rate_diff/delete")
def delete_rate_diff(
    body: RateDiffDeleteRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft-delete one diff row; the previous row for that item (if any) applies again."""
    try:
        record = db.get(JuteRateDiffMst, int(body.rate_diff_id))
        if record is None or record.co_id != int(body.co_id):
            raise HTTPException(status_code=404, detail="Rate difference not found")
        record.active = 0
        record.updated_by = token_data.get("user_id")
        db.commit()
        return {"data": {"rate_diff_id": record.rate_diff_id, "active": 0}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error deleting rate diff")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Daily market rate entry
# =============================================================================


@router.get("/rates")
def get_rates(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Every raw-jute item with its saved rate for (date, source) and the diff in force that
    day, plus which sources already have entries on that date."""
    co_id = _require_int(request, "co_id")
    source_id = _require_int(request, "source_id")
    rate_date = _require_date(request, "rate_date")
    try:
        _get_source(db, co_id, source_id)
        saved = {
            int(r.item_id): float(r.rate)
            for r in db.execute(
                get_rates_for_day_query(), {"co_id": co_id, "rate_date": rate_date, "source_id": source_id}
            ).fetchall()
        }
        diffs = diff_map_as_of(db, co_id, rate_date)
        items = []
        for it in _raw_jute_items(db, co_id):
            iid = int(it["item_id"])
            base = diffs.get(iid)
            items.append(
                {
                    **it,
                    "rate": saved.get(iid),
                    "base_item_id": base[0] if base else None,
                    "diff_amount": base[1] if base else None,
                }
            )
        entered = [
            dict(r._mapping)
            for r in db.execute(get_day_source_counts_query(), {"co_id": co_id, "rate_date": rate_date}).fetchall()
        ]
        return {"data": {"items": items, "entered_sources": entered}}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching market rates")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rates/save")
def save_rates(
    body: RatesSaveRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Replace the (date, source) rate set: lines with a rate are upserted, saved lines left
    blank / missing are soft-deleted. Other sources on the same day are untouched."""
    try:
        co_id, source_id = int(body.co_id), int(body.source_id)
        if body.rate_date > date.today():
            raise HTTPException(status_code=400, detail="Market rate date cannot be in the future")
        _get_source(db, co_id, source_id)

        entered = {}
        for line in body.rates:
            if line.rate is None:
                continue
            if line.rate <= 0:
                raise HTTPException(status_code=400, detail=f"Rate must be greater than 0 (item {line.item_id})")
            entered[int(line.item_id)] = float(line.rate)

        valid = {int(i["item_id"]) for i in _raw_jute_items(db, co_id)}
        bad = sorted(set(entered) - valid)
        if bad:
            raise HTTPException(status_code=400, detail=f"Not raw-jute items of this company: {bad}")

        existing = {
            r.item_id: r
            for r in db.query(JuteMarketRate).filter(
                JuteMarketRate.co_id == co_id,
                JuteMarketRate.rate_date == body.rate_date,
                JuteMarketRate.source_id == source_id,
            )
        }
        user_id = token_data.get("user_id")
        for item_id, rate in entered.items():
            rec = existing.get(item_id)
            if rec is None:
                db.add(
                    JuteMarketRate(
                        co_id=co_id, rate_date=body.rate_date, source_id=source_id,
                        item_id=item_id, rate=rate, active=1, updated_by=user_id,
                    )
                )
            elif rec.active != 1 or rec.rate != rate:
                rec.rate, rec.active, rec.updated_by = rate, 1, user_id
        for item_id, rec in existing.items():
            if item_id not in entered and rec.active == 1:
                rec.active, rec.updated_by = 0, user_id
        db.commit()
        return {"data": {"saved": len(entered)}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error saving market rates")
        raise HTTPException(status_code=500, detail=str(e))

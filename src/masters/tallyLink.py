"""Tally Link Master — ERP record -> exact Tally master name, per company.

Tally rejects an import whose ledger / stock item / godown name differs from its books, and
every company keeps its own books, so the mapping is per co_id (table tally_link_mst):
  * PARTY  -> ref_id = party_mst.party_id   (customer ledger name)
  * ITEM   -> ref_id = item_mst.item_id     (stock item name; raw-jute items for now)
  * GODOWN -> ref_id = 0                    (company default godown)
  * PURCHASE_LEDGER / PURCHASE_CLAIM / TDS_194Q / SALES_LEDGER / SALES_CLAIM / ROUND_OFF
            -> ref_id = 0                    (company ledger names, see LEDGER_DEFAULTS)
  * DESTINATION       -> ref_id = branch_mst.branch_id (own branch): the Tally "Destination"
                         of every raw-jute sale whose chain ROOT MR was received at that branch
  * DESTINATION_PARTY -> ref_id = party_mst.party_id: Destination override for sales to that buyer
Exporters fall back to the ERP name (ledgers: LEDGER_DEFAULTS) when no active row exists. Soft delete via active.

Co-scoped only. Portal persona: get_tenant_db + get_current_user_with_refresh.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.masters.tallyLinkQueries import (
    LEDGER_DEFAULTS,
    get_branches_query,
    get_parties_query,
    get_raw_jute_items_query,
    get_tally_link_table_query,
)
from src.models.mst import TallyLinkMst

logger = logging.getLogger(__name__)

router = APIRouter()

# Types mapped once per company (ref_id = 0): the default godown and the ledger names.
COMPANY_LEVEL_TYPES = ("GODOWN", *LEDGER_DEFAULTS)
LINK_TYPES = ("PARTY", "ITEM", *COMPANY_LEVEL_TYPES, "DESTINATION", "DESTINATION_PARTY")
_LABELS = {"PARTY": "party", "ITEM": "item", "GODOWN": "company default godown",
           "DESTINATION": "branch", "DESTINATION_PARTY": "party"}
_OPTION_QUERIES = {"PARTY": get_parties_query, "DESTINATION_PARTY": get_parties_query,
                   "ITEM": get_raw_jute_items_query, "DESTINATION": get_branches_query}


class TallyLinkCreateRequest(BaseModel):
    co_id: int
    link_type: str
    ref_id: Optional[int] = None
    tally_name: str


class TallyLinkUpdateRequest(BaseModel):
    co_id: int
    tally_link_id: int
    tally_name: str
    active: int = 1


def clean_tally_name(name: Optional[str]) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tally name is required")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="Tally name is too long (max 255)")
    return name


def clean_link(link_type: Optional[str], ref_id: Optional[int]) -> tuple[str, int]:
    """Normalised (link_type, ref_id): type must be one of LINK_TYPES; ref_id > 0 for
    PARTY/ITEM, forced to 0 for the company-level types."""
    link_type = (link_type or "").strip().upper()
    if link_type not in LINK_TYPES:
        raise HTTPException(status_code=400, detail=f"link_type must be one of {', '.join(LINK_TYPES)}")
    if link_type in COMPANY_LEVEL_TYPES:
        return link_type, 0
    if not ref_id or int(ref_id) <= 0:
        raise HTTPException(status_code=400, detail=f"Select the {_LABELS[link_type]} to map")
    return link_type, int(ref_id)


def _options(db: Session, link_type: str, co_id: int, ref_id: Optional[int] = None) -> list:
    query = _OPTION_QUERIES[link_type]()
    return [dict(r._mapping) for r in db.execute(query, {"co_id": co_id, "ref_id": ref_id}).fetchall()]


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid co_id format")


@router.get("/setup")
def get_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Link types + pickers (active parties and raw-jute items of the company) + the
    ledger wording the exports use when a company has no ledger row."""
    co_id = _require_co_id(request)
    try:
        return {
            "data": {
                "link_types": list(LINK_TYPES),
                "parties": _options(db, "PARTY", co_id),
                "items": _options(db, "ITEM", co_id),
                "branches": _options(db, "DESTINATION", co_id),
                "ledger_defaults": LEDGER_DEFAULTS,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching tally link setup")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
def get_tally_link_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """All rows of the company (inactive too). Optional link_type filter and search over
    the ERP name / Tally name."""
    co_id = _require_co_id(request)
    link_type = (request.query_params.get("link_type") or "").strip().upper() or None
    if link_type is not None and link_type not in LINK_TYPES:
        raise HTTPException(status_code=400, detail=f"link_type must be one of {', '.join(LINK_TYPES)}")
    search = (request.query_params.get("search") or "").strip()
    try:
        rows = db.execute(
            get_tally_link_table_query(),
            {"co_id": co_id, "link_type": link_type, "search": f"%{search}%" if search else None},
        ).fetchall()
        return {"data": [dict(r._mapping) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching tally link table")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create")
def create_tally_link(
    body: TallyLinkCreateRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Add a mapping. An inactive row for the same (co, type, record) is revived with the new
    name instead of failing; an active one is a 400."""
    try:
        co_id = int(body.co_id)
        link_type, ref_id = clean_link(body.link_type, body.ref_id)
        tally_name = clean_tally_name(body.tally_name)
        if link_type not in COMPANY_LEVEL_TYPES and not _options(db, link_type, co_id, ref_id):
            raise HTTPException(
                status_code=400,
                detail=f"That {_LABELS[link_type]} does not belong to this company"
                + (" or is not a raw-jute item" if link_type == "ITEM" else ""),
            )

        record = (
            db.query(TallyLinkMst)
            .filter(TallyLinkMst.co_id == co_id, TallyLinkMst.link_type == link_type, TallyLinkMst.ref_id == ref_id)
            .first()
        )
        if record is not None and record.active == 1:
            raise HTTPException(
                status_code=400,
                detail=f"A Tally name is already mapped for this {_LABELS.get(link_type, 'ledger')} ('{record.tally_name}') — edit that row instead",
            )
        if record is None:
            record = TallyLinkMst(co_id=co_id, link_type=link_type, ref_id=ref_id)
            db.add(record)
        record.tally_name = tally_name
        record.active = 1
        db.commit()
        db.refresh(record)
        return {"data": {"tally_link_id": record.tally_link_id}}
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        # two saves racing on uq_tally_link
        db.rollback()
        raise HTTPException(status_code=400, detail="A Tally name is already mapped for this record — refresh and edit it")
    except Exception as e:
        db.rollback()
        logger.exception("Error creating tally link")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update")
def update_tally_link(
    body: TallyLinkUpdateRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Change the Tally name and/or active (active = 0 is the soft delete)."""
    try:
        if body.active not in (0, 1):
            raise HTTPException(status_code=400, detail="active must be 0 or 1")
        tally_name = clean_tally_name(body.tally_name)
        record = db.get(TallyLinkMst, int(body.tally_link_id))
        if record is None or record.co_id != int(body.co_id):
            raise HTTPException(status_code=404, detail="Tally link not found")
        record.tally_name = tally_name
        record.active = body.active
        db.commit()
        return {"data": {"tally_link_id": record.tally_link_id, "active": record.active}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error updating tally link")
        raise HTTPException(status_code=500, detail=str(e))

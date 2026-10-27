"""Jute SQC Standard Moisture Master (jute_sqc_std_mr_mst) — CRUD + the shared resolver.

One master row = the standard moisture-regain % for a (company, jute-yarn item group,
process) triple. It replaces the std MR% constants the four yarn-group-keyed SQC pages used
to hardcode (breaker card 16 / inter card 20 / drawhead 16 / finisher drawing 16):

    BREAKER   -> breaker_card_swt.py      CARDING   -> card_sliver_wt.py
    DRAWHEAD  -> draw_sliver_wt.py        FIN_DRAW  -> fin_draw_sliver_wt.py

Owner decision: a MISSING master row BLOCKS the SQC save with a 400 (no silent constant
fallback) — resolve_std_mr_pct below is the single place that 400 is raised, so the message
is identical on all four pages. std_mr_map_for_process is the read-only companion the four
setup endpoints use to show the configured value next to the selected group.

Scope is co_id only (item_grp_mst carries co_id, not branch_id). Save UPSERTS on the unique
key (co_id, item_grp_id, process) — a "delete" only flips active to 0, so re-adding a
previously deleted combination revives that row instead of hitting a duplicate-key 500.

Portal persona: get_tenant_db + get_current_user_with_refresh, {"data": ...} responses,
co_id validation, None for SQL NULLs, type-cast binds.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteSQC.constants import (
    SQC_STD_MR_PROCESS_CODES,
    SQC_STD_MR_PROCESS_LABELS,
    SQC_STD_MR_PROCESSES,
)
from src.juteSQC.query import (
    get_card_section_yarn_groups_query,
    get_sqc_std_mr_by_id_query,
    get_sqc_std_mr_by_process_query,
    get_sqc_std_mr_lookup_query,
    get_sqc_std_mr_table_query,
)
from src.models.jute import JuteSqcStdMrMst

logger = logging.getLogger(__name__)

router = APIRouter()

NOT_FOUND_MSG = "Standard moisture row not found"


# =============================================================================
# Pydantic models
# =============================================================================


class StdMrSaveRequest(BaseModel):
    """Upsert one (co_id, item_grp_id, process) master row. std_mr_id is accepted for
    symmetry with the FE grid but is NOT the key — the unique triple is."""

    co_id: int
    item_grp_id: int
    process: str
    std_mr_pct: float
    std_mr_id: Optional[int] = None


class StdMrDeleteRequest(BaseModel):
    """Soft-delete one master row (active = 0), co-scoped."""

    co_id: int
    std_mr_id: int


# =============================================================================
# Shared helpers — imported by the four SQC entry pages
# =============================================================================


def resolve_std_mr_pct(
    db: Session,
    co_id: int,
    item_grp_id: int,
    process: str,
    group_label: Optional[str] = None,
) -> float:
    """Std MR% for a (co, yarn group, process). Raises HTTPException(400) when unset.

    The ONLY place the "no standard moisture" 400 is raised, so the four SQC saves report it
    identically. group_label is the group name when the caller already has it (the message
    falls back to 'group #<id>')."""
    row = db.execute(
        get_sqc_std_mr_lookup_query(),
        {"co_id": int(co_id), "item_grp_id": int(item_grp_id), "process": process},
    ).fetchone()
    if row is None or row.std_mr_pct is None:
        label = group_label or f"group #{int(item_grp_id)}"
        process_label = SQC_STD_MR_PROCESS_LABELS.get(process, process)
        raise HTTPException(
            status_code=400,
            detail=(
                f"No standard moisture set for yarn group '{label}' at {process_label}. "
                f"Add it in Jute SQC -> Std Moisture Master."
            ),
        )
    return float(row.std_mr_pct)


def std_mr_map_for_process(db: Session, co_id: int, process: str) -> dict:
    """{item_grp_id: std_mr_pct} for one process — ONE query, for read-only display on the
    entry forms (the save still resolves per row through resolve_std_mr_pct)."""
    rows = db.execute(
        get_sqc_std_mr_by_process_query(), {"co_id": int(co_id), "process": process}
    ).fetchall()
    return {int(r.item_grp_id): float(r.std_mr_pct) for r in rows}


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid co_id format")


def _fetch_groups(db: Session, co_id: int) -> list:
    """Jute-yarn item groups (item_grp_mst, item_type 4) — the SAME picker the four SQC
    entry pages use, so the master can only be keyed on a group they can select."""
    return [
        {
            "item_grp_id": int(m["item_grp_id"]),
            "item_grp_name": m.get("item_grp_name"),
            "co_id": m.get("co_id"),
        }
        for m in (
            dict(r._mapping)
            for r in db.execute(
                get_card_section_yarn_groups_query(), {"co_id": co_id}
            ).fetchall()
        )
    ]


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/std_mr_master/setup")
def get_std_mr_master_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Picker data: the fixed process vocabulary + the co's jute-yarn item groups."""
    co_id = _require_co_id(request)
    try:
        return {
            "data": {
                "processes": [
                    {"code": code, "label": label} for code, label in SQC_STD_MR_PROCESSES
                ],
                "item_groups": _fetch_groups(db, co_id),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching std MR master setup")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/std_mr_master/table")
def get_std_mr_master_table(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """The co's active master rows, group-name labelled. Optional item_grp_id / process /
    search (search matches the group name) — unsupplied filters bind NULL (no filter)."""
    co_id = _require_co_id(request)
    try:
        raw_grp = request.query_params.get("item_grp_id")
        try:
            item_grp_id = int(raw_grp) if raw_grp else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid item_grp_id format")

        process = request.query_params.get("process") or None
        search = request.query_params.get("search")

        rows = db.execute(
            get_sqc_std_mr_table_query(),
            {
                "co_id": co_id,
                "item_grp_id": item_grp_id,
                "process": process,
                "search": f"%{search}%" if search else None,
            },
        ).fetchall()

        return {
            "data": [
                {
                    "std_mr_id": int(m["std_mr_id"]),
                    "co_id": m["co_id"],
                    "item_grp_id": m["item_grp_id"],
                    "item_grp_name": m.get("item_grp_name"),
                    "process": m.get("process"),
                    "process_label": SQC_STD_MR_PROCESS_LABELS.get(
                        m.get("process"), m.get("process")
                    ),
                    "std_mr_pct": float(m["std_mr_pct"]) if m.get("std_mr_pct") is not None else None,
                    "active": m.get("active"),
                }
                for m in (dict(r._mapping) for r in rows)
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching std MR master table")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/std_mr_master/save")
def save_std_mr_master(
    body: StdMrSaveRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Upsert on the unique key (co_id, item_grp_id, process): an existing row (even a
    soft-deleted one) is updated + revived, otherwise a new row is inserted. Validates the
    process token, the 0 < std_mr_pct < 100 range, and that the group is a live jute-yarn
    group of that company."""
    try:
        process = (body.process or "").strip().upper()
        if process not in SQC_STD_MR_PROCESS_CODES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"process must be one of {', '.join(SQC_STD_MR_PROCESS_CODES)}, "
                    f"got '{body.process}'"
                ),
            )

        std_mr_pct = float(body.std_mr_pct)
        if not (0 < std_mr_pct < 100):
            raise HTTPException(
                status_code=400, detail="std_mr_pct must be greater than 0 and less than 100"
            )

        co_id = int(body.co_id)
        item_grp_id = int(body.item_grp_id)
        # The group must be one the SQC pages can actually select (item_type 4, this co).
        valid_groups = {g["item_grp_id"] for g in _fetch_groups(db, co_id)}
        if item_grp_id not in valid_groups:
            raise HTTPException(
                status_code=400,
                detail=f"item_grp_id {item_grp_id} is not a jute-yarn item group of this company",
            )

        existing = (
            db.query(JuteSqcStdMrMst)
            .filter(
                JuteSqcStdMrMst.co_id == co_id,
                JuteSqcStdMrMst.item_grp_id == item_grp_id,
                JuteSqcStdMrMst.process == process,
            )
            .first()
        )
        if existing is not None:
            existing.std_mr_pct = std_mr_pct
            existing.active = 1
            existing.updated_by = token_data.get("user_id")
            record = existing
        else:
            record = JuteSqcStdMrMst(
                co_id=co_id,
                item_grp_id=item_grp_id,
                process=process,
                std_mr_pct=std_mr_pct,
                active=1,
                updated_by=token_data.get("user_id"),
            )
            db.add(record)

        db.commit()
        db.refresh(record)
        return {"data": {"std_mr_id": record.std_mr_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error saving std MR master row")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/std_mr_master/delete")
def delete_std_mr_master(
    body: StdMrDeleteRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft-delete (active = 0) one row of THIS company. The row stays addressable so a
    later save on the same triple revives it."""
    try:
        co_id = int(body.co_id)
        std_mr_id = int(body.std_mr_id)

        existing = db.execute(
            get_sqc_std_mr_by_id_query(), {"std_mr_id": std_mr_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        # Ownership is already proven by the co-scoped check above -> plain PK fetch.
        record = db.get(JuteSqcStdMrMst, std_mr_id)
        record.active = 0
        record.updated_by = token_data.get("user_id")
        db.commit()

        return {"data": {"std_mr_id": std_mr_id, "active": 0}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Error deleting std MR master row")
        raise HTTPException(status_code=500, detail=str(e))

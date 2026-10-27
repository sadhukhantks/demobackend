"""Roll Stock view endpoints (current bin/group inventory + per-item summary)."""

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import DEFAULT_MATURITY_HOURS
from src.juteProduction.query import (
    get_bins_with_stock_query,
    get_item_maturity_map_query,
)
from src.juteProcurement.issuePlanner import UnknownBranchError
from src.juteProduction.roll_stock_target import (
    BranchCompanyMismatchError,
    build_roll_stock_target_vs_actual,
)
from src.juteProduction.services.blend import blend_label, target_maturity_hours


router = APIRouter()


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id")


def _optional_branch_id(request: Request) -> Optional[int]:
    raw = request.query_params.get("branch_id")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid branch_id")


def _optional_as_of_dt(request: Request) -> Optional[datetime]:
    """Optional 'as at' cutoff; absent/empty means live stock."""
    raw = request.query_params.get("as_of_dt")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid as_of_dt format, expected ISO datetime"
        )


@router.get("/roll_stock")
def roll_stock(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    as_of_dt = _optional_as_of_dt(request)
    try:
        rows = db.execute(
            get_bins_with_stock_query(),
            {"co_id": co_id, "branch_id": branch_id, "as_of_dt": as_of_dt},
        ).fetchall()
        maturity_map: dict = {
            int(r._mapping["item_id"]): int(r._mapping["maturity_hours"])
            for r in db.execute(get_item_maturity_map_query(), {"co_id": co_id}).fetchall()
        }
        now = as_of_dt or datetime.now()
        data = []
        for r in rows:
            m = dict(r._mapping)
            avg_dt: Optional[datetime] = m.get("avg_entry_dt")
            if avg_dt and not isinstance(avg_dt, datetime):
                try:
                    avg_dt = datetime.fromisoformat(str(avg_dt))
                except Exception:
                    avg_dt = None
            maturity_hours = (
                max(int((now - avg_dt).total_seconds() // 3600), 0) if avg_dt else 0
            )
            data.append(
                {
                    "bin_id": int(m["bin_id"]),
                    "bin_code": m.get("bin_code"),
                    "entry_id_grp": int(m["entry_id_grp"]),
                    "item_id": int(m["item_id"]),
                    "item_name": m.get("item_name"),
                    "item_id_2": int(m["item_id_2"]) if m.get("item_id_2") is not None else None,
                    "item_name_2": m.get("item_name_2"),
                    "ratio_1": int(m["ratio_1"] or 1),
                    "ratio_2": int(m["ratio_2"]) if m.get("ratio_2") is not None else None,
                    "quality_label": m.get("quality_label"),
                    "produced_rolls": int(m["produced_rolls"]),
                    "issued_rolls": int(m["issued_rolls"]),
                    "current_rolls": int(m["current_rolls"]),
                    "produced_kg": float(m["produced_kg"] or 0),
                    "issued_kg": float(m["issued_kg"] or 0),
                    "current_kg": float(m["current_kg"] or 0),
                    "current_mt": round(float(m["current_kg"] or 0) / 1000.0, 3),
                    "avg_entry_dt": avg_dt.isoformat() if avg_dt else None,
                    "maturity_hrs": maturity_hours,
                    "target_maturity_hrs": target_maturity_hours(
                        maturity_map,
                        int(m["item_id"]),
                        int(m["item_id_2"]) if m.get("item_id_2") is not None else None,
                        int(m["ratio_1"] or 1),
                        int(m["ratio_2"]) if m.get("ratio_2") is not None else None,
                        DEFAULT_MATURITY_HOURS,
                    ),
                }
            )
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/roll_stock_quality_summary")
def roll_stock_quality_summary(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    as_of_dt = _optional_as_of_dt(request)
    try:
        rows = db.execute(
            get_bins_with_stock_query(),
            {"co_id": co_id, "branch_id": branch_id, "as_of_dt": as_of_dt},
        ).fetchall()
        summary: dict = {}
        for r in rows:
            m = dict(r._mapping)
            item_id = int(m["item_id"])
            item_id_2 = int(m["item_id_2"]) if m.get("item_id_2") is not None else None
            ratio_1 = int(m["ratio_1"] or 1)
            ratio_2 = int(m["ratio_2"]) if m.get("ratio_2") is not None else None
            key = (item_id, item_id_2, ratio_1, ratio_2)
            entry = summary.setdefault(
                key,
                {
                    "item_id": item_id,
                    "item_id_2": item_id_2,
                    "ratio_1": ratio_1,
                    "ratio_2": ratio_2,
                    "item_name": m.get("item_name"),
                    "item_name_2": m.get("item_name_2"),
                    "quality_label": m.get("quality_label")
                    or blend_label(m.get("item_name"), m.get("item_name_2"), ratio_1, ratio_2),
                    "closing_rolls": 0,
                    "closing_kg": 0.0,
                },
            )
            entry["closing_rolls"] += int(m["current_rolls"])
            entry["closing_kg"] += float(m["current_kg"] or 0)
        data = []
        for entry in summary.values():
            entry["closing_mt"] = round(entry["closing_kg"] / 1000.0, 3)
            r1, r2 = entry["ratio_1"], entry["ratio_2"]
            if entry["item_id_2"] is not None:
                r2 = r2 or 1  # ponytail: malformed import rows (item_id_2 set, ratio_2 NULL) — API validator forbids this; treat as 1 like target_maturity_hours does
                share1 = r1 / (r1 + r2)
                entry["comp1_item_id"] = entry["item_id"]
                entry["comp1_kg"] = round(entry["closing_kg"] * share1, 3)
                entry["comp2_item_id"] = entry["item_id_2"]
                entry["comp2_kg"] = round(entry["closing_kg"] - entry["comp1_kg"], 3)
            else:
                entry["comp1_item_id"] = entry["item_id"]
                entry["comp1_kg"] = round(entry["closing_kg"], 3)
                entry["comp2_item_id"] = None
                entry["comp2_kg"] = 0.0
            data.append(entry)
        data.sort(key=lambda x: (x["quality_label"] or ""))
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/roll_stock_target_vs_actual")
def roll_stock_target_vs_actual(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Target vs actual roll stock per jute quality at an "as at" time (TKT-000170).

    Target = Jute Issue Planner target roll stock for the mill day containing
    as_of_dt (mill day D = D 06:00 .. D+1 06:00); actual = roll stock at
    as_of_dt with blends split into component qualities.

    Query params: co_id, branch_id (both required), as_of_dt (ISO datetime,
    default now), target_date (YYYY-MM-DD, optional override of the mill day).
    """
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    if branch_id is None:
        raise HTTPException(status_code=400, detail="branch_id is required")
    as_of_dt = _optional_as_of_dt(request) or datetime.now().replace(
        minute=0, second=0, microsecond=0
    )
    raw_target = request.query_params.get("target_date")
    target_day: Optional[date] = None
    if raw_target:
        try:
            target_day = date.fromisoformat(raw_target)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid target_date format, expected YYYY-MM-DD"
            )
    try:
        data = build_roll_stock_target_vs_actual(
            db, co_id, branch_id, as_of_dt, target_day
        )
        return {"data": data}
    except UnknownBranchError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BranchCompanyMismatchError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

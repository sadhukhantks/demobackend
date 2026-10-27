"""Bale Production reports (/api/juteProductionReports/bale_daily_sheet,
/bale_heavy_light) — docs/bale-production-spec-sheet.md §5, §6.3, §8.3, §8.4.

Two read-only reports over the per-bale table jute_prod_bale_entry:

* **Daily Bale Sheet** — every bale of one day, grouped into one block per
  finishing quality, ordered by weigh time; block footers with PROD / REPACK
  counts, gross / net totals, PROD average net and its deviation from the
  standard bale weight; grand footers per quality type and overall.
* **Bale Heavy/Light (HY/LT)** — observed vs standard vs moisture-corrected
  bale weight per quality over a date range (F1–F8), then SACKING / HESSIAN /
  OVERALL group rows (F6). Positive % = heavy, negative = light — the same
  sign convention as bag_weight.py and the DPR yarn HY/LT.

REPACK rows (a bale opened and re-pressed) are listed and sub-totalled on the
daily sheet but never count as production and never enter HY/LT (spec D2/Q4).

Shape follows the sibling report routers (wastage_report.py,
lowprod_winding.py): sync ``def`` handlers, ``get_tenant_db`` +
``get_current_user_with_refresh``, ``{"data": ...}`` responses, parameters
validated before the try/except so a bad input is a 400 and never a 500, and
DB-free pure builders (``build_daily_sheet``, ``build_heavy_light``) that the
tests hit directly with stub rows.
"""

from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.bale_reports_query import (
    get_bale_daily_sheet_rows_query,
    get_bale_heavy_light_day_rows_query,
    get_branch_name_query,
    get_packing_mr_group_daily_query,
    get_packing_mr_item_daily_query,
)
from src.juteProduction.constants import (
    BALE_ENTRY_TYPE_PROD,
    BALE_ENTRY_TYPE_REPACK,
    BALE_ENTRY_TYPES,
    FINISHING_QUALITY_TYPE_HESSIAN,
    FINISHING_QUALITY_TYPE_SACKING,
)
from src.juteProduction.gain_loss_query import verify_branch_co_query
from src.juteSQC.bag_weight import row_corr

router = APIRouter()
bale_reports_router = router

BAD_BRANCH_MSG = "branch_id must be an active branch of this company"

# quality_type -> the jute_sqc_packing_mr.quality_group it falls back to (F7)
# and the group row label the HY/LT report prints (F6).
QUALITY_TYPE_GROUP = {
    FINISHING_QUALITY_TYPE_HESSIAN: "HESSIAN",
    FINISHING_QUALITY_TYPE_SACKING: "SACKING",
}
# Sheet order of the group rows: SACKING, HESSIAN, then OVERALL.
HEAVY_LIGHT_GROUP_ORDER = ("SACKING", "HESSIAN")
OVERALL_LABEL = "OVERALL"

# Standard MR% when the quality master has none (the STD tab values the
# migration seeds: 16 hessian / 20 sacking).
DEFAULT_STD_MR_PCT = {
    FINISHING_QUALITY_TYPE_HESSIAN: 16.0,
    FINISHING_QUALITY_TYPE_SACKING: 20.0,
}

MR_SOURCE_ITEM = "item"
MR_SOURCE_GROUP = "group"
MR_SOURCE_NONE = "none"

# Per (quality, day) aggregates are cheap, but a range wider than a year is
# never a HY/LT question — the FE quick buttons are Today / MTD.
MAX_RANGE_DAYS = 366


# =============================================================================
# Small helpers
# =============================================================================


def _f(v, default: float = 0.0) -> float:
    return default if v is None else float(v)


def _opt_f(v) -> Optional[float]:
    return None if v is None else float(v)


def _round(v: Optional[float], nd: int) -> Optional[float]:
    return None if v is None else round(float(v), nd)


def _day_key(v) -> str:
    """Date part of a date/datetime/string, as the index key both sides share."""
    return str(v)[:10] if v is not None else ""


def _iso(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _pct_of_std(value: Optional[float], std: float) -> Optional[float]:
    """(value − std) / std × 100, 2 dp; None when either side is missing."""
    if value is None or not std:
        return None
    return round((value - std) / std * 100.0, 2)


# =============================================================================
# Pure builders (no DB) — tested directly
# =============================================================================


def build_daily_sheet(rows: List[dict]) -> dict:
    """One block per finishing quality (rows arrive quality-first, weigh_time
    ordered — arrival order is kept inside a block), block footers and the
    per-type / overall grand footers of spec §6.3.

    Production figures (total_gross, total_net, avg_net, dev_pct) are PROD
    rows only; REPACK rows are listed with their flag and original number,
    counted in repack_bales and summed separately in repack_total_gross /
    repack_total_net (Q4: a repack re-presses an already-counted bale).
    """
    blocks: Dict[int, dict] = {}
    order: List[int] = []
    for r in rows:
        qid = int(r["finishing_quality_id"])
        b = blocks.get(qid)
        if b is None:
            b = blocks[qid] = {
                "finishing_quality_id": qid,
                "item_id": r.get("item_id"),
                "item_name": r.get("item_name"),
                "quality_type": r.get("quality_type"),
                "std_bale_wt": None,
                "packsheet_wt": None,
                "_quality_std": _opt_f(r.get("quality_std_bale_wt")),
                "_quality_packsheet": _opt_f(r.get("quality_packsheet_wt")),
                "rows": [],
                "prod_bales": 0,
                "repack_bales": 0,
                "total_gross": 0.0,
                "total_net": 0.0,
                "repack_total_gross": 0.0,
                "repack_total_net": 0.0,
            }
            order.append(qid)
        # snapshot on the rows wins; the master's current value is the fallback
        if b["std_bale_wt"] is None and r.get("std_bale_wt") is not None:
            b["std_bale_wt"] = float(r["std_bale_wt"])
        if b["packsheet_wt"] is None and r.get("packsheet_wt") is not None:
            b["packsheet_wt"] = float(r["packsheet_wt"])

        gross = _f(r.get("gross_wt"))
        net = _f(r.get("net_wt"))
        entry_type = str(r.get("entry_type") or BALE_ENTRY_TYPE_PROD).upper()
        is_repack = entry_type == BALE_ENTRY_TYPE_REPACK
        b["rows"].append(
            {
                "sl_no": len(b["rows"]) + 1,
                "bale_entry_id": r.get("bale_entry_id"),
                "entry_type": entry_type,
                "series_name": r.get("series_name"),
                "bale_no": r.get("bale_no"),
                "original_bale_no": r.get("original_bale_no") if is_repack else None,
                "spell_code": r.get("spell_code"),
                "gross_wt": round(gross, 3),
                "trolly_name": r.get("trolly_name"),
                "trolly_wt": _round(r.get("trolly_wt"), 3),
                "net_wt": round(net, 3),
                "weigh_time": _iso(r.get("weigh_time")),
                "remarks": r.get("remarks"),
            }
        )
        if is_repack:
            b["repack_bales"] += 1
            b["repack_total_gross"] += gross
            b["repack_total_net"] += net
        else:
            b["prod_bales"] += 1
            b["total_gross"] += gross
            b["total_net"] += net

    out_blocks = []
    by_type: Dict[int, dict] = {}
    overall = {"prod_bales": 0, "repack_bales": 0, "total_gross": 0.0, "total_net": 0.0}
    for qid in order:
        b = blocks[qid]
        quality_std = b.pop("_quality_std")
        quality_packsheet = b.pop("_quality_packsheet")
        std = b["std_bale_wt"] if b["std_bale_wt"] is not None else quality_std
        packsheet = b["packsheet_wt"] if b["packsheet_wt"] is not None else quality_packsheet
        avg_net = b["total_net"] / b["prod_bales"] if b["prod_bales"] else None
        b.update(
            {
                "std_bale_wt": std,
                "packsheet_wt": packsheet,
                "total_gross": round(b["total_gross"], 3),
                "total_net": round(b["total_net"], 3),
                "repack_total_gross": round(b["repack_total_gross"], 3),
                "repack_total_net": round(b["repack_total_net"], 3),
                "avg_net": _round(avg_net, 2),
                "dev_pct": _pct_of_std(avg_net, std or 0.0),
            }
        )
        out_blocks.append(b)

        qt = b["quality_type"]
        t = by_type.setdefault(
            qt,
            {
                "quality_type": qt,
                "prod_bales": 0,
                "repack_bales": 0,
                "total_gross": 0.0,
                "total_net": 0.0,
            },
        )
        for tgt in (t, overall):
            tgt["prod_bales"] += b["prod_bales"]
            tgt["repack_bales"] += b["repack_bales"]
            tgt["total_gross"] += b["total_gross"]
            tgt["total_net"] += b["total_net"]

    def _fin(t: dict) -> dict:
        t["total_gross"] = round(t["total_gross"], 3)
        t["total_net"] = round(t["total_net"], 3)
        return t

    return {
        "blocks": out_blocks,
        "totals": {
            "by_type": [
                _fin(by_type[k])
                for k in sorted(by_type, key=lambda x: (x is None, x if x is not None else 0))
            ],
            "overall": _fin(overall),
        },
    }


def resolve_mr(
    day_key: str,
    item_id,
    quality_type,
    mr_item_index: Dict[Tuple[str, int], float],
    mr_group_index: Dict[Tuple[str, str], float],
) -> Tuple[Optional[float], str]:
    """F7 for one (date, quality): item-level Packing-MR average, else the
    HESSIAN / SACKING group average of that day, else (None, 'none')."""
    if item_id is not None:
        v = mr_item_index.get((day_key, int(item_id)))
        if v is not None:
            return v, MR_SOURCE_ITEM
    group = QUALITY_TYPE_GROUP.get(quality_type)
    if group is not None:
        v = mr_group_index.get((day_key, group))
        if v is not None:
            return v, MR_SOURCE_GROUP
    return None, MR_SOURCE_NONE


def build_heavy_light(
    day_rows: List[dict], mr_item_rows: List[dict], mr_group_rows: List[dict]
) -> dict:
    """F1–F8 over per-(quality, day) PROD aggregates.

    Per quality across the range: bales and Σnet are summed; the standard is
    the bale-weighted snapshot (total_std = Σ n_day × std_day, F8); avg_mr is
    the bale-count-weighted mean of the per-day F7 values over the days that
    have a reading. corr_* / total_corr are None when no MR reading exists
    for any day of the range and the row is flagged mr_source='none'.

    Group rows (F6) sum total_std / total_obs / total_corr over their
    qualities; corr_pct compares Σcorr with the Σstd of the qualities that
    HAVE a corrected figure, so a quality without MR leaves corr_pct honest
    instead of dragging it down.
    """
    mr_item_index: Dict[Tuple[str, int], float] = {}
    for m in mr_item_rows:
        if m.get("avg_mr") is not None and m.get("item_id") is not None:
            mr_item_index[(_day_key(m["entry_date"]), int(m["item_id"]))] = float(m["avg_mr"])
    mr_group_index: Dict[Tuple[str, str], float] = {}
    for m in mr_group_rows:
        if m.get("avg_mr") is not None and m.get("quality_group"):
            mr_group_index[(_day_key(m["entry_date"]), str(m["quality_group"]).upper())] = float(
                m["avg_mr"]
            )

    acc: Dict[int, dict] = {}
    order: List[int] = []
    for r in day_rows:
        n = int(r.get("bales") or 0)
        if n <= 0:
            continue
        qid = int(r["finishing_quality_id"])
        a = acc.get(qid)
        if a is None:
            qt = r.get("quality_type")
            a = acc[qid] = {
                "finishing_quality_id": qid,
                "item_id": r.get("item_id"),
                "item_name": r.get("item_name"),
                "quality_type": qt,
                "weft_count": _opt_f(r.get("weft_count")),
                "std_mr_pct": (
                    float(r["std_mr_pct"])
                    if r.get("std_mr_pct") is not None
                    else DEFAULT_STD_MR_PCT.get(qt)
                ),
                "bales": 0,
                "total_net": 0.0,
                "total_std": 0.0,
                "mr_weighted": 0.0,
                "mr_bales": 0,
                "days": 0,
                "days_with_mr": 0,
                "sources": set(),
            }
            order.append(qid)
        day_key = _day_key(r.get("tran_date"))
        mr, source = resolve_mr(day_key, a["item_id"], a["quality_type"], mr_item_index, mr_group_index)
        a["bales"] += n
        a["total_net"] += _f(r.get("total_net"))
        a["total_std"] += n * _f(r.get("std_bale_wt"))
        a["days"] += 1
        if mr is not None:
            a["mr_weighted"] += n * mr
            a["mr_bales"] += n
            a["days_with_mr"] += 1
            a["sources"].add(source)

    rows = []
    group_acc: Dict[str, dict] = {
        label: {"bales": 0, "total_std": 0.0, "total_obs": 0.0, "total_corr": 0.0, "corr_std": 0.0, "has_corr": False}
        for label in HEAVY_LIGHT_GROUP_ORDER
    }
    overall = {"bales": 0, "total_std": 0.0, "total_obs": 0.0, "total_corr": 0.0, "corr_std": 0.0, "has_corr": False}
    no_mr: List[str] = []
    partial_mr: List[str] = []

    for qid in order:
        a = acc[qid]
        n = a["bales"]
        std = a["total_std"] / n if n else 0.0
        obs = a["total_net"] / n  # F1 (net already excludes trolley + packsheet)
        avg_mr = a["mr_weighted"] / a["mr_bales"] if a["mr_bales"] else None  # F8
        if avg_mr is None:
            mr_source = MR_SOURCE_NONE
            no_mr.append(str(a["item_name"]))
        else:
            mr_source = MR_SOURCE_GROUP if MR_SOURCE_GROUP in a["sources"] else MR_SOURCE_ITEM
            if a["days_with_mr"] < a["days"]:
                partial_mr.append(
                    f"{a['item_name']}: MR reading on {a['days_with_mr']} of {a['days']} days — "
                    "avg MR taken over the covered days"
                )
        std_mr = a["std_mr_pct"]
        corr = (
            row_corr(obs, avg_mr, std_mr)  # F2
            if avg_mr is not None and std_mr is not None
            else None
        )
        total_std = n * std  # F5 (= Σ n_day × std_day)
        total_obs = a["total_net"]
        total_corr = n * corr if corr is not None else None

        rows.append(
            {
                "finishing_quality_id": qid,
                "item_id": a["item_id"],
                "item_name": a["item_name"],
                "quality_type": a["quality_type"],
                "bales": n,
                "weft_count": a["weft_count"],
                "avg_mr": _round(avg_mr, 2),
                "mr_source": mr_source,
                "std_bale_wt": round(std, 3),
                "std_mr_pct": std_mr,
                "obs_per_bale": round(obs, 2),
                "corr_per_bale": _round(corr, 2),
                "obs_pct": _pct_of_std(obs, std),  # F3
                "corr_pct": _pct_of_std(corr, std),  # F4
                "total_std": round(total_std, 3),
                "total_obs": round(total_obs, 3),
                "total_corr": _round(total_corr, 3),
            }
        )

        label = QUALITY_TYPE_GROUP.get(a["quality_type"])
        targets = [overall] + ([group_acc[label]] if label in group_acc else [])
        for g in targets:
            g["bales"] += n
            g["total_std"] += total_std
            g["total_obs"] += total_obs
            if total_corr is not None:
                g["total_corr"] += total_corr
                g["corr_std"] += total_std
                g["has_corr"] = True

    def _group_row(label: str, g: dict) -> dict:
        total_corr = g["total_corr"] if g["has_corr"] else None
        return {
            "label": label,
            "bales": g["bales"],
            "total_std": round(g["total_std"], 3),
            "total_obs": round(g["total_obs"], 3),
            "total_corr": _round(total_corr, 3),
            "obs_pct": (
                round((g["total_obs"] - g["total_std"]) / g["total_std"] * 100.0, 2)
                if g["total_std"]
                else None
            ),
            "corr_pct": (
                round((total_corr - g["corr_std"]) / g["corr_std"] * 100.0, 2)
                if total_corr is not None and g["corr_std"]
                else None
            ),
        }

    groups = [_group_row(label, group_acc[label]) for label in HEAVY_LIGHT_GROUP_ORDER]
    groups.append(_group_row(OVERALL_LABEL, overall))

    notes: List[str] = []
    if no_mr:
        n_q = len(no_mr)
        notes.append(
            f"{n_q} {'quality' if n_q == 1 else 'qualities'} had no MR reading — "
            f"corrected columns left blank: {', '.join(no_mr)}"
        )
    notes.extend(partial_mr)

    return {"rows": rows, "groups": groups, "notes": notes}


# =============================================================================
# Parameter validation
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


def _require_date_param(request: Request, name: str) -> date:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name} format — expected YYYY-MM-DD")


def _optional_entry_type(request: Request) -> Optional[str]:
    raw = (request.query_params.get("entry_type") or "").strip().upper()
    if not raw:
        return None
    if raw not in BALE_ENTRY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid entry_type — expected {' or '.join(BALE_ENTRY_TYPES)}",
        )
    return raw


def _date_range(request: Request) -> Tuple[date, date]:
    d_from = _require_date_param(request, "date_from")
    d_to = _require_date_param(request, "date_to")
    if d_to < d_from:
        raise HTTPException(status_code=400, detail="date_to is before date_from")
    if (d_to - d_from).days >= MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range too large — at most {MAX_RANGE_DAYS} days",
        )
    return d_from, d_to


def _rows(db: Session, query, binds: dict) -> List[dict]:
    return [dict(r._mapping) for r in db.execute(query, binds).fetchall()]


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/bale_daily_sheet")
def bale_daily_sheet(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Daily Bale Sheet — ?co_id&branch_id&tran_date[&spell_id][&entry_type]."""
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    tran_date = _require_date_param(request, "tran_date")
    spell_id = _optional_int_param(request, "spell_id")
    entry_type = _optional_entry_type(request)
    try:
        branch = db.execute(
            get_branch_name_query(), {"co_id": co_id, "branch_id": branch_id}
        ).fetchone()
        if not branch:
            raise HTTPException(status_code=400, detail=BAD_BRANCH_MSG)

        rows = _rows(
            db,
            get_bale_daily_sheet_rows_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "tran_date": tran_date,
                "spell_id": spell_id,
                "entry_type": entry_type,
            },
        )
        sheet = build_daily_sheet(rows)
        return {
            "data": {
                "tran_date": str(tran_date),
                "branch_id": branch_id,
                "branch_name": dict(branch._mapping).get("branch_name"),
                "spell_id": spell_id,
                "entry_type": entry_type,
                **sheet,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bale_heavy_light")
def bale_heavy_light(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Bale Heavy/Light — ?co_id&branch_id&date_from&date_to (PROD bales only)."""
    co_id = _require_int_param(request, "co_id")
    branch_id = _require_int_param(request, "branch_id")
    d_from, d_to = _date_range(request)
    try:
        ok = db.execute(
            verify_branch_co_query(), {"co_id": co_id, "branch_id": branch_id}
        ).fetchone()
        if not ok:
            raise HTTPException(status_code=400, detail=BAD_BRANCH_MSG)

        binds = {
            "co_id": co_id,
            "branch_id": branch_id,
            "date_from": d_from,
            "date_to": d_to,
        }
        day_rows = _rows(
            db,
            get_bale_heavy_light_day_rows_query(),
            {**binds, "entry_type": BALE_ENTRY_TYPE_PROD},
        )
        mr_item_rows = _rows(db, get_packing_mr_item_daily_query(), binds)
        mr_group_rows = _rows(db, get_packing_mr_group_daily_query(), binds)

        report = build_heavy_light(day_rows, mr_item_rows, mr_group_rows)
        return {
            "data": {
                "date_from": str(d_from),
                "date_to": str(d_to),
                "branch_id": branch_id,
                **report,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

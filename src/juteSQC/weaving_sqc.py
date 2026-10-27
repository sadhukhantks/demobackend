"""Weaving Pick-SQC capture endpoints (jute SQC module).

One capture surface feeds the weaving planning grid:
  * jute_sqc_weaving_pick - multi-observation pick readings. Each save is a NEW
    reading (insert-only, no upsert); Act Picks(date, quality) = AVG(picks),
    surfaced via vw_weaving_pick_act and resolved downstream by last-date.

Portal persona: get_tenant_db + get_current_user_with_refresh, {"data": ...}
responses, _require_co_id / _optional_branch_id / _require_entry_date / _f / _i
helpers mirrored from spinning_sqc.py, and the try/except pattern (GET:
HTTPException re-raise, ValueError 400, Exception 500; POST/DELETE: rollback
before raise/500).

The loom list (loom-type machines) and weaving-quality lookups are reused
directly from src/juteProduction/weaving_query.py.
"""

from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import WEAVING_ID_TYPE_QLTY, WEAVING_MACHINE_TYPE_NAME
from src.juteProduction.weaving_query import (
    get_weaving_entry_machines_query,
    get_weaving_entry_qualities_query,
)
# Shared spell-resolution contract (spell_id preferred, branch-scoped code fallback,
# NO global fallback) — import, do NOT copy. See spinning_entry._resolve_spell.
from src.juteProduction.spinning_entry import _resolve_spell
from src.juteProduction.weaving_query import flag_weaving_stale_for_pick_query
from src.juteSQC.weaving_sqc_query import (
    get_weaving_pick_active_row_query,
    get_weaving_pick_coverage_groups_query,
    get_weaving_pick_readings_by_date_query,
    get_weaving_pick_std_history_query,
    get_weaving_pick_summary_query,
    insert_weaving_pick_query,
    soft_delete_weaving_pick_query,
)


router = APIRouter()

PICK_NOT_FOUND_MSG = "SQC pick reading not found"
PICK_VALUE_REQUIRED_MSG = (
    "Each entry needs width or a pick value (picks_dm / picks) — at least one"
)

# 1 inch = 0.254 dm, so PPI = picks_dm * 0.254. The crew measures picks per
# decimetre; the production formula (36 * picks) needs picks per inch, so the
# stored ``picks`` column stays PPI and ``picks_dm`` keeps the as-typed reading.
PICKS_DM_TO_PPI = 0.254

# ITM-013 (2026-10-18) — standard-picks hint + measured-vs-standard coverage.
# "The standard" = jute_prod_weaving_target_map id_type='qid' (WEAVING_ID_TYPE_QLTY),
# value_role='standard', param='picks' (stored as PPI), LAST effective_date <= the
# reading date, newest id tiebreak, company-wide — exactly resolve_param's rule.
# It is never the summary's std_picks (STDDEV_SAMP) nor the weaving log's
# exact-day std_picks (the SQC average itself).
STD_PICKS_VALUE_ROLE = "standard"
STD_PICKS_PARAM = "picks"
# Equality is tested in PPI at 2 dp on both sides: stored picks = round(picks_dm
# * 0.254, 3) DECIMAL(10,3), so comparing at 2 dp absorbs the 3rd decimal. The FE
# equalsStandard() applies the same 2-dp PPI rule.
STD_EQUALITY_DP = 2
MAX_COVERAGE_DAYS = 93
COVERAGE_RANGE_MSG = f"Date range too large — at most {MAX_COVERAGE_DAYS} days"
COVERAGE_ORDER_MSG = "date_to is before date_from"
COVERAGE_STD_RULE = (
    "jute_prod_weaving_target_map id_type=qid value_role=standard param=picks (PPI), "
    "latest effective_date <= reading date, company-wide; "
    "equal = ROUND(picks,2) == ROUND(std,2); width-only readings excluded"
)


# =============================================================================
# Pydantic models
# =============================================================================


class WeavingPickRow(BaseModel):
    weaving_quality_id: int
    machine_id: int
    # Width and picks are each optional, but every entry must carry at least one
    # of them (validated at save) — width-only and picks-only readings are legal.
    width: Optional[float] = Field(default=None, gt=0)
    # picks_dm (picks per decimetre) is what the crew types; picks (PPI) is
    # accepted for backward compat;
    # when picks_dm is present the server computes picks = picks_dm * 0.254.
    picks_dm: Optional[float] = Field(default=None, ge=0)
    picks: Optional[float] = Field(default=None, ge=0)


class WeavingPickSave(BaseModel):
    co_id: int
    branch_id: Optional[int] = None
    entry_date: date
    # Optional spell: spell_id (preferred, branch-validated) or DEPRECATED spell CODE
    # (branch-scoped fallback); stamped on every reading of the batch (Phase 1c
    # join-key hygiene). Neither => NULL.
    spell_id: Optional[int] = None
    spell: Optional[str] = None
    entries: List[WeavingPickRow] = Field(default_factory=list)


# =============================================================================
# Helpers (mirrored from spinning_sqc.py)
# =============================================================================


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


def _require_entry_date(request: Request) -> date:
    raw = request.query_params.get("entry_date")
    if not raw:
        raise HTTPException(status_code=400, detail="entry_date is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid entry_date format")


def _require_date(request: Request, key: str) -> date:
    """Required ISO date query param (mirrors _require_entry_date for any key)."""
    raw = request.query_params.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {key} format")


def _as_date(v) -> date:
    """Coerce a DATE column value to ``date``: date passthrough, datetime.date(),
    ISO string via date.fromisoformat (first 10 chars, so a datetime ISO string
    also works)."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _i(v, default: Optional[int] = None) -> Optional[int]:
    return default if v is None else int(v)


def _fetch_looms(db: Session, branch_id: Optional[int]) -> list:
    looms = []
    for r in db.execute(
        get_weaving_entry_machines_query(),
        {"loom_type": WEAVING_MACHINE_TYPE_NAME, "branch_id": branch_id},
    ).fetchall():
        m = dict(r._mapping)
        looms.append(
            {
                "machine_id": m["machine_id"],
                "machine_name": m["machine_name"],
                "mech_code": m["mech_code"],
                "line_no": m.get("line_no"),
                "branch_id": m.get("branch_id"),
            }
        )
    return looms


def _fetch_qualities(db: Session, co_id: int) -> list:
    qualities = []
    for r in db.execute(
        get_weaving_entry_qualities_query(), {"co_id": co_id, "item_id": None}
    ).fetchall():
        m = dict(r._mapping)
        qualities.append(
            {
                "weaving_quality_id": m["weaving_quality_id"],
                "item_id": m.get("item_id"),
                "item_code": m.get("item_code"),
                "item_name": m.get("item_name"),
                "weaving_quality_code": m.get("weaving_quality_code"),
                "weaving_quality_name": m.get("weaving_quality_name"),
            }
        )
    return qualities


def _reading_row_out(r) -> dict:
    m = dict(r._mapping)
    return {
        "weaving_sqc_pick_id": m["weaving_sqc_pick_id"],
        "co_id": m["co_id"],
        "branch_id": m.get("branch_id"),
        "entry_date": str(m["entry_date"]) if m.get("entry_date") is not None else None,
        "weaving_quality_id": m.get("weaving_quality_id"),
        "weaving_quality_code": m.get("weaving_quality_code"),
        "weaving_quality_name": m.get("weaving_quality_name"),
        "item_code": m.get("item_code"),
        "item_name": m.get("item_name"),
        "machine_id": m.get("machine_id"),
        "mech_code": m.get("mech_code"),
        "machine_name": m.get("machine_name"),
        "line_no": m.get("line_no"),
        "width": None if m.get("width") is None else _f(m.get("width")),
        "picks": None if m.get("picks") is None else _f(m.get("picks")),
        # As-entered picks/dm; legacy PPI-entered rows derive it for display.
        # Width-only readings carry neither — both stay NULL.
        "picks_dm": (
            _f(m.get("picks_dm"))
            if m.get("picks_dm") is not None
            else (
                round(_f(m.get("picks")) / PICKS_DM_TO_PPI, 2)
                if m.get("picks") is not None
                else None
            )
        ),
    }


def _summary_row_out(r) -> dict:
    m = dict(r._mapping)
    return {
        "weaving_quality_id": m.get("weaving_quality_id"),
        "weaving_quality_code": m.get("weaving_quality_code"),
        "weaving_quality_name": m.get("weaving_quality_name"),
        # NULL when every reading for the quality/date is width-only.
        "avg_picks": None if m.get("avg_picks") is None else _f(m.get("avg_picks")),
        # Derived display value — the stored stats stay PPI (vw_weaving_pick_act).
        "avg_picks_dm": (
            None
            if m.get("avg_picks") is None
            else round(_f(m.get("avg_picks")) / PICKS_DM_TO_PPI, 2)
        ),
        "std_picks": _f(m.get("std_picks")),
        "min_picks": None if m.get("min_picks") is None else _f(m.get("min_picks")),
        "max_picks": None if m.get("max_picks") is None else _f(m.get("max_picks")),
        "avg_width": None if m.get("avg_width") is None else _f(m.get("avg_width")),
        "min_width": None if m.get("min_width") is None else _f(m.get("min_width")),
        "max_width": None if m.get("max_width") is None else _f(m.get("max_width")),
        "n_obs": _i(m.get("n_obs"), 0),
    }


# =============================================================================
# jute_sqc_weaving_pick — multi-observation pick readings
# =============================================================================


@router.get("/weaving_sqc_pick_setup")
def weaving_sqc_pick_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    entry_date = _require_entry_date(request)
    try:
        qualities = _fetch_qualities(db, co_id)
        looms = _fetch_looms(db, branch_id)
        reading_rows = db.execute(
            get_weaving_pick_readings_by_date_query(),
            {"co_id": co_id, "entry_date": entry_date, "branch_id": branch_id},
        ).fetchall()
        readings = [_reading_row_out(r) for r in reading_rows]
        summary_rows = db.execute(
            get_weaving_pick_summary_query(),
            {"co_id": co_id, "entry_date": entry_date},
        ).fetchall()
        summary = [_summary_row_out(r) for r in summary_rows]
        return {
            "data": {
                "qualities": qualities,
                "looms": looms,
                "readings": readings,
                "summary": summary,
            }
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid entry_date format")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/weaving_sqc_pick_save")
def weaving_sqc_pick_save(
    body: WeavingPickSave,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Insert-only, multi-observation save (no upsert). Each entry is a NEW
    pick reading; Act Picks resolves downstream as AVG(picks) per quality/date.
    An optional body.spell (code) is resolved branch-aware once and stamped on
    every reading (spell_id NULL when omitted).

    Picks unit (2026-08-21): the crew enters picks_dm (per decimetre); the stored
    ``picks`` (PPI) = picks_dm * 0.254 so the production formula stays PPI-based.
    A raw ``picks`` (PPI) is still accepted from older clients (picks_dm NULL).
    After saving, every STORED weaving unit the readings feed (exact day std_picks
    + later days' act_picks up to the next reading) is stale-flagged so the
    weaving page shows "Sync to refresh"."""
    try:
        user_id = token_data.get("user_id")
        spell_id = None
        if body.spell_id is not None or body.spell:
            spell_id = _resolve_spell(db, body.spell_id, body.spell, _i(body.branch_id))
        saved = 0
        for e in body.entries:
            if e.picks_dm is not None:
                picks_dm = float(e.picks_dm)
                picks = round(picks_dm * PICKS_DM_TO_PPI, 3)
            elif e.picks is not None:
                picks_dm = None
                picks = float(e.picks)
            else:
                # Width-only reading (picks NULL) — legal since 2026-08-24.
                picks_dm = None
                picks = None
            if picks is None and e.width is None:
                raise HTTPException(status_code=400, detail=PICK_VALUE_REQUIRED_MSG)
            db.execute(
                insert_weaving_pick_query(),
                {
                    "co_id": body.co_id,
                    "branch_id": body.branch_id,
                    "entry_date": body.entry_date,
                    "weaving_quality_id": int(e.weaving_quality_id),
                    "machine_id": int(e.machine_id),
                    "spell_id": spell_id,
                    "width": None if e.width is None else float(e.width),
                    "picks": picks,
                    "picks_dm": picks_dm,
                    "updated_by": user_id,
                },
            )
            saved += 1
        # Stale-flag stored weaving units per distinct quality (idempotent UPDATE).
        # Width-only entries carry no pick value, so they can't move any average.
        for qid in {
            int(e.weaving_quality_id)
            for e in body.entries
            if e.picks_dm is not None or e.picks is not None
        }:
            db.execute(
                flag_weaving_stale_for_pick_query(),
                {"co_id": body.co_id, "entry_date": body.entry_date,
                 "weaving_quality_id": qid},
            )
        db.commit()
        return {"data": {"saved": saved}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/weaving_sqc_pick_by_date")
def weaving_sqc_pick_by_date(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    entry_date = _require_entry_date(request)
    try:
        reading_rows = db.execute(
            get_weaving_pick_readings_by_date_query(),
            {"co_id": co_id, "entry_date": entry_date, "branch_id": branch_id},
        ).fetchall()
        readings = [_reading_row_out(r) for r in reading_rows]
        summary_rows = db.execute(
            get_weaving_pick_summary_query(),
            {"co_id": co_id, "entry_date": entry_date},
        ).fetchall()
        summary = [_summary_row_out(r) for r in summary_rows]
        return {"data": {"readings": readings, "summary": summary}}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid entry_date format")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/weaving_sqc_pick_delete/{pick_id}")
def weaving_sqc_pick_delete(
    pick_id: int,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        existing = db.execute(
            get_weaving_pick_active_row_query(),
            {"id": pick_id},
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=PICK_NOT_FOUND_MSG)

        db.execute(
            soft_delete_weaving_pick_query(),
            {"id": pick_id, "updated_by": token_data.get("user_id")},
        )
        # Removing a reading shifts the day's AVG (and later days' act_picks) —
        # stale-flag the stored weaving units it fed.
        db.execute(
            flag_weaving_stale_for_pick_query(),
            {"co_id": existing.co_id, "entry_date": existing.entry_date,
             "weaving_quality_id": existing.weaving_quality_id},
        )
        db.commit()
        return {"data": {"message": "Deleted"}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# ITM-013 — standard picks (hint) + measured-vs-standard coverage
# =============================================================================

# One standard-picks history entry: (effective_date, value_ppi_or_None, map_id).
StdHistoryEntry = Tuple[date, Optional[float], int]
StdHistory = Dict[int, List[StdHistoryEntry]]


def _row_map(r) -> dict:
    """Row -> plain dict (SQLAlchemy Row via ._mapping, or an already-plain dict)."""
    return dict(r._mapping) if hasattr(r, "_mapping") else dict(r)


def std_history_by_quality(rows) -> StdHistory:
    """Group jute_prod_weaving_target_map rows (ref_id, value, effective_date,
    weaving_target_map_id) per weaving_quality_id, each list sorted
    (effective_date DESC, id DESC) regardless of input order. Dates are coerced
    via _as_date; value NULL is kept as None (a NULL/0 latest row means
    "no standard" — see resolve_std_picks_on). Pure: no DB."""
    history: StdHistory = {}
    for r in rows:
        m = _row_map(r)
        qid = int(m["ref_id"])
        value = None if m.get("value") is None else float(m["value"])
        map_id = int(m.get("weaving_target_map_id") or 0)
        history.setdefault(qid, []).append((_as_date(m["effective_date"]), value, map_id))
    for entries in history.values():
        entries.sort(key=lambda e: (e[0], e[2]), reverse=True)
    return history


def _resolve_std_entry(
    history: StdHistory, weaving_quality_id, on_date
) -> Optional[StdHistoryEntry]:
    """The LAST-DATE entry (effective_date <= on_date, newest id tiebreak) for the
    quality, or None — same ranking as resolve_param. Does NOT skip a NULL/0 row."""
    on = _as_date(on_date)
    for entry in history.get(int(weaving_quality_id), []):
        if entry[0] <= on:
            return entry
    return None


def resolve_std_picks_on(history: StdHistory, weaving_quality_id, on_date) -> Optional[float]:
    """Standard picks (PPI) in force for the quality on ``on_date``: the first
    history entry with effective_date <= on_date. Returns float(value) only when
    that entry's value is not None and > 0, else None — a latest row with 0/NULL
    means "no standard" and never falls back to an older row (resolve_param
    semantics). Pure: no DB."""
    entry = _resolve_std_entry(history, weaving_quality_id, on_date)
    if entry is None:
        return None
    value = entry[1]
    if value is None or value <= 0:
        return None
    return float(value)


def picks_equal_to_std(picks_ppi, std_ppi) -> bool:
    """True when a reading (PPI) equals the standard (PPI) at STD_EQUALITY_DP
    decimals. False when either is None or the standard is <= 0. Stored picks are
    DECIMAL(10,3) = round(picks_dm * 0.254, 3), so the 2-dp comparison absorbs
    the 3rd decimal; the FE equalsStandard() uses the same rule. Pure."""
    if picks_ppi is None or std_ppi is None:
        return False
    std = float(std_ppi)
    if std <= 0:
        return False
    return abs(round(float(picks_ppi), STD_EQUALITY_DP) - round(std, STD_EQUALITY_DP)) < 1e-6


def _quality_label(code, name, weaving_quality_id: int) -> str:
    parts = [str(p).strip() for p in (code, name) if p is not None and str(p).strip()]
    return " — ".join(parts) if parts else f"Quality #{weaving_quality_id}"


def build_pick_coverage(group_rows, std_history: StdHistory) -> List[dict]:
    """Coverage rows from get_weaving_pick_coverage_groups_query() groups
    (entry_date, weaving_quality_id, weaving_quality_code, weaving_quality_name,
    picks_2dp, n) and the standard history.

    One row per (date, quality) that has >= 1 reading with picks (width-only
    readings never reach this builder); readings = SUM(n); equal_to_std = SUM(n)
    over the groups whose picks_2dp equals the date-resolved standard;
    measured_share_pct = round(100 * (readings - equal_to_std) / readings, 1).
    A quality with no standard on that date: has_std False, std_* None,
    equal_to_std 0, measured_share_pct None (never a fake 100). Ordered by date,
    quality name, quality id. Pure: no DB."""
    buckets: Dict[Tuple[date, int], dict] = {}
    for r in group_rows:
        m = _row_map(r)
        d = _as_date(m["entry_date"])
        qid = int(m["weaving_quality_id"])
        n = int(m.get("n") or 0)
        key = (d, qid)
        b = buckets.get(key)
        if b is None:
            std = resolve_std_picks_on(std_history, qid, d)
            b = {
                "date": d.isoformat(),
                "weaving_quality_id": qid,
                "weaving_quality_code": m.get("weaving_quality_code"),
                "weaving_quality_name": m.get("weaving_quality_name"),
                "quality": _quality_label(
                    m.get("weaving_quality_code"), m.get("weaving_quality_name"), qid
                ),
                "readings": 0,
                "equal_to_std": 0,
                "measured_share_pct": None,
                "std_picks_ppi": std,
                "std_picks_dm": None if std is None else round(std / PICKS_DM_TO_PPI, 2),
                "has_std": std is not None,
            }
            buckets[key] = b
        b["readings"] += n
        if b["has_std"] and picks_equal_to_std(m.get("picks_2dp"), b["std_picks_ppi"]):
            b["equal_to_std"] += n

    rows: List[dict] = []
    for (d, qid), b in sorted(
        buckets.items(),
        key=lambda kv: (kv[0][0], str(kv[1]["weaving_quality_name"] or ""), kv[0][1]),
    ):
        if b["has_std"] and b["readings"] > 0:
            b["measured_share_pct"] = round(
                100.0 * (b["readings"] - b["equal_to_std"]) / b["readings"], 1
            )
        rows.append(b)
    return rows


def coverage_totals(rows: List[dict]) -> dict:
    """Totals over build_pick_coverage rows: readings (all), readings_with_std,
    equal_to_std, measured_share_pct over readings_with_std only (None when 0),
    days = distinct dates, qualities = distinct quality ids. Pure."""
    readings = sum(int(r["readings"]) for r in rows)
    with_std = sum(int(r["readings"]) for r in rows if r.get("has_std"))
    equal = sum(int(r["equal_to_std"]) for r in rows if r.get("has_std"))
    share = round(100.0 * (with_std - equal) / with_std, 1) if with_std > 0 else None
    return {
        "readings": readings,
        "readings_with_std": with_std,
        "equal_to_std": equal,
        "measured_share_pct": share,
        "days": len({r["date"] for r in rows}),
        "qualities": len({r["weaving_quality_id"] for r in rows}),
    }


def _std_history_binds(co_id: int, on_date: date) -> dict:
    return {
        "co_id": co_id,
        "id_type": WEAVING_ID_TYPE_QLTY,
        "value_role": STD_PICKS_VALUE_ROLE,
        "param": STD_PICKS_PARAM,
        "on_date": on_date,
    }


@router.get("/weaving_sqc_pick_standards")
def weaving_sqc_pick_standards(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Per-quality standard picks in force on entry_date — the HINT shown beside
    the Picks/dm input (ITM-013). Company-wide (no branch_id: the target map is
    resolved branch-agnostic, like resolve_param). Deliberately a separate
    endpoint: the setup payload carries NO pick default, so the entry screen can
    never prefill a standard. Qualities with no row, or whose latest row <=
    entry_date is NULL/0, are omitted (absent = "no standard")."""
    co_id = _require_co_id(request)
    entry_date = _require_entry_date(request)
    try:
        std_rows = db.execute(
            get_weaving_pick_std_history_query(), _std_history_binds(co_id, entry_date)
        ).fetchall()
        history = std_history_by_quality(std_rows)
        standards = []
        for qid in sorted(history):
            std = resolve_std_picks_on(history, qid, entry_date)
            if std is None:
                continue
            entry = _resolve_std_entry(history, qid, entry_date)
            standards.append(
                {
                    "weaving_quality_id": qid,
                    "std_picks_ppi": std,
                    "std_picks_dm": round(std / PICKS_DM_TO_PPI, 2),
                    "effective_date": entry[0].isoformat() if entry else None,
                }
            )
        return {"data": {"entry_date": entry_date.isoformat(), "standards": standards}}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/weaving_pick_coverage")
def weaving_pick_coverage(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Picks Coverage report (ITM-013): per (date, quality) how many pick readings
    equal the quality's standard at 2 dp PPI, and the measured share.

    Validation (before any DB call): co_id, optional branch_id, date_from,
    date_to, date_to >= date_from, range <= MAX_COVERAGE_DAYS. Exactly two
    executes: the 2-dp groups, then the standard history up to date_to (one
    fetch serves every date of the range). Readings honour the branch filter;
    the standard is company-wide."""
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    date_from = _require_date(request, "date_from")
    date_to = _require_date(request, "date_to")
    if date_to < date_from:
        raise HTTPException(status_code=400, detail=COVERAGE_ORDER_MSG)
    if (date_to - date_from).days + 1 > MAX_COVERAGE_DAYS:
        raise HTTPException(status_code=400, detail=COVERAGE_RANGE_MSG)
    try:
        group_rows = db.execute(
            get_weaving_pick_coverage_groups_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "date_from": date_from,
                "date_to": date_to,
            },
        ).fetchall()
        std_rows = db.execute(
            get_weaving_pick_std_history_query(), _std_history_binds(co_id, date_to)
        ).fetchall()
        rows = build_pick_coverage(group_rows, std_history_by_quality(std_rows))
        totals = coverage_totals(rows)
        return {
            "data": rows,
            "meta": {
                "co_id": co_id,
                "branch_id": branch_id,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "max_days": MAX_COVERAGE_DAYS,
                "std_rule": COVERAGE_STD_RULE,
                "totals": totals,
            },
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

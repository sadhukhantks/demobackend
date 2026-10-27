"""Loom->Quality map bulk tools (TKT-000119), mounted under /api/weavingProd.

POST /quality_map_carry_forward
    "Fill from previous". For every (date, spell) cell in [from_date, to_date] (all the
    branch's spells, or the ones asked for), each loom WITHOUT an active map row gets
    a row copying its previous mapping. Existing rows are never changed. Works in time
    order, so a loom remapped on day 1 carries the new quality into day 2. Set
    ``dry_run`` to get the per-cell counts without writing (the page's confirm step).

    Rules:
      * previous mapping = the loom's latest active map row before the cell
        (walked in date + spell-start order; seeded from the last LOOKBACK_DAYS before
        from_date, then the weaving_loom_current_quality pointer when it is older);
      * a production row already captured in the cell with a quality wins over the
        carried value (the map is written to match it, never the other way round);
      * production rows captured with NO quality (entry before the map existed) are
        re-stamped with the filled quality, exactly as Save Map does;
      * Processed (locked) cells are SKIPPED, whatever the user's level — remapping
        frozen data stays an explicit Save Map on that one spell (Edit gate there);
        stored-but-unlocked cells that receive rows get the reprocess flag;
      * no future dates; at most MAX_FILL_DAYS per call.

GET /quality_map_groups
    Loom groupings for "select by group" in the map grid: Hands Norms machine groups
    (jute_prod_machine_group_*) and the Loom Groups master (jute_prod_loom_group*),
    loom members of the branch only. Reuses existing masters; no new grouping concept.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import WEAVING_MACHINE_TYPE_NAME
from src.juteProduction.weaving_lock import flag_reprocess_if_locked, is_unit_locked
from src.juteProduction.weaving_quality_map_bulk_query import (
    get_branch_looms_query,
    get_current_quality_pointers_query,
    get_loom_group_looms_query,
    get_machine_group_looms_query,
    get_map_rows_in_range_query,
    get_production_rows_in_range_query,
    insert_carry_forward_map_rows_query,
)
from src.juteProduction.weaving_query import (
    get_weaving_spells_query,
    update_weaving_daily_quality_stamp_query,
    upsert_weaving_current_quality_query,
)

router = APIRouter()

MAX_FILL_DAYS = 31
LOOKBACK_DAYS = 14

Cell = Tuple[date, int]  # (tran_date, spell_id)

# The mills work on IST while this server's clock is UTC: between 00:00 and 05:30 IST a
# plain date.today() is still "yesterday" and would refuse today's spells. Take the later
# of the two so the check is never stricter than either clock (fixed offset — no tzdata).
_IST = timezone(timedelta(hours=5, minutes=30))


def plant_today() -> date:
    return max(date.today(), datetime.now(_IST).date())


# =============================================================================
# Request model
# =============================================================================


class CarryForwardRequest(BaseModel):
    co_id: int
    branch_id: int
    from_date: Optional[date] = None  # defaults to to_date
    to_date: date
    spell_ids: Optional[List[int]] = None  # defaults to every spell of the branch
    dry_run: bool = False


# =============================================================================
# Pure planner (no DB) — unit-tested directly
# =============================================================================


def plan_carry_forward(
    ordered_cells: List[Cell],
    target_cells: Set[Cell],
    looms: Iterable[int],
    seed: Dict[int, int],
    existing: Dict[Cell, Dict[int, int]],
    production: Dict[Cell, Dict[int, Optional[int]]],
    locked: Set[Cell],
) -> Tuple[Dict[Cell, Dict[int, int]], List[dict], Dict[int, Cell]]:
    """Decide which (cell, loom) rows to insert and with which quality.

    ordered_cells   every (date, spell) cell of the range in time order — INCLUDING
                    non-target spells, so their saved rows still move the carry.
    target_cells    the cells the user asked to fill.
    seed            loom -> quality in force just before the first cell.
    existing        cell -> {loom: quality} active map rows already saved.
    production      cell -> {loom: stored quality or None} active production rows.
    locked          processed cells — never written, but the carry passes through.

    Returns (fills, summary, last_fill):
      fills      cell -> {loom: quality} rows to insert;
      summary    one dict per target cell (counts for the confirm dialog / result);
      last_fill  loom -> cell of its last fill when that fill is the loom's latest
                 known mapping in the range (drives the current-quality pointer).
    """
    loom_list = sorted(set(int(m) for m in looms))
    current: Dict[int, int] = {int(k): int(v) for k, v in seed.items()}
    fills: Dict[Cell, Dict[int, int]] = {}
    summary: List[dict] = []
    last_fill: Dict[int, Cell] = {}

    for cell in ordered_cells:
        saved = existing.get(cell, {})
        prod = production.get(cell, {})
        is_target = cell in target_cells
        is_locked = cell in locked
        cell_fills: Dict[int, int] = {}
        already = no_previous = from_production = restamp = would_fill_locked = 0

        for m in loom_list:
            if m in saved:
                current[m] = saved[m]
                last_fill.pop(m, None)  # a saved row after the fill owns the pointer
                if is_target:
                    already += 1
                continue
            stored_q = prod.get(m)
            q = stored_q if stored_q is not None else current.get(m)
            if q is None:
                if is_target:
                    no_previous += 1
                continue
            # Carry passes through every cell, filled or not, so the next cell sees it.
            current[m] = q
            if not is_target:
                continue
            if is_locked:
                would_fill_locked += 1
                continue
            cell_fills[m] = q
            last_fill[m] = cell
            if stored_q is not None:
                from_production += 1
            elif m in prod:  # production row with NULL quality -> gets re-stamped
                restamp += 1

        if cell_fills:
            fills[cell] = cell_fills
        if is_target:
            summary.append(
                {
                    "tran_date": str(cell[0]),
                    "spell_id": cell[1],
                    "locked": is_locked,
                    "already_mapped": already,
                    "to_fill": len(cell_fills),
                    "no_previous": no_previous,
                    "from_production": from_production,
                    "restamp": restamp,
                    "skipped_locked": would_fill_locked,
                }
            )
    return fills, summary, last_fill


def latest_per_loom(rows: Iterable[dict], spell_rank: Dict[int, int]) -> Dict[int, int]:
    """loom -> quality of its newest row, ordered by (date, spell start, map id)."""
    best: Dict[int, Tuple[tuple, int]] = {}
    for r in rows:
        m = int(r["machine_id"])
        key = (r["tran_date"], spell_rank.get(int(r["spell_id"]), -1),
               int(r.get("weaving_quality_map_id") or 0))
        if m not in best or key > best[m][0]:
            best[m] = (key, int(r["weaving_quality_id"]))
    return {m: q for m, (_, q) in best.items()}


def validate_range(from_date: date, to_date: date, today: date) -> None:
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="from_date must be on or before to_date")
    if to_date > today:
        raise HTTPException(status_code=400, detail="Future dates cannot be mapped")
    if (to_date - from_date).days + 1 > MAX_FILL_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_FILL_DAYS} days can be filled in one action",
        )


def _days(from_date: date, to_date: date) -> List[date]:
    return [from_date + timedelta(days=i) for i in range((to_date - from_date).days + 1)]


# =============================================================================
# DB helpers (patched in endpoint tests)
# =============================================================================


def _branch_spells(db: Session, branch_id: int) -> List[dict]:
    """Branch spells in start-time order, de-duplicated by code (same as setup)."""
    spells, seen = [], set()
    for r in db.execute(get_weaving_spells_query(), {"branch_id": branch_id}).fetchall():
        m = dict(r._mapping)
        if m["spell_code"] in seen:
            continue
        seen.add(m["spell_code"])
        spells.append({"spell_id": int(m["spell_id"]), "spell_code": m["spell_code"]})
    return spells


def _branch_looms(db: Session, branch_id: int) -> List[int]:
    rows = db.execute(
        get_branch_looms_query(),
        {"branch_id": branch_id, "loom_type": WEAVING_MACHINE_TYPE_NAME},
    ).fetchall()
    return [int(r.machine_id) for r in rows]


def _map_rows(db: Session, co_id: int, from_date: date, to_date: date,
              looms: Set[int]) -> List[dict]:
    rows = db.execute(
        get_map_rows_in_range_query(),
        {"co_id": co_id, "from_date": from_date, "to_date": to_date},
    ).fetchall()
    out = []
    for r in rows:
        m = dict(r._mapping)
        if int(m["machine_id"]) in looms:
            out.append(m)
    return out


def _production_rows(db: Session, co_id: int, branch_id: int, from_date: date,
                     to_date: date, looms: Set[int]) -> List[dict]:
    rows = db.execute(
        get_production_rows_in_range_query(),
        {"co_id": co_id, "branch_id": branch_id, "from_date": from_date, "to_date": to_date},
    ).fetchall()
    return [dict(r._mapping) for r in rows if int(r._mapping["machine_id"]) in looms]


def _pointers(db: Session, co_id: int, branch_id: int) -> List[dict]:
    rows = db.execute(
        get_current_quality_pointers_query(), {"co_id": co_id, "branch_id": branch_id}
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _is_locked(db: Session, co_id: int, branch_id: int, d: date, spell_id: int) -> bool:
    return is_unit_locked(db, co_id, branch_id, d, spell_id)


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/quality_map_carry_forward")
def quality_map_carry_forward(
    body: CarryForwardRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Fill unmapped looms of a date/spell range from their previous mapping.

    Response: {"data": {"dry_run", "filled", "restamped", "cells": [...]}} where each
    cell carries tran_date, spell_id, spell_code, locked, already_mapped, to_fill,
    no_previous, from_production, restamp, skipped_locked.
    """
    from_date = body.from_date or body.to_date
    to_date = body.to_date
    validate_range(from_date, to_date, plant_today())
    co_id, branch_id = int(body.co_id), int(body.branch_id)
    try:
        spells = _branch_spells(db, branch_id)
        if not spells:
            raise HTTPException(status_code=400, detail="No spells configured for this branch")
        spell_ids = [s["spell_id"] for s in spells]
        spell_code = {s["spell_id"]: s["spell_code"] for s in spells}
        spell_rank = {sid: i for i, sid in enumerate(spell_ids)}
        if body.spell_ids:
            unknown = sorted(set(int(s) for s in body.spell_ids) - set(spell_ids))
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"Spell(s) {unknown} do not belong to this branch",
                )
            wanted = set(int(s) for s in body.spell_ids)
        else:
            wanted = set(spell_ids)

        looms = _branch_looms(db, branch_id)
        if not looms:
            raise HTTPException(status_code=400, detail="No weaving looms found for this branch")
        loom_set = set(looms)

        days = _days(from_date, to_date)
        ordered_cells: List[Cell] = [(d, sid) for d in days for sid in spell_ids]
        target_cells = {c for c in ordered_cells if c[1] in wanted}

        # Seed: newest row in the look-back window, else the pointer when it is older.
        seed = latest_per_loom(
            _map_rows(db, co_id, from_date - timedelta(days=LOOKBACK_DAYS),
                      from_date - timedelta(days=1), loom_set),
            spell_rank,
        )
        for p in _pointers(db, co_id, branch_id):
            m = int(p["machine_id"])
            if (m in loom_set and m not in seed and p.get("weaving_quality_id") is not None
                    and p["tran_date"] < from_date):
                seed[m] = int(p["weaving_quality_id"])

        existing: Dict[Cell, Dict[int, int]] = defaultdict(dict)
        newest_id: Dict[Tuple[Cell, int], int] = {}
        for r in _map_rows(db, co_id, from_date, to_date, loom_set):
            cell = (r["tran_date"], int(r["spell_id"]))
            m = int(r["machine_id"])
            rid = int(r.get("weaving_quality_map_id") or 0)
            if (cell, m) not in newest_id or rid > newest_id[(cell, m)]:
                newest_id[(cell, m)] = rid
                existing[cell][m] = int(r["weaving_quality_id"])

        production: Dict[Cell, Dict[int, Optional[int]]] = defaultdict(dict)
        prod_ids: Dict[Tuple[Cell, int], List[int]] = defaultdict(list)
        for r in _production_rows(db, co_id, branch_id, from_date, to_date, loom_set):
            cell = (r["tran_date"], int(r["spell_id"]))
            m = int(r["machine_id"])
            q = r.get("weaving_quality_id")
            # Any stored quality beats a NULL one for the same loom/cell.
            if production[cell].get(m) is None:
                production[cell][m] = None if q is None else int(q)
            if q is None:
                prod_ids[(cell, m)].append(int(r["weaving_daily_id"]))

        locked = {c for c in target_cells if _is_locked(db, co_id, branch_id, c[0], c[1])}

        fills, summary, last_fill = plan_carry_forward(
            ordered_cells, target_cells, looms, seed, existing, production, locked
        )
        for s in summary:
            s["spell_code"] = spell_code.get(s["spell_id"])
        total = sum(len(v) for v in fills.values())
        restamp_ids = [
            (rid, q)
            for cell, per_loom in fills.items()
            for m, q in per_loom.items()
            for rid in prod_ids.get((cell, m), [])
        ]

        if body.dry_run or total == 0:
            db.rollback()
            return {"data": {"dry_run": body.dry_run, "filled": 0,
                             "to_fill": total, "restamped": 0, "cells": summary}}

        user_id = token_data.get("user_id")
        insert_rows = [
            {"co_id": co_id, "branch_id": branch_id, "tran_date": cell[0],
             "spell_id": cell[1], "machine_id": m, "weaving_quality_id": q,
             "updated_by": user_id}
            for cell, per_loom in fills.items()
            for m, q in per_loom.items()
        ]
        db.execute(insert_carry_forward_map_rows_query(), insert_rows)
        if restamp_ids:
            db.execute(
                update_weaving_daily_quality_stamp_query(),
                [{"id": rid, "weaving_quality_id": q, "updated_by": user_id}
                 for rid, q in restamp_ids],
            )
        pointer_rows = [
            {"co_id": co_id, "branch_id": branch_id, "machine_id": m,
             "weaving_quality_id": fills[cell][m], "tran_date": cell[0], "spell_id": cell[1]}
            for m, cell in last_fill.items()
        ]
        if pointer_rows:
            db.execute(upsert_weaving_current_quality_query(), pointer_rows)
        # A stored (synced, unlocked) unit's cache is now stale — flag it like Save Map.
        for cell in fills:
            flag_reprocess_if_locked(db, co_id, cell[0], cell[1])
        db.commit()
        return {"data": {"dry_run": False, "filled": total, "to_fill": total,
                         "restamped": len(restamp_ids), "cells": summary}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


def _group_rows(db: Session, query, binds: dict) -> List[dict]:
    """Rows of one grouping source; a tenant without that master's tables yields []."""
    try:
        return [dict(r._mapping) for r in db.execute(query, binds).fetchall()]
    except ProgrammingError as e:  # 1146: table doesn't exist on this tenant
        if getattr(getattr(e, "orig", None), "args", [None])[0] == 1146:
            db.rollback()
            return []
        raise


def collect_groups(source: str, rows: Iterable[dict]) -> List[dict]:
    """Fold (group, machine) rows into [{source, group_id, code, name, machine_ids}]."""
    groups: Dict[int, dict] = {}
    for r in rows:
        gid = int(r["group_id"])
        g = groups.setdefault(
            gid,
            {"source": source, "group_id": gid, "group_code": r.get("group_code"),
             "group_name": r.get("group_name"), "machine_ids": []},
        )
        mid = int(r["machine_id"])
        if mid not in g["machine_ids"]:
            g["machine_ids"].append(mid)
    return list(groups.values())


@router.get("/quality_map_groups")
def quality_map_groups(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Existing loom groupings of a branch, for bulk selection on the map grid."""
    try:
        co_id = int(request.query_params.get("co_id") or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        branch_id = int(request.query_params.get("branch_id") or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="branch_id is required")
    try:
        binds = {"co_id": co_id, "branch_id": branch_id, "loom_type": WEAVING_MACHINE_TYPE_NAME}
        data = collect_groups(
            "machine_group", _group_rows(db, get_machine_group_looms_query(), binds)
        ) + collect_groups(
            "loom_group", _group_rows(db, get_loom_group_looms_query(), binds)
        )
        return {"data": data}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

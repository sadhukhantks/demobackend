"""Weaving Process lock lookups + the locked-unit permission gate.

A (co, branch, tran_date, spell) unit is locked once Processed. While locked,
weaving-page mutations require Edit (access_type_id >= 4); Write-only (3) is
rejected 403. Reads use is_unit_locked to choose frozen-log vs live slice."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.common.portal.query import get_user_menu_access_level_query
from src.juteProduction.weaving_query import (
    flag_weaving_unit_reprocess_query,
    get_weaving_daily_spells_query,
    get_weaving_day_headers_query,
    get_weaving_entries_by_date_query,
    get_weaving_log_rows_query,
)

WEAVING_MENU_PATH = "juteProduction/weaving"
EDIT_LEVEL = 4
LOCKED_EDIT_ONLY_MSG = (
    "This day/spell is processed and locked. Editing a locked weaving entry "
    "requires Edit permission for the Weaving Production menu."
)


def get_process_lock(db: Session, co_id, branch_id, tran_date, spell_id):
    """Active sync/lock header for the unit (or None). A header exists once the
    unit was synced (is_locked=0) or processed (is_locked=1) — either way the
    jute_prod_weaving_log snapshot exists and readers serve it.

    A unit can carry one header PER BRANCH (branch-scoped syncs), so a LOCKED
    header must win over a newer sync-only one — otherwise a later sync would
    mask the lock and let a Write-only user overwrite frozen data co-wide
    (is_locked DESC before the id tiebreak)."""
    return db.execute(
        text(
            """
            SELECT weaving_process_lock_id, branch_id, is_locked, reprocess_needed,
                   processed_date_time, synced_date_time
            FROM jute_prod_weaving_process_lock
            WHERE co_id = :co_id AND tran_date = :tran_date AND spell_id = :spell_id
              AND (:branch_id IS NULL OR branch_id = :branch_id OR branch_id IS NULL)
              AND active = 1
            ORDER BY is_locked DESC, weaving_process_lock_id DESC
            LIMIT 1
            """
        ),
        {"co_id": int(co_id), "tran_date": tran_date, "spell_id": int(spell_id),
         "branch_id": None if branch_id is None else int(branch_id)},
    ).fetchone()


def is_unit_locked(db: Session, co_id, branch_id, tran_date, spell_id) -> bool:
    row = get_process_lock(db, co_id, branch_id, tran_date, spell_id)
    return bool(row and row.is_locked)


def header_covers_scope(header_branch_id, request_branch_id) -> bool:
    """Does a header's scope COVER the requested read scope? A co-wide header
    (branch NULL) covers everything; a branch-scoped header covers only
    branch-scoped requests (the header queries already exclude other branches
    when a branch is given). A co-wide READ must never serve the log off a
    branch-scoped header — the log would be missing other branches' rows."""
    return header_branch_id is None or request_branch_id is not None


def is_unit_stored(db: Session, co_id, branch_id, tran_date, spell_id) -> bool:
    """True when a sync/lock header COVERING the request scope exists — i.e.
    stored log rows fully represent this read and should be served (with the
    stale flag) instead of recomputing live."""
    rows = db.execute(
        get_weaving_day_headers_query(),
        {"co_id": int(co_id), "tran_date": tran_date, "spell_id": int(spell_id),
         "branch_id": None if branch_id is None else int(branch_id)},
    ).fetchall()
    return any(header_covers_scope(r.branch_id, branch_id) for r in rows)


def user_menu_access_level(db: Session, user_id, co_id, branch_id, menu_path) -> int:
    lvl = db.execute(
        get_user_menu_access_level_query(),
        {"user_id": int(user_id), "co_id": int(co_id),
         "branch_id": None if branch_id is None else int(branch_id),
         "menu_path": menu_path},
    ).scalar()
    return int(lvl) if lvl is not None else 0


def require_edit_if_locked(db: Session, token_data: dict, co_id, branch_id,
                           tran_date, spell_id) -> None:
    """Raise 403 when the unit is locked and the user lacks Edit (>= 4)."""
    if not is_unit_locked(db, co_id, branch_id, tran_date, spell_id):
        return
    level = user_menu_access_level(
        db, token_data.get("user_id"), co_id, branch_id, WEAVING_MENU_PATH
    )
    if level < EDIT_LEVEL:
        raise HTTPException(status_code=403, detail=LOCKED_EDIT_ONLY_MSG)


def fetch_weaving_day_rows(db: Session, co_id, tran_date, branch_id) -> list:
    """Full-day weaving rows for REPORTS (DPR, winding-weaving difference):
    FRESH stored log rows per spell when available (latest active header with
    reprocess_needed = 0), live day-slice recompute for stale or never-synced
    spells — a report must never serve stale cached numbers silently (unlike the
    interactive grids, which serve stale stored rows WITH a warning chip).

    Same row shape either way (the log projection reuses the day-slice aliases);
    with no fresh header the whole day is one live call — pre-sync behavior.

    Multi-branch rule: a spell is served from the log only when a header COVERS
    this read scope (see header_covers_scope) AND every header of the spell is
    fresh — one branch's stale header means part of the log is stale, so the
    whole spell recomputes live rather than mixing fresh and stale stored rows."""
    binds = {"co_id": int(co_id), "tran_date": tran_date,
             "branch_id": None if branch_id is None else int(branch_id)}
    headers = {}
    for h in db.execute(
        get_weaving_day_headers_query(), {**binds, "spell_id": None}
    ).fetchall():
        headers.setdefault(int(h.spell_id), []).append(h)
    fresh = {
        sid for sid, hs in headers.items()
        if any(header_covers_scope(h.branch_id, branch_id) for h in hs)
        and all(not h.reprocess_needed for h in hs)
    }

    if not fresh:
        res = db.execute(
            get_weaving_entries_by_date_query(),
            {**binds, "spell_id": None, "machine_id": None},
        )
        return [dict(r._mapping) for r in res.fetchall()]

    rows = []
    for r in db.execute(get_weaving_daily_spells_query(), binds).fetchall():
        sid = int(r.spell_id)
        q = (get_weaving_log_rows_query() if sid in fresh
             else get_weaving_entries_by_date_query())
        rows.extend(
            dict(x._mapping)
            for x in db.execute(q, {**binds, "spell_id": sid, "machine_id": None}).fetchall()
        )
    return rows


def flag_reprocess_if_locked(db: Session, co_id, tran_date, spell_id) -> None:
    """Raise the stale flag (reprocess_needed) on the unit's header — locked OR
    sync-only (no-op when the unit has no header, i.e. nothing stored).

    Spec §7 + 2026-08-21 sync: any mutation of a stored unit invalidates its
    snapshot/cache, so the unit must be flagged for re-sync/re-process. Safe to
    call on any mutation path."""
    if co_id is None or spell_id is None:
        return
    db.execute(
        flag_weaving_unit_reprocess_query(),
        {"co_id": int(co_id), "tran_date": tran_date, "spell_id": int(spell_id)},
    )

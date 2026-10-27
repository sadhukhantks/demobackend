"""Weaving Process + Sync + status endpoints (spec §6/§8 + 2026-08-21 sync).

Process is set-based, one transaction: BLOCK on unmapped quality AND on daily rows
whose stored quality is NULL/stale vs the active map, collect WARN lists (no_worker /
no_standard / no_picks / negative_jugar / operator_ambiguous), resolve open_jugar per
unit row (full-history two-probe), resolve + stamp the loom worker from attendance
(weaver-designation cascade, spinning's proven pattern), soft-delete + INSERT...SELECT
freeze from weaving_day_slice_sql (parity oracle — never the Python resolvers), lock
header upsert. Every unit query is branch-scoped when the body sends branch_id
(NULL = co-wide).

Sync (POST /sync) is the SAME recompute-and-store WITHOUT the lock: unmapped /
stale-quality looms are WARNED, never blocked (their rows store zero production
until mapped — a later sync heals them), and the header is upserted with
is_locked untouched (0 on a fresh header). The stored jute_prod_weaving_log rows
then serve the planning grid / entries / reports as a cache; any mutation of the
unit's inputs (entries, SQC picks, stoppage, target-map standards) raises the
stale flag and the FE shows "Sync to refresh". Attendance edits (working/idle
hours feed loom hours since 2026-08-26) have no write-time flag — the on-read
drift probe catches them. process_status reports locked/synced/stale for any
header and runs the SQC/stoppage/attendance drift probe."""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.weaving_entry import _derive_co_id, _i
# Shared spell-resolution contract (spell_id preferred, branch-scoped code fallback) —
# import, do NOT copy. See spinning_entry._resolve_spell.
from src.juteProduction.spinning_entry import _resolve_spell, _optional_spell_id_param
from src.juteProduction.weaving_lock import get_process_lock, require_edit_if_locked
from src.juteProduction.constants import WEAVING_SYNC_RANGE_MAX_DAYS
from src.juteProduction.weaving_query import (
    get_weaving_unit_daily_ids_query,
    get_weaving_unmapped_produced_looms_query,
    get_weaving_process_quality_mismatch_query,
    get_weaving_process_negative_jugar_query,
    get_weaving_process_no_worker_query,
    get_weaving_process_no_standard_query,
    get_weaving_process_no_picks_query,
    get_weaving_null_branch_rows_query,
    soft_delete_weaving_log_for_unit_query,
    insert_weaving_log_from_slice_query,
    WEAVER_DESIG_PREFIX,
    get_weaving_operator_candidates_query,
    clear_weaving_daily_eb_query,
    stamp_weaving_daily_eb_bulk_query,
    get_weaving_unit_open_jugar_query,
    get_weaving_units_in_range_query,
    update_weaving_daily_open_jugar_bulk_query,
    bulk_case_binds,
    get_weaving_process_lock_row_query,
    insert_weaving_process_lock_query,
    update_weaving_process_lock_query,
    update_weaving_process_lock_reprocess_query,
    insert_weaving_sync_header_query,
    update_weaving_header_synced_query,
    get_weaving_drift_query,
)

router = APIRouter()

BLOCK_MSG = "Cannot process: these looms have production but no mapped quality."


class ProcessRequest(BaseModel):
    co_id: Optional[int] = None
    branch_id: Optional[int] = None
    tran_date: date
    spell_id: Optional[int] = None  # preferred — validated against the branch's shifts
    spell: Optional[str] = None  # DEPRECATED spell CODE fallback (branch-scoped)
    # Escape hatch for tenants whose designation_mst is not weaver-named: takes
    # ANY attendance row marked on the loom (spinning's ignore_designation).
    ignore_designation: bool = False


def _rows(res):
    return [dict(r._mapping) for r in res.fetchall()]


def _resolve_operators(cand_rows) -> tuple:
    """Attendance candidates -> ({mc_id: eb_id}, [ambiguity warnings]).

    Winner = the lowest daily_atten_id (first-marked, deterministic). Any loom
    with >1 DISTINCT eb is ALSO reported, because marking order is arbitrary
    between two real weavers — visible, never a silent guess. Duplicate marks of
    the SAME eb are not ambiguous. Pure function, kept out of the DB path so it
    is unit-testable."""
    by_mc: dict = {}
    for r in cand_rows:
        by_mc.setdefault(int(r.mc_id), []).append((int(r.daily_atten_id), int(r.eb_id)))
    resolved, ambiguous = {}, []
    for mc_id, cands in by_mc.items():
        _, chosen_eb = min(cands)
        resolved[mc_id] = chosen_eb
        eb_ids = sorted({eb for _, eb in cands})
        if len(eb_ids) > 1:
            ambiguous.append({"machine_id": mc_id, "eb_ids": eb_ids,
                              "stamped_eb_id": chosen_eb})
    return resolved, ambiguous


def _resolve_unit(db: Session, body: ProcessRequest):
    """Shared Process/Sync param resolution -> (co_id, spell_id, unit binds).

    branch_id scopes every unit query (NULL = co-wide) so branch A's run never
    re-freezes branch B's rows on shared-spell tenants."""
    spell_id = _resolve_spell(db, body.spell_id, body.spell, body.branch_id)
    co_id = body.co_id if body.co_id is not None else _derive_co_id(db, body.branch_id)
    if co_id is None:
        raise HTTPException(
            status_code=400,
            detail="co_id is required (or a branch_id it can be derived from)",
        )
    binds = {
        "co_id": int(co_id),
        "tran_date": body.tran_date,
        "spell_id": int(spell_id),
        "branch_id": _i(body.branch_id),
    }
    return co_id, spell_id, binds


def _recompute_and_store_unit(db: Session, binds: dict, user_id,
                              ignore_designation: bool = False) -> tuple:
    """Shared Process/Sync core: WARN collectors, attendance worker resolve+stamp,
    per-row open_jugar re-resolve, soft-delete + INSERT...SELECT freeze from the
    day-slice. Returns (stored_rowcount, warnings). Caller owns the transaction +
    header."""
    # The weaver gate — shared by the no_worker WARN and the stamp so the warning
    # can never disagree with what actually gets stamped.
    desig_binds = {**binds, "ignore_designation": 1 if ignore_designation else 0,
                   "desig_prefix": WEAVER_DESIG_PREFIX}
    # WARN collectors.
    warnings = {
        "no_worker": _rows(db.execute(get_weaving_process_no_worker_query(), desig_binds)),
        "no_standard": _rows(db.execute(get_weaving_process_no_standard_query(), binds)),
        "no_picks": _rows(db.execute(get_weaving_process_no_picks_query(), binds)),
    }
    # Branch-scoped run: NULL-branch daily rows are NOT frozen (branch-strict
    # freeze) yet show in live reads — warn so they don't silently vanish once
    # the unit serves the stored log. A co-wide run freezes them (no warning).
    if binds.get("branch_id") is not None:
        null_branch = _rows(db.execute(get_weaving_null_branch_rows_query(), binds))
        if null_branch:
            warnings["null_branch_rows"] = null_branch

    # The unit's active daily rows — one fetch feeding both the eb stamp scope
    # and the open_jugar loop below.
    unit_rows = db.execute(get_weaving_unit_daily_ids_query(), binds).fetchall()
    unit_mcs = {int(r.machine_id) for r in unit_rows if r.machine_id is not None}

    # Worker (eb_id) resolve + stamp — spinning's attendance cascade. Runs BEFORE
    # the freeze so the frozen log inherits eb_id through the day-slice instead of
    # being patched afterwards. Clear first: eb_id is sync-owned, so a worker
    # removed from a loom must not keep a stale stamp on the re-run.
    cand_rows = db.execute(
        get_weaving_operator_candidates_query(),
        {"tran_date": binds["tran_date"], "spell_id": binds["spell_id"],
         "branch_id": binds["branch_id"],
         "ignore_designation": desig_binds["ignore_designation"],
         "desig_prefix": WEAVER_DESIG_PREFIX},
    ).fetchall()
    operator_by_mc, ambiguous = _resolve_operators(cand_rows)
    # Attendance covers every machine in the branch (spinning frames, winders,
    # ...), so scope stamps AND warnings to the unit's own looms — otherwise
    # ignore_designation=1 reports another department's ambiguity as weaving's
    # and fires a no-op UPDATE per foreign machine.
    warnings["operator_ambiguous"] = [a for a in ambiguous
                                      if a["machine_id"] in unit_mcs]
    db.execute(clear_weaving_daily_eb_query(), binds)
    stamps = [(mc_id, operator_by_mc[mc_id])
              for mc_id in sorted(unit_mcs & operator_by_mc.keys())]
    if stamps:
        db.execute(stamp_weaving_daily_eb_bulk_query(len(stamps)),
                   {**binds, **bulk_case_binds("m", "e", stamps)})

    # open_jugar resolve — SET-BASED (TKT-000134): one SELECT resolves every unit
    # row with the same two-probe expression the entry writers use per row, one
    # CASE UPDATE writes back only the rows that changed. The old per-row
    # SELECT+UPDATE pair was ~970 round trips for sls's 485-loom unit — minutes
    # over a WAN, longer than the FE waits, and every re-click then queued on the
    # first run's row locks (1205). A single correlated UPDATE would hit MySQL 1093.
    changed = []
    carried = []
    for r in db.execute(get_weaving_unit_open_jugar_query(), binds).fetchall():
        resolved = float(r.open_jugar or 0)
        if r.stored_value is None or float(r.stored_value) != resolved:
            changed.append((int(r.weaving_daily_id), resolved))
        # Idle loom (cuts + close_jugar = 0): close_jugar := open_jugar, so its
        # production is 0 and the chain carries the reading forward. A previous
        # carry (cuts 0, close == old stored open) is re-carried when open shifts.
        cuts, cj = float(r.cuts or 0), float(r.close_jugar or 0)
        stored = float(r.stored_value or 0)
        if cuts == 0 and (cj == 0 or cj == stored) and cj != resolved:
            carried.append((int(r.weaving_daily_id), resolved))
    if changed:
        db.execute(update_weaving_daily_open_jugar_bulk_query(len(changed)),
                   bulk_case_binds("i", "v", changed))
    if carried:
        db.execute(update_weaving_daily_open_jugar_bulk_query(len(carried), "close_jugar"),
                   bulk_case_binds("i", "v", carried))

    # WARN: negative straight-count total_jugar (by design representable —
    # e.g. mid-spell beam change — so surfaced, never clamped or blocked).
    # Collected AFTER the open_jugar re-resolve so it sees the repaired values.
    warnings["negative_jugar"] = _rows(
        db.execute(get_weaving_process_negative_jugar_query(), binds))

    # Soft-delete prior log rows (idempotent re-run), then freeze: INSERT...SELECT
    # from the day-slice (+ SQC fingerprint). branch_id rides in binds — a
    # branch-scoped run stores only its rows.
    db.execute(soft_delete_weaving_log_for_unit_query(), {**binds, "updated_by": user_id})
    slice_binds = {**binds, "machine_id": None, "updated_by": user_id}
    result = db.execute(insert_weaving_log_from_slice_query(), slice_binds)
    return int(result.rowcount), warnings


@router.post("/process")
def process_weaving(
    body: ProcessRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        co_id, spell_id, binds = _resolve_unit(db, body)
        user_id = token_data.get("user_id")

        # Re-processing a locked unit requires Edit.
        require_edit_if_locked(db, token_data, co_id, body.branch_id, body.tran_date, spell_id)

        # BLOCK: any produced loom without a mapped quality, plus any daily row
        # whose STORED quality is NULL/stale vs the active map (capture-before-map or
        # pre-re-stamp remap) — freezing either would compute zero/wrong production.
        unmapped = _rows(db.execute(get_weaving_unmapped_produced_looms_query(), binds))
        mismatch = _rows(db.execute(get_weaving_process_quality_mismatch_query(), binds))
        if unmapped or mismatch:
            raise HTTPException(status_code=400, detail={
                "message": BLOCK_MSG,
                "unmapped": unmapped,
                "quality_mismatch": [r["mech_code"] for r in mismatch],
            })

        processed, warnings = _recompute_and_store_unit(
            db, binds, user_id, body.ignore_designation)

        # Lock header upsert.
        lock = db.execute(get_weaving_process_lock_row_query(), binds).fetchone()
        if lock:
            db.execute(update_weaving_process_lock_query(),
                       {"id": lock.weaving_process_lock_id, "processed_by": user_id})
        else:
            db.execute(insert_weaving_process_lock_query(),
                       {**binds, "processed_by": user_id})

        db.commit()
        return {"data": {"processed": processed, "warnings": warnings}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


def _sync_unit(db: Session, body: ProcessRequest, token_data: dict) -> dict:
    """One unit's Sync inside the caller's transaction: gate, WARN collectors,
    recompute-and-store, header upsert. Shared by /sync and /sync_range."""
    co_id, spell_id, binds = _resolve_unit(db, body)
    user_id = token_data.get("user_id")
    require_edit_if_locked(db, token_data, co_id, body.branch_id, body.tran_date, spell_id)
    warnings = {
        "unmapped": _rows(db.execute(get_weaving_unmapped_produced_looms_query(), binds)),
        "quality_mismatch": [
            r["mech_code"]
            for r in _rows(db.execute(get_weaving_process_quality_mismatch_query(), binds))
        ],
    }
    synced, core_warnings = _recompute_and_store_unit(
        db, binds, user_id, body.ignore_designation)
    warnings.update(core_warnings)
    header = db.execute(get_weaving_process_lock_row_query(), binds).fetchone()
    if header:
        db.execute(update_weaving_header_synced_query(),
                   {"id": header.weaving_process_lock_id, "synced_by": user_id})
    else:
        db.execute(insert_weaving_sync_header_query(),
                   {**binds, "synced_by": user_id})
    return {"synced": synced, "warnings": warnings}


class SyncRangeRequest(BaseModel):
    co_id: Optional[int] = None
    branch_id: Optional[int] = None
    from_date: date
    to_date: date
    ignore_designation: bool = False


@router.post("/sync_range")
def sync_weaving_range(
    body: SyncRangeRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """OPS endpoint (no FE caller): Sync every (date, spell) unit with entries in
    [from_date, to_date], ONE transaction per unit - a failing unit is reported in
    its result row and the rest continue. Range capped at WEAVING_SYNC_RANGE_MAX_DAYS
    so the synchronous request finishes inside a gateway timeout; because commits
    are per unit a timeout loses no work, re-run the same range.
    Response: {"data": {"units", "synced_units", "results": [{tran_date, spell_id,
    synced, warnings}]}} where warnings are COUNTS per key (/sync returns the lists).
    2026-09-16: replaces the click-per-unit re-sync after chain/standards fixes."""
    try:
        if body.to_date < body.from_date:
            raise HTTPException(status_code=400, detail="to_date must be >= from_date")
        if (body.to_date - body.from_date).days + 1 > WEAVING_SYNC_RANGE_MAX_DAYS:  # inclusive days
            raise HTTPException(status_code=400,
                                detail=f"range exceeds {WEAVING_SYNC_RANGE_MAX_DAYS} days")
        if body.co_id is None and body.branch_id is None:
            raise HTTPException(status_code=400, detail="co_id or branch_id is required")
        co_id = body.co_id if body.co_id is not None else _derive_co_id(db, body.branch_id)
        if co_id is None:
            raise HTTPException(
                status_code=400,
                detail="co_id is required (or a branch_id it can be derived from)")
        units = db.execute(get_weaving_units_in_range_query(), {
            "co_id": int(co_id), "branch_id": _i(body.branch_id),
            "from_date": body.from_date, "to_date": body.to_date,
        }).fetchall()
        results = []
        for u in units:
            unit = {"tran_date": str(u.tran_date), "spell_id": int(u.spell_id)}
            try:
                out = _sync_unit(db, ProcessRequest(
                    co_id=int(co_id), branch_id=body.branch_id, tran_date=u.tran_date,
                    spell_id=int(u.spell_id), ignore_designation=body.ignore_designation,
                ), token_data)
                row = {**unit, "synced": out["synced"],
                       "warnings": {k: len(v) for k, v in out["warnings"].items()}}
                db.commit()
                results.append(row)
            except HTTPException as e:
                db.rollback()
                results.append({**unit, "error": e.detail})
            except Exception as e:  # keep going - one bad unit must not stop the range
                db.rollback()
                results.append({**unit, "error": str(e)})
        return {"data": {"units": len(units),
                         "synced_units": sum(1 for r in results if "error" not in r),
                         "results": results}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync")
def sync_weaving(
    body: ProcessRequest,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Recompute-and-store the unit's derived rows WITHOUT locking (2026-08-21).

    Same freeze as Process, but unmapped/stale-quality looms only WARN — their
    rows store zero production until the quality map is fixed and the unit is
    re-synced. The header keeps its lock state (fresh header: is_locked=0) and
    the stale flag clears. Re-syncing a LOCKED unit overwrites frozen data, so it
    stays behind the same Edit gate as reprocess."""
    try:
        out = _sync_unit(db, body, token_data)
        db.commit()
        return {"data": out}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/process_status")
def process_status(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        co_raw = request.query_params.get("co_id")
        if not co_raw:
            raise HTTPException(status_code=400, detail="co_id is required")
        d = request.query_params.get("tran_date")
        spell_raw = request.query_params.get("spell")
        spell_id_param = _optional_spell_id_param(request)
        if not d or (spell_id_param is None and not spell_raw):
            raise HTTPException(
                status_code=400,
                detail="tran_date and spell_id (or spell) are required",
            )
        branch_raw = request.query_params.get("branch_id")
        branch_id = int(branch_raw) if branch_raw else None
        co_id = int(co_raw)
        d_val = date.fromisoformat(d)
        spell_id = _resolve_spell(db, spell_id_param, spell_raw, branch_id)

        lock = get_process_lock(db, co_id, branch_id, d_val, spell_id)
        if not lock:
            # Nothing stored: not locked, not synced, nothing to be stale.
            return {"data": {"locked": False, "synced": False, "stale": False,
                             "reprocess_needed": False,
                             "synced_at": None, "processed_at": None}}

        # A header (sync-only or locked) means stored log rows serve reads — run
        # the SQC/stoppage drift probe either way and persist the stale flag.
        drift = db.execute(
            get_weaving_drift_query(),
            {"co_id": co_id, "tran_date": d_val, "spell_id": spell_id},
        ).fetchone()
        needed = drift is not None
        if needed and not lock.reprocess_needed:
            db.execute(update_weaving_process_lock_reprocess_query(),
                       {"id": lock.weaving_process_lock_id})
            db.commit()
        stale = bool(needed or lock.reprocess_needed)
        return {"data": {
            "locked": bool(lock.is_locked),
            "synced": True,
            "stale": stale,
            "reprocess_needed": stale,  # legacy key (pre-sync FE reads this)
            "synced_at": str(lock.synced_date_time) if lock.synced_date_time else None,
            "processed_at": str(lock.processed_date_time) if lock.processed_date_time else None,
        }}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tran_date")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

"""Bale Production (Bale Press) entry endpoints — prefix ``/api/baleProd``.

One row per bale weighed at the bale press (docs/bale-production-spec-sheet.md). The
operator sets date + spell + machine once, then per bale picks the quality, the series
(auto-resolved: hessian -> the item's own series, sacking -> the consignee series), takes
the pre-filled next bale number, types the gross weight, keeps the trolley and presses
Enter. Repack is the same form flagged ``entry_type = 'REPACK'`` with the number of the
bale that was opened in ``original_bale_no`` (D2).

Persona: Portal (``Depends(get_tenant_db)`` + ``get_current_user_with_refresh``,
``{"data": ...}`` responses, soft delete via ``active = 0``, NO approval workflow,
trigger-based audit). Conventions mirror waste_entry.py: ``_require_co_id`` /
``_optional_branch_id`` / ``_f`` / ``_i`` helpers and the try/except (GET:
HTTPException raise / ValueError 400 / Exception 500; POST/PUT/DELETE: ``db.rollback()``
then raise / 500) pattern. Spell resolution is IMPORTED from spinning_entry
(``_resolve_spell`` — spell_id preferred, branch-scoped code fallback), never copied.

Entry endpoints (§6, response contract §6.3):
  GET    /entry_setup?co_id&branch_id[&tran_date]  -> {qualities, series, machines, trollies, spells[, entries_today_count]}
  GET    /next_bale_no?co_id&bale_series_id        -> {bale_series_id, series_name, last_bale_no, next_bale_no, last_entry}
  GET    /entries_by_date?co_id&branch_id&tran_date[&spell_id][&entry_type] -> {rows: BaleRow[], totals}
  POST   /entry_create                              -> {row: BaleRow, last_bale_no}   (409 on clash / unknown original)
  PUT    /entry_edit/{id}?co_id                     -> {row: BaleRow, last_bale_no}   (same body; snapshots recomputed)
  DELETE /entry_delete/{id}?co_id                   -> {bale_entry_id}
  GET    /bale_search?co_id&branch_id&bale_no[&bale_series_id] -> {rows: BaleRow[]}

Validation (§6.1, 400 unless stated): entry_type in BALE_ENTRY_TYPES; tran_date not in the
future; BALE_GROSS_MIN <= gross_wt <= BALE_GROSS_MAX; 1 <= bale_no <= BALE_NO_MAX; quality
and series exist in the company (series in the branch); series.quality_type == the
quality's; a series bound to an item must match the quality's item; spell via
_resolve_spell; machine (if given) active and in the branch; trolley (if given) exists;
net_wt = gross − trolly_wt − packsheet_wt > 0 (F0, snapshots stored); PROD rows must send
original_bale_no = null; REPACK rows need original_bale_no >= 1 AND an active bale with
that number in the series (else **409**); a (series, bale_no) clash with ANY active row is
**409** — both 409s are bypassed by ``allow_duplicate = true`` (Q5). On every save
``last_bale_no = max(last_bale_no, bale_no)`` (F9); deleting / renumbering the series' TOP
bale winds it back to max(opening_bale_no, highest remaining active bale_no), where
opening_bale_no is the number last set in the master. After every create / edit / delete
``sync_balepress_daily`` (services/bale_sync.py) refreshes the finishing balepress daily
row for the affected key(s) — an edit that moves a bale syncs the old AND new key (F10).

The same router also carries the Bale Series master (waste-entry precedent — same
module, same prefix, no separate registration): series_setup, series_list,
series_create, series_edit/{id}, series_delete/{id} (400 while active bales reference it).
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.bale_query import (
    bump_bale_series_last_no_query,
    count_bale_series_active_bales_query,
    get_bale_active_by_id_query,
    get_bale_duplicate_query,
    get_bale_entries_count_query,
    get_bale_machine_row_query,
    get_bale_original_query,
    get_bale_parties_query,
    get_bale_qualities_query,
    get_bale_quality_row_query,
    get_bale_rows_query,
    get_bale_series_duplicate_query,
    get_bale_series_last_entry_query,
    get_bale_series_last_no_query,
    get_bale_series_list_query,
    get_bale_series_row_query,
    get_bale_series_setup_query,
    get_bale_states_query,
    get_bale_totals_by_date_query,
    get_bale_trollies_query,
    get_bale_trolly_row_query,
    insert_bale_entry_query,
    insert_bale_series_query,
    rewind_bale_series_last_no_query,
    soft_delete_bale_entry_query,
    soft_delete_bale_series_query,
    update_bale_entry_query,
    update_bale_series_query,
    verify_bale_series_item_query,
)
from src.juteProduction.constants import (
    BACKDATE_DAYS_MAX,
    BALE_ENTRY_TYPE_PROD,
    BALE_ENTRY_TYPE_REPACK,
    BALE_ENTRY_TYPES,
    BALE_GROSS_MAX,
    BALE_GROSS_MIN,
    BALE_NO_MAX,
    BALE_PRESS_PROCESS,
    FINISHING_ITEM_TYPE_ID,
    FINISHING_MACHINE_TYPE_NAMES,
    FINISHING_QUALITY_TYPE_BAG,
    FINISHING_QUALITY_TYPE_CLOTH,
)
from src.juteProduction.finishing_query import (
    get_finishing_items_query,
    get_finishing_machines_query,
    get_finishing_spells_query,
)
from src.juteProduction.services.bale_sync import (
    bale_daily_key,
    same_bale_daily_key,
    sync_balepress_daily,
)
# Shared spell-resolution contract (spell_id preferred, branch-scoped code fallback) —
# import, do NOT copy. See spinning_entry._resolve_spell.
from src.juteProduction.spinning_entry import _resolve_spell


router = APIRouter()

BALE_PRESS_MACHINE_TYPES = list(FINISHING_MACHINE_TYPE_NAMES[BALE_PRESS_PROCESS])
QUALITY_TYPES = (FINISHING_QUALITY_TYPE_CLOTH, FINISHING_QUALITY_TYPE_BAG)
QUALITY_TYPE_LABELS = {FINISHING_QUALITY_TYPE_CLOTH: "Hessian", FINISHING_QUALITY_TYPE_BAG: "Sacking"}

NOT_FOUND_MSG = "Bale entry not found"
SERIES_NOT_FOUND_MSG = "Bale series not found"
ENTRY_TYPE_MSG = "entry_type must be one of " + " / ".join(BALE_ENTRY_TYPES)
FUTURE_DATE_MSG = "tran_date cannot be in the future"
BACKDATE_MSG = f"tran_date cannot be more than {BACKDATE_DAYS_MAX} days in the past"
GROSS_GATE_MSG = f"gross_wt must be between {BALE_GROSS_MIN} and {BALE_GROSS_MAX} kg"
BALE_NO_GATE_MSG = f"bale_no must be between 1 and {BALE_NO_MAX}"
ORIGINAL_NO_GATE_MSG = f"original_bale_no must be between 1 and {BALE_NO_MAX}"
PROD_ORIGINAL_MSG = "original_bale_no must be null on a PROD row"
REPACK_ORIGINAL_MSG = "original_bale_no is required on a REPACK row"
UNKNOWN_QUALITY_MSG = "finishing_quality_id must be an active finishing quality of this company"
UNKNOWN_SERIES_MSG = "bale_series_id must be an active bale series of this company"
SERIES_BRANCH_MSG = "bale_series_id belongs to a different branch than the entry"
TYPE_MISMATCH_MSG = "Series quality type does not match the selected quality (hessian vs sacking)"
SERIES_ITEM_MSG = "This series is bound to a different quality item"
UNKNOWN_MACHINE_MSG = "machine_id must be an active Bale Press machine"
MACHINE_BRANCH_MSG = "machine_id belongs to a different branch than the entry"
UNKNOWN_TROLLY_MSG = "Unknown trolly_id"
NET_WT_MSG = "net_wt must be greater than 0 (gross_wt {gross} − trolly {trolly} − packsheet {packsheet} = {net})"
SERIES_NAME_MSG = "series_name is required (max 60 characters)"
SERIES_TYPE_MSG = "quality_type must be 1 (hessian) or 2 (sacking)"
SERIES_LAST_NO_MSG = f"last_bale_no must be between 0 and {BALE_NO_MAX}"
SERIES_ITEM_TYPE_MSG = (
    f"item_id must be an active item of item type {FINISHING_ITEM_TYPE_ID} (jute cloth) for this company"
)
SERIES_DUP_MSG = "A bale series with this name already exists for the selected branch"
SERIES_REFERENCED_MSG = "This series has {n} active bale(s) and cannot be deleted"
SERIES_TYPE_LOCKED_MSG = "quality_type cannot change while {n} active bale(s) reference this series"


# =============================================================================
# Pydantic models
# =============================================================================


class BaleEntryBody(BaseModel):
    """POST /entry_create and PUT /entry_edit/{id} share this body (§6.3)."""

    co_id: int
    branch_id: int
    tran_date: date
    spell_id: Optional[int] = None  # preferred
    spell: Optional[str] = None  # DEPRECATED spell CODE fallback (branch-scoped) for _resolve_spell
    machine_id: Optional[int] = None  # Bale Press; None for branches without a machine row
    entry_type: str = BALE_ENTRY_TYPE_PROD  # 'PROD' | 'REPACK'
    finishing_quality_id: int
    bale_series_id: int
    bale_no: int
    original_bale_no: Optional[int] = None  # REPACK only
    trolly_id: Optional[int] = None
    gross_wt: float
    remarks: Optional[str] = None
    allow_duplicate: bool = False  # bypasses BOTH 409s (clash + unknown original), Q5


class BaleSeriesBody(BaseModel):
    """POST /series_create and PUT /series_edit/{id} (co_id from the query on edit)."""

    co_id: Optional[int] = None
    branch_id: int
    series_name: str
    quality_type: int  # 1=hessian, 2=sacking
    item_id: Optional[int] = None  # bound quality (hessian); None = any quality of the type
    state_id: Optional[int] = None
    party_id: Optional[int] = None
    last_bale_no: int = 0  # editable = manual reset (D3)


# =============================================================================
# Helpers (mirror waste_entry.py)
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


def _optional_int_param(request: Request, name: str) -> Optional[int]:
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _required_int_param(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _optional_entry_type(request: Request) -> Optional[str]:
    raw = request.query_params.get("entry_type")
    if not raw:
        return None
    val = raw.strip().upper()
    if val not in BALE_ENTRY_TYPES:
        raise HTTPException(status_code=400, detail=ENTRY_TYPE_MSG)
    return val


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _fn(v) -> Optional[float]:
    """Cast to float, keeping None."""
    return None if v is None else float(v)


def _i(v, default: Optional[int] = None) -> Optional[int]:
    return default if v is None else int(v)


_FLOAT_COLS = ("trolly_wt", "packsheet_wt", "gross_wt", "net_wt", "std_bale_wt")


def _serialize_row(row) -> Dict[str, Any]:
    """BaleRow: dict(_mapping) with Decimal -> float, date -> 'YYYY-MM-DD', datetime -> ISO."""
    m = dict(row._mapping)
    for k in _FLOAT_COLS:
        if m.get(k) is not None:
            m[k] = float(m[k])
    for k in ("bale_no", "original_bale_no"):
        if m.get(k) is not None:
            m[k] = int(m[k])
    if m.get("tran_date") is not None:
        m["tran_date"] = str(m["tran_date"])
    wt = m.get("weigh_time")
    if isinstance(wt, datetime):
        m["weigh_time"] = wt.isoformat(sep="T", timespec="seconds")
    elif wt is not None:
        m["weigh_time"] = str(wt)
    return m


def _fetch_bale_row(db: Session, co_id: int, bale_entry_id: int) -> Optional[Dict[str, Any]]:
    """The BaleRow echo for one id (create / edit response)."""
    row = db.execute(
        get_bale_rows_query(),
        {
            "co_id": int(co_id),
            "bale_entry_id": int(bale_entry_id),
            "branch_id": None,
            "tran_date": None,
            "spell_id": None,
            "entry_type": None,
            "bale_series_id": None,
            "bale_no": None,
        },
    ).fetchone()
    return _serialize_row(row) if row else None


def _validate_gross(gross_wt) -> float:
    try:
        g = float(gross_wt)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=GROSS_GATE_MSG)
    if g < BALE_GROSS_MIN or g > BALE_GROSS_MAX:
        raise HTTPException(status_code=400, detail=GROSS_GATE_MSG)
    return g


def _validate_bale_no(value, msg: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=msg)
    if n < 1 or n > BALE_NO_MAX:
        raise HTTPException(status_code=400, detail=msg)
    return n


def _fetch_quality(db: Session, co_id: int, finishing_quality_id: int):
    row = db.execute(
        get_bale_quality_row_query(),
        {"finishing_quality_id": int(finishing_quality_id), "co_id": int(co_id)},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=UNKNOWN_QUALITY_MSG)
    return row


def _fetch_series(db: Session, co_id: int, bale_series_id: int, status_code: int = 400):
    row = db.execute(
        get_bale_series_row_query(),
        {"bale_series_id": int(bale_series_id), "co_id": int(co_id)},
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=status_code,
            detail=UNKNOWN_SERIES_MSG if status_code == 400 else SERIES_NOT_FOUND_MSG,
        )
    return row


def _validate_machine(db: Session, machine_id: int, branch_id: int) -> int:
    """Active Bale Press machine in the entry's branch (400 otherwise).

    Type check is case-insensitive (tenant vocabularies differ: 'Bale Press' vs
    'BALE PRESS'); a machine whose type is not a Bale Press type is refused.
    """
    row = db.execute(get_bale_machine_row_query(), {"machine_id": int(machine_id)}).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=UNKNOWN_MACHINE_MSG)
    type_name = (getattr(row, "machine_type_name", None) or "").strip().lower()
    allowed = {n.strip().lower() for n in BALE_PRESS_MACHINE_TYPES}
    if type_name and type_name not in allowed:
        raise HTTPException(status_code=400, detail=UNKNOWN_MACHINE_MSG)
    if row.branch_id is not None and int(row.branch_id) != int(branch_id):
        raise HTTPException(status_code=400, detail=MACHINE_BRANCH_MSG)
    return int(machine_id)


def _fetch_trolly(db: Session, trolly_id: int):
    row = db.execute(get_bale_trolly_row_query(), {"trolly_id": int(trolly_id)}).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=UNKNOWN_TROLLY_MSG)
    return row


def _prepare_entry(
    db: Session,
    body: BaleEntryBody,
    co_id: int,
    user_id,
    exclude_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Run every §6.1 gate and build the INSERT / UPDATE bind dict.

    Cheap in-memory gates first (entry_type, date, gross, numbers, PROD/REPACK shape),
    then the DB-backed ones in a fixed order — quality, series, spell, machine, trolley
    — then the F0 net computation, then the two 409 probes (original, clash), both
    skipped when ``allow_duplicate``. ``exclude_id`` is the row being edited so it never
    collides with (or vouches for) itself. Returns the binds plus ``series`` (for the F9
    bump) under the ``_series`` key.
    """
    entry_type = (body.entry_type or BALE_ENTRY_TYPE_PROD).strip().upper()
    if entry_type not in BALE_ENTRY_TYPES:
        raise HTTPException(status_code=400, detail=ENTRY_TYPE_MSG)
    if body.tran_date > date.today():
        raise HTTPException(status_code=400, detail=FUTURE_DATE_MSG)
    if body.tran_date < date.today() - timedelta(days=BACKDATE_DAYS_MAX):
        raise HTTPException(status_code=400, detail=BACKDATE_MSG)  # spec §7d
    gross_wt = _validate_gross(body.gross_wt)
    bale_no = _validate_bale_no(body.bale_no, BALE_NO_GATE_MSG)

    original_bale_no: Optional[int] = None
    if entry_type == BALE_ENTRY_TYPE_PROD:
        if body.original_bale_no is not None:
            raise HTTPException(status_code=400, detail=PROD_ORIGINAL_MSG)
    else:
        if body.original_bale_no is None:
            raise HTTPException(status_code=400, detail=REPACK_ORIGINAL_MSG)
        original_bale_no = _validate_bale_no(body.original_bale_no, ORIGINAL_NO_GATE_MSG)

    quality = _fetch_quality(db, co_id, body.finishing_quality_id)
    series = _fetch_series(db, co_id, body.bale_series_id)
    if int(series.branch_id) != int(body.branch_id):
        raise HTTPException(status_code=400, detail=SERIES_BRANCH_MSG)
    if int(series.quality_type) != int(quality.quality_type):
        raise HTTPException(status_code=400, detail=TYPE_MISMATCH_MSG)
    if series.item_id is not None and int(series.item_id) != int(quality.item_id):
        raise HTTPException(status_code=400, detail=SERIES_ITEM_MSG)

    spell_id = _resolve_spell(db, body.spell_id, body.spell, body.branch_id)

    machine_id = None
    if body.machine_id is not None:
        machine_id = _validate_machine(db, body.machine_id, body.branch_id)

    trolly_id = None
    trolly_wt = 0.0
    if body.trolly_id is not None:
        trolly = _fetch_trolly(db, body.trolly_id)
        trolly_id = int(body.trolly_id)
        trolly_wt = _f(trolly.trolly_weight)

    packsheet_wt = _f(quality.packsheet_wt)
    std_bale_wt = _fn(quality.std_bale_weight)
    net_wt = round(gross_wt - trolly_wt - packsheet_wt, 3)  # F0
    if net_wt <= 0:
        raise HTTPException(
            status_code=400,
            detail=NET_WT_MSG.format(
                gross=gross_wt, trolly=trolly_wt, packsheet=packsheet_wt, net=net_wt
            ),
        )

    if not body.allow_duplicate:
        if entry_type == BALE_ENTRY_TYPE_REPACK:
            original = db.execute(
                get_bale_original_query(),
                {
                    "bale_series_id": int(series.bale_series_id),
                    "original_bale_no": int(original_bale_no),
                    "exclude_id": _i(exclude_id),
                },
            ).fetchone()
            if not original:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"No bale {original_bale_no} found in series "
                        f"{series.series_name} to repack"
                    ),
                )
        clash = db.execute(
            get_bale_duplicate_query(),
            {
                "bale_series_id": int(series.bale_series_id),
                "bale_no": bale_no,
                "exclude_id": _i(exclude_id),
            },
        ).fetchone()
        if clash:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Bale no {bale_no} already exists in series {series.series_name} "
                    f"(entry #{clash.bale_entry_id}, {clash.entry_type} on {clash.tran_date}"
                    f"{', spell ' + str(clash.spell_code) if getattr(clash, 'spell_code', None) else ''}, "
                    f"gross {_f(clash.gross_wt):g} kg) — confirm to save anyway"
                ),
            )

    return {
        "co_id": int(co_id),
        "branch_id": int(body.branch_id),
        "tran_date": body.tran_date,
        "spell_id": int(spell_id),
        "machine_id": machine_id,
        "entry_type": entry_type,
        "finishing_quality_id": int(quality.finishing_quality_id),
        "bale_series_id": int(series.bale_series_id),
        "bale_no": bale_no,
        "original_bale_no": original_bale_no,
        "trolly_id": trolly_id,
        "trolly_wt": trolly_wt,
        "packsheet_wt": packsheet_wt,
        "gross_wt": gross_wt,
        "net_wt": net_wt,
        "std_bale_wt": std_bale_wt,
        "remarks": (body.remarks.strip() or None) if isinstance(body.remarks, str) else None,
        "updated_by": user_id,
        "_series": series,
    }


def _bump_series(db: Session, series, bale_no: int, co_id: int, user_id) -> int:
    """F9: last_bale_no = max(last_bale_no, bale_no); returns the new counter value."""
    db.execute(
        bump_bale_series_last_no_query(),
        {
            "bale_series_id": int(series.bale_series_id),
            "co_id": int(co_id),
            "bale_no": int(bale_no),
            "updated_by": user_id,
        },
    )
    return max(int(series.last_bale_no or 0), int(bale_no))


def _rewind_series(db: Session, co_id: int, bale_series_id: int, bale_no: int, user_id) -> Optional[int]:
    """A bale left number ``bale_no`` (deleted / renumbered): wind the counter back if it
    was the top, floored at opening_bale_no. Returns the series' counter afterwards."""
    binds = {"bale_series_id": int(bale_series_id), "co_id": int(co_id)}
    db.execute(
        rewind_bale_series_last_no_query(),
        dict(binds, bale_no=int(bale_no), updated_by=user_id),
    )
    row = db.execute(get_bale_series_last_no_query(), binds).fetchone()
    return int(row.last_bale_no) if row else None


def _key_from_binds(binds: Dict[str, Any]) -> Dict[str, Any]:
    return bale_daily_key(
        binds["co_id"], binds["branch_id"], binds["tran_date"], binds["spell_id"],
        binds["machine_id"], binds["finishing_quality_id"],
    )


def _key_from_existing(existing) -> Dict[str, Any]:
    return bale_daily_key(
        existing.co_id, existing.branch_id, existing.tran_date, existing.spell_id,
        existing.machine_id, existing.finishing_quality_id,
    )


# =============================================================================
# Endpoints — setup + reads
# =============================================================================


@router.get("/entry_setup")
def entry_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Dropdown payload for the entry page (§6.3): qualities (item + standards + weft
    count), series, Bale Press machines (branch-scoped), trollies (Bale Press-tagged
    first, then untagged), spells — plus ``entries_today_count`` when tran_date is given."""
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    try:
        qualities: List[Dict[str, Any]] = []
        for r in db.execute(
            get_bale_qualities_query(), {"co_id": co_id, "branch_id": branch_id}
        ).fetchall():
            m = dict(r._mapping)
            qualities.append(
                {
                    "finishing_quality_id": int(m["finishing_quality_id"]),
                    "item_id": _i(m.get("item_id")),
                    "item_code": m.get("item_code"),
                    "item_name": m.get("item_name"),
                    "quality_type": _i(m.get("quality_type")),
                    "std_bale_weight": _fn(m.get("std_bale_weight")),
                    "packsheet_wt": _fn(m.get("packsheet_wt")),
                    "std_mr_pct": _fn(m.get("std_mr_pct")),
                    "weft_yarn_item_id": _i(m.get("weft_yarn_item_id")),
                    "weft_count": _fn(m.get("weft_count")),
                }
            )

        series: List[Dict[str, Any]] = []
        for r in db.execute(
            get_bale_series_setup_query(), {"co_id": co_id, "branch_id": branch_id}
        ).fetchall():
            m = dict(r._mapping)
            series.append(
                {
                    "bale_series_id": int(m["bale_series_id"]),
                    "series_name": m["series_name"],
                    "quality_type": _i(m.get("quality_type")),
                    "item_id": _i(m.get("item_id")),
                    "state_id": _i(m.get("state_id")),
                    "party_id": _i(m.get("party_id")),
                    "last_bale_no": _i(m.get("last_bale_no"), 0),
                }
            )

        machines = [
            {
                "machine_id": int(r._mapping["machine_id"]),
                "machine_name": r._mapping.get("machine_name"),
                "mech_code": r._mapping.get("mech_code"),
            }
            for r in db.execute(
                get_finishing_machines_query(),
                {"machine_types": BALE_PRESS_MACHINE_TYPES, "branch_id": branch_id},
            ).fetchall()
        ]

        trollies = [
            {
                "trolly_id": int(r._mapping["trolly_id"]),
                "trolly_name": r._mapping.get("trolly_name"),
                "trolly_weight": _f(r._mapping.get("trolly_weight")),
            }
            for r in db.execute(
                get_bale_trollies_query(),
                {"machine_types": BALE_PRESS_MACHINE_TYPES, "branch_id": branch_id},
            ).fetchall()
        ]

        spells: List[Dict[str, Any]] = []
        seen = set()
        for r in db.execute(get_finishing_spells_query(), {"branch_id": branch_id}).fetchall():
            m = dict(r._mapping)
            code = m["spell_code"]
            if code in seen:
                continue
            seen.add(code)
            spells.append(
                {
                    "spell_id": int(m["spell_id"]),
                    "spell_code": code,
                    "spell_name": m.get("spell_name"),
                    "working_hours": _f(m.get("working_hours")),
                }
            )

        data: Dict[str, Any] = {
            "qualities": qualities,
            "series": series,
            "machines": machines,
            "trollies": trollies,
            "spells": spells,
        }
        d = request.query_params.get("tran_date")
        if d:
            d_val = date.fromisoformat(d)
            cnt = db.execute(
                get_bale_entries_count_query(),
                {"co_id": co_id, "branch_id": branch_id, "tran_date": d_val},
            ).fetchone()
            data["entries_today_count"] = int(cnt.cnt or 0) if cnt else 0
        return {"data": data}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tran_date format")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/next_bale_no")
def next_bale_no(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """F9 pre-fill: ``next_bale_no = last_bale_no + 1`` plus the series' last entry."""
    co_id = _require_co_id(request)
    bale_series_id = _required_int_param(request, "bale_series_id")
    try:
        series = _fetch_series(db, co_id, bale_series_id, status_code=404)
        last = db.execute(
            get_bale_series_last_entry_query(),
            {"bale_series_id": int(series.bale_series_id), "co_id": co_id},
        ).fetchone()
        last_entry = None
        if last:
            last_entry = {
                "bale_entry_id": int(last.bale_entry_id),
                "tran_date": str(last.tran_date) if last.tran_date is not None else None,
                "bale_no": _i(last.bale_no),
                "gross_wt": _fn(last.gross_wt),
                "net_wt": _fn(last.net_wt),
                "trolly_id": _i(last.trolly_id),
                "finishing_quality_id": _i(last.finishing_quality_id),
            }
        last_no = int(series.last_bale_no or 0)
        return {
            "data": {
                "bale_series_id": int(series.bale_series_id),
                "series_name": series.series_name,
                "last_bale_no": last_no,
                "next_bale_no": last_no + 1,
                "last_entry": last_entry,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entries_by_date")
def entries_by_date(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Day grid: BaleRow[] for the date (optional spell_id / entry_type) + per-quality
    totals split by entry_type (PROD and REPACK counted separately, Q4)."""
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    d = request.query_params.get("tran_date")
    if not d:
        raise HTTPException(status_code=400, detail="tran_date is required")
    spell_id = _optional_int_param(request, "spell_id")
    entry_type = _optional_entry_type(request)
    try:
        d_val = date.fromisoformat(d)
        rows = [
            _serialize_row(r)
            for r in db.execute(
                get_bale_rows_query(),
                {
                    "co_id": co_id,
                    "bale_entry_id": None,
                    "branch_id": branch_id,
                    "tran_date": d_val,
                    "spell_id": spell_id,
                    "entry_type": entry_type,
                    "bale_series_id": None,
                    "bale_no": None,
                },
            ).fetchall()
        ]
        totals = []
        for r in db.execute(
            get_bale_totals_by_date_query(),
            {
                "co_id": co_id,
                "branch_id": branch_id,
                "tran_date": d_val,
                "spell_id": spell_id,
                "entry_type": entry_type,
            },
        ).fetchall():
            m = dict(r._mapping)
            totals.append(
                {
                    "finishing_quality_id": _i(m.get("finishing_quality_id")),
                    "item_name": m.get("item_name"),
                    "quality_type": _i(m.get("quality_type")),
                    "entry_type": m.get("entry_type"),
                    "bales": _i(m.get("bales"), 0),
                    "total_gross": round(_f(m.get("total_gross")), 3),
                    "total_net": round(_f(m.get("total_net")), 3),
                    "avg_net": round(_f(m.get("avg_net")), 3),
                    "std_bale_wt": _fn(m.get("std_bale_wt")),
                }
            )
        return {"data": {"rows": rows, "totals": totals}}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tran_date format")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bale_search")
def bale_search(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """History of a bale number: every active row whose bale_no OR original_bale_no
    equals it (PROD + REPACK), optionally narrowed to one series / branch."""
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    bale_no = _required_int_param(request, "bale_no")
    bale_series_id = _optional_int_param(request, "bale_series_id")
    try:
        rows = [
            _serialize_row(r)
            for r in db.execute(
                get_bale_rows_query(),
                {
                    "co_id": co_id,
                    "bale_entry_id": None,
                    "branch_id": branch_id,
                    "tran_date": None,
                    "spell_id": None,
                    "entry_type": None,
                    "bale_series_id": bale_series_id,
                    "bale_no": bale_no,
                },
            ).fetchall()
        ]
        return {"data": {"rows": rows}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — entry CRUD
# =============================================================================


@router.post("/entry_create")
def entry_create(
    body: BaleEntryBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Insert one bale (§6.1 gates), bump the series counter (F9), re-sync the
    finishing balepress daily row (F10), and echo the BaleRow + new last_bale_no."""
    try:
        user_id = token_data.get("user_id")
        binds = _prepare_entry(db, body, body.co_id, user_id)
        series = binds.pop("_series")

        result = db.execute(insert_bale_entry_query(), binds)
        bale_entry_id = int(result.lastrowid)
        last_bale_no = _bump_series(db, series, binds["bale_no"], body.co_id, user_id)
        sync_balepress_daily(db, user_id=user_id, **_key_from_binds(binds))

        row = _fetch_bale_row(db, body.co_id, bale_entry_id)
        db.commit()
        return {"data": {"row": row, "last_bale_no": last_bale_no}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/entry_edit/{bale_entry_id}")
def entry_edit(
    bale_entry_id: int,
    body: BaleEntryBody,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Replace one bale's fields (same body as create; snapshots recomputed).

    Requires co_id: the row is located by (bale_entry_id, co_id), so another company's
    row reads as 404. The clash / original probes exclude the row itself. After the
    UPDATE the series counter is bumped (F9) and the daily figure re-synced for the NEW
    key and — when the key changed — the OLD key too (F10).
    """
    try:
        co_id = _require_co_id(request)
        existing = db.execute(
            get_bale_active_by_id_query(), {"id": bale_entry_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        user_id = token_data.get("user_id")
        binds = _prepare_entry(db, body, co_id, user_id, exclude_id=bale_entry_id)
        series = binds.pop("_series")

        db.execute(update_bale_entry_query(), dict(binds, id=bale_entry_id))
        last_bale_no = _bump_series(db, series, binds["bale_no"], co_id, user_id)
        # Renumbered / moved: the OLD number may have been its series' top (e.g. a
        # mistyped 17900 corrected to 17795) — rewind after the bump so it sees both.
        old_series_id = int(existing.bale_series_id)
        if (old_series_id, int(existing.bale_no)) != (int(series.bale_series_id), binds["bale_no"]):
            rewound = _rewind_series(db, co_id, old_series_id, existing.bale_no, user_id)
            if old_series_id == int(series.bale_series_id) and rewound is not None:
                last_bale_no = rewound

        new_key = _key_from_binds(binds)
        old_key = _key_from_existing(existing)
        sync_balepress_daily(db, user_id=user_id, **new_key)
        if not same_bale_daily_key(old_key, new_key):
            sync_balepress_daily(db, user_id=user_id, **old_key)

        row = _fetch_bale_row(db, co_id, bale_entry_id)
        db.commit()
        return {"data": {"row": row, "last_bale_no": last_bale_no}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/entry_delete/{bale_entry_id}")
def entry_delete(
    bale_entry_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete (active = 0), co-scoped in the read guard AND the UPDATE, wind the
    series counter back when this was its top bale (floored at opening_bale_no), then
    re-sync the daily figure for the row's key (F10). Echoes the series' new counter."""
    try:
        co_id = _require_co_id(request)
        existing = db.execute(
            get_bale_active_by_id_query(), {"id": bale_entry_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        user_id = token_data.get("user_id")
        db.execute(
            soft_delete_bale_entry_query(),
            {"id": bale_entry_id, "co_id": co_id, "updated_by": user_id},
        )
        bale_series_id = int(existing.bale_series_id)
        last_bale_no = _rewind_series(db, co_id, bale_series_id, existing.bale_no, user_id)
        sync_balepress_daily(db, user_id=user_id, **_key_from_existing(existing))
        db.commit()
        return {
            "data": {
                "bale_entry_id": bale_entry_id,
                "bale_series_id": bale_series_id,
                "last_bale_no": last_bale_no,
            }
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Bale Series master (rides on the same router — waste-entry precedent)
# =============================================================================


def _clean_series_name(name: str) -> str:
    val = (name or "").strip()
    if not val or len(val) > 60:
        raise HTTPException(status_code=400, detail=SERIES_NAME_MSG)
    return val


def _validate_series_body(db: Session, body: BaleSeriesBody, co_id: int, exclude_id: Optional[int]):
    """Shared create / edit gates; returns the cleaned bind dict (minus ids / audit)."""
    series_name = _clean_series_name(body.series_name)
    quality_type = int(body.quality_type)
    if quality_type not in QUALITY_TYPES:
        raise HTTPException(status_code=400, detail=SERIES_TYPE_MSG)
    try:
        last_bale_no = int(body.last_bale_no)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=SERIES_LAST_NO_MSG)
    if last_bale_no < 0 or last_bale_no > BALE_NO_MAX:
        raise HTTPException(status_code=400, detail=SERIES_LAST_NO_MSG)

    item_id = None
    if body.item_id is not None:
        ok = db.execute(
            verify_bale_series_item_query(),
            {"item_id": int(body.item_id), "co_id": int(co_id), "item_type_id": FINISHING_ITEM_TYPE_ID},
        ).fetchone()
        if not ok:
            raise HTTPException(status_code=400, detail=SERIES_ITEM_TYPE_MSG)
        item_id = int(body.item_id)

    dup = db.execute(
        get_bale_series_duplicate_query(),
        {
            "co_id": int(co_id),
            "branch_id": int(body.branch_id),
            "series_name": series_name,
            "exclude_id": _i(exclude_id),
        },
    ).fetchone()
    if dup:
        raise HTTPException(status_code=400, detail=SERIES_DUP_MSG)

    return {
        "branch_id": int(body.branch_id),
        "series_name": series_name,
        "quality_type": quality_type,
        "item_id": item_id,
        "state_id": _i(body.state_id),
        "party_id": _i(body.party_id),
        "last_bale_no": last_bale_no,
    }


def _count_series_bales(db: Session, bale_series_id: int) -> int:
    row = db.execute(
        count_bale_series_active_bales_query(), {"bale_series_id": int(bale_series_id)}
    ).fetchone()
    return int(row.cnt or 0) if row else 0


@router.get("/series_setup")
def series_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Series master form lookups (§6.3): items (type 5), states, parties (customers)."""
    co_id = _require_co_id(request)
    try:
        items = [
            {
                "item_id": int(r._mapping["item_id"]),
                "item_code": r._mapping.get("item_code"),
                "item_name": r._mapping.get("item_name"),
            }
            for r in db.execute(
                get_finishing_items_query(),
                {"co_id": co_id, "item_grp_id": None, "item_type_id": FINISHING_ITEM_TYPE_ID},
            ).fetchall()
        ]
        states = [
            {"state_id": int(r._mapping["state_id"]), "state": r._mapping.get("state")}
            for r in db.execute(get_bale_states_query()).fetchall()
        ]
        parties = [
            {"party_id": int(r._mapping["party_id"]), "supp_name": r._mapping.get("supp_name")}
            for r in db.execute(get_bale_parties_query(), {"co_id": co_id}).fetchall()
        ]
        return {"data": {"items": items, "states": states, "parties": parties}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/series_list")
def series_list(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Master grid rows (§6.3) with joined labels and the active-bale count."""
    co_id = _require_co_id(request)
    branch_id = _optional_branch_id(request)
    try:
        rows = []
        for r in db.execute(
            get_bale_series_list_query(), {"co_id": co_id, "branch_id": branch_id}
        ).fetchall():
            m = dict(r._mapping)
            m["last_bale_no"] = _i(m.get("last_bale_no"), 0)
            m["bales_total"] = _i(m.get("bales_total"), 0)
            rows.append(m)
        return {"data": {"rows": rows}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/series_create")
def series_create(
    body: BaleSeriesBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Create one series. Name unique within (co, branch) among active rows; a bound
    item must be an active type-5 item of the company."""
    try:
        if body.co_id is None:
            raise HTTPException(status_code=400, detail="co_id is required")
        co_id = int(body.co_id)
        binds = _validate_series_body(db, body, co_id, None)
        result = db.execute(
            insert_bale_series_query(),
            dict(binds, co_id=co_id, updated_by=token_data.get("user_id")),
        )
        db.commit()
        return {"data": {"bale_series_id": result.lastrowid}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/series_edit/{bale_series_id}")
def series_edit(
    bale_series_id: int,
    body: BaleSeriesBody,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Replace every field — editing last_bale_no IS the manual reset (D3).

    Requires co_id (row located by id + co_id -> 404 for another company's series).
    quality_type is locked while active bales reference the series: flipping it would
    orphan those rows' type checks.
    """
    try:
        co_id = _require_co_id(request)
        existing = _fetch_series(db, co_id, bale_series_id, status_code=404)
        binds = _validate_series_body(db, body, co_id, bale_series_id)
        if int(binds["quality_type"]) != int(existing.quality_type):
            n = _count_series_bales(db, bale_series_id)
            if n > 0:
                raise HTTPException(status_code=400, detail=SERIES_TYPE_LOCKED_MSG.format(n=n))
        db.execute(
            update_bale_series_query(),
            dict(
                binds,
                bale_series_id=bale_series_id,
                co_id=co_id,
                updated_by=token_data.get("user_id"),
            ),
        )
        db.commit()
        return {"data": {"bale_series_id": bale_series_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/series_delete/{bale_series_id}")
def series_delete(
    bale_series_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete a series (co-scoped); 400 while active bales reference it."""
    try:
        co_id = _require_co_id(request)
        _fetch_series(db, co_id, bale_series_id, status_code=404)
        n = _count_series_bales(db, bale_series_id)
        if n > 0:
            raise HTTPException(status_code=400, detail=SERIES_REFERENCED_MSG.format(n=n))
        db.execute(
            soft_delete_bale_series_query(),
            {
                "bale_series_id": bale_series_id,
                "co_id": co_id,
                "updated_by": token_data.get("user_id"),
            },
        )
        db.commit()
        return {"data": {"bale_series_id": bale_series_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

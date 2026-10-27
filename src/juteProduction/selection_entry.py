"""Selection Trolley Weight entry endpoints — prefix ``/api/selectionProd``.

One row per loaded trolley weighed at the end of the manual jute-selection step
(../vowerp3ui/docs/claude/modules/jute-production/selection-trolley-weight-spec.md).
The operator sets date [+ spell] once, then per trolley picks the jute quality, an
optional gate entry, the trolley, types the gross scale reading and presses Enter. The
server snapshots the trolley tare and stores ``net_wt = gross_wt − trolly_wt`` (D2).

Persona: Portal (``Depends(get_tenant_db)`` + ``get_current_user_with_refresh``,
``{"data": ...}`` responses, soft delete via ``active = 0``, NO approval workflow (D6),
trigger-based audit). Conventions mirror bale_entry.py: ``_require_co_id`` /
``_required_int_param`` / ``_f`` / ``_i`` helpers and the try/except (GET:
HTTPException raise / ValueError 400 / Exception 500; POST/PUT/DELETE: ``db.rollback()``
then raise / 500) pattern. Spell resolution is IMPORTED from spinning_entry
(``_resolve_spell``), never copied.

Endpoints (§3.1):
  GET    /entry_setup?co_id&branch_id[&tran_date]   -> {qualities, gate_entries, trollies, spells, quality_window}
  GET    /entries_by_date?co_id&branch_id&tran_date[&spell_id] -> {rows: SelectionRow[], totals, by_quality}
  POST   /entry_create                               -> {row: SelectionRow}
  PUT    /entry_edit/{id}?co_id                      -> {row: SelectionRow}   (same body)
  DELETE /entry_delete/{id}?co_id                    -> {selection_wt_id}

Validation (§3.2, 400 unless stated): tran_date not in the future; branch belongs to
co_id; item_id is one of the D4 window qualities of that branch/date; trolly_id exists,
is type 'T', belongs to the branch and is tagged 'Selection' (D1);
SELECTION_GROSS_MIN <= gross_wt <= SELECTION_GROSS_MAX; spell_id (when given) via
_resolve_spell; jute_mr_id (when given) is one of the D7 window gate entries carrying
the chosen item; net_wt = gross − tare > 0. Tare snapshot rule: create reads
trolly_mst.trolly_weight; edit KEEPS the stored snapshot while the trolley is
unchanged and re-reads the master only when it changes. Edit/delete: the row must be
active and belong to co_id (404 otherwise).
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProduction.constants import (
    SELECTION_GROSS_MAX,
    SELECTION_GROSS_MIN,
    SELECTION_MACHINE_TYPE_NAME,
    SELECTION_QUALITY_WINDOW_DAYS,
)
from src.juteProduction.gain_loss_query import verify_branch_co_query
from src.juteProduction.selection_query import (
    get_selection_active_by_id_query,
    get_selection_by_quality_query,
    get_selection_gate_entries_query,
    get_selection_qualities_query,
    get_selection_rows_query,
    get_selection_trolly_row_query,
    insert_selection_entry_query,
    soft_delete_selection_entry_query,
    update_selection_entry_query,
    verify_selection_gate_entry_query,
    verify_selection_quality_query,
)
# Shared spell-resolution contract (spell_id preferred, branch-scoped) — import, do NOT
# copy. See spinning_entry._resolve_spell.
from src.juteProduction.spinning_entry import _resolve_spell
from src.juteProduction.spinning_query import get_spells_query, get_trollies_query


router = APIRouter()

NOT_FOUND_MSG = "Selection entry not found"
BAD_BRANCH_MSG = "branch_id must be an active branch of this company"
FUTURE_DATE_MSG = "tran_date cannot be in the future"
GROSS_GATE_MSG = f"gross_wt must be between {SELECTION_GROSS_MIN} and {SELECTION_GROSS_MAX} kg"
QUALITY_WINDOW_MSG = (
    f"Quality was not issued to this branch in the last {SELECTION_QUALITY_WINDOW_DAYS} days"
)
UNKNOWN_TROLLY_MSG = "Unknown trolly_id"
TROLLY_NOT_TAGGED_MSG = "Trolley is not tagged to Selection in Trolly Master"
GATE_ENTRY_MSG = (
    f"Gate entry has no issue of this quality in the last {SELECTION_QUALITY_WINDOW_DAYS} days"
)
NET_WT_MSG = "net_wt must be greater than 0 (gross_wt {gross} − trolley {trolly} = {net})"

# trolly_mst.trolly_type value of a weighing trolley (D1).
TROLLY_TYPE_TROLLEY = "T"


# =============================================================================
# Pydantic models
# =============================================================================


class SelectionEntryBody(BaseModel):
    """POST /entry_create and PUT /entry_edit/{id} share this body (§3.1)."""

    co_id: int
    branch_id: int
    tran_date: date
    spell_id: Optional[int] = None  # D5, optional
    item_id: int  # jute quality = raw-jute item_mst row (D3)
    trolly_id: int
    jute_mr_id: Optional[int] = None  # D7, optional gate entry
    gross_wt: float
    remarks: Optional[str] = None


# =============================================================================
# Helpers (mirror bale_entry.py)
# =============================================================================


def _require_co_id(request: Request) -> int:
    raw = request.query_params.get("co_id")
    if not raw:
        raise HTTPException(status_code=400, detail="co_id is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid co_id")


def _required_int_param(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _optional_int_param(request: Request, name: str) -> Optional[int]:
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}")


def _required_date_param(request: Request, name: str) -> date:
    raw = request.query_params.get(name)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name} format")


def _f(v, default: float = 0.0) -> float:
    """Cast a possibly-None / Decimal value to float."""
    return default if v is None else float(v)


def _fn(v) -> Optional[float]:
    """Cast to float, keeping None."""
    return None if v is None else float(v)


def _i(v, default: Optional[int] = None) -> Optional[int]:
    return default if v is None else int(v)


def _d(v) -> Optional[str]:
    """date / datetime / str -> 'YYYY-MM-DD' (None kept)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def window_bounds(tran_date: date, days: int = SELECTION_QUALITY_WINDOW_DAYS) -> Dict[str, Any]:
    """The D4 window as inclusive calendar bounds for the FE:
    ``issue_date > tran_date − days AND issue_date <= tran_date`` ⇔
    ``tran_date − (days − 1) <= issue_date <= tran_date``."""
    return {
        "date_from": (tran_date - timedelta(days=days - 1)).isoformat(),
        "date_to": tran_date.isoformat(),
        "days": days,
    }


_FLOAT_COLS = ("trolly_wt", "gross_wt", "net_wt")
_INT_COLS = ("selection_wt_id", "co_id", "branch_id", "spell_id", "item_id", "trolly_id",
             "jute_mr_id", "gate_entry_no")


def _serialize_row(row) -> Dict[str, Any]:
    """SelectionRow (§3.1): dict(_mapping) with Decimal -> float, dates -> 'YYYY-MM-DD',
    weigh_time -> 'YYYY-MM-DD HH:MM:SS'."""
    m = dict(row._mapping)
    for k in _FLOAT_COLS:
        if m.get(k) is not None:
            m[k] = float(m[k])
    for k in _INT_COLS:
        if m.get(k) is not None:
            m[k] = int(m[k])
    for k in ("tran_date", "gate_entry_date"):
        if m.get(k) is not None:
            m[k] = _d(m[k])
    wt = m.get("weigh_time")
    if isinstance(wt, datetime):
        m["weigh_time"] = wt.isoformat(sep=" ", timespec="seconds")
    elif wt is not None:
        m["weigh_time"] = str(wt)
    return m


def _fetch_row(db: Session, co_id: int, selection_wt_id: int) -> Optional[Dict[str, Any]]:
    """The SelectionRow echo for one id (create / edit response)."""
    row = db.execute(
        get_selection_rows_query(),
        {
            "co_id": int(co_id),
            "selection_wt_id": int(selection_wt_id),
            "branch_id": None,
            "tran_date": None,
            "spell_id": None,
        },
    ).fetchone()
    return _serialize_row(row) if row else None


def _validate_gross(gross_wt) -> float:
    try:
        g = float(gross_wt)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=GROSS_GATE_MSG)
    if g < SELECTION_GROSS_MIN or g > SELECTION_GROSS_MAX:
        raise HTTPException(status_code=400, detail=GROSS_GATE_MSG)
    return g


def _verify_branch(db: Session, co_id: int, branch_id: int) -> None:
    ok = db.execute(
        verify_branch_co_query(), {"co_id": int(co_id), "branch_id": int(branch_id)}
    ).fetchone()
    if not ok:
        raise HTTPException(status_code=400, detail=BAD_BRANCH_MSG)


def _window_binds(branch_id: int, tran_date: date) -> Dict[str, Any]:
    return {
        "branch_id": int(branch_id),
        "tran_date": tran_date,
        "days": SELECTION_QUALITY_WINDOW_DAYS,
    }


def _verify_quality(db: Session, branch_id: int, tran_date: date, item_id: int) -> int:
    """D4 gate: the quality was issued to the branch in the window."""
    ok = db.execute(
        verify_selection_quality_query(),
        dict(_window_binds(branch_id, tran_date), item_id=int(item_id)),
    ).fetchone()
    if not ok:
        raise HTTPException(status_code=400, detail=QUALITY_WINDOW_MSG)
    return int(item_id)


def _fetch_trolly(db: Session, trolly_id: int, branch_id: int):
    """D1 gate: exists, type 'T', in the branch, tagged to the Selection stage."""
    row = db.execute(get_selection_trolly_row_query(), {"trolly_id": int(trolly_id)}).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail=UNKNOWN_TROLLY_MSG)
    trolly_type = (getattr(row, "trolly_type", None) or TROLLY_TYPE_TROLLEY).strip().upper()
    type_name = (getattr(row, "machine_type_name", None) or "").strip().lower()
    if (
        trolly_type != TROLLY_TYPE_TROLLEY
        or (row.branch_id is not None and int(row.branch_id) != int(branch_id))
        or type_name != SELECTION_MACHINE_TYPE_NAME.lower()
    ):
        raise HTTPException(status_code=400, detail=TROLLY_NOT_TAGGED_MSG)
    return row


def _verify_gate_entry(db: Session, branch_id: int, tran_date: date, jute_mr_id: int, item_id: int) -> int:
    """D7 gate: the MR has a line of this quality issued to the branch in the window."""
    ok = db.execute(
        verify_selection_gate_entry_query(),
        dict(_window_binds(branch_id, tran_date), jute_mr_id=int(jute_mr_id), item_id=int(item_id)),
    ).fetchone()
    if not ok:
        raise HTTPException(status_code=400, detail=GATE_ENTRY_MSG)
    return int(jute_mr_id)


def _prepare_entry(
    db: Session,
    body: SelectionEntryBody,
    co_id: int,
    user_id,
    existing=None,
) -> Dict[str, Any]:
    """Run every §3.2 gate and build the INSERT / UPDATE bind dict.

    Cheap in-memory gates first (date, gross), then the DB-backed ones in a fixed
    order — branch, quality (D4), trolley (D1), spell, gate entry (D7) — then the tare
    snapshot rule and the net computation. ``existing`` is the row being edited: its
    stored ``trolly_wt`` is kept when the trolley is unchanged.
    """
    if body.tran_date > date.today():
        raise HTTPException(status_code=400, detail=FUTURE_DATE_MSG)
    gross_wt = _validate_gross(body.gross_wt)

    _verify_branch(db, co_id, body.branch_id)
    item_id = _verify_quality(db, body.branch_id, body.tran_date, body.item_id)
    trolly = _fetch_trolly(db, body.trolly_id, body.branch_id)

    spell_id = None
    if body.spell_id is not None:
        spell_id = int(_resolve_spell(db, body.spell_id, None, body.branch_id))

    jute_mr_id = None
    if body.jute_mr_id is not None:
        jute_mr_id = _verify_gate_entry(db, body.branch_id, body.tran_date, body.jute_mr_id, item_id)

    # Tare snapshot rule (§3.2): keep the stored snapshot while the trolley is unchanged.
    if existing is not None and _i(existing.trolly_id) == int(body.trolly_id):
        trolly_wt = _f(existing.trolly_wt)
    else:
        trolly_wt = _f(trolly.trolly_weight)

    net_wt = round(gross_wt - trolly_wt, 3)  # D2
    if net_wt <= 0:
        raise HTTPException(
            status_code=400,
            detail=NET_WT_MSG.format(gross=gross_wt, trolly=trolly_wt, net=net_wt),
        )

    return {
        "co_id": int(co_id),
        "branch_id": int(body.branch_id),
        "tran_date": body.tran_date,
        "spell_id": spell_id,
        "item_id": item_id,
        "trolly_id": int(body.trolly_id),
        "jute_mr_id": jute_mr_id,
        "trolly_wt": trolly_wt,
        "gross_wt": gross_wt,
        "net_wt": net_wt,
        "remarks": (body.remarks.strip() or None) if isinstance(body.remarks, str) else None,
        "updated_by": user_id,
    }


def _split_item_ids(raw) -> List[int]:
    """GROUP_CONCAT result ('269847,269848') -> [269847, 269848]."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [int(x) for x in raw]
    return [int(x) for x in str(raw).split(",") if str(x).strip()]


# =============================================================================
# Endpoints — setup + reads
# =============================================================================


@router.get("/entry_setup")
def entry_setup(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Dropdown payload for the entry page (§3.1): qualities issued in the D4 window
    (with issued kg), gate entries of the window (D7, with their item_ids), Selection-
    tagged trolleys (D1), spells (de-duplicated by code) and the window bounds."""
    co_id = _require_co_id(request)
    branch_id = _required_int_param(request, "branch_id")
    raw_date = request.query_params.get("tran_date")
    try:
        tran_date = date.fromisoformat(raw_date) if raw_date else date.today()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tran_date format")
    try:
        _verify_branch(db, co_id, branch_id)
        binds = _window_binds(branch_id, tran_date)

        qualities = [
            {
                "item_id": int(r._mapping["item_id"]),
                "item_code": r._mapping.get("item_code"),
                "item_name": r._mapping.get("item_name"),
                "issued_kg": round(_f(r._mapping.get("issued_kg")), 3),
            }
            for r in db.execute(get_selection_qualities_query(), binds).fetchall()
        ]

        gate_entries = [
            {
                "jute_mr_id": int(r._mapping["jute_mr_id"]),
                "gate_entry_no": _i(r._mapping.get("gate_entry_no")),
                "gate_entry_date": _d(r._mapping.get("gate_entry_date")),
                "supplier_name": r._mapping.get("supplier_name"),
                "item_ids": _split_item_ids(r._mapping.get("item_ids")),
                "issued_kg": round(_f(r._mapping.get("issued_kg")), 3),
            }
            for r in db.execute(get_selection_gate_entries_query(), binds).fetchall()
        ]

        trollies = []
        for r in db.execute(
            get_trollies_query(),
            {"branch_id": branch_id, "machine_type_name": SELECTION_MACHINE_TYPE_NAME},
        ).fetchall():
            m = dict(r._mapping)
            if (m.get("trolly_type") or TROLLY_TYPE_TROLLEY).strip().upper() != TROLLY_TYPE_TROLLEY:
                continue
            trollies.append(
                {
                    "trolly_id": int(m["trolly_id"]),
                    "trolly_name": m.get("trolly_name"),
                    "trolly_weight": _f(m.get("trolly_weight")),
                    "trolly_posting_code": m.get("trolly_posting_code"),
                }
            )

        spells: List[Dict[str, Any]] = []
        seen = set()
        for r in db.execute(get_spells_query(), {"branch_id": branch_id}).fetchall():
            m = dict(r._mapping)
            code = m["spell_code"]
            if code in seen:
                continue
            seen.add(code)
            spells.append(
                {"spell_id": int(m["spell_id"]), "spell_code": code, "spell_name": m.get("spell_name")}
            )

        return {
            "data": {
                "qualities": qualities,
                "gate_entries": gate_entries,
                "trollies": trollies,
                "spells": spells,
                "quality_window": window_bounds(tran_date),
            }
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/entries_by_date")
def entries_by_date(
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Day grid: SelectionRow[] for the (branch, date) [+ spell], day totals and the
    per-quality roll-up (§3.1)."""
    co_id = _require_co_id(request)
    branch_id = _required_int_param(request, "branch_id")
    tran_date = _required_date_param(request, "tran_date")
    spell_id = _optional_int_param(request, "spell_id")
    try:
        rows = [
            _serialize_row(r)
            for r in db.execute(
                get_selection_rows_query(),
                {
                    "co_id": co_id,
                    "selection_wt_id": None,
                    "branch_id": branch_id,
                    "tran_date": tran_date,
                    "spell_id": spell_id,
                },
            ).fetchall()
        ]
        totals = {
            "trolleys": len(rows),
            "gross_wt": round(sum(_f(r.get("gross_wt")) for r in rows), 3),
            "trolly_wt": round(sum(_f(r.get("trolly_wt")) for r in rows), 3),
            "net_wt": round(sum(_f(r.get("net_wt")) for r in rows), 3),
        }
        by_quality = [
            {
                "item_id": _i(r._mapping.get("item_id")),
                "item_name": r._mapping.get("item_name"),
                "trolleys": _i(r._mapping.get("trolleys"), 0),
                "net_wt": round(_f(r._mapping.get("net_wt")), 3),
            }
            for r in db.execute(
                get_selection_by_quality_query(),
                {"co_id": co_id, "branch_id": branch_id, "tran_date": tran_date, "spell_id": spell_id},
            ).fetchall()
        ]
        return {"data": {"rows": rows, "totals": totals, "by_quality": by_quality}}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Endpoints — entry CRUD
# =============================================================================


@router.post("/entry_create")
def entry_create(
    body: SelectionEntryBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Insert one weighed trolley (§3.2 gates, tare snapshotted) and echo the row."""
    try:
        user_id = token_data.get("user_id")
        binds = _prepare_entry(db, body, body.co_id, user_id)
        result = db.execute(insert_selection_entry_query(), binds)
        selection_wt_id = int(result.lastrowid)
        row = _fetch_row(db, body.co_id, selection_wt_id)
        db.commit()
        return {"data": {"row": row}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/entry_edit/{selection_wt_id}")
def entry_edit(
    selection_wt_id: int,
    body: SelectionEntryBody,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Replace one row's fields (same body as create). Requires co_id: the row is
    located by (id, co_id), so another company's row reads as 404. The tare snapshot
    is kept while the trolley is unchanged (§3.2)."""
    try:
        co_id = _require_co_id(request)
        existing = db.execute(
            get_selection_active_by_id_query(), {"id": selection_wt_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)

        user_id = token_data.get("user_id")
        binds = _prepare_entry(db, body, co_id, user_id, existing=existing)
        db.execute(update_selection_entry_query(), dict(binds, id=selection_wt_id))
        row = _fetch_row(db, co_id, selection_wt_id)
        db.commit()
        return {"data": {"row": row}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/entry_delete/{selection_wt_id}")
def entry_delete(
    selection_wt_id: int,
    request: Request,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Soft delete (active = 0), co-scoped in the read guard AND the UPDATE."""
    try:
        co_id = _require_co_id(request)
        existing = db.execute(
            get_selection_active_by_id_query(), {"id": selection_wt_id, "co_id": co_id}
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=NOT_FOUND_MSG)
        db.execute(
            soft_delete_selection_entry_query(),
            {"id": selection_wt_id, "co_id": co_id, "updated_by": token_data.get("user_id")},
        )
        db.commit()
        return {"data": {"selection_wt_id": selection_wt_id}}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

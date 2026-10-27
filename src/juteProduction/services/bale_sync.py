"""F10 — keep the Finishing › Bale Press daily figure in step with the per-bale log.

Decision D4 (docs/bale-production-spec-sheet.md §5 F10, §9): bale production is entered
in exactly ONE place (the per-bale page), and the ``jute_prod_finishing_daily`` row with
``process = 'balepress'`` that the DPR rolls up is MAINTAINED from it — never typed twice.

After every create / edit / delete the router calls :func:`sync_balepress_daily` for the
affected key(s); an edit that moves a bale to another key syncs BOTH the old and the new
key. The key is (co_id, tran_date, spell_id, machine_id, finishing_quality_id) with a
NULL-able machine_id (branches without a Bale Press machine row), so the daily-row match
is NULL-safe. The figure written is::

    prod_qty   = count of ACTIVE PROD bales on the key   (REPACK excluded, Q4)
    prod_uom   = 'bale'
    prod_wt_kg = Σ net_wt of those bales

count 0 -> the daily row (if any) is soft-deleted; otherwise it is updated in place or
inserted. The function issues statements on the caller's session and does NOT commit —
it runs inside the bale save's transaction so the log and the figure can never diverge
(a failed sync rolls the bale save back too). Pure orchestration over the bale_query
builders, so it is unit-testable with a MagicMock session (src/test/test_bale_sync.py).
"""

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from src.juteProduction.bale_query import (
    aggregate_prod_bales_for_key_query,
    get_balepress_daily_row_query,
    insert_balepress_daily_query,
    soft_delete_balepress_daily_query,
    update_balepress_daily_query,
)
from src.juteProduction.constants import (
    BALE_ENTRY_TYPE_PROD,
    BALE_PRESS_PROCESS,
    BALE_PRESS_PROD_UOM,
)


def bale_daily_key(
    co_id: int,
    branch_id: Optional[int],
    tran_date,
    spell_id: int,
    machine_id: Optional[int],
    finishing_quality_id: int,
) -> Dict[str, Any]:
    """Normalise one F10 key into the kwargs :func:`sync_balepress_daily` takes.

    branch_id rides along (it is stored on the daily row) but is NOT part of the key.
    Typed binds throughout; ``None`` stays ``None`` (SQL NULL) for machine_id.
    """
    return {
        "co_id": int(co_id),
        "branch_id": None if branch_id is None else int(branch_id),
        "tran_date": tran_date,
        "spell_id": int(spell_id),
        "machine_id": None if machine_id is None else int(machine_id),
        "finishing_quality_id": int(finishing_quality_id),
    }


def same_bale_daily_key(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """True when two keys address the same daily row (branch_id ignored, dates by value)."""
    return (
        a["co_id"] == b["co_id"]
        and str(a["tran_date"]) == str(b["tran_date"])
        and a["spell_id"] == b["spell_id"]
        and a["machine_id"] == b["machine_id"]
        and a["finishing_quality_id"] == b["finishing_quality_id"]
    )


def sync_balepress_daily(
    db: Session,
    *,
    co_id: int,
    branch_id: Optional[int],
    tran_date,
    spell_id: int,
    machine_id: Optional[int],
    finishing_quality_id: int,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Upsert / soft-delete the balepress daily row for ONE key from the bale log (F10).

    Returns ``{"action": 'inserted'|'updated'|'deleted'|'none', "finishing_daily_id",
    "bales", "total_net"}`` so callers (and tests) can see what happened. No commit here.
    """
    key = bale_daily_key(
        co_id, branch_id, tran_date, spell_id, machine_id, finishing_quality_id
    )

    agg = db.execute(
        aggregate_prod_bales_for_key_query(),
        {
            "co_id": key["co_id"],
            "tran_date": key["tran_date"],
            "spell_id": key["spell_id"],
            "machine_id": key["machine_id"],
            "finishing_quality_id": key["finishing_quality_id"],
            "entry_type": BALE_ENTRY_TYPE_PROD,
        },
    ).fetchone()
    bales = int(agg.bales or 0) if agg is not None else 0
    total_net = float(agg.total_net or 0) if agg is not None else 0.0

    existing = db.execute(
        get_balepress_daily_row_query(),
        {
            "co_id": key["co_id"],
            "tran_date": key["tran_date"],
            "spell_id": key["spell_id"],
            "process": BALE_PRESS_PROCESS,
            "machine_id": key["machine_id"],
            "finishing_quality_id": key["finishing_quality_id"],
        },
    ).fetchone()
    existing_id = int(existing.finishing_daily_id) if existing is not None else None

    result: Dict[str, Any] = {
        "finishing_daily_id": existing_id,
        "bales": bales,
        "total_net": round(total_net, 3),
    }

    if bales == 0:
        if existing_id is None:
            result["action"] = "none"
            return result
        db.execute(
            soft_delete_balepress_daily_query(),
            {"id": existing_id, "updated_by": user_id},
        )
        result["action"] = "deleted"
        return result

    figure = {
        "branch_id": key["branch_id"],
        "prod_qty": float(bales),
        "prod_uom": BALE_PRESS_PROD_UOM,
        "prod_wt_kg": round(total_net, 3),
        "updated_by": user_id,
    }
    if existing_id is not None:
        db.execute(update_balepress_daily_query(), dict(figure, id=existing_id))
        result["action"] = "updated"
        return result

    inserted = db.execute(
        insert_balepress_daily_query(),
        dict(
            figure,
            co_id=key["co_id"],
            tran_date=key["tran_date"],
            spell_id=key["spell_id"],
            process=BALE_PRESS_PROCESS,
            machine_id=key["machine_id"],
            finishing_quality_id=key["finishing_quality_id"],
        ),
    )
    lastrowid = getattr(inserted, "lastrowid", None)
    result["finishing_daily_id"] = int(lastrowid) if isinstance(lastrowid, int) else None
    result["action"] = "inserted"
    return result

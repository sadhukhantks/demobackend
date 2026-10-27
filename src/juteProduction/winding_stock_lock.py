"""Winding stock lock lookups + the locked-day permission gate.

A (co, branch, tran_date) day is locked once the operator finalises the count.
While locked, mutations require Edit (access_type_id >= 4); Write-only (3) is
rejected 403. This mirrors weaving_lock.py, minus spell (a stock count has no
shift).

The gate is deliberately a permission check rather than a flat refusal: a signed
report must not be edited under by the operator, but a supervisor still has to
be able to correct a miscount without a DBA.
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.common.portal.query import get_user_menu_access_level_query
from src.juteProduction.constants import WINDING_STOCK_MENU_PATH
from src.juteProduction.winding_stock_query import get_winding_stock_lock_row_query

EDIT_LEVEL = 4
LOCKED_EDIT_ONLY_MSG = (
    "This day is locked because its stock report has been taken. Editing a "
    "locked winding stock entry requires Edit permission for the Winding Stock "
    "menu."
)


def get_stock_lock(db: Session, co_id, branch_id, tran_date):
    """Active lock header for the day (or None)."""
    return db.execute(
        get_winding_stock_lock_row_query(),
        {
            "co_id": int(co_id),
            "tran_date": tran_date,
            "branch_id": None if branch_id is None else int(branch_id),
        },
    ).fetchone()


def is_day_locked(db: Session, co_id, branch_id, tran_date) -> bool:
    row = get_stock_lock(db, co_id, branch_id, tran_date)
    return bool(row and row.is_locked)


def user_menu_access_level(db: Session, user_id, co_id, branch_id, menu_path) -> int:
    lvl = db.execute(
        get_user_menu_access_level_query(),
        {
            "user_id": int(user_id),
            "co_id": int(co_id),
            "branch_id": None if branch_id is None else int(branch_id),
            "menu_path": menu_path,
        },
    ).scalar()
    return int(lvl) if lvl is not None else 0


def require_edit_if_locked(db: Session, token_data: dict, co_id, branch_id, tran_date) -> None:
    """Raise 403 when the day is locked and the user lacks Edit (>= 4)."""
    if not is_day_locked(db, co_id, branch_id, tran_date):
        return
    level = user_menu_access_level(
        db, token_data.get("user_id"), co_id, branch_id, WINDING_STOCK_MENU_PATH
    )
    if level < EDIT_LEVEL:
        raise HTTPException(status_code=403, detail=LOCKED_EDIT_ONLY_MSG)

"""Winding stock lock gate.

Locked days are not frozen outright: mutating one requires Edit (access level
>= 4) on the Winding Stock menu, so a supervisor can still correct a bad count
while a Write-only operator cannot edit a day whose report has been signed.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.juteProduction.winding_stock_lock import (
    EDIT_LEVEL,
    is_day_locked,
    require_edit_if_locked,
)

D = date(2026, 1, 27)


def _row(**attrs):
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


def _session(lock_row, access_level=None):
    session = MagicMock()
    lock_res = MagicMock()
    lock_res.fetchone.return_value = lock_row
    level_res = MagicMock()
    level_res.scalar.return_value = access_level
    session.execute.side_effect = [lock_res, level_res]
    return session


def test_unlocked_day_is_not_locked():
    assert is_day_locked(_session(None), 1, 2, D) is False


def test_locked_flag_false_is_not_locked():
    assert is_day_locked(_session(_row(winding_stock_lock_id=5, is_locked=0)), 1, 2, D) is False


def test_unlocked_day_never_checks_permission():
    """No lock means no permission query at all — the gate must short-circuit."""
    session = _session(None)
    require_edit_if_locked(session, {"user_id": 9}, 1, 2, D)
    assert session.execute.call_count == 1


def test_locked_day_rejects_write_only_user():
    session = _session(_row(winding_stock_lock_id=5, is_locked=1), access_level=3)
    with pytest.raises(HTTPException) as exc:
        require_edit_if_locked(session, {"user_id": 9}, 1, 2, D)
    assert exc.value.status_code == 403


def test_locked_day_allows_edit_user():
    session = _session(_row(winding_stock_lock_id=5, is_locked=1), access_level=EDIT_LEVEL)
    require_edit_if_locked(session, {"user_id": 9}, 1, 2, D)


def test_missing_access_level_is_treated_as_no_access():
    """scalar() returns None when the user has no mapping — must not pass the gate."""
    session = _session(_row(winding_stock_lock_id=5, is_locked=1), access_level=None)
    with pytest.raises(HTTPException) as exc:
        require_edit_if_locked(session, {"user_id": 9}, 1, 2, D)
    assert exc.value.status_code == 403

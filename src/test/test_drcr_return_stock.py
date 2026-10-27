"""
Regression tests: goods returned to the supplier leave stock.

Real case (sls, branch 87): SRN LC/FACTORY/24-25/000122 line for item 141-0056
received 20 pcs on 2024-05-21; DR note 62 returned all 20 to the supplier on
2024-05-31 ("RECEIVED SMALL SIZE"). Stores still issued 17 pcs in Jun/Jul 2026,
because:

1. `vw_approved_inward_qty.balance_qty` was `approved_qty - issue_qty`, with no
   DR-note term — so the Material Issue picker still offered the returned lot.
   Fixed by dbqueries/migrations/20260823_drcr_return_reduces_stock.sql.
2. Nothing re-checked availability on save, so the picker was the only gate.
   Fixed by `_assert_lots_available` in src/inventory/issue.py.

These cover (2) — the layer that stops a stale page or hand-made payload.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.inventory.issue import _assert_lots_available
from src.inventory.query import get_lot_availability_query


class _Line:
    def __init__(self, inward_dtl_id, issue_qty):
        self.inward_dtl_id = inward_dtl_id
        self.issue_qty = issue_qty


def _db(lots):
    """lots: {inward_dtl_id: available_qty} -> a db whose execute returns them."""
    rows = [
        MagicMock(
            _mapping={
                "inward_dtl_id": dtl_id,
                "available_qty": qty,
                "item_name": f"ITEM-{dtl_id}",
                "sr_no": "LC/FACTORY/24-25/000122",
            }
        )
        for dtl_id, qty in lots.items()
    ]
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows
    return db


def test_issue_within_available_passes():
    _assert_lots_available(_db({15499: 20.0}), [_Line(15499, 17.0)])


def test_fully_returned_lot_cannot_be_issued():
    """The reported bug: 20 received, 20 returned -> 0 available, 17 requested."""
    with pytest.raises(HTTPException) as exc:
        _assert_lots_available(_db({15499: 0.0}), [_Line(15499, 17.0)])
    assert exc.value.status_code == 400
    assert "17" in exc.value.detail and "debit note" in exc.value.detail


def test_two_lines_on_the_same_lot_are_summed():
    """6 + 5 on one lot with 10 available must fail, even though each line fits."""
    with pytest.raises(HTTPException):
        _assert_lots_available(_db({15499: 10.0}), [_Line(15499, 6.0), _Line(15499, 5.0)])


def test_lot_missing_from_stock_view_is_rejected():
    """Unapproved SR / deactivated inward line -> not issuable, never silently allowed."""
    with pytest.raises(HTTPException) as exc:
        _assert_lots_available(_db({}), [_Line(99999, 1.0)])
    assert "no longer issuable" in exc.value.detail


def test_float_noise_does_not_trip_the_guard():
    _assert_lots_available(_db({15499: 3.84}), [_Line(15499, 3.8400000000000003)])


def test_lines_without_a_lot_are_skipped():
    db = MagicMock()
    _assert_lots_available(db, [_Line(None, 5.0)])
    db.execute.assert_not_called()


def test_edit_adds_back_the_issue_own_lines():
    """Editing an issue must not count its own current qty against itself."""
    sql = str(get_lot_availability_query(1, exclude_issue=True))
    assert "issue_id = :issue_id" in sql
    assert "COALESCE(own.qty, 0)" in sql
    create_sql = str(get_lot_availability_query(1, exclude_issue=False))
    assert "issue_id" not in create_sql


def test_availability_reads_the_return_aware_view():
    sql = str(get_lot_availability_query(2, exclude_issue=False))
    assert "vw_approved_inward_qty" in sql
    assert ":d0" in sql and ":d1" in sql

"""
Owner ruling 2026-10-05: an MR / QC save may not cut a line's quantity below what is
already issued from it, nor change the item of a line with live issues (sls MRs 28586 /
28592: the item-59 line was emptied into the item-57 line after issues were drawn, and
stock read -1,318 / -1,950 kg). Weight-only over-issue stays a warning (2026-09-03).

Pinned:
- quantity cut below issued -> 409 naming the line and its issues;
- item changed on an issued line -> 409;
- an unchanged line that was ALREADY short does not block an unrelated save;
- quantity cut that still covers the issues is fine; no issued lines -> no-op;
- both writers (update_mr, complete_inspection) snapshot before and check after.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.juteProcurement import materialInspection, mr
from src.juteProcurement._jute_scope_guards import assert_issued_lines_intact, snapshot_issued_lines


def _row(li, item, qty, issued, ids="1,2"):
    return SimpleNamespace(jute_mr_li_id=li, actual_item_id=item, actual_qty=qty, issued_qty=issued, issue_ids=ids)


def _db(rows):
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = rows
    return db


def test_quantity_cut_below_issued_is_refused():
    before = {46278: _row(46278, 59, 9, 9, "89018,89019")}
    with pytest.raises(HTTPException) as e:
        assert_issued_lines_intact(_db([_row(46278, 59, 0, 9, "89018,89019")]), 28586, before)
    assert e.value.status_code == 409
    assert "line 46278" in e.value.detail and "89018,89019" in e.value.detail


def test_item_change_on_issued_line_is_refused():
    before = {46278: _row(46278, 59, 9, 9)}
    with pytest.raises(HTTPException) as e:
        assert_issued_lines_intact(_db([_row(46278, 57, 9, 9)]), 28586, before)
    assert e.value.status_code == 409 and "item cannot change" in e.value.detail


def test_unchanged_line_already_short_does_not_block():
    before = {1: _row(1, 59, 5, 9)}
    assert_issued_lines_intact(_db([_row(1, 59, 5, 9)]), 1, before)


def test_cut_that_still_covers_issues_is_allowed():
    before = {1: _row(1, 57, 75, 66)}
    assert_issued_lines_intact(_db([_row(1, 57, 70, 66)]), 1, before)


def test_no_issued_lines_is_a_no_op():
    db = _db([])
    assert_issued_lines_intact(db, 1, {})
    db.execute.assert_not_called()


def test_snapshot_keys_by_line():
    snap = snapshot_issued_lines(_db([_row(7, 57, 10, 4)]), 1)
    assert list(snap) == [7]


@pytest.mark.parametrize("fn", [mr.update_jute_mr, materialInspection.complete_material_inspection])
def test_both_writers_snapshot_then_check(fn):
    src = inspect.getsource(fn)
    snap, check = src.index("snapshot_issued_lines("), src.index("assert_issued_lines_intact(")
    assert snap < check < src.index("db.commit()")

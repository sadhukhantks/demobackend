"""
TKT-000143: a DRAFTED issue must not make a lot vanish from the issue picker.

sls, branch 87, item 134-5516: the Item Ledger showed 1 PC while the Material
Issue picker showed nothing at all. Lot inward_dtl_id 23090 (approved 1) was
entirely committed by drafted issue 58424 (issue_pass_no 1433), and the two
sides of the app count drafts differently:

* vw_approved_inward_qty.balance_qty subtracts every issue line whose header is
  not cancelled (status <> 6) -- drafts included -- so balance_qty was 0 and the
  picker's `balance_qty > 0` filter dropped the row.
* Item Ledger / Stock Report count issues only in status 1/3/5/20 -- closing 1.0.

Neither definition was changed (the view is per-tenant DDL; the ledger is right
to ignore unapproved paper). The picker now SHOWS the lot with the hold spelled
out: available_qty (issuable now) 0, draft_held_qty 1, ledger_qty 1,
draft_held_refs '1433'. Over-issue stays blocked by _assert_lots_available,
which reads the draft-exclusive view.

The join takes an :issue_id so the draft being EDITED is not reported as
holding stock against itself — the same self-exclusion get_lot_availability_query
does with own_qty.
"""

import sqlite3
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.inventory.issue import _assert_lots_available
from src.inventory.query import (
    get_available_inward_inventory_query,
    get_searchable_inventory_count_query,
    get_searchable_inventory_list_query,
)

_SCHEMA = """
CREATE TABLE vw_approved_inward_qty (inward_dtl_id INT, inward_id INT, inward_date TEXT,
    branch_id INT, item_id INT, uom_id INT, approved_qty REAL, issue_qty REAL, balance_qty REAL);
CREATE TABLE proc_inward (inward_id INT, inward_sequence_no INT, sr_no TEXT);
CREATE TABLE proc_inward_dtl (inward_dtl_id INT, inward_id INT, item_make_id INT,
    warehouse_id INT, approved_qty REAL, rate REAL, accepted_rate REAL, discount_amount REAL);
CREATE TABLE branch_mst (branch_id INT, branch_name TEXT);
CREATE TABLE vw_item_with_group_path (item_id INT, item_name TEXT, item_code TEXT, item_grp_id INT,
    item_group_name_display TEXT, item_group_code_display TEXT, full_item_code TEXT);
CREATE TABLE item_make (item_make_id INT, item_make_name TEXT);
CREATE TABLE uom_mst (uom_id INT, uom_name TEXT);
CREATE TABLE warehouse_mst (warehouse_id INT, warehouse_name TEXT);
CREATE TABLE issue_hdr (issue_id INT, issue_pass_no INT, issue_pass_print_no TEXT,
    status_id INT, active INT);
CREATE TABLE issue_li (issue_id INT, inward_dtl_id INT, issue_qty REAL);
"""

# (inward_dtl_id, item_id, approved_qty, issued-per-the-view, balance_qty)
_LOTS = [
    (23090, 900, 1.0, 1.0, 0.0),   # fully held by drafted issue 1433 -> the bug
    (23091, 901, 5.0, 2.0, 3.0),   # ordinary lot, only an APPROVED issue against it
    (23092, 901, 4.0, 4.0, 0.0),   # genuinely exhausted -> must stay hidden
    (23093, 900, 10.0, 6.0, 4.0),  # partly issuable, partly held by two drafts
    (23094, 901, 8.0, 3.0, 5.0),   # partly held by ONE draft -> the edit-mode case
]
# (issue_id, pass_no, print_no, status_id, active, inward_dtl_id, qty)
_ISSUES = [
    (58424, 1433, None, 21, 1, 23090, 1.0),
    (58000, 1200, "EI/FACTORY/26-27/001200", 3, 1, 23091, 2.0),
    (58001, 1201, "EI/FACTORY/26-27/001201", 3, 1, 23092, 4.0),
    (58502, 1502, None, 21, 0, 23092, 1.0),   # deactivated draft holds nothing
    (58500, 1500, "EI/FACTORY/26-27/001500", 21, 1, 23093, 3.0),
    (58501, 1501, None, 21, 1, 23093, 3.0),
    (58600, 1600, None, 21, 1, 23094, 3.0),
]


def _mysql_to_sqlite(sql: str) -> str:
    """Only MySQL-ism in these queries: GROUP_CONCAT's ORDER BY/SEPARATOR clause."""
    return sql.replace("ORDER BY ih.issue_pass_no SEPARATOR ', '", "")


def _db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    con.execute("INSERT INTO proc_inward VALUES (1, 35, 'LC/FACTORY/26-27/000035')")
    con.execute("INSERT INTO branch_mst VALUES (87, 'FACTORY')")
    con.execute("INSERT INTO uom_mst VALUES (1, 'PCS')")
    con.execute("INSERT INTO item_make VALUES (1, 'GENERIC')")
    con.execute("INSERT INTO warehouse_mst VALUES (1, 'MAIN')")
    con.executemany(
        "INSERT INTO vw_item_with_group_path VALUES (?, ?, ?, 134, 'SPARES', '134', ?)",
        [(900, "V BELT", "134-5516", "134-5516"), (901, "BOLT", "141-0056", "141-0056")],
    )
    for dtl_id, item_id, approved, issued, balance in _LOTS:
        con.execute(
            "INSERT INTO vw_approved_inward_qty VALUES (?, 1, '2026-06-01', 87, ?, 1, ?, ?, ?)",
            (dtl_id, item_id, approved, issued, balance),
        )
        con.execute(
            "INSERT INTO proc_inward_dtl VALUES (?, 1, 1, 1, ?, 100.0, NULL, 0.0)",
            (dtl_id, approved),
        )
    for issue_id, pass_no, print_no, status, active, dtl_id, qty in _ISSUES:
        con.execute(
            "INSERT INTO issue_hdr VALUES (?, ?, ?, ?, ?)",
            (issue_id, pass_no, print_no, status, active),
        )
        con.execute("INSERT INTO issue_li VALUES (?, ?, ?)", (issue_id, dtl_id, qty))
    return con


def _rows(query, params):
    con = _db()
    return [dict(r) for r in con.execute(_mysql_to_sqlite(str(query)), params).fetchall()]


def _picker(item_id=None, issue_id=None):
    return _rows(
        get_available_inward_inventory_query(),
        {"branch_id": 87, "item_id": item_id, "item_grp_id": None, "issue_id": issue_id},
    )


def _by_lot(rows):
    return {r["inward_dtl_id"]: r for r in rows}


def test_fully_draft_held_lot_is_visible_with_its_hold_explained():
    """The reported case: ledger 1 PC, picker empty. Now it shows, at zero available."""
    lot = _by_lot(_picker(item_id=900))[23090]
    assert lot["available_qty"] == 0          # nothing may be issued right now
    assert lot["draft_held_qty"] == 1
    assert lot["ledger_qty"] == 1             # what the Item Ledger reports
    assert "1433" in lot["draft_held_refs"]   # the drafted issue to chase


def test_lot_without_a_draft_reports_zero_hold_and_ledger_equals_available():
    lot = _by_lot(_picker())[23091]
    assert lot["draft_held_qty"] == 0
    assert lot["draft_held_refs"] is None
    assert lot["ledger_qty"] == lot["available_qty"] == 3


def test_exhausted_lot_with_no_draft_stays_hidden():
    """Visibility is widened only by drafts -- and a deactivated draft is not one."""
    assert 23092 not in _by_lot(_picker())


def test_partial_hold_keeps_available_and_ledger_apart():
    lot = _by_lot(_picker())[23093]
    assert lot["available_qty"] == 4
    assert lot["draft_held_qty"] == 6
    assert lot["ledger_qty"] == 10
    assert "1500" in lot["draft_held_refs"] and "1501" in lot["draft_held_refs"]


def test_existing_columns_are_untouched():
    lot = _by_lot(_picker())[23091]
    assert lot["sr_no"] == "LC/FACTORY/26-27/000035"
    assert lot["item_code"] == "141-0056"
    assert lot["approved_qty"] == 5 and lot["issue_qty"] == 2
    assert lot["rate"] == 100.0 and lot["uom_name"] == "PCS"


def test_count_and_list_agree_on_which_lots_qualify():
    """Pagination totals must match the rows, draft-held lots included."""
    listed = _rows(
        get_searchable_inventory_list_query(),
        {"branch_id": 87, "search_like": None, "limit": 50, "offset": 0, "issue_id": None},
    )
    total = _rows(
        get_searchable_inventory_count_query(),
        {"branch_id": 87, "search_like": None, "issue_id": None},
    )[0]["total"]
    assert set(_by_lot(listed)) == {23090, 23091, 23093, 23094}
    assert total == len(listed) == 4

    searched = _rows(
        get_searchable_inventory_list_query(),
        {"branch_id": 87, "search_like": "%134-5516%", "limit": 50, "offset": 0,
         "issue_id": None},
    )
    searched_total = _rows(
        get_searchable_inventory_count_query(),
        {"branch_id": 87, "search_like": "%134-5516%", "issue_id": None},
    )[0]["total"]
    assert set(_by_lot(searched)) == {23090, 23093}
    assert searched_total == len(searched) == 2

    # Editing draft 58424 drops lot 23090 (its whole hold was that issue's own
    # line); the count must drop with it or the pager shows a phantom row.
    editing = _rows(
        get_searchable_inventory_list_query(),
        {"branch_id": 87, "search_like": None, "limit": 50, "offset": 0, "issue_id": 58424},
    )
    editing_total = _rows(
        get_searchable_inventory_count_query(),
        {"branch_id": 87, "search_like": None, "issue_id": 58424},
    )[0]["total"]
    assert set(_by_lot(editing)) == {23091, 23093, 23094}
    assert editing_total == len(editing) == 3


def test_edited_draft_does_not_hold_stock_against_itself():
    """Open draft 1600, re-pick its own lot: it must not read as held by 1600."""
    held = _by_lot(_picker())[23094]
    assert held["draft_held_qty"] == 3 and "1600" in held["draft_held_refs"]

    editing = _by_lot(_picker(issue_id=58600))[23094]
    assert editing["draft_held_qty"] == 0
    assert editing["draft_held_refs"] is None
    assert editing["available_qty"] == 5      # still issuable
    assert editing["ledger_qty"] == 5


def test_other_drafts_still_report_held_while_editing():
    """Excluding the edited issue must not blind the picker to everyone else."""
    lot = _by_lot(_picker(issue_id=58500))[23093]   # 58500 and 58501 both hold it
    assert lot["draft_held_qty"] == 3
    assert "1501" in lot["draft_held_refs"] and "1500" not in lot["draft_held_refs"]


def test_editing_a_lots_only_draft_leaves_pre_existing_zero_balance_behaviour():
    """balance_qty still nets the editor's own line (pre-existing, untouched here):
    lot 23090's entire qty sits on draft 58424, so editing 58424 leaves balance 0
    and no foreign hold -- the lot is as invisible as it was before this change,
    not newly mislabelled as held by the issue on screen."""
    assert 23090 not in _by_lot(_picker(issue_id=58424))


def test_all_three_picker_queries_share_one_visibility_rule():
    for query in (
        get_available_inward_inventory_query(),
        get_searchable_inventory_list_query(),
        get_searchable_inventory_count_query(),
    ):
        sql = str(query)
        assert "(v.balance_qty > 0 OR COALESCE(dh.draft_qty, 0) > 0)" in sql
        assert "ih.status_id = 21" in sql
        # every query carrying the join must bind :issue_id, or execute() fails
        assert "(:issue_id IS NULL OR ih.issue_id <> :issue_id)" in sql


def test_visible_draft_held_lot_still_cannot_be_issued():
    """Showing the lot must never become a route to over-issue (guard reads the view)."""
    line = MagicMock(inward_dtl_id=23090, issue_qty=1.0)
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [
        MagicMock(
            _mapping={
                "inward_dtl_id": 23090,
                "available_qty": 0.0,  # vw_approved_inward_qty: drafts already deducted
                "item_name": "V BELT",
                "sr_no": "LC/FACTORY/26-27/000035",
            }
        )
    ]
    with pytest.raises(HTTPException) as exc:
        _assert_lots_available(db, [line])
    assert exc.value.status_code == 400
    assert "only 0 available" in exc.value.detail

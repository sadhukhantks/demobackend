"""Purchased Yarn Ledger — GET /api/juteProductionReports/purchased_yarn_ledger.

Phase 5 of docs/superpowers/specs/2026-09-25-yarn-purchase-issue-consumption-
design.md (§3.5, rulings 3 and 4): per yarn item and month, the godown book
(opening + purchases - issues + returns) checked against the godown count, the
floor counts per department, and the CONSUMPTION derived from them.

The headline fixture is the worked example from the design discussion:

    purchases 26,000 + 34,300; issues 10,000 + 8,000 to Weaving, 4,000 to
    Beaming; return 1,500 from Weaving; godown counts 2,000 (opening) and
    41,600 (closing); weaving floor 1,200 -> 3,100; beaming floor 0 -> 900.

    godown book closing  = 2,000 + 60,300 - 22,000 + 1,500 = 41,800
    godown difference    = 41,600 - 41,800                 =   -200
    weaving consumption  = 1,200 + 18,000 - 1,500 - 3,100  = 14,600
    beaming consumption  =     0 +  4,000 -     0 -   900  =  3,100

build_ledger is pure (plain dicts in, plain dicts out); the endpoint is
exercised with a mocked session driven by a side_effect list, one entry per
statement in the order the handler runs them.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.juteProcurement.constants import YARN_PURCHASE_CUTOVER_DATE
from src.juteProduction.constants import (
    WINDING_STOCK_BUCKET_PURCHASE,
    WINDING_STOCK_LOC_FLOOR,
    WINDING_STOCK_LOC_GODOWN,
    YARN_LEDGER_EXC_DEPT_NO_FLOOR_COUNT,
    YARN_LEDGER_EXC_FLOOR_DAYS_OVER,
    YARN_LEDGER_EXC_GODOWN_BOOK_NEGATIVE,
    YARN_LEDGER_EXC_GODOWN_DIFF,
    YARN_LEDGER_EXC_ITEM_IN_BOOK_NOT_COUNTED,
    YARN_LEDGER_EXC_ITEM_WITHOUT_YARN_MASTER,
    YARN_LEDGER_EXC_LEGACY_STORES_RECEIPTS,
    YARN_LEDGER_FLOOR_DAYS_MAX,
    YARN_LEDGER_GODOWN_TOL_KG,
    YARN_LEDGER_OPENING_BOOK,
    YARN_LEDGER_OPENING_COUNT,
)
from src.juteProduction.yarn_ledger import (
    LEDGER_NUMERIC_KEYS,
    build_ledger,
    days_in_window,
    month_window,
)
import src.juteProduction.yarn_ledger_query as q
from src.main import app

client = TestClient(app)

ITEM = 335556
FROM, TO = date(2026, 9, 1), date(2026, 9, 30)
TODAY = date(2026, 10, 15)          # the month is over unless a test says otherwise
WEAVING, BEAMING = 7, 9


def _lbl(**extra):
    base = {"item_id": ITEM, "item_code": "13SKWP", "item_name": "13 LBS SKWP",
            "yarn_count": 13.0}
    base.update(extra)
    return base


def _purchase(kg, has_master=1, **extra):
    return _lbl(kg=kg, has_yarn_master=has_master, **extra)


def _move(dept_id, dept_desc, issue_type, kg, **extra):
    return _lbl(dept_id=dept_id, dept_desc=dept_desc, issue_type=issue_type, kg=kg, **extra)


def _count(kg, on, dept_id=None, dept_desc=None, **extra):
    return _lbl(tran_date=on, kg=kg, dept_id=dept_id, dept_desc=dept_desc, **extra)


def _example(**overrides):
    kw = dict(
        from_date=FROM,
        to_date=TO,
        purchase_rows=[_purchase(60300)],           # 26,000 + 34,300, one item
        book_rows=[],
        movement_rows=[
            _move(WEAVING, "WEAVING", "ISSUE", 18000),   # 10,000 + 8,000
            _move(BEAMING, "BEAMING", "ISSUE", 4000),
            _move(WEAVING, "WEAVING", "RETURN", 1500),
        ],
        godown_opening_rows=[_count(2000, date(2026, 8, 31))],
        godown_closing_rows=[_count(41600, date(2026, 9, 30))],
        floor_opening_rows=[_count(1200, date(2026, 8, 31), WEAVING, "WEAVING")],
        floor_closing_rows=[
            _count(3100, date(2026, 9, 30), WEAVING, "WEAVING"),
            _count(900, date(2026, 9, 30), BEAMING, "BEAMING"),
        ],
        legacy_stores_kg=0.0,
        today=TODAY,
    )
    kw.update(overrides)
    return build_ledger(**kw)


# ------------------------------------------------------------ worked example


def test_worked_example_godown_book_and_difference():
    out = _example()
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["item_id"] == ITEM
    assert row["item_name"] == "13 LBS SKWP"
    assert row["item_code"] == "13SKWP"
    assert row["yarn_count"] == 13.0
    assert row["godown_opening_kg"] == 2000.0
    assert row["purchases_kg"] == 60300.0
    assert row["issues_kg"] == 22000.0
    assert row["returns_kg"] == 1500.0
    assert row["godown_book_closing_kg"] == 41800.0
    assert row["godown_count_closing_kg"] == 41600.0
    assert row["godown_diff_kg"] == -200.0


def test_worked_example_floor_and_consumption_per_department():
    row = _example()["rows"][0]
    assert row["floor_opening_kg"] == 1200.0
    assert row["floor_closing_kg"] == 4000.0
    assert row["consumption_kg"] == 17700.0        # 14,600 + 3,100
    # 4,000 kg on the floor against 17,700 / 30 days = 590 kg a day
    assert row["days_of_stock"] == round(4000 / (17700 / 30), 1) == 6.8
    assert row["depts"] == [
        {"dept_id": BEAMING, "dept_desc": "BEAMING", "floor_opening_kg": 0.0,
         "issues_kg": 4000.0, "returns_kg": 0.0, "floor_closing_kg": 900.0,
         "consumption_kg": 3100.0},
        {"dept_id": WEAVING, "dept_desc": "WEAVING", "floor_opening_kg": 1200.0,
         "issues_kg": 18000.0, "returns_kg": 1500.0, "floor_closing_kg": 3100.0,
         "consumption_kg": 14600.0},
    ]


def test_worked_example_meta_totals_and_no_exceptions():
    out = _example()
    assert set(out) == {"from_date", "to_date", "meta", "rows", "totals", "exceptions"}
    assert out["from_date"] == "2026-09-01" and out["to_date"] == "2026-09-30"
    assert out["meta"] == {
        "godown_opening_count_date": "2026-08-31",
        "godown_closing_count_date": "2026-09-30",
        "floor_count_dates": {WEAVING: "2026-09-30", BEAMING: "2026-09-30"},
        "opening_basis": YARN_LEDGER_OPENING_COUNT,
        "days_in_window": 30,
    }
    row = out["rows"][0]
    assert set(out["totals"]) == set(LEDGER_NUMERIC_KEYS)
    for k in LEDGER_NUMERIC_KEYS:
        assert out["totals"][k] == row[k], k
    # -200 is inside max(50, 1 % of 62,300 = 623) and 6.8 days is under 15
    assert out["exceptions"] == []


def test_row_keys_are_exactly_the_documented_shape():
    row = _example()["rows"][0]
    assert set(row) == {
        "item_id", "item_name", "item_code", "yarn_count",
        "godown_opening_kg", "purchases_kg", "issues_kg", "returns_kg",
        "godown_book_closing_kg", "godown_count_closing_kg", "godown_diff_kg",
        "floor_opening_kg", "floor_closing_kg", "consumption_kg", "days_of_stock",
        "depts",
    }
    assert set(row["depts"][0]) == {
        "dept_id", "dept_desc", "floor_opening_kg", "issues_kg", "returns_kg",
        "floor_closing_kg", "consumption_kg",
    }


def test_two_items_total_across_rows_and_days_come_from_the_totals():
    other = dict(item_id=2, item_code="9HSWP", item_name="9 LBS HSWP", yarn_count=9.0)
    out = _example(
        purchase_rows=[_purchase(60300), _purchase(1000, **other)],
        movement_rows=[
            _move(WEAVING, "WEAVING", "ISSUE", 18000),
            _move(BEAMING, "BEAMING", "ISSUE", 4000),
            _move(WEAVING, "WEAVING", "RETURN", 1500),
            _move(WEAVING, "WEAVING", "ISSUE", 300, **other),
        ],
    )
    assert [r["item_name"] for r in out["rows"]] == ["13 LBS SKWP", "9 LBS HSWP"]
    t = out["totals"]
    assert t["purchases_kg"] == 61300.0
    assert t["issues_kg"] == 22300.0
    assert t["consumption_kg"] == 18000.0           # 17,700 + 300 (no count -> 0 floor)
    assert t["floor_closing_kg"] == 4000.0
    assert t["days_of_stock"] == round(4000 / (18000 / 30), 1)
    assert t["godown_book_closing_kg"] == 41800.0 + 700.0
    # the second item was never counted in the godown on the closing date: 0, not null
    assert out["rows"][1]["godown_count_closing_kg"] == 0.0
    assert out["rows"][1]["godown_diff_kg"] == -700.0


# ------------------------------------------------------------ opening basis


def test_opening_basis_is_book_when_no_godown_count_precedes_the_month():
    out = _example(
        godown_opening_rows=[],
        book_rows=[_lbl(purchases_kg=5000, issues_kg=2000, returns_kg=500)],
    )
    assert out["meta"]["opening_basis"] == YARN_LEDGER_OPENING_BOOK
    assert out["meta"]["godown_opening_count_date"] is None
    row = out["rows"][0]
    assert row["godown_opening_kg"] == 3500.0
    assert row["godown_book_closing_kg"] == 3500.0 + 60300 - 22000 + 1500


def test_count_basis_ignores_the_document_book_before_the_month():
    """A count exists: it is the opening, whatever the documents say."""
    out = _example(book_rows=[_lbl(purchases_kg=5000, issues_kg=2000, returns_kg=500)])
    assert out["meta"]["opening_basis"] == YARN_LEDGER_OPENING_COUNT
    assert out["rows"][0]["godown_opening_kg"] == 2000.0


def test_book_basis_item_with_only_a_pre_month_balance_still_gets_a_row():
    out = _example(
        purchase_rows=[], movement_rows=[], godown_opening_rows=[],
        godown_closing_rows=[], floor_opening_rows=[], floor_closing_rows=[],
        book_rows=[_lbl(purchases_kg=5000, issues_kg=0, returns_kg=0)],
    )
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["godown_opening_kg"] == row["godown_book_closing_kg"] == 5000.0
    assert row["godown_count_closing_kg"] is None
    assert row["godown_diff_kg"] is None
    assert row["days_of_stock"] is None
    assert row["depts"] == []
    assert YARN_LEDGER_EXC_ITEM_IN_BOOK_NOT_COUNTED not in _codes(out)   # book basis: nothing to compare


def test_count_basis_keeps_a_book_only_item_and_flags_it():
    """The opening count listed 13 LBS SKWP only, but the document book also
    carries 3,500 kg of 9 LBS HSWP (bought, partly issued, never counted).
    Dropping it would lose stock; opening it at 0 would show a phantom
    negative book. It keeps its book opening and the ledger says so."""
    other = dict(item_id=2, item_code="9HSWP", item_name="9 LBS HSWP", yarn_count=9.0)
    out = _example(
        book_rows=[
            _lbl(purchases_kg=5000, issues_kg=2000, returns_kg=500),          # counted: ignored
            _lbl(purchases_kg=4000, issues_kg=500, returns_kg=0, **other),   # not counted: kept
        ],
    )
    assert out["meta"]["opening_basis"] == YARN_LEDGER_OPENING_COUNT
    assert [r["item_name"] for r in out["rows"]] == ["13 LBS SKWP", "9 LBS HSWP"]
    assert out["rows"][0]["godown_opening_kg"] == 2000.0                     # the count, not 3,500
    hswp = out["rows"][1]
    assert hswp["godown_opening_kg"] == hswp["godown_book_closing_kg"] == 3500.0
    assert hswp["godown_count_closing_kg"] == 0.0        # a closing count exists; it had none
    assert hswp["godown_diff_kg"] == -3500.0
    flagged = [e for e in out["exceptions"] if e["code"] == YARN_LEDGER_EXC_ITEM_IN_BOOK_NOT_COUNTED]
    assert len(flagged) == 1
    assert flagged[0]["item_id"] == 2 and flagged[0]["value"] == 3500.0
    assert "2026-08-31" in flagged[0]["message"] and "9 LBS HSWP" in flagged[0]["message"]
    assert out["totals"]["godown_opening_kg"] == 5500.0


def test_count_basis_book_only_item_with_a_zero_balance_is_not_flagged():
    other = dict(item_id=2, item_code="9HSWP", item_name="9 LBS HSWP", yarn_count=9.0)
    out = _example(book_rows=[_lbl(purchases_kg=4000, issues_kg=4000, returns_kg=0, **other)])
    assert len(out["rows"]) == 1
    assert YARN_LEDGER_EXC_ITEM_IN_BOOK_NOT_COUNTED not in _codes(out)


# ------------------------------------------------- closing counts: in-period only


def test_closing_counts_dated_before_the_month_are_not_closings():
    """A godown count of 31 Aug and a beaming floor count of 20 Aug reach
    build_ledger as 'closing' rows (they would, without the SQL lower bound):
    the godown has NO closing count (null, no GODOWN_DIFF — an in-progress
    month must not report its own net movement as a difference), beaming
    closes at 0 and is flagged, and weaving's real 30 Sep count still counts."""
    out = _example(
        godown_closing_rows=[_count(2000, date(2026, 8, 31))],
        floor_closing_rows=[
            _count(3100, date(2026, 9, 30), WEAVING, "WEAVING"),
            _count(700, date(2026, 8, 20), BEAMING, "BEAMING"),
        ],
    )
    row = out["rows"][0]
    assert row["godown_count_closing_kg"] is None
    assert row["godown_diff_kg"] is None
    assert out["totals"]["godown_count_closing_kg"] is None
    assert out["meta"]["godown_closing_count_date"] is None
    assert out["meta"]["floor_count_dates"] == {WEAVING: "2026-09-30"}
    beaming = next(d for d in row["depts"] if d["dept_id"] == BEAMING)
    assert beaming["floor_closing_kg"] == 0.0 and beaming["consumption_kg"] == 4000.0
    assert _codes(out) == [YARN_LEDGER_EXC_DEPT_NO_FLOOR_COUNT]
    assert out["exceptions"][0]["dept_id"] == BEAMING
    assert "in the period" in out["exceptions"][0]["message"]


def test_department_last_counted_before_the_month_is_flagged():
    """Beaming was counted on 20 Aug only. That count is its OPENING; there
    is no closing, so DEPT_NO_FLOOR_COUNT fires even though a count exists."""
    out = _example(
        floor_opening_rows=[
            _count(1200, date(2026, 8, 31), WEAVING, "WEAVING"),
            _count(700, date(2026, 8, 20), BEAMING, "BEAMING"),
        ],
        floor_closing_rows=[_count(3100, date(2026, 9, 30), WEAVING, "WEAVING")],
    )
    beaming = next(d for d in out["rows"][0]["depts"] if d["dept_id"] == BEAMING)
    assert beaming["floor_opening_kg"] == 700.0
    assert beaming["floor_closing_kg"] == 0.0
    assert beaming["consumption_kg"] == 4700.0
    assert _codes(out) == [YARN_LEDGER_EXC_DEPT_NO_FLOOR_COUNT]
    assert out["exceptions"][0]["dept_id"] == BEAMING


def test_pre_phase4_floor_rows_do_not_stand_in_as_a_closing():
    """Rows saved before stock_loc existed are FLOOR with dept NULL, dated
    whenever they were keyed. One from August must not become September's
    '(no department)' closing figure."""
    out = _example(
        floor_closing_rows=[
            _count(3100, date(2026, 9, 30), WEAVING, "WEAVING"),
            _count(900, date(2026, 9, 30), BEAMING, "BEAMING"),
            _count(5000, date(2026, 8, 15), None, None),
        ],
    )
    assert [d["dept_id"] for d in out["rows"][0]["depts"]] == [BEAMING, WEAVING]
    assert out["rows"][0]["floor_closing_kg"] == 4000.0
    assert None not in out["meta"]["floor_count_dates"]
    assert out["exceptions"] == []


# ---------------------------------------------------------- days in the window


def test_days_in_window_is_bounded_by_the_last_count_or_today():
    assert days_in_window(FROM, TO, date(2026, 9, 30), TODAY) == 30
    assert days_in_window(FROM, TO, date(2026, 9, 15), TODAY) == 15     # counted mid-month
    assert days_in_window(FROM, TO, None, date(2026, 9, 26)) == 26      # in progress, no count
    assert days_in_window(FROM, TO, None, TODAY) == 30                  # over, never counted
    assert days_in_window(FROM, TO, date(2026, 9, 10), date(2026, 9, 5)) == 10  # count wins
    assert days_in_window(FROM, TO, None, date(2026, 8, 1)) == 1        # a future month: floor 1


def test_days_of_stock_in_an_in_progress_month_uses_the_days_counted_so_far():
    """Counted on 15 Sep: 4,000 kg on the floor against 17,700 kg consumed in
    FIFTEEN days (1,180 kg/day) = 3.4 days, not 6.8 as over a full month."""
    out = _example(
        godown_closing_rows=[_count(41600, date(2026, 9, 15))],
        floor_closing_rows=[
            _count(3100, date(2026, 9, 15), WEAVING, "WEAVING"),
            _count(900, date(2026, 9, 15), BEAMING, "BEAMING"),
        ],
        today=date(2026, 9, 26),
    )
    assert out["meta"]["days_in_window"] == 15
    assert out["meta"]["godown_closing_count_date"] == "2026-09-15"
    assert out["rows"][0]["days_of_stock"] == round(4000 / (17700 / 15), 1) == 3.4
    assert out["totals"]["days_of_stock"] == 3.4


def test_days_in_window_takes_the_latest_count_date_godown_or_floor():
    """Godown counted on the 10th, weaving floor on the 20th: the window
    runs to the 20th."""
    out = _example(
        godown_closing_rows=[_count(41600, date(2026, 9, 10))],
        floor_closing_rows=[
            _count(3100, date(2026, 9, 20), WEAVING, "WEAVING"),
            _count(900, date(2026, 9, 12), BEAMING, "BEAMING"),
        ],
        today=date(2026, 9, 26),
    )
    assert out["meta"]["days_in_window"] == 20


def test_days_in_window_falls_back_to_today_when_nothing_is_counted_yet():
    """26 Sep, no count keyed this month: issues so far are spread over 26
    days; the floor closes at 0 so days of stock is 0.0 and the departments
    are flagged."""
    out = _example(
        godown_closing_rows=[], floor_closing_rows=[], today=date(2026, 9, 26),
    )
    assert out["meta"]["days_in_window"] == 26
    assert out["rows"][0]["consumption_kg"] == 1200 + 22000 - 1500
    assert out["rows"][0]["days_of_stock"] == 0.0
    assert sorted(_codes(out)) == [YARN_LEDGER_EXC_DEPT_NO_FLOOR_COUNT] * 2


# ---------------------------------------------------------------- empty month


def test_empty_month_returns_zero_totals_and_null_counts():
    out = _example(
        purchase_rows=[], book_rows=[], movement_rows=[], godown_opening_rows=[],
        godown_closing_rows=[], floor_opening_rows=[], floor_closing_rows=[],
    )
    assert out["rows"] == []
    assert out["exceptions"] == []
    assert out["meta"] == {
        "godown_opening_count_date": None,
        "godown_closing_count_date": None,
        "floor_count_dates": {},
        "opening_basis": YARN_LEDGER_OPENING_BOOK,
        "days_in_window": 30,               # TODAY is past the month: the whole of it
    }
    t = out["totals"]
    assert t["godown_count_closing_kg"] is None
    assert t["godown_diff_kg"] is None
    assert t["days_of_stock"] is None
    for k in LEDGER_NUMERIC_KEYS:
        if k not in ("godown_count_closing_kg", "godown_diff_kg", "days_of_stock"):
            assert t[k] == 0.0, k


# ----------------------------------------------------------------- exceptions


def _codes(out):
    return [e["code"] for e in out["exceptions"]]


def test_godown_diff_exception_beyond_tolerance():
    """Tolerance = max(50 kg, 1 % of opening + purchases) = 623 kg here.
    A 1,800 kg shortfall is flagged; the worked example's 200 kg is not."""
    out = _example(godown_closing_rows=[_count(40000, date(2026, 9, 30))])
    assert _codes(out) == [YARN_LEDGER_EXC_GODOWN_DIFF]
    exc = out["exceptions"][0]
    assert exc["item_id"] == ITEM
    assert exc["value"] == -1800.0
    assert "1800" in exc["message"] or "1,800" in exc["message"]


def test_godown_diff_tolerance_floor_is_fifty_kg():
    """Tiny month: opening 0 + purchases 1,000 -> 1 % = 10 kg, so the 50 kg
    floor applies. 40 kg passes, 60 kg is flagged."""
    assert YARN_LEDGER_GODOWN_TOL_KG == 50.0
    base = dict(
        purchase_rows=[_purchase(1000)], movement_rows=[],
        godown_opening_rows=[_count(0, date(2026, 8, 31))],
        floor_opening_rows=[], floor_closing_rows=[],
    )
    ok = _example(godown_closing_rows=[_count(960, date(2026, 9, 30))], **base)
    bad = _example(godown_closing_rows=[_count(940, date(2026, 9, 30))], **base)
    assert YARN_LEDGER_EXC_GODOWN_DIFF not in _codes(ok)
    assert YARN_LEDGER_EXC_GODOWN_DIFF in _codes(bad)


def test_godown_book_negative_exception():
    out = _example(
        purchase_rows=[], godown_opening_rows=[], book_rows=[],
        movement_rows=[_move(WEAVING, "WEAVING", "ISSUE", 500)],
        godown_closing_rows=[], floor_opening_rows=[], floor_closing_rows=[],
    )
    assert YARN_LEDGER_EXC_GODOWN_BOOK_NEGATIVE in _codes(out)
    exc = next(e for e in out["exceptions"] if e["code"] == YARN_LEDGER_EXC_GODOWN_BOOK_NEGATIVE)
    assert exc["item_id"] == ITEM and exc["value"] == -500.0
    assert out["rows"][0]["godown_book_closing_kg"] == -500.0


def test_dept_with_issues_but_no_floor_count_is_flagged_and_reads_zero_closing():
    """Beaming drew 4,000 kg and was never counted: consumption falls to the
    whole 4,000 (closing 0 per the definition) AND the ledger says why."""
    out = _example(floor_closing_rows=[_count(3100, date(2026, 9, 30), WEAVING, "WEAVING")])
    assert _codes(out) == [YARN_LEDGER_EXC_DEPT_NO_FLOOR_COUNT]
    exc = out["exceptions"][0]
    assert exc["dept_id"] == BEAMING
    assert exc["value"] == 4000.0
    assert "item_id" not in exc or exc["item_id"] is None
    assert "no purchased-yarn floor count in the period" in exc["message"]
    assert out["meta"]["floor_count_dates"] == {WEAVING: "2026-09-30"}
    beaming = next(d for d in out["rows"][0]["depts"] if d["dept_id"] == BEAMING)
    assert beaming["floor_closing_kg"] == 0.0
    assert beaming["consumption_kg"] == 4000.0


def test_dept_with_only_returns_is_not_flagged_for_a_missing_count():
    out = _example(
        movement_rows=[_move(BEAMING, "BEAMING", "RETURN", 100)],
        floor_closing_rows=[],
    )
    assert YARN_LEDGER_EXC_DEPT_NO_FLOOR_COUNT not in _codes(out)


def test_floor_days_over_exception():
    """4,000 kg on the floor against 100 kg consumed in 30 days = 1,200 days."""
    assert YARN_LEDGER_FLOOR_DAYS_MAX == 15.0
    out = _example(
        movement_rows=[_move(WEAVING, "WEAVING", "ISSUE", 2900)],
        floor_opening_rows=[_count(1200, date(2026, 8, 31), WEAVING, "WEAVING")],
        floor_closing_rows=[_count(4000, date(2026, 9, 30), WEAVING, "WEAVING")],
        godown_closing_rows=[_count(59400, date(2026, 9, 30))],
    )
    row = out["rows"][0]
    assert row["consumption_kg"] == 100.0
    assert row["days_of_stock"] == 1200.0
    assert _codes(out) == [YARN_LEDGER_EXC_FLOOR_DAYS_OVER]
    assert out["exceptions"][0]["value"] == 1200.0
    assert out["exceptions"][0]["item_id"] == ITEM


def test_days_of_stock_is_null_when_nothing_was_consumed():
    out = _example(
        movement_rows=[], floor_opening_rows=[],
        floor_closing_rows=[_count(4000, date(2026, 9, 30), WEAVING, "WEAVING")],
        godown_closing_rows=[_count(62300, date(2026, 9, 30))],
    )
    row = out["rows"][0]
    assert row["consumption_kg"] == -4000.0        # counted more than was issued
    assert row["days_of_stock"] is None
    assert YARN_LEDGER_EXC_FLOOR_DAYS_OVER not in _codes(out)


def test_legacy_stores_receipts_exception_only_without_documents():
    """Stores receipts of the legacy yarn item exist and nothing was booked as
    a Yarn Purchase — the transition guard from §3.5."""
    out = _example(
        purchase_rows=[], movement_rows=[], godown_opening_rows=[],
        godown_closing_rows=[], floor_opening_rows=[], floor_closing_rows=[],
        legacy_stores_kg=6038.0,
    )
    assert _codes(out) == [YARN_LEDGER_EXC_LEGACY_STORES_RECEIPTS]
    assert out["exceptions"][0]["value"] == 6038.0
    assert "6.038 MT" in out["exceptions"][0]["message"]
    # documents exist -> no exception
    assert YARN_LEDGER_EXC_LEGACY_STORES_RECEIPTS not in _codes(_example(legacy_stores_kg=6038.0))


def test_legacy_stores_receipts_in_an_unrecognised_unit_are_flagged():
    """sls: the legacy item is booked in MTR, which purchase_qty_to_mt refuses
    to convert — the converted figure is 0 kg, so the tonnage rule alone
    would let the month pass. The unit names keep the guard honest; the
    value is None because the tonnage is exactly what is unknown."""
    out = _example(
        purchase_rows=[], movement_rows=[], godown_opening_rows=[],
        godown_closing_rows=[], floor_opening_rows=[], floor_closing_rows=[],
        legacy_stores_kg=0.0, legacy_unknown_uoms=["MTR"],
    )
    assert _codes(out) == [YARN_LEDGER_EXC_LEGACY_STORES_RECEIPTS]
    exc = out["exceptions"][0]
    assert exc["value"] is None
    assert exc["message"] == (
        "Stores receipts of yarn-type items exist in unrecognised unit(s) MTR "
        "— book them as Yarn Purchase documents"
    )
    # documents exist too: the MTR receipts are still unaccounted for
    out = _example(legacy_unknown_uoms=["MTR"])
    assert YARN_LEDGER_EXC_LEGACY_STORES_RECEIPTS in _codes(out)
    # both gaps at once: one exception carrying the tonnage
    out = _example(
        purchase_rows=[], movement_rows=[], godown_opening_rows=[],
        godown_closing_rows=[], floor_opening_rows=[], floor_closing_rows=[],
        legacy_stores_kg=6038.0, legacy_unknown_uoms=["MTR", "PCS"],
    )
    assert _codes(out) == [YARN_LEDGER_EXC_LEGACY_STORES_RECEIPTS]
    assert out["exceptions"][0]["value"] == 6038.0
    assert "6.038 MT" in out["exceptions"][0]["message"]
    assert "MTR, PCS" in out["exceptions"][0]["message"]


def test_legacy_stores_exception_is_silent_before_the_cutover():
    assert YARN_PURCHASE_CUTOVER_DATE == date(2026, 4, 1)
    out = _example(
        from_date=date(2026, 3, 1), to_date=date(2026, 3, 31),
        purchase_rows=[], movement_rows=[], godown_opening_rows=[],
        godown_closing_rows=[], floor_opening_rows=[], floor_closing_rows=[],
        legacy_stores_kg=6038.0, legacy_unknown_uoms=["MTR"],
    )
    assert out["exceptions"] == []


def test_item_without_yarn_master_exception():
    out = _example(purchase_rows=[_purchase(60300, has_master=0)])
    assert YARN_LEDGER_EXC_ITEM_WITHOUT_YARN_MASTER in _codes(out)
    exc = next(e for e in out["exceptions"] if e["code"] == YARN_LEDGER_EXC_ITEM_WITHOUT_YARN_MASTER)
    assert exc["item_id"] == ITEM


def test_every_exception_has_the_documented_shape():
    out = _example(
        purchase_rows=[_purchase(60300, has_master=0)],
        godown_closing_rows=[_count(40000, date(2026, 9, 30))],
        floor_closing_rows=[],
    )
    assert len(out["exceptions"]) >= 3
    for e in out["exceptions"]:
        assert {"code", "message"} <= set(e)
        assert set(e) <= {"code", "message", "item_id", "dept_id", "value"}
        assert isinstance(e["message"], str) and e["message"]


# -------------------------------------------------------------- month window


def test_month_window_covers_the_whole_calendar_month():
    assert month_window("2026-09") == (date(2026, 9, 1), date(2026, 9, 30))
    assert month_window("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_window("2028-02") == (date(2028, 2, 1), date(2028, 2, 29))
    assert month_window("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))


@pytest.mark.parametrize("bad", ["2026", "2026-13", "2026-9", "09-2026", "2026-09-01", "", "abc"])
def test_month_window_rejects_malformed_months(bad):
    with pytest.raises(ValueError):
        month_window(bad)


# -------------------------------------------------------------- SQL contracts

WINDOW = {"co_id", "branch_id", "date_from", "date_to"}
BEFORE = {"co_id", "branch_id", "before"}
IN_MONTH = {"co_id", "branch_id", "date_from", "before"}


def test_ledger_builders_construct_and_declare_their_binds():
    expect = {
        "get_yarn_ledger_purchase_rows_query": WINDOW,
        "get_yarn_ledger_movement_rows_query": WINDOW,
        "get_yarn_ledger_book_opening_query": BEFORE,
        "get_yarn_ledger_godown_count_query": BEFORE,
        "get_yarn_ledger_godown_closing_count_query": IN_MONTH,
        "get_yarn_ledger_floor_count_query": BEFORE,
        "get_yarn_ledger_floor_closing_count_query": IN_MONTH,
    }
    for name, binds in expect.items():
        stmt = getattr(q, name)()
        assert stmt is not None, name
        assert set(stmt.compile().binds) == binds, name


def test_purchase_rows_are_approved_documents_with_a_yarn_master_probe():
    sql = str(q.get_yarn_ledger_purchase_rows_query())
    assert "jute_yarn_purchase_li" in sql
    assert "h.status_id = 3" in sql
    assert "h.co_id = :co_id" in sql and "h.branch_id = :branch_id" in sql
    assert "h.tran_date BETWEEN :date_from AND :date_to" in sql
    assert "has_yarn_master" in sql and "jute_yarn_mst" in sql
    assert "GROUP BY" in sql


def test_movement_rows_exclude_rejected_cancelled_and_draft():
    """Same tuple as vw_yarn_godown_outstanding: status NOT IN (4, 6, 21)."""
    sql = str(q.get_yarn_ledger_movement_rows_query())
    assert "jute_yarn_issue_li" in sql
    assert "i.status_id NOT IN (4, 6, 21)" in sql
    assert "i.issue_type" in sql and "i.dept_id" in sql and "dm.dept_desc" in sql
    assert "i.tran_date BETWEEN :date_from AND :date_to" in sql


def test_book_opening_nets_purchases_issues_and_returns_before_the_date():
    sql = str(q.get_yarn_ledger_book_opening_query())
    assert "h.tran_date < :before" in sql
    assert "i.tran_date < :before" in sql
    assert "h.status_id = 3" in sql
    assert "i.status_id NOT IN (4, 6, 21)" in sql
    assert "purchases_kg" in sql and "issues_kg" in sql and "returns_kg" in sql


def test_godown_count_reads_purchase_bucket_godown_rows_on_the_last_date():
    sql = str(q.get_yarn_ledger_godown_count_query())
    assert f"ws.bucket = '{WINDING_STOCK_BUCKET_PURCHASE}'" in sql
    assert f"ws.stock_loc = '{WINDING_STOCK_LOC_GODOWN}'" in sql
    assert "MAX(" in sql and "tran_date < :before" in sql
    assert "no_of_samples * ws.wt_per_sample" in sql
    assert "ws.active = 1" in sql
    assert ":date_from" not in sql            # opening: the last count BEFORE the month


def test_godown_closing_count_is_bounded_to_the_period():
    """The closing MAX must not reach back before the month: a count from
    August would otherwise become September's closing and show the month's
    whole net movement as a godown difference."""
    sql = str(q.get_yarn_ledger_godown_closing_count_query())
    assert "x.tran_date < :before" in sql
    assert "x.tran_date >= :date_from" in sql
    assert sql.index("MAX(") < sql.index(":date_from")   # inside the MAX subquery


def test_floor_count_reads_each_departments_own_last_date():
    sql = str(q.get_yarn_ledger_floor_count_query())
    assert f"ws.bucket = '{WINDING_STOCK_BUCKET_PURCHASE}'" in sql
    assert f"ws.stock_loc = '{WINDING_STOCK_LOC_FLOOR}'" in sql
    assert "GROUP BY dept_id" in sql          # one last-date per department
    assert "<=> ws.dept_id" in sql            # legacy NULL-dept floor rows still match
    assert "tran_date < :before" in sql
    assert ":date_from" not in sql


def test_floor_closing_count_is_bounded_to_the_period_per_department():
    """A department last counted before the month has NO closing row, so
    the ledger raises DEPT_NO_FLOOR_COUNT instead of reusing the stale count."""
    sql = str(q.get_yarn_ledger_floor_closing_count_query())
    assert "tran_date < :before" in sql
    assert "tran_date >= :date_from" in sql
    assert sql.index("MAX(tran_date)") < sql.index(":date_from") < sql.index("GROUP BY dept_id")


# ------------------------------------------------------------------ endpoint


def _mock_row(mapping):
    r = MagicMock()
    r._mapping = mapping
    return r


def _res(*, fetchall=None, fetchone=None, first=None):
    r = MagicMock()
    r.fetchall.return_value = [] if fetchall is None else fetchall
    r.fetchone.return_value = fetchone
    r.first.return_value = first
    return r


def _attr(**attrs):
    o = MagicMock()
    for k, v in attrs.items():
        setattr(o, k, v)
    return o


URL = "/api/juteProductionReports/purchased_yarn_ledger"


class TestPurchasedYarnLedgerEndpoint:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self.session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.session
        yield
        app.dependency_overrides.clear()

    def test_requires_co_branch_and_month(self):
        assert client.get(f"{URL}?branch_id=29&month=2026-09").status_code == 400
        assert client.get(f"{URL}?co_id=2&month=2026-09").status_code == 400
        assert client.get(f"{URL}?co_id=2&branch_id=29").status_code == 400
        assert client.get(f"{URL}?co_id=2&branch_id=29&month=2026-9").status_code == 400
        assert client.get(f"{URL}?co_id=2&branch_id=29&month=2026-09-01").status_code == 400
        assert self.session.execute.call_count == 0

    def test_rejects_a_branch_of_another_company(self):
        self.session.execute.side_effect = [_res(first=None)]
        resp = client.get(f"{URL}?co_id=2&branch_id=87&month=2026-09")
        assert resp.status_code == 400
        assert "branch" in resp.json()["detail"].lower()

    def test_worked_example_through_the_router(self):
        """Statements in handler order: pair check, purchases, book opening,
        movements, godown opening, godown closing, floor opening, floor
        closing, legacy Stores."""
        lbl = {"item_id": ITEM, "item_code": "13SKWP", "item_name": "13 LBS SKWP",
               "yarn_count": 13.0}
        self.session.execute.side_effect = [
            _res(first=(1,)),
            _res(fetchall=[_mock_row({**lbl, "kg": 60300, "has_yarn_master": 1})]),
            _res(fetchall=[]),
            _res(fetchall=[
                _mock_row({**lbl, "dept_id": WEAVING, "dept_desc": "WEAVING",
                           "issue_type": "ISSUE", "kg": 18000}),
                _mock_row({**lbl, "dept_id": BEAMING, "dept_desc": "BEAMING",
                           "issue_type": "ISSUE", "kg": 4000}),
                _mock_row({**lbl, "dept_id": WEAVING, "dept_desc": "WEAVING",
                           "issue_type": "RETURN", "kg": 1500}),
            ]),
            _res(fetchall=[_mock_row({**lbl, "tran_date": date(2026, 8, 31), "kg": 2000})]),
            _res(fetchall=[_mock_row({**lbl, "tran_date": date(2026, 9, 30), "kg": 41600})]),
            _res(fetchall=[_mock_row({**lbl, "dept_id": WEAVING, "dept_desc": "WEAVING",
                                      "tran_date": date(2026, 8, 31), "kg": 1200})]),
            _res(fetchall=[
                _mock_row({**lbl, "dept_id": WEAVING, "dept_desc": "WEAVING",
                           "tran_date": date(2026, 9, 30), "kg": 3100}),
                _mock_row({**lbl, "dept_id": BEAMING, "dept_desc": "BEAMING",
                           "tran_date": date(2026, 9, 30), "kg": 900}),
            ]),
            _res(fetchone=_attr(purchase_qty=0, uoms=None)),
        ]
        resp = client.get(f"{URL}?co_id=2&branch_id=29&month=2026-09")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert set(data) == {"month", "from_date", "to_date", "meta", "rows", "totals", "exceptions"}
        assert data["month"] == "2026-09"
        assert data["from_date"] == "2026-09-01" and data["to_date"] == "2026-09-30"
        row = data["rows"][0]
        assert row["godown_book_closing_kg"] == 41800.0
        assert row["godown_diff_kg"] == -200.0
        assert row["consumption_kg"] == 17700.0
        assert data["meta"]["floor_count_dates"] == {"7": "2026-09-30", "9": "2026-09-30"}
        assert data["exceptions"] == []

        calls = self.session.execute.call_args_list
        assert len(calls) == 9
        for c in calls[1:]:
            missing = set(c.args[0].compile().binds) - set(c.args[1])
            assert not missing, missing
        window = {"co_id": 2, "branch_id": 29, "date_from": date(2026, 9, 1), "date_to": date(2026, 9, 30)}
        assert calls[1].args[1] == window          # purchases
        assert calls[3].args[1] == window          # movements
        assert calls[8].args[1] == window          # legacy Stores cross-check
        before = {"co_id": 2, "branch_id": 29, "before": date(2026, 9, 1)}
        in_month = {"co_id": 2, "branch_id": 29, "date_from": date(2026, 9, 1),
                    "before": date(2026, 10, 1)}
        assert calls[2].args[1] == before          # book opening: < from
        assert calls[4].args[1] == before          # godown opening: < from
        assert calls[5].args[1] == in_month        # godown closing: from <= d <= to
        assert calls[6].args[1] == before          # floor opening
        assert calls[7].args[1] == in_month        # floor closing
        assert ":date_from" in str(calls[5].args[0]) and ":date_from" in str(calls[7].args[0])
        assert ":date_from" not in str(calls[4].args[0])
        assert data["meta"]["days_in_window"] == 30

    def test_empty_month_through_the_router(self):
        self.session.execute.side_effect = [_res(first=(1,))] + [_res()] * 7 + [
            _res(fetchone=_attr(purchase_qty=0, uoms=None))
        ]
        resp = client.get(f"{URL}?co_id=2&branch_id=29&month=2026-09")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["rows"] == [] and data["exceptions"] == []
        assert data["meta"]["opening_basis"] == YARN_LEDGER_OPENING_BOOK

    def test_legacy_stores_figure_raises_the_transition_exception(self):
        self.session.execute.side_effect = [_res(first=(1,))] + [_res()] * 7 + [
            _res(fetchone=_attr(purchase_qty=6.038, uoms="MT"))
        ]
        resp = client.get(f"{URL}?co_id=2&branch_id=29&month=2026-09")
        assert resp.status_code == 200, resp.text
        exc = resp.json()["data"]["exceptions"]
        assert [e["code"] for e in exc] == [YARN_LEDGER_EXC_LEGACY_STORES_RECEIPTS]
        assert exc[0]["value"] == 6038.0

    def test_legacy_stores_figure_in_metres_still_raises_the_transition_exception(self):
        """sls: 654.608 MTR of the legacy item. Converted tonnage is 0 (MTR
        is a length), which used to slip through the guard unnoticed."""
        self.session.execute.side_effect = [_res(first=(1,))] + [_res()] * 7 + [
            _res(fetchone=_attr(purchase_qty=654.608, uoms="MTR"))
        ]
        resp = client.get(f"{URL}?co_id=2&branch_id=29&month=2026-09")
        assert resp.status_code == 200, resp.text
        exc = resp.json()["data"]["exceptions"]
        assert [e["code"] for e in exc] == [YARN_LEDGER_EXC_LEGACY_STORES_RECEIPTS]
        assert exc[0]["value"] is None
        assert "MTR" in exc[0]["message"]

    def test_zero_legacy_quantity_never_raises_even_with_a_unit_name(self):
        self.session.execute.side_effect = [_res(first=(1,))] + [_res()] * 7 + [
            _res(fetchone=_attr(purchase_qty=0, uoms="MTR"))
        ]
        resp = client.get(f"{URL}?co_id=2&branch_id=29&month=2026-09")
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["exceptions"] == []


def test_ledger_route_is_registered_under_production_reports():
    paths = {r.path for r in app.routes}
    assert "/api/juteProductionReports/purchased_yarn_ledger" in paths

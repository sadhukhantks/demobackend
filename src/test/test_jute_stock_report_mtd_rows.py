"""
Jute Stock report: an item with only month-to-date movement keeps its row.

The defect (2026-08-13 .. 2026-10-05): the row-presence test looked at the five
day figures — opening, receipt, issue, sold, closing — and not at the two MTD
columns the report also prints. An item received and used up (or sold off)
earlier in the month has all five at zero on the report date, so its row was
dropped and its MTD receipt / issue vanished from every total summed off the
rows: the report's Grand Total and cards, and the Daily Production Report's
jute section. sls branch 29 on 2026-09-30 printed an MTD issue of 360,660 kg
against 455,631 kg actually issued (7 items hidden).

What is locked down here:

1. The default query keeps a row when ANY printed column is non-zero; a row
   whose seven columns are all zero is still hidden.
2. MTD totals off the rows equal the un-hidden issue / receipt legs.
3. The day figures and the position do not change.
4. The Issue Planner still asks for position rows only (its godown cover must
   not list a quality just because it moved this month); the report endpoint
   and the Daily Production Report use the default.

The real SQL is executed on SQLite; only MySQL's month-start date arithmetic is
swapped for SQLite's.
"""

import inspect
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects import mysql

from src.juteProcurement import issuePlanner
from src.juteProcurement.constants import RAW_JUTE_INVOICE_TYPE_ID, SALES_APPROVED_STATUS_ID
from src.juteProcurement.reportQueries import (
    _issue_cte,
    _receipt_cte,
    _sold_cte,
    get_jute_stock_report_query,
)
from src.juteProcurement.reports import get_jute_stock_report
from src.juteProduction import dpr_reports
from src.juteProduction.dpr_reports import build_jute_section

MTD_RECEIPT_TEST = "OR ROUND(COALESCE(rm.wt, 0), 2) <> 0"
MTD_ISSUE_TEST = "OR ROUND(COALESCE(im2.wt, 0), 2) <> 0"

MYSQL_MONTH_START = "DATE_SUB(:report_date, INTERVAL DAYOFMONTH(:report_date) - 1 DAY)"
SQLITE_MONTH_START = "date(:report_date, 'start of month')"

POSITION = ("opening_weight", "receipt_weight", "issue_weight", "sold_weight", "closing_weight")


def _flat(sql) -> str:
    return " ".join(str(sql).split())


# ---------------------------------------------------------------------------
# SQL shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("basis", ["actual", "accounts"])
def test_default_presence_test_covers_every_printed_column(basis):
    sql = _flat(get_jute_stock_report_query(basis))
    where = sql.rpartition(" WHERE (")[2]
    for expr in (
        "ROUND((COALESCE(rb.wt, 0) - COALESCE(ib.wt, 0) - COALESCE(sb.wt, 0)), 2) <> 0",   # opening
        "OR ROUND(COALESCE(ro.wt, 0), 2) <> 0",                                             # receipt
        "OR ROUND(COALESCE(io.wt, 0), 2) <> 0",                                             # issue
        "OR ROUND(COALESCE(so.wt, 0), 2) <> 0",                                             # sold
        MTD_RECEIPT_TEST,
        MTD_ISSUE_TEST,
    ):
        assert expr in where, f"{basis}: presence test misses {expr}"
    # ...and the MTD columns it tests are the ones it prints.
    assert "ROUND(COALESCE(rm.wt, 0), 3) AS mtd_receipt_weight" in sql
    assert "ROUND(COALESCE(im2.wt, 0), 3) AS mtd_issue_weight" in sql


@pytest.mark.parametrize("basis", ["actual", "accounts"])
def test_position_only_variant_differs_by_the_two_mtd_tests_and_nothing_else(basis):
    full = str(get_jute_stock_report_query(basis))
    position = str(get_jute_stock_report_query(basis, mtd_rows=False))
    assert MTD_RECEIPT_TEST not in position.rpartition("WHERE (")[2]
    assert MTD_ISSUE_TEST not in position.rpartition("WHERE (")[2]
    removed = f"\n            {MTD_RECEIPT_TEST}\n            {MTD_ISSUE_TEST}"
    assert full.count(removed) == 1
    assert full.replace(removed, "") == position


def test_both_variants_compile_as_mysql_with_only_the_caller_params():
    for mtd_rows in (True, False):
        stmt = get_jute_stock_report_query(mtd_rows=mtd_rows)
        compiled = stmt.compile(dialect=mysql.dialect())
        assert set(compiled.params) - set(stmt._bindparams) <= {"branch_id", "report_date"}


# ---------------------------------------------------------------------------
# Executed on an in-memory ledger
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger():
    """Branch 29, report date 2026-09-30.

    item 10 D TD-5     in stock: 3,000 kg from August, 200 issued 12 Sep, 500 on the day
    item 20 N TD-10    received 5 Sep (1,190) and fully issued 12 Sep   -> MTD only
    item 30 A TD-6 (L) received in August (1,400), fully issued 3 Sep   -> MTD issue only
    item 40 OLD        received and fully issued in July                -> nothing to print
    item 50 SOLD OFF   received 2 Sep (600), sold on an approved raw-jute
                       invoice 10 Sep                                   -> MTD receipt only
    item 60 CADDIES    process waste: 56 kg booked 8 Sep, re-issued 9 Sep -> MTD only, is_waste
    """
    eng = create_engine("sqlite://")

    @event.listens_for(eng, "connect")
    def _mysql_functions(dbapi_conn, _record):
        # CONCAT is built in only from SQLite 3.44.
        dbapi_conn.create_function("CONCAT", -1, lambda *a: "".join("" if x is None else str(x) for x in a))

    with eng.begin() as c:
        c.execute(text("CREATE TABLE item_grp_mst (item_grp_id INT, item_grp_name TEXT, parent_grp_id INT, item_type_id INT)"))
        c.execute(text("CREATE TABLE item_mst (item_id INT, item_grp_id INT, item_name TEXT)"))
        c.execute(text("CREATE TABLE jute_mr (jute_mr_id INT, branch_id INT, out_date TEXT, "
                       "jute_mr_date TEXT, jute_gate_entry_date TEXT, status_id INT)"))
        c.execute(text("CREATE TABLE jute_mr_li (jute_mr_li_id INT, jute_mr_id INT, actual_item_id INT, "
                       "actual_qty REAL, actual_weight REAL, accepted_weight REAL, rate REAL, "
                       "total_price REAL, active INT, status TEXT)"))
        c.execute(text("CREATE TABLE dept_mst (dept_id INT, branch_id INT)"))
        c.execute(text("CREATE TABLE jute_prod_waste_entry (waste_entry_id INT, item_id INT, "
                       "waste_qty REAL, dept_id INT, branch_id INT, tran_date TEXT, active INT)"))
        c.execute(text("CREATE TABLE jute_issue (jute_issue_id INT, branch_id INT, issue_date TEXT, "
                       "jute_mr_li_id INT, waste_entry_id INT, item_id INT, quantity REAL, "
                       "weight REAL, issue_value REAL, status_id INT)"))
        c.execute(text("CREATE TABLE sales_invoice (invoice_id INT, branch_id INT, invoice_type INT, "
                       "status_id INT, active INT, invoice_date TEXT)"))
        c.execute(text("CREATE TABLE sales_invoice_dtl (invoice_id INT, item_id INT, sales_weight REAL, "
                       "quantity REAL, jute_mr_li_id INT)"))

        c.execute(text("INSERT INTO item_grp_mst VALUES (1, 'JUTE', NULL, 2), (11, 'DESI', 1, 2), "
                       "(12, 'NORTHERN', 1, 2), (42, 'JUTE WASTE', NULL, 3)"))
        c.execute(text("INSERT INTO item_mst VALUES (10, 11, 'D TD-5'), (20, 12, 'N TD-10'), "
                       "(30, 11, 'A TD-6 (L)'), (40, 11, 'OLD'), (50, 12, 'SOLD OFF'), (60, 42, 'CADDIES')"))
        mrs = [  # id, received on
            (1, "2026-08-20"), (2, "2026-09-05"), (3, "2026-08-10"), (4, "2026-07-01"), (5, "2026-09-02"),
        ]
        for mr_id, d in mrs:
            c.execute(text("INSERT INTO jute_mr VALUES (:i, 29, :d, :d, :d, 3)"), {"i": mr_id, "d": d})
        lines = [  # li id, mr id, item, kg
            (101, 1, 10, 3000), (102, 2, 20, 1190), (103, 3, 30, 1400), (104, 4, 40, 800), (105, 5, 50, 600),
        ]
        for li, mr_id, item, kg in lines:
            c.execute(
                text("INSERT INTO jute_mr_li VALUES (:li, :mr, :item, :q, :kg, :kg, 5000, :val, 1, NULL)"),
                {"li": li, "mr": mr_id, "item": item, "q": kg / 150, "kg": kg, "val": kg * 50},
            )
        c.execute(text("INSERT INTO dept_mst VALUES (7, 29)"))
        c.execute(text("INSERT INTO jute_prod_waste_entry VALUES (901, 60, 56, 7, 29, '2026-09-08', 1)"))
        issues = [  # id, date, mr line, waste entry, item, kg, status
            (1, "2026-09-12", 101, None, 10, 200, 3),
            (2, "2026-09-30", 101, None, 10, 500, 21),
            (3, "2026-09-12", 102, None, 20, 1190, 3),
            (4, "2026-09-03", 103, None, 30, 1400, 3),
            (5, "2026-07-20", 104, None, 40, 800, 3),
            (6, "2026-09-09", None, 901, 60, 56, 3),
        ]
        for i, d, li, waste, item, kg, status in issues:
            c.execute(
                text("INSERT INTO jute_issue VALUES (:i, 29, :d, :li, :w, :item, :q, :kg, :val, :s)"),
                {"i": i, "d": d, "li": li, "w": waste, "item": item, "q": kg / 150, "kg": kg, "val": kg * 50, "s": status},
            )
        c.execute(
            text("INSERT INTO sales_invoice VALUES (1, 29, :t, :s, 1, '2026-09-10')"),
            {"t": RAW_JUTE_INVOICE_TYPE_ID, "s": SALES_APPROVED_STATUS_ID},
        )
        c.execute(text("INSERT INTO sales_invoice_dtl VALUES (1, 50, 600, 600, 105)"))
    return eng


BINDS = {
    "branch_id": 29,
    "report_date": "2026-09-30",
    "raw_jute_invoice_type_id": RAW_JUTE_INVOICE_TYPE_ID,
    "approved_status_id": SALES_APPROVED_STATUS_ID,
}


def _run(ledger, **kwargs):
    """The real report SQL, with MySQL's month-start arithmetic swapped for SQLite's."""
    sql = str(get_jute_stock_report_query(**kwargs))
    assert sql.count(MYSQL_MONTH_START) == 4, "receipt_mtd + issue_mtd, two legs each"
    sql = sql.replace(MYSQL_MONTH_START, SQLITE_MONTH_START)
    with ledger.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), BINDS)]


def _leg_total(ledger, cte):
    with ledger.connect() as c:
        return c.execute(text(f"SELECT COALESCE(SUM(wt), 0) FROM ({cte}) x"), {**BINDS, "month_start": "2026-09-01"}).scalar()


MONTH = "{d} BETWEEN :month_start AND :report_date"


def test_executed_item_used_up_earlier_in_the_month_keeps_its_row(ledger):
    rows = {r["item_name"]: r for r in _run(ledger)}
    assert set(rows) == {"D TD-5", "N TD-10", "A TD-6 (L)", "SOLD OFF", "CADDIES"}

    n = rows["N TD-10"]
    assert all(n[f] == 0 for f in POSITION)
    assert (n["mtd_receipt_weight"], n["mtd_issue_weight"]) == (1190.0, 1190.0)

    a = rows["A TD-6 (L)"]            # received in August: MTD issue only
    assert all(a[f] == 0 for f in POSITION)
    assert (a["mtd_receipt_weight"], a["mtd_issue_weight"]) == (0.0, 1400.0)

    s = rows["SOLD OFF"]              # sold, not issued: MTD receipt only
    assert all(s[f] == 0 for f in POSITION)
    assert (s["mtd_receipt_weight"], s["mtd_issue_weight"]) == (600.0, 0.0)

    w = rows["CADDIES"]               # process waste booked and re-issued in the month
    assert w["is_waste"] == 1 and all(w[f] == 0 for f in POSITION)
    assert (w["mtd_receipt_weight"], w["mtd_issue_weight"]) == (56.0, 56.0)

    assert rows["N TD-10"]["item_group_name"] == "JUTE > NORTHERN"


def test_executed_item_with_nothing_to_print_is_still_hidden(ledger):
    """Received and used up in July: all seven columns are zero on 30 Sep."""
    assert "OLD" not in {r["item_name"] for r in _run(ledger)}


def test_executed_mtd_totals_equal_the_un_hidden_legs(ledger):
    rows = _run(ledger)
    mtd_issue = sum(r["mtd_issue_weight"] for r in rows)
    mtd_receipt = sum(r["mtd_receipt_weight"] for r in rows)
    assert mtd_issue == pytest.approx(_leg_total(ledger, _issue_cte(MONTH))) == pytest.approx(3346.0)
    assert mtd_receipt == pytest.approx(_leg_total(ledger, _receipt_cte(MONTH))) == pytest.approx(1846.0)


def test_executed_position_only_rule_under_reports_the_month(ledger):
    """The rule the report used to run — kept for the Issue Planner — proves the gap."""
    old = _run(ledger, mtd_rows=False)
    assert [r["item_name"] for r in old] == ["D TD-5"]
    assert sum(r["mtd_issue_weight"] for r in old) == pytest.approx(700.0)       # 3,346 actually issued
    assert sum(r["mtd_receipt_weight"] for r in old) == pytest.approx(0.0)       # 1,846 actually received


def test_executed_day_figures_and_position_are_unchanged(ledger):
    new = {r["item_id"]: r for r in _run(ledger)}
    old = {r["item_id"]: r for r in _run(ledger, mtd_rows=False)}
    # Every row the position-only rule returns is returned unchanged...
    assert all(new[i] == old[i] for i in old)
    # ...and the extra rows add nothing to any day figure or to the position.
    for f in POSITION:
        assert sum(r[f] for r in new.values()) == pytest.approx(sum(r[f] for r in old.values()))
    d = new[10]
    assert (d["opening_weight"], d["issue_weight"], d["closing_weight"]) == (2800.0, 500.0, 2300.0)


def test_executed_sold_leg_reaches_the_report(ledger):
    """SOLD OFF closes at zero only because the sale is counted (opening = 600 - 600)."""
    with ledger.connect() as c:
        sold = c.execute(
            text(f"SELECT COALESCE(SUM(wt), 0) FROM ({_sold_cte('{d} < :report_date')}) x"), BINDS
        ).scalar()
    assert sold == pytest.approx(600.0)


# ---------------------------------------------------------------------------
# Who asks for which rows
# ---------------------------------------------------------------------------


class _RecordingDb:
    def __init__(self):
        self.query = None

    def execute(self, query, _binds):
        self.query = query
        return SimpleNamespace(fetchall=lambda: [])


@pytest.mark.parametrize("basis", ["actual", "accounts"])
def test_stock_endpoint_runs_the_query_that_keeps_mtd_rows(basis):
    db = _RecordingDb()
    request = SimpleNamespace(query_params={"branch_id": "29", "date": "2026-09-30", "basis": basis})
    assert get_jute_stock_report(request, db=db, token_data={}) == {"data": []}
    assert str(db.query) == str(get_jute_stock_report_query(basis))
    assert MTD_ISSUE_TEST in str(db.query) and MTD_RECEIPT_TEST in str(db.query)


def test_issue_planner_asks_for_position_rows_only():
    """Its godown cover reads closing stock alone; a quality that merely moved
    earlier in the month must not appear there as a 0 kg line."""

    def _result(rows=None, scalar=None):
        res = MagicMock()
        res.fetchall.return_value = rows or []
        res.scalar.return_value = scalar
        return res

    db = MagicMock()
    db.execute.side_effect = [_result(scalar=1)] + [_result() for _ in range(5)]
    with patch.object(issuePlanner, "get_jute_stock_report_query", wraps=get_jute_stock_report_query) as spy:
        issuePlanner.build_issue_planner_report(db, 29, date(2026, 9, 30))
    spy.assert_called_once_with(mtd_rows=False)
    planner_sql = str(db.execute.call_args_list[-1].args[0])
    assert MTD_ISSUE_TEST not in planner_sql.rpartition("WHERE (")[2]


def test_daily_production_report_uses_the_default_rows():
    """Its jute section prints MTD receipt / issue totals summed off these rows."""
    src = inspect.getsource(dpr_reports.build_dpr_data)
    assert "get_jute_stock_report_query()" in src
    assert "mtd_rows=False" not in src


def test_dpr_jute_totals_are_a_plain_sum_of_the_rows_it_is_given():
    """So a hidden row is a missing figure: the DPR had the same gap as the report."""

    def row(name, group, **w):
        base = {f: 0.0 for f in POSITION + ("mtd_receipt_weight", "mtd_issue_weight")}
        return {"item_name": name, "item_group_name": group, "is_waste": 0, **base, **w}

    in_stock = row("D TD-5", "JUTE > DESI", opening_weight=2800.0, issue_weight=500.0,
                   closing_weight=2300.0, mtd_issue_weight=700.0)
    used_up = row("N TD-10", "JUTE > NORTHERN", mtd_receipt_weight=1190.0, mtd_issue_weight=1190.0)

    before = build_jute_section([in_stock], 30)
    after = build_jute_section([in_stock, used_up], 30)
    assert before["totals"]["mtd_issue_weight"] == 700.0
    assert after["totals"]["mtd_issue_weight"] == 1890.0
    assert after["totals"]["mtd_receipt_weight"] == 1190.0
    for f in POSITION:
        assert after["totals"][f] == before["totals"][f]
    assert [g["item_group_name"] for g in after["groups"]] == ["JUTE > DESI", "JUTE > NORTHERN"]

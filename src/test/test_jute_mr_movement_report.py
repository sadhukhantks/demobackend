"""
Tests for the Jute MR-wise Movement Detail report (Issue Detail + raw-jute sales).
Covers src/juteProcurement/reports.py @
    GET /api/juteReports/mr-movement-detail
and get_jute_mr_movement_detail_query in src/juteProcurement/reportQueries.py.

The endpoint shares _mr_scoped_params with /mr-issue-detail, so the param-
validation matrix lives in test_jute_mr_issue_report.py and is not repeated
here beyond one guard that the sharing did not break it. What IS unique to this
report — the sales attribution guard, the one-row-per-movement shape and the
unattributed block — is executed against a real (SQLite) database, because a
substring assertion would pass on SQL that silently multiplies rows.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from sqlalchemy import create_engine, event, text

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProcurement.reportQueries import get_jute_mr_movement_detail_query

client = TestClient(app)

MOVEMENT_URL = "/api/juteReports/mr-movement-detail"


class TestJuteMrMovementEndpoint:
    """Scope params reach the builder; the shared reader still validates."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        self._mock_session.execute.return_value.fetchall.return_value = []
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def _executed_params(self):
        return self._mock_session.execute.call_args[0][1]

    def test_missing_co_id_returns_400(self):
        r = client.get(
            f"{MOVEMENT_URL}?branch_id=2&date_from=2026-01-01&date_to=2026-12-31"
        )
        assert r.status_code == 400
        assert "co_id" in r.json()["detail"].lower()

    def test_no_mr_and_no_dates_returns_400(self):
        r = client.get(f"{MOVEMENT_URL}?co_id=1&branch_id=2")
        assert r.status_code == 400
        assert "date_from" in r.json()["detail"].lower()

    def test_invalid_mr_id_returns_400(self):
        r = client.get(f"{MOVEMENT_URL}?co_id=1&branch_id=2&mr_id=abc")
        assert r.status_code == 400

    def test_mr_id_wins_over_dates(self):
        """A picked MR must not also bind a range — the SQL has no such clause."""
        r = client.get(
            f"{MOVEMENT_URL}?co_id=1&branch_id=2&mr_id=27927"
            "&date_from=2026-01-01&date_to=2026-12-31"
        )
        assert r.status_code == 200
        assert self._executed_params() == {
            "co_id": 1,
            "branch_id": 2,
            "mr_id": 27927,
        }

    def test_response_is_wrapped_in_data(self):
        r = client.get(
            f"{MOVEMENT_URL}?co_id=1&branch_id=2"
            "&date_from=2026-01-01&date_to=2026-12-31"
        )
        assert r.status_code == 200
        assert r.json() == {"data": []}


# ---------------------------------------------------------------------------
# Executed against a real (SQLite) database.
#
# SQLite stands in for MySQL; the builder uses CONCAT/LPAD/MOD/MONTH/YEAR and
# nothing else non-portable, so those are registered on the connection.
# ---------------------------------------------------------------------------

_DDL = [
    "CREATE TABLE co_mst (co_id INTEGER, co_prefix TEXT)",
    "CREATE TABLE branch_mst (branch_id INTEGER, co_id INTEGER, branch_prefix TEXT)",
    "CREATE TABLE party_mst (party_id INTEGER, supp_name TEXT)",
    "CREATE TABLE jute_supplier_mst (supplier_id INTEGER, supplier_name TEXT)",
    "CREATE TABLE warehouse_mst (warehouse_id INTEGER, warehouse_name TEXT)",
    "CREATE TABLE item_mst (item_id INTEGER, item_name TEXT)",
    "CREATE TABLE status_mst (status_id INTEGER, status_name TEXT)",
    "CREATE TABLE jute_yarn_mst "
    "(jute_yarn_id INTEGER, item_id INTEGER, jute_yarn_name TEXT)",
    "CREATE TABLE vw_jute_stock_outstanding "
    "(jute_mr_li_id INTEGER, bal_qty REAL, bal_weight REAL)",
    "CREATE TABLE jute_mr (jute_mr_id INTEGER, branch_id INTEGER, status_id INTEGER,"
    " jute_mr_date TEXT, out_date TEXT, jute_gate_entry_date TEXT,"
    " jute_gate_entry_no TEXT, branch_mr_no INTEGER, party_id TEXT,"
    " jute_supplier_id INTEGER)",
    "CREATE TABLE jute_mr_li (jute_mr_li_id INTEGER, jute_mr_id INTEGER,"
    " active INTEGER, status TEXT, warehouse_id INTEGER, actual_item_id INTEGER,"
    " actual_quality TEXT, marka TEXT, crop_year TEXT, actual_qty REAL,"
    " actual_weight REAL, actual_rate REAL)",
    "CREATE TABLE jute_issue (jute_issue_id INTEGER, jute_mr_li_id INTEGER,"
    " status_id INTEGER, item_id INTEGER, yarn_type_id INTEGER, issue_date TEXT,"
    " quantity REAL, weight REAL, issue_value REAL, updated_by TEXT,"
    " update_date_time TEXT)",
    "CREATE TABLE sales_invoice (invoice_id INTEGER, invoice_no TEXT,"
    " invoice_date TEXT, invoice_type INTEGER, branch_id INTEGER, party_id INTEGER,"
    " status_id INTEGER, active INTEGER, updated_by TEXT, updated_date_time TEXT)",
    "CREATE TABLE sales_invoice_dtl (invoice_line_item_id INTEGER,"
    " invoice_id INTEGER, item_id INTEGER, quantity REAL, sales_weight REAL,"
    " rate REAL, amount_without_tax REAL)",
    "CREATE TABLE sales_invoice_jute (sales_invoice_jute_id INTEGER,"
    " invoice_id INTEGER, mr_id INTEGER, mr_no TEXT)",
]

# MR 1 (line 10)  -> 2 issues + 1 attributed sale
# MR 2 (line 20)  -> nothing at all
# MR 3 (lines 30, 31) -> two lines of the SAME item; a sale must land on one
# MR 4 (line 40)  -> out of the date range (no date columns at all)
# MR 9 lives on branch 3, so a sale claiming it from branch 2 is unattributed.
_SEED = [
    "INSERT INTO co_mst VALUES (1, 'EJM')",
    "INSERT INTO branch_mst VALUES (2, 1, 'F'), (3, 1, 'H')",
    "INSERT INTO party_mst VALUES (77, 'SHEMA JUTE AGENCY'), (88, 'ACME EXPORTS')",
    "INSERT INTO warehouse_mst VALUES (9, 'GODOWN A')",
    "INSERT INTO item_mst VALUES (501, 'D TD 5'), (502, 'W 3'), (601, 'YARN 8LB')",
    "INSERT INTO status_mst VALUES "
    "(1, 'Open'), (3, 'Approved'), (4, 'Rejected'), (6, 'Cancelled')",
    "INSERT INTO jute_yarn_mst VALUES (901, 601, 'Hessian Warp')",
    "INSERT INTO jute_mr VALUES "
    "(1, 2, 3, NULL, '2026-05-10', NULL, 'GE1', 7,  '77', NULL),"
    "(2, 2, 3, NULL, '2026-05-11', NULL, 'GE2', 8,  '77', NULL),"
    "(3, 2, 3, NULL, '2026-05-12', NULL, 'GE3', 9,  '77', NULL),"
    "(4, 2, 3, NULL, NULL,         NULL, 'GE4', 10, '77', NULL),"
    "(9, 3, 3, NULL, '2026-05-13', NULL, 'GE9', 11, '77', NULL)",
    "INSERT INTO jute_mr_li VALUES "
    "(10, 1, 1, NULL, 9, 501, 'DTD5', 'MK1', '2025', 100, 10000, 55),"
    "(20, 2, 1, NULL, 9, 501, 'DTD5', 'MK2', '2025', 200, 20000, 55),"
    "(30, 3, 1, NULL, 9, 501, 'DTD5', 'MK3', '2025', 300, 30000, 55),"
    "(31, 3, 1, NULL, 9, 501, 'DTD5', 'MK3', '2025',  50,  5000, 55),"
    "(40, 4, 1, NULL, 9, 501, 'DTD5', 'MK4', '2025', 400, 40000, 55),"
    "(90, 9, 1, NULL, 9, 501, 'DTD5', 'MK9', '2025', 900, 90000, 55)",
    "INSERT INTO vw_jute_stock_outstanding VALUES "
    "(10, 92, 9200), (20, 200, 20000), (30, 300, 30000), (31, 50, 5000),"
    "(40, 400, 40000), (90, 900, 90000)",
    "INSERT INTO jute_issue VALUES "
    "(100, 10, 3, 502,  901, '2026-05-15', 5, 500, 1000, 'bob', '2026-05-15 10:00'),"
    "(101, 10, 1, NULL, NULL,'2026-05-16', 3, 300,  600, 'amy', '2026-05-16 10:00')",
    # 1000 attaches to MR 1  | 1001 claims MR 9 (other branch)
    # 1002 claims MR 1 but sells item 502, which MR 1 never held
    # 1003 has no mr_id at all | 1004 is cancelled | 1005 attaches to MR 3
    # 1006 is outside the date range and unattributed
    "INSERT INTO sales_invoice VALUES "
    "(1000, 'INV-1', '2026-05-20', 5, 2, 88, 3, 1, 'sal', '2026-05-20 09:00'),"
    "(1001, 'INV-2', '2026-05-21', 5, 2, 88, 3, 1, 'sal', '2026-05-21 09:00'),"
    "(1002, 'INV-3', '2026-05-22', 5, 2, 88, 3, 1, 'sal', '2026-05-22 09:00'),"
    "(1003, 'INV-4', '2026-05-23', 5, 2, 88, 1, 1, 'sal', '2026-05-23 09:00'),"
    "(1004, 'INV-5', '2026-05-24', 5, 2, 88, 6, 1, 'sal', '2026-05-24 09:00'),"
    "(1005, 'INV-6', '2026-05-25', 5, 2, 88, 3, 1, 'sal', '2026-05-25 09:00'),"
    "(1006, 'INV-7', '2026-08-01', 5, 2, 88, 3, 1, 'sal', '2026-08-01 09:00'),"
    # Not raw jute — must never appear.
    "(1007, 'INV-8', '2026-05-26', 1, 2, 88, 3, 1, 'sal', '2026-05-26 09:00')",
    # Invoice 1003 bills two lines, so invoice_id alone cannot key a grid row.
    "INSERT INTO sales_invoice_dtl VALUES "
    "(2000, 1000, 501, 10, 1000, 80, 80000),"
    "(2001, 1001, 501, 11, 1100, 80, 88000),"
    "(2002, 1002, 502, 12, 1200, 80, 96000),"
    "(2003, 1003, 501, 13, 1300, 80, 104000),"
    "(2004, 1003, 502, 14, 1400, 80, 112000),"
    "(2005, 1004, 501, 15, 1500, 80, 120000),"
    "(2006, 1005, 501, 16, 1600, 80, 128000),"
    "(2007, 1006, 501, 17, 1700, 80, 136000),"
    "(2008, 1007, 501, 18, 1800, 80, 144000)",
    "INSERT INTO sales_invoice_jute VALUES "
    "(1, 1000, 1, 'MR-1'), (2, 1001, 9, 'MR-9'), (3, 1002, 1, 'MR-1'),"
    "(4, 1004, 1, 'MR-1'), (5, 1005, 3, 'MR-3'), (6, 1006, NULL, NULL)",
]

_ISSUE_SIDE_COLUMNS = (
    "jute_issue_id", "issue_date", "issued_qty", "issued_weight", "issue_value",
    "issue_item_name", "yarn_type_name", "issue_status",
)
_SALE_SIDE_COLUMNS = (
    "invoice_id", "invoice_line_item_id", "invoice_no", "invoice_date",
    "customer_name", "sale_item_name", "sold_qty", "sold_weight", "sale_rate",
    "sale_value", "invoice_status", "claimed_mr_id", "claimed_mr_no",
)
_MR_SIDE_COLUMNS = (
    "jute_mr_id", "jute_mr_li_id", "mr_number", "mr_label", "mr_date",
    "jute_mr_date", "out_date", "jute_gate_entry_no", "party_name",
    "warehouse_name", "quality", "marka", "crop_year", "received_qty",
    "received_weight", "actual_rate",
)


def _fixture_engine():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _register_mysql_functions(dbapi_conn, _record):
        def concat(*args):
            if any(a is None for a in args):
                return None  # MySQL CONCAT is NULL-propagating
            return "".join(str(a) for a in args)

        def lpad(value, length, pad):
            if value is None:
                return None
            s = str(value)
            return s[:length] if len(s) >= length else (pad * length + s)[-length:]

        dbapi_conn.create_function("CONCAT", -1, concat)
        dbapi_conn.create_function("LPAD", 3, lpad)
        dbapi_conn.create_function("MOD", 2, lambda a, b: None if a is None else a % b)
        dbapi_conn.create_function("MONTH", 1, lambda d: d and int(d[5:7]))
        dbapi_conn.create_function("YEAR", 1, lambda d: d and int(d[:4]))

    with engine.begin() as conn:
        for stmt in _DDL + _SEED:
            conn.execute(text(stmt))
    return engine


def _run(**params):
    engine = _fixture_engine()
    query = get_jute_mr_movement_detail_query(
        has_mr_filter="mr_id" in params,
        has_date_filter="date_from" in params,
    )
    with engine.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(query, params).fetchall()]


class TestMovementDetailDateRange:
    DATE_RANGE = {"co_id": 1, "branch_id": 2,
                  "date_from": "2026-05-01", "date_to": "2026-05-31"}

    @pytest.fixture(scope="class")
    def rows(self):
        return _run(**self.DATE_RANGE)

    def test_row_ids_are_unique(self, rows):
        """The grid keys on these four; invoice 1003 bills two lines."""
        keys = [
            (r["row_group"], r["jute_mr_li_id"], r["jute_issue_id"],
             r["invoice_line_item_id"])
            for r in rows
        ]
        assert len(keys) == len(set(keys))

    def test_one_row_per_movement_no_multiplication(self, rows):
        """Line 10 has 2 issues AND 1 sale — that is 3 rows, not 2 x 1 = 2."""
        line10 = [r for r in rows if r["jute_mr_li_id"] == 10]
        assert len(line10) == 3
        assert sorted(r["movement_type"] for r in line10) == [
            "Issue", "Issue", "Sale",
        ]

    def test_issue_row_has_no_sale_columns(self, rows):
        issue = [r for r in rows if r["jute_issue_id"] == 100][0]
        assert issue["movement_type"] == "Issue"
        assert ({c: issue[c] for c in _SALE_SIDE_COLUMNS}
                == dict.fromkeys(_SALE_SIDE_COLUMNS, None))

    def test_attributed_sale_row_has_no_issue_columns_but_keeps_the_mr(self, rows):
        sale = [r for r in rows
                if r["movement_type"] == "Sale" and r["jute_mr_li_id"] == 10][0]
        assert ({c: sale[c] for c in _ISSUE_SIDE_COLUMNS}
                == dict.fromkeys(_ISSUE_SIDE_COLUMNS, None))
        assert sale["quality"] == "D TD 5"
        assert sale["mr_number"] == "EJM/F/JMR/26-27/00007"
        assert sale["customer_name"] == "ACME EXPORTS"
        assert sale["sold_weight"] == 1000
        assert sale["sale_value"] == 80000
        assert sale["row_group"] == 0

    def test_line_with_no_movement_appears_once_with_null_type(self, rows):
        line20 = [r for r in rows if r["jute_mr_li_id"] == 20]
        assert len(line20) == 1
        assert line20[0]["movement_type"] is None
        assert line20[0]["received_qty"] == 200
        assert ({c: line20[0][c] for c in _ISSUE_SIDE_COLUMNS + _SALE_SIDE_COLUMNS}
                == dict.fromkeys(_ISSUE_SIDE_COLUMNS + _SALE_SIDE_COLUMNS, None))

    def test_sale_lands_on_one_line_when_the_mr_repeats_an_item(self, rows):
        """MR 3 has lines 30 and 31 of item 501. One sale, one row, lowest id."""
        sales = [r for r in rows
                 if r["movement_type"] == "Sale" and r["jute_mr_id"] == 3]
        assert len(sales) == 1
        assert sales[0]["jute_mr_li_id"] == 30
        # ...and line 31 still shows, as a no-movement row.
        line31 = [r for r in rows if r["jute_mr_li_id"] == 31]
        assert len(line31) == 1
        assert line31[0]["movement_type"] is None

    def test_wrong_branch_claim_is_unattributed(self, rows):
        """Invoice 1001 names MR 9, which sits on branch 3."""
        row = [r for r in rows if r["invoice_id"] == 1001][0]
        assert row["movement_type"] == "Sale (unattributed)"
        assert row["row_group"] == 1
        assert row["claimed_mr_id"] == 9
        assert row["claimed_mr_no"] == "MR-9"

    def test_item_not_on_the_claimed_mr_is_unattributed(self, rows):
        """Invoice 1002 names MR 1 but sells item 502, which MR 1 never held."""
        row = [r for r in rows if r["invoice_id"] == 1002][0]
        assert row["movement_type"] == "Sale (unattributed)"
        assert row["claimed_mr_id"] == 1

    def test_unattributed_rows_carry_no_mr_or_balance_columns(self, rows):
        row = [r for r in rows if r["invoice_id"] == 1002][0]
        assert ({c: row[c] for c in _MR_SIDE_COLUMNS}
                == dict.fromkeys(_MR_SIDE_COLUMNS, None))
        assert row["balance_qty"] is None
        assert row["balance_weight"] is None

    def test_invoice_with_no_jute_row_is_kept_as_unattributed(self, rows):
        """Both lines of invoice 1003 survive; neither is silently dropped."""
        lines = [r for r in rows if r["invoice_id"] == 1003]
        assert len(lines) == 2
        assert {r["invoice_line_item_id"] for r in lines} == {2003, 2004}
        assert all(r["movement_type"] == "Sale (unattributed)" for r in lines)

    def test_non_approved_invoice_is_kept_and_labelled(self, rows):
        """Mirrors the issue side, which counts drafts. Status stays visible."""
        row = [r for r in rows if r["invoice_id"] == 1003][0]
        assert row["invoice_status"] == "Open"

    def test_cancelled_invoice_is_excluded(self, rows):
        assert not [r for r in rows if r["invoice_id"] == 1004]

    def test_non_raw_jute_invoice_is_excluded(self, rows):
        assert not [r for r in rows if r["invoice_id"] == 1007]

    def test_unattributed_sale_outside_the_range_is_excluded(self, rows):
        """It has no MR to inherit a period from, so invoice_date must gate it."""
        assert not [r for r in rows if r["invoice_id"] == 1006]

    def test_unattributed_block_sorts_last(self, rows):
        groups = [r["row_group"] for r in rows]
        assert groups == sorted(groups)

    def test_out_of_range_mr_is_excluded(self, rows):
        assert not [r for r in rows if r["jute_mr_li_id"] == 40]


class TestMovementDetailSingleMr:
    """One MR picked: the range is unused and unattributed sales are dropped."""

    @pytest.fixture(scope="class")
    def rows(self):
        return _run(co_id=1, branch_id=2, mr_id=1)

    def test_only_that_mr(self, rows):
        assert {r["jute_mr_id"] for r in rows} == {1}

    def test_its_issues_and_its_sale_are_all_present(self, rows):
        assert sorted(r["movement_type"] for r in rows) == ["Issue", "Issue", "Sale"]

    def test_no_unattributed_block(self, rows):
        assert all(r["row_group"] == 0 for r in rows)

    def test_a_dateless_mr_is_reachable_by_id(self):
        """MR 4 has no date at all, so only the picker can reach it."""
        rows = _run(co_id=1, branch_id=2, mr_id=4)
        assert len(rows) == 1
        assert rows[0]["jute_mr_li_id"] == 40
        assert rows[0]["movement_type"] is None

"""
Tests for the Jute PO reports (TKT-000115).
Covers src/juteProcurement/reports.py @
    GET /api/juteReports/po-options
    GET /api/juteReports/po-mr-receipt-detail
    GET /api/juteReports/po-pending
and the query builders in src/juteProcurement/reportQueries.py.

The pending builder uses DATE_ADD(.. INTERVAL ..) and GREATEST, which SQLite
cannot emulate cheaply, so only the receipt-detail builder is executed for
real; the pending builder gets bind-param and predicate assertions.
"""

import re

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from sqlalchemy import create_engine, event, text

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProcurement.constants import (
    JUTE_PO_CLOSED_STATUS,
    JUTE_PO_INACTIVE_STATUSES,
)
from src.juteProcurement.reportQueries import (
    get_jute_po_mr_receipt_detail_query,
    get_jute_po_options_query,
    get_jute_po_pending_query,
)

client = TestClient(app)

OPTIONS_URL = "/api/juteReports/po-options"
DETAIL_URL = "/api/juteReports/po-mr-receipt-detail"
PENDING_URL = "/api/juteReports/po-pending"


def _bound_names(query):
    """Parameter names a text() query expects, e.g. {'co_id', 'branch_id'}."""
    return set(re.findall(r"(?<!:):([a-z_][a-z0-9_]*)", str(query)))


class TestJutePoReportEndpoints:
    """Endpoint param validation and the po_id-overrides-dates rule."""

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

    # -- /po-options ------------------------------------------------------

    def test_options_missing_co_id_returns_400(self):
        r = client.get(f"{OPTIONS_URL}?branch_id=2")
        assert r.status_code == 400
        assert "co_id" in r.json()["detail"].lower()

    def test_options_missing_branch_id_returns_400(self):
        r = client.get(f"{OPTIONS_URL}?co_id=1")
        assert r.status_code == 400
        assert "branch_id" in r.json()["detail"].lower()

    def test_options_half_open_date_range_returns_400(self):
        r = client.get(f"{OPTIONS_URL}?co_id=1&branch_id=2&date_from=2026-01-01")
        assert r.status_code == 400
        assert "date_to" in r.json()["detail"].lower()

    def test_options_without_dates_binds_no_date_params(self):
        r = client.get(f"{OPTIONS_URL}?co_id=1&branch_id=2")
        assert r.status_code == 200
        assert r.json() == {"data": []}
        assert self._executed_params() == {"co_id": 1, "branch_id": 2}

    # -- /po-mr-receipt-detail --------------------------------------------

    def test_detail_missing_co_id_returns_400(self):
        r = client.get(
            f"{DETAIL_URL}?branch_id=2&date_from=2026-01-01&date_to=2026-12-31"
        )
        assert r.status_code == 400
        assert "co_id" in r.json()["detail"].lower()

    def test_detail_without_po_id_requires_dates(self):
        r = client.get(f"{DETAIL_URL}?co_id=1&branch_id=2")
        assert r.status_code == 400
        assert "date_from" in r.json()["detail"].lower()

    def test_detail_invalid_po_id_returns_400(self):
        r = client.get(f"{DETAIL_URL}?co_id=1&branch_id=2&po_id=xyz")
        assert r.status_code == 400
        assert "po_id" in r.json()["detail"].lower()

    def test_detail_po_id_overrides_dates(self):
        """A picked PO wins: the date range is dropped, not applied."""
        r = client.get(
            f"{DETAIL_URL}?co_id=1&branch_id=2&po_id=13315"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        assert r.status_code == 200
        assert self._executed_params() == {"co_id": 1, "branch_id": 2, "po_id": 13315}
        sql = str(self._mock_session.execute.call_args[0][0])
        assert ":po_id" in sql
        assert ":date_from" not in sql

    def test_detail_date_range_happy_path(self):
        r = client.get(
            f"{DETAIL_URL}?co_id=1&branch_id=2"
            "&date_from=2026-01-01&date_to=2026-12-31"
        )
        assert r.status_code == 200
        assert list(r.json().keys()) == ["data"]
        assert self._executed_params() == {
            "co_id": 1,
            "branch_id": 2,
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
        }

    # -- /po-pending ------------------------------------------------------

    def test_pending_missing_co_id_returns_400(self):
        r = client.get(f"{PENDING_URL}?branch_id=2")
        assert r.status_code == 400
        assert "co_id" in r.json()["detail"].lower()

    def test_pending_happy_path(self):
        r = client.get(f"{PENDING_URL}?co_id=1&branch_id=2")
        assert r.status_code == 200
        assert r.json() == {"data": []}
        assert self._executed_params() == {"co_id": 1, "branch_id": 2}


class TestJutePoReportQueries:
    """The SQL builders bind what the handlers pass, and nothing else."""

    def test_options_query_params(self):
        assert _bound_names(get_jute_po_options_query(False)) == {"co_id", "branch_id"}
        assert _bound_names(get_jute_po_options_query(True)) == {
            "co_id", "branch_id", "date_from", "date_to",
        }

    def test_detail_query_params(self):
        assert _bound_names(get_jute_po_mr_receipt_detail_query(False, True)) == {
            "co_id", "branch_id", "date_from", "date_to",
        }
        assert _bound_names(get_jute_po_mr_receipt_detail_query(True, False)) == {
            "co_id", "branch_id", "po_id",
        }

    def test_pending_query_params(self):
        assert _bound_names(get_jute_po_pending_query()) == {"co_id", "branch_id"}

    def test_pending_excludes_closed_and_inactive_pos(self):
        """CLOSED is complete by definition; drafts/rejected/cancelled are
        not real orders. Both must sit in the status exclusion."""
        sql = str(get_jute_po_pending_query())
        excluded = ", ".join(
            str(s) for s in JUTE_PO_INACTIVE_STATUSES + (JUTE_PO_CLOSED_STATUS,)
        )
        assert f"jp.status_id NOT IN ({excluded})" in sql

    def test_detail_matches_by_item_not_po_li_id(self):
        """jute_mr_li.jute_po_li_id is populated on 3/40,852 sls rows — the
        item match through jute_mr.po_id is the only usable linkage."""
        sql = str(get_jute_po_mr_receipt_detail_query(False, True))
        assert "COALESCE(mrli.actual_item_id, mrli.challan_item_id)" in sql
        assert "jute_po_li_id" not in sql.split("receipts AS", 1)[1]

    def test_detail_scopes_company_through_branch_mst(self):
        """jute_po has no co_id."""
        sql = str(get_jute_po_mr_receipt_detail_query(False, True))
        assert "INNER JOIN branch_mst bm ON bm.branch_id = jp.branch_id" in sql
        assert "bm.co_id = :co_id" in sql

    def test_no_unit_conversion_on_weights(self):
        """dev3 kg vs sls quintal ambiguity — never scale."""
        for sql in (str(get_jute_po_mr_receipt_detail_query(False, True)),
                    str(get_jute_po_pending_query())):
            assert "/ 100" not in sql


# ---------------------------------------------------------------------------
# The receipt-detail builder executed against a real (SQLite) database — the
# union + per-item aggregation is where a broken join would multiply or drop
# rows, and no substring test would notice.
# ---------------------------------------------------------------------------

_DDL = [
    "CREATE TABLE co_mst (co_id INTEGER, co_prefix TEXT)",
    "CREATE TABLE branch_mst (branch_id INTEGER, co_id INTEGER, branch_prefix TEXT)",
    "CREATE TABLE party_mst (party_id INTEGER, supp_name TEXT)",
    "CREATE TABLE jute_supplier_mst (supplier_id INTEGER, supplier_name TEXT)",
    "CREATE TABLE jute_mukam_mst (mukam_id INTEGER, mukam_name TEXT)",
    "CREATE TABLE item_mst (item_id INTEGER, item_name TEXT)",
    "CREATE TABLE status_mst (status_id INTEGER, status_name TEXT)",
    "CREATE TABLE jute_po (jute_po_id INTEGER, branch_id INTEGER,"
    " supplier_id INTEGER, party_id INTEGER, jute_mukam_id INTEGER,"
    " po_no INTEGER, po_date TEXT, status_id INTEGER, weight REAL)",
    "CREATE TABLE jute_po_li (jute_po_li_id INTEGER, jute_po_id INTEGER,"
    " item_id INTEGER, quantity REAL, rate REAL, jute_uom TEXT, active INTEGER)",
    "CREATE TABLE jute_mr (jute_mr_id INTEGER, branch_id INTEGER, po_id INTEGER,"
    " status_id INTEGER, jute_mr_date TEXT, out_date TEXT,"
    " jute_gate_entry_date TEXT, jute_gate_entry_no TEXT, branch_mr_no INTEGER,"
    " party_id TEXT, jute_supplier_id INTEGER, net_weight REAL)",
    "CREATE TABLE jute_mr_li (jute_mr_li_id INTEGER, jute_mr_id INTEGER,"
    " active INTEGER, status TEXT, challan_item_id INTEGER,"
    " challan_quantity REAL, challan_weight REAL, actual_item_id INTEGER,"
    " actual_qty REAL, actual_weight REAL, accepted_weight REAL, rate REAL,"
    " shortage_kgs INTEGER)",
]

_SEED = [
    "INSERT INTO co_mst VALUES (1, 'EJM')",
    "INSERT INTO branch_mst VALUES (2, 1, 'F'), (9, 1, 'X')",
    "INSERT INTO item_mst VALUES (501, 'D TD 5'), (502, 'W 3'), (503, 'B BOT')",
    "INSERT INTO status_mst VALUES (1, 'Open'), (3, 'Approved'),"
    " (4, 'Rejected'), (21, 'Drafted')",
    # PO 100: item 501 split over TWO active lines (60 + 40) + one inactive
    # line that must not count, and item 502 with no receipts.
    # PO 200: no receipts at all. PO 300: other branch. PO 400: draft.
    "INSERT INTO jute_po VALUES "
    "(100, 2, NULL, NULL, NULL, 5, '2026-05-01', 3, 10000),"
    "(200, 2, NULL, NULL, NULL, 6, '2026-05-02', 3, 7000),"
    "(300, 9, NULL, NULL, NULL, 7, '2026-05-03', 3, 5000),"
    "(400, 2, NULL, NULL, NULL, 8, '2026-05-04', 21, 4000)",
    "INSERT INTO jute_po_li VALUES "
    "(1, 100, 501, 60, 5000, 'LOOSE', 1),"
    "(2, 100, 501, 40, 5100, 'LOOSE', 1),"
    "(3, 100, 501, 99, 9999, 'LOOSE', 0),"  # inactive — excluded
    "(4, 100, 502, 50, 4000, 'BALE', 1),"
    "(5, 200, 501, 70, 5200, 'LOOSE', 1),"
    "(6, 400, 501, 10, 100, 'LOOSE', 1)",
    # MR 1 (eligible): line 10 matches PO item 501; line 11 is item 503,
    # ordered nowhere on PO 100 -> row_group 1.
    # MR 2 is rejected -> its line 20 must not appear at all.
    "INSERT INTO jute_mr VALUES "
    "(1, 2, 100, 3, NULL, '2026-05-10', NULL, 'GE1', 7, NULL, NULL, 9000),"
    "(2, 2, 100, 4, NULL, '2026-05-11', NULL, 'GE2', 8, NULL, NULL, 500)",
    "INSERT INTO jute_mr_li VALUES "
    "(10, 1, 1, NULL, 501, 90, 9100, 501, 90, 9000, 8900, 5000, 20),"
    "(11, 1, 1, NULL, 503, 5, 500, 503, 5, 480, 470, 4500, 0),"
    "(20, 2, 1, NULL, 501, 99, 9900, 501, 99, 9900, 9900, 5000, 0)",
]


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
    query = get_jute_po_mr_receipt_detail_query(
        has_po_filter="po_id" in params,
        has_date_filter="date_from" in params,
    )
    with engine.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(query, params).fetchall()]


class TestJutePoMrReceiptDetailExecuted:
    """The builder run for real, not string-matched."""

    DATE_RANGE = {"co_id": 1, "branch_id": 2,
                  "date_from": "2026-05-01", "date_to": "2026-05-31"}

    @pytest.fixture(scope="class")
    def rows(self):
        return _run(**self.DATE_RANGE)

    def test_exact_row_count(self, rows):
        # PO100/501 x MR line 10; PO100/502 zero-receipt; PO100 unmatched
        # line 11; PO200/501 zero-receipt. Draft PO400 and branch-9 PO300 out.
        assert len(rows) == 4

    def test_same_item_po_lines_aggregate_once(self, rows):
        """Item 501 spans two active PO lines (60+40) — ONE ordered row, and
        the inactive 99-qty line stays out."""
        r501 = [r for r in rows if r["jute_po_id"] == 100 and r["item_id"] == 501]
        assert len(r501) == 1
        assert r501[0]["ordered_qty"] == 100
        assert r501[0]["received_qty"] == 90
        assert r501[0]["mr_label"] == "EJM/F/JMR/26-27/00007"

    def test_zero_receipt_po_item_survives_with_null_mr_columns(self, rows):
        r502 = [r for r in rows if r["jute_po_id"] == 100 and r["item_id"] == 502]
        assert len(r502) == 1
        assert r502[0]["ordered_qty"] == 50
        assert r502[0]["jute_mr_li_id"] is None
        assert r502[0]["received_qty"] is None

    def test_unmatched_receipt_comes_back_as_row_group_1(self, rows):
        """A receipt whose item was never ordered must stay visible."""
        um = [r for r in rows if r["row_group"] == 1]
        assert len(um) == 1
        assert um[0]["item_id"] == 503
        assert um[0]["ordered_qty"] is None
        assert um[0]["received_qty"] == 5

    def test_rejected_mr_is_excluded(self, rows):
        assert 20 not in [r["jute_mr_li_id"] for r in rows]

    def test_zero_receipt_po_survives_entirely(self, rows):
        po200 = [r for r in rows if r["jute_po_id"] == 200]
        assert len(po200) == 1
        assert po200[0]["jute_mr_id"] is None

    def test_draft_po_excluded_in_range_mode_but_reachable_picked(self, rows):
        assert 400 not in [r["jute_po_id"] for r in rows]
        picked = _run(co_id=1, branch_id=2, po_id=400)
        assert [r["jute_po_id"] for r in picked] == [400]

    def test_other_branch_po_never_leaks(self, rows):
        assert 300 not in [r["jute_po_id"] for r in rows]

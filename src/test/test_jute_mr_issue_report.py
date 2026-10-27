"""
Tests for the Jute MR-wise Issue Detail report.
Covers src/juteProcurement/reports.py @
    GET /api/juteReports/mr-options
    GET /api/juteReports/mr-issue-detail
and the query builders in src/juteProcurement/reportQueries.py.
"""

import re

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from sqlalchemy import create_engine, event, text

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProcurement.constants import JUTE_ISSUE_EXCLUDED_STATUS
from src.juteProcurement.reportQueries import (
    _MR_RECEIPT_DATE,
    get_jute_mr_issue_detail_query,
    get_jute_mr_options_query,
)

client = TestClient(app)

OPTIONS_URL = "/api/juteReports/mr-options"
DETAIL_URL = "/api/juteReports/mr-issue-detail"


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _bound_names(query):
    """Parameter names a text() query expects, e.g. {'co_id', 'branch_id'}."""
    return set(re.findall(r"(?<!:):([a-z_][a-z0-9_]*)", str(query)))


class TestJuteMrIssueReportEndpoints:
    """Endpoint param validation and the mr_id-overrides-dates rule."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        self._mock_session.execute.return_value.fetchall.return_value = []
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def _executed_params(self):
        """The params dict handed to db.execute() on the last call."""
        return self._mock_session.execute.call_args[0][1]

    # -- /mr-options ------------------------------------------------------

    def test_options_missing_co_id_returns_400(self):
        r = client.get(f"{OPTIONS_URL}?branch_id=2")
        assert r.status_code == 400
        assert "co_id" in r.json()["detail"].lower()

    def test_options_missing_branch_id_returns_400(self):
        r = client.get(f"{OPTIONS_URL}?co_id=1")
        assert r.status_code == 400
        assert "branch_id" in r.json()["detail"].lower()

    def test_options_invalid_co_id_returns_400(self):
        r = client.get(f"{OPTIONS_URL}?co_id=abc&branch_id=2")
        assert r.status_code == 400

    def test_options_half_open_date_range_returns_400(self):
        """Only one of date_from/date_to is ambiguous, not a silent open range."""
        r = client.get(f"{OPTIONS_URL}?co_id=1&branch_id=2&date_from=2026-01-01")
        assert r.status_code == 400
        assert "date_to" in r.json()["detail"].lower()

    def test_options_bad_date_format_returns_400(self):
        r = client.get(
            f"{OPTIONS_URL}?co_id=1&branch_id=2"
            "&date_from=01-01-2026&date_to=2026-12-31"
        )
        assert r.status_code == 400
        assert "date_from" in r.json()["detail"].lower()

    def test_options_without_dates_binds_no_date_params(self):
        r = client.get(f"{OPTIONS_URL}?co_id=1&branch_id=2")
        assert r.status_code == 200
        assert r.json() == {"data": []}
        assert self._executed_params() == {"co_id": 1, "branch_id": 2}

    def test_options_happy_path_returns_wrapped_data(self):
        self._mock_session.execute.return_value.fetchall.return_value = [
            _mock_row({
                "jute_mr_id": 27927,
                "mr_number": "EJM/F/JMR/26-27/00001",
                "mr_label": "EJM/F/JMR/26-27/00001",
                "mr_date": "2026-04-01",
                "party_name": "SHEMA JUTE AGENCY",
            })
        ]
        r = client.get(
            f"{OPTIONS_URL}?co_id=1&branch_id=2"
            "&date_from=2026-01-01&date_to=2026-12-31"
        )
        assert r.status_code == 200
        body = r.json()
        assert list(body.keys()) == ["data"]
        assert body["data"][0]["mr_label"] == "EJM/F/JMR/26-27/00001"
        assert self._executed_params() == {
            "co_id": 1,
            "branch_id": 2,
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
        }

    # -- /mr-issue-detail -------------------------------------------------

    def test_detail_missing_co_id_returns_400(self):
        r = client.get(
            f"{DETAIL_URL}?branch_id=2&date_from=2026-01-01&date_to=2026-12-31"
        )
        assert r.status_code == 400
        assert "co_id" in r.json()["detail"].lower()

    def test_detail_missing_branch_id_returns_400(self):
        r = client.get(f"{DETAIL_URL}?co_id=1&date_from=2026-01-01&date_to=2026-12-31")
        assert r.status_code == 400
        assert "branch_id" in r.json()["detail"].lower()

    def test_detail_without_mr_id_requires_dates(self):
        r = client.get(f"{DETAIL_URL}?co_id=1&branch_id=2")
        assert r.status_code == 400
        assert "date_from" in r.json()["detail"].lower()

    def test_detail_invalid_mr_id_returns_400(self):
        r = client.get(f"{DETAIL_URL}?co_id=1&branch_id=2&mr_id=xyz")
        assert r.status_code == 400
        assert "mr_id" in r.json()["detail"].lower()

    def test_detail_mr_id_overrides_dates(self):
        """A picked MR wins: the date range is dropped, not applied."""
        r = client.get(
            f"{DETAIL_URL}?co_id=1&branch_id=2&mr_id=27743"
            "&date_from=2026-01-01&date_to=2026-01-31"
        )
        assert r.status_code == 200
        params = self._executed_params()
        assert params == {"co_id": 1, "branch_id": 2, "mr_id": 27743}
        sql = str(self._mock_session.execute.call_args[0][0])
        assert ":mr_id" in sql
        assert ":date_from" not in sql

    def test_detail_mr_id_alone_needs_no_dates(self):
        r = client.get(f"{DETAIL_URL}?co_id=1&branch_id=2&mr_id=27743")
        assert r.status_code == 200
        assert r.json() == {"data": []}

    def test_detail_date_range_happy_path(self):
        self._mock_session.execute.return_value.fetchall.return_value = [
            _mock_row({
                "jute_mr_id": 27939,
                "jute_mr_li_id": 45046,
                "mr_label": "EJM/F/JMR/25-26/00003",
                "jute_issue_id": None,
                "balance_qty": 126.0,
            })
        ]
        r = client.get(
            f"{DETAIL_URL}?co_id=1&branch_id=2"
            "&date_from=2026-01-01&date_to=2026-12-31"
        )
        assert r.status_code == 200
        assert list(r.json().keys()) == ["data"]
        # A zero-issue MR line survives the LEFT JOIN.
        assert r.json()["data"][0]["jute_issue_id"] is None
        assert self._executed_params() == {
            "co_id": 1,
            "branch_id": 2,
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
        }


class TestJuteMrIssueReportQueries:
    """The SQL builders bind what the handlers pass, and nothing else."""

    def test_options_query_params(self):
        assert _bound_names(get_jute_mr_options_query(False)) == {"co_id", "branch_id"}
        assert _bound_names(get_jute_mr_options_query(True)) == {
            "co_id", "branch_id", "date_from", "date_to",
        }

    def test_detail_query_params(self):
        assert _bound_names(get_jute_mr_issue_detail_query(False, True)) == {
            "co_id", "branch_id", "date_from", "date_to",
        }
        assert _bound_names(get_jute_mr_issue_detail_query(True, False)) == {
            "co_id", "branch_id", "mr_id",
        }

    def test_issue_status_exclusion_is_in_the_join_not_the_where(self):
        """
        In the WHERE it would collapse the LEFT JOIN to an inner join and every
        MR line with zero issues would silently vanish from the report.
        """
        sql = str(get_jute_mr_issue_detail_query(False, True))
        predicate = f"ji.status_id <> {JUTE_ISSUE_EXCLUDED_STATUS}"
        assert predicate in sql
        join_clause = sql.split("LEFT JOIN jute_issue ji", 1)[1].split("LEFT JOIN", 1)[0]
        assert predicate in join_clause
        assert predicate not in sql.split("WHERE bm.co_id = :co_id", 1)[1]

    def test_detail_scopes_company_through_branch_mst(self):
        """Neither jute_mr nor jute_issue has co_id."""
        sql = str(get_jute_mr_issue_detail_query(False, True))
        assert "INNER JOIN branch_mst bm ON bm.branch_id = jm.branch_id" in sql
        assert "bm.co_id = :co_id" in sql

    def test_party_join_casts_varchar_party_id(self):
        for sql in (str(get_jute_mr_options_query(False)),
                    str(get_jute_mr_issue_detail_query(False, True))):
            assert "CAST(jm.party_id AS UNSIGNED)" in sql
            assert "pm.supp_name" in sql

    def test_dates_use_the_shared_module_receipt_date(self):
        """
        One MR must land in one period across every jute report. A second,
        differently-ordered COALESCE here would bucket it differently and the
        balances would stop tying out to the stock reports.
        """
        for sql in (str(get_jute_mr_options_query(True)),
                    str(get_jute_mr_issue_detail_query(False, True))):
            assert f"{_MR_RECEIPT_DATE} BETWEEN :date_from" in sql
            assert "COALESCE(jm.jute_mr_date, jm.out_date)" not in sql

    def test_mr_label_is_never_null(self):
        """branch_mr_no is NULL until approval — the picker still needs a label."""
        for sql in (str(get_jute_mr_options_query(False)),
                    str(get_jute_mr_issue_detail_query(True, False))):
            assert "AS mr_label" in sql
            assert "CONCAT('GE-', jm.jute_gate_entry_no)" in sql
            assert "CONCAT('MR-', jm.jute_mr_id)" in sql

    def test_no_unit_conversion_on_weights(self):
        """dev3 stores KG, sls stores QUINTAL in the same columns — never scale."""
        sql = str(get_jute_mr_issue_detail_query(False, True))
        assert "ROUND(mrli.actual_weight, 3) AS received_weight" in sql
        assert "ROUND(ji.weight, 3) AS issued_weight" in sql
        assert "/ 100" not in sql

    def test_issue_item_fallback_is_guarded_on_an_issue_existing(self):
        """
        ji.item_id is NULL on 68/506 dev3 rows, so the MR-line fallback stays —
        but only when there IS an issue. Unguarded it stamped an item name on
        all 57 zero-issue rows and filtering the export for blank issue item
        (the way to find un-issued lines) returned nothing.
        """
        sql = " ".join(str(get_jute_mr_issue_detail_query(False, True)).split())
        assert ("CASE WHEN ji.jute_issue_id IS NULL THEN NULL ELSE "
                "COALESCE(iim.item_name, qim.item_name) END AS issue_item_name") in sql


# ---------------------------------------------------------------------------
# Executed against a real (SQLite) database.
#
# Every test above asserts on SQL substrings and would pass on semantically
# broken SQL — an added `AND ji.item_id IS NOT NULL` in the WHERE would
# collapse the LEFT JOIN just as effectively as moving the status predicate
# there, and no substring test would notice. These run the builder.
#
# SQLite stands in for MySQL. The builder uses five MySQL scalar functions
# (CONCAT/LPAD/MOD/MONTH/YEAR) and nothing else non-portable, so they are
# registered on the connection rather than the SQL being rewritten.
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
    " actual_weight REAL, actual_rate REAL, accepted_weight REAL)",
    "CREATE TABLE jute_issue (jute_issue_id INTEGER, jute_mr_li_id INTEGER,"
    " status_id INTEGER, item_id INTEGER, yarn_type_id INTEGER, issue_date TEXT,"
    " quantity REAL, weight REAL, issue_value REAL, updated_by TEXT,"
    " update_date_time TEXT)",
]

_SEED = [
    "INSERT INTO co_mst VALUES (1, 'EJM')",
    "INSERT INTO branch_mst VALUES (2, 1, 'F')",
    "INSERT INTO party_mst VALUES (77, 'SHEMA JUTE AGENCY')",
    "INSERT INTO warehouse_mst VALUES (9, 'GODOWN A')",
    "INSERT INTO item_mst VALUES (501, 'D TD 5'), (502, 'W 3'), (601, 'YARN 8LB')",
    "INSERT INTO status_mst VALUES (1, 'Open'), (3, 'Approved'), (4, 'Rejected')",
    "INSERT INTO jute_yarn_mst VALUES (901, 601, 'Hessian Warp')",
    # MR 1 two issues | MR 2 zero issues | MR 3 one rejected issue
    # | MR 4 all three date columns NULL
    "INSERT INTO jute_mr VALUES "
    "(1, 2, 3, NULL, '2026-05-10', NULL, 'GE1', 7,  '77', NULL),"
    "(2, 2, 3, NULL, '2026-05-11', NULL, 'GE2', 8,  '77', NULL),"
    "(3, 2, 3, NULL, '2026-05-12', NULL, 'GE3', 9,  '77', NULL),"
    "(4, 2, 3, NULL, NULL,         NULL, 'GE4', 10, '77', NULL)",
    "INSERT INTO jute_mr_li VALUES "
    "(10, 1, 1, NULL, 9, 501, 'DTD5', 'MK1', '2025', 100, 10000, 55, 9800),"
    "(20, 2, 1, NULL, 9, 501, 'DTD5', 'MK2', '2025', 200, 20000, 55, 19800),"
    "(30, 3, 1, NULL, 9, 501, 'DTD5', 'MK3', '2025', 300, 30000, 55, 29800),"
    "(40, 4, 1, NULL, 9, 501, 'DTD5', 'MK4', '2025', 400, 40000, 55, 39800)",
    "INSERT INTO vw_jute_stock_outstanding VALUES "
    "(10, 92, 9200), (20, 200, 20000), (30, 300, 30000), (40, 400, 40000)",
    # Issue 101 has a NULL item_id — 68/506 dev3 rows do; the MR-line fallback
    # must still fire for it. Issue 102 is rejected (status 4).
    "INSERT INTO jute_issue VALUES "
    "(100, 10, 3, 502,  901, '2026-05-15', 5, 500, 1000, 'bob', '2026-05-15 10:00'),"
    "(101, 10, 1, NULL, NULL,'2026-05-16', 3, 300,  600, 'amy', '2026-05-16 10:00'),"
    "(102, 30, 4, 502,  901, '2026-05-17', 7, 700, 1400, 'bob', '2026-05-17 10:00')",
]

# Everything the LEFT JOIN to jute_issue contributes. All of it must be NULL on
# a zero-issue MR line, or the export cannot be filtered for un-issued lines.
_ISSUE_SIDE_COLUMNS = (
    "jute_issue_id", "issue_date", "issued_qty", "issued_weight", "issue_value",
    "issue_item_name", "yarn_type_name", "issue_status", "updated_by",
    "update_date_time",
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
    """Execute the detail builder against the fixture and return dict rows."""
    engine = _fixture_engine()
    query = get_jute_mr_issue_detail_query(
        has_mr_filter="mr_id" in params,
        has_date_filter="date_from" in params,
    )
    with engine.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(query, params).fetchall()]


class TestJuteMrIssueDetailExecuted:
    """The builder run for real, not string-matched."""

    DATE_RANGE = {"co_id": 1, "branch_id": 2,
                  "date_from": "2026-05-01", "date_to": "2026-05-31"}

    @pytest.fixture(scope="class")
    def rows(self):
        return _run(**self.DATE_RANGE)

    def test_exact_row_count(self, rows):
        # MR1 line 10 -> 2 issue rows; MR2 line 20 -> 1 zero-issue row;
        # MR3 line 30 -> 1 row (its only issue is rejected); MR4 -> no date.
        assert len(rows) == 4

    def test_two_issues_on_one_line_produce_two_rows(self, rows):
        line10 = [r for r in rows if r["jute_mr_li_id"] == 10]
        assert sorted(r["jute_issue_id"] for r in line10) == [100, 101]

    def test_zero_issue_line_has_every_issue_column_null(self, rows):
        line20 = [r for r in rows if r["jute_mr_li_id"] == 20]
        assert len(line20) == 1
        assert ({c: line20[0][c] for c in _ISSUE_SIDE_COLUMNS}
                == dict.fromkeys(_ISSUE_SIDE_COLUMNS, None))

    def test_zero_issue_line_still_carries_its_mr_data(self, rows):
        """NULLing the issue side must not blank the MR side."""
        line20 = [r for r in rows if r["jute_mr_li_id"] == 20][0]
        assert line20["quality"] == "D TD 5"
        assert line20["received_qty"] == 200
        assert line20["mr_number"] == "EJM/F/JMR/26-27/00008"

    def test_rejected_issue_excluded_without_dropping_its_mr_line(self, rows):
        line30 = [r for r in rows if r["jute_mr_li_id"] == 30]
        assert len(line30) == 1
        assert line30[0]["jute_issue_id"] is None
        assert line30[0]["issue_item_name"] is None
        assert 102 not in [r["jute_issue_id"] for r in rows]

    def test_mr_line_item_fallback_still_fires_for_a_null_issue_item(self, rows):
        issue101 = [r for r in rows if r["jute_issue_id"] == 101][0]
        assert issue101["issue_item_name"] == "D TD 5"

    def test_all_null_date_mr_is_dropped_by_the_date_filter(self, rows):
        """
        Not an eligibility failure — the same line comes back when the MR is
        picked directly. With out_date, jute_mr_date AND jute_gate_entry_date
        all NULL there is no date to bucket it into.
        """
        assert 40 not in [r["jute_mr_li_id"] for r in rows]
        picked = _run(co_id=1, branch_id=2, mr_id=4)
        assert [r["jute_mr_li_id"] for r in picked] == [40]

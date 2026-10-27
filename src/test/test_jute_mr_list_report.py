"""
Tests for the Jute MR List report endpoint.
Tests for src/juteProcurement/reports.py @ GET /api/juteReports/mr-list
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh

client = TestClient(app)


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


class TestJuteMrListReport:
    """Tests for GET /api/juteReports/mr-list."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        """Override FastAPI dependencies for all endpoint tests."""
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def test_missing_co_id_returns_400(self):
        """Should return 400 when co_id is missing."""
        response = client.get(
            "/api/juteReports/mr-list"
            "?date_from=2026-01-01&date_to=2026-01-31"
        )
        assert response.status_code == 400
        assert "co_id" in response.json().get("detail", "").lower()

    def test_missing_date_from_returns_400(self):
        """Should return 400 when date_from is missing."""
        response = client.get(
            "/api/juteReports/mr-list?co_id=1&date_to=2026-01-31"
        )
        assert response.status_code == 400
        assert "date_from" in response.json().get("detail", "").lower()

    def test_missing_date_to_returns_400(self):
        """Should return 400 when date_to is missing."""
        response = client.get(
            "/api/juteReports/mr-list?co_id=1&date_from=2026-01-01"
        )
        assert response.status_code == 400
        assert "date_to" in response.json().get("detail", "").lower()

    def test_bad_date_format_returns_400(self):
        """Should return 400 when date_from is not in YYYY-MM-DD format."""
        response = client.get(
            "/api/juteReports/mr-list"
            "?co_id=1&date_from=01-01-2026&date_to=2026-01-31"
        )
        assert response.status_code == 400
        assert "date_from" in response.json().get("detail", "").lower()

    def test_bad_date_to_format_returns_400(self):
        """Should return 400 when date_to is not in YYYY-MM-DD format."""
        response = client.get(
            "/api/juteReports/mr-list"
            "?co_id=1&date_from=2026-01-01&date_to=31/01/2026"
        )
        assert response.status_code == 400
        assert "date_to" in response.json().get("detail", "").lower()

    def test_invalid_co_id_returns_400(self):
        """Should return 400 when co_id is not an integer."""
        response = client.get(
            "/api/juteReports/mr-list"
            "?co_id=abc&date_from=2026-01-01&date_to=2026-01-31"
        )
        assert response.status_code == 400

    def test_happy_path_returns_200_with_data_and_total(self):
        """Should return 200 with a data list and total when the query succeeds."""
        row = _mock_row({
            "jute_mr_id": 101,
            "mr_number": "ABC/FAC/JMR/25-26/00001",
            "jute_mr_date": "2026-01-15",
            "branch_id": 1,
            "branch_name": "Main Factory",
            "party_id": 55,
            "party_name": "Acme Jute Traders",
            "vehicle_no": "WB12AB1234",
            "challan_no": "CH/001",
            "challan_date": "2026-01-14",
            "gross_weight": 5000.0,
            "net_weight": 4800.0,
            "actual_weight": 4750.0,
            "total_amount": 120000.0,
            "net_total": 118500.0,
            "status_id": 3,
            "status_name": "Approved",
        })

        # Sequence of execute() calls in the handler:
        #   1) list query  -> .fetchall() returns rows
        #   2) count query -> .scalar() returns total count
        list_execute_result = MagicMock()
        list_execute_result.fetchall.return_value = [row]

        count_execute_result = MagicMock()
        count_execute_result.scalar.return_value = 1

        self._mock_session.execute.side_effect = [
            list_execute_result,
            count_execute_result,
        ]

        response = client.get(
            "/api/juteReports/mr-list"
            "?co_id=1&branch_id=1"
            "&date_from=2026-01-01&date_to=2026-01-31"
            "&search=Acme&page=1&limit=50"
        )

        assert response.status_code == 200
        body = response.json()
        assert "data" in body
        assert "total" in body
        assert body["total"] == 1
        assert isinstance(body["data"], list)
        assert len(body["data"]) == 1

        first = body["data"][0]
        # Verify all expected aliased columns are present
        expected_keys = {
            "jute_mr_id", "mr_number", "jute_mr_date", "branch_id", "branch_name",
            "party_id", "party_name", "vehicle_no", "challan_no", "challan_date",
            "gross_weight", "net_weight", "actual_weight", "total_amount",
            "net_total", "status_id", "status_name",
        }
        assert expected_keys.issubset(set(first.keys()))
        assert first["jute_mr_id"] == 101
        assert first["mr_number"] == "ABC/FAC/JMR/25-26/00001"
        assert first["status_id"] == 3

    def test_happy_path_without_branch_and_search(self):
        """Should return 200 with empty data when no rows match; optional params omitted."""
        list_execute_result = MagicMock()
        list_execute_result.fetchall.return_value = []

        count_execute_result = MagicMock()
        count_execute_result.scalar.return_value = 0

        self._mock_session.execute.side_effect = [
            list_execute_result,
            count_execute_result,
        ]

        response = client.get(
            "/api/juteReports/mr-list"
            "?co_id=1&date_from=2026-01-01&date_to=2026-01-31"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["data"] == []
        assert body["total"] == 0


class TestJuteMrListStatusMode:
    """GET /api/juteReports/mr-list?status_ids=... (owner request 2026-10-07: tick
    any mix of statuses; the Tally sheet stays approved-only)."""

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def _run(self, extra: str) -> list:
        list_result = MagicMock()
        list_result.fetchall.return_value = []
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        self._mock_session.execute.side_effect = [list_result, count_result]
        response = client.get(
            "/api/juteReports/mr-list?co_id=2&branch_id=29&date_from=2026-09-01&date_to=2026-09-30" + extra
        )
        assert response.status_code == 200, response.text
        return [str(call.args[0]) for call in self._mock_session.execute.call_args_list]

    def test_no_status_ids_lists_every_mr_by_mr_or_gate_entry_date(self):
        """A Pending lorry out on a transfer chain has no MR date yet: it is
        placed by its gate-entry date and shown with its status."""
        list_sql, count_sql = self._run("")
        for sql in (list_sql, count_sql):
            assert "status_id IN" not in sql
            assert "COALESCE(jm.jute_mr_date, jm.jute_gate_entry_date) BETWEEN :date_from AND :date_to" in sql
        assert "jm.jute_gate_entry_date," in list_sql and "jm.is_returned," in list_sql
        assert "ORDER BY COALESCE(jm.jute_mr_date, jm.jute_gate_entry_date) DESC" in list_sql

    def test_status_ids_filter_to_the_ticked_statuses(self):
        list_sql, count_sql = self._run("&status_ids=1, 3,13")
        for sql in (list_sql, count_sql):
            assert "AND jm.status_id IN (1, 3, 13)" in sql

    def test_bad_status_ids_is_400(self):
        response = client.get(
            "/api/juteReports/mr-list?co_id=2&date_from=2026-09-01&date_to=2026-09-30&status_ids=3,x"
        )
        assert response.status_code == 400
        assert "status_ids" in response.json()["detail"]

    def test_statuses_endpoint_lists_company_mr_statuses(self):
        result = MagicMock()
        result.fetchall.return_value = [_mock_row({"status_id": 3, "status_name": "APPROVED"})]
        self._mock_session.execute.side_effect = [result]
        response = client.get("/api/juteReports/mr-list-statuses?co_id=2")
        assert response.status_code == 200
        assert response.json() == {"data": [{"status_id": 3, "status_name": "APPROVED"}]}
        assert self._mock_session.execute.call_args.args[1] == {"co_id": 2}

    def test_statuses_endpoint_requires_co_id(self):
        assert client.get("/api/juteReports/mr-list-statuses").status_code == 400

    def test_gate_entry_date_is_serialised(self):
        from datetime import date as _date
        row = _mock_row({"jute_mr_id": 1, "jute_mr_date": None, "jute_gate_entry_date": _date(2026, 9, 3),
                         "challan_date": None, "status_id": 13, "status_name": "Pending", "is_returned": 0})
        list_result = MagicMock()
        list_result.fetchall.return_value = [row]
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        self._mock_session.execute.side_effect = [list_result, count_result]
        response = client.get(
            "/api/juteReports/mr-list?co_id=2&date_from=2026-09-01&date_to=2026-09-30"
        )
        assert response.status_code == 200
        first = response.json()["data"][0]
        assert first["jute_gate_entry_date"] == "2026-09-03" and first["jute_mr_date"] is None
        assert first["status_name"] == "Pending"


class TestJuteTallyApprovedOnly:
    """The Tally sheet carries Approved (3) MRs only, whatever the MR list shows."""

    def test_every_tally_query_filters_status_3_only(self):
        from src.juteProcurement.reportQueries import (
            get_jute_purchase_tally_header_query,
            get_jute_purchase_tally_line_query,
        )
        for query in (get_jute_purchase_tally_header_query(), get_jute_purchase_tally_line_query()):
            sql = str(query)
            assert "status_id IN (3)" in sql
            assert "3, 13" not in sql

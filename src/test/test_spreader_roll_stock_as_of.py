"""Optional as_of_dt ("as at" datetime) cutoff on spreader roll stock endpoints."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.main import app

client = TestClient(app)


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _exec_params(mock_session, call_index: int) -> dict:
    return mock_session.execute.call_args_list[call_index][0][1]


class TestRollStockAsOf:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def _stub_stock(self, rows=None):
        # roll_stock executes: bins-with-stock, then maturity map
        self._mock_session.execute.return_value.fetchall.side_effect = [rows or [], []]

    def test_absent_as_of_dt_binds_none(self):
        self._stub_stock()
        resp = client.get("/api/spreaderProd/roll_stock?co_id=1")
        assert resp.status_code == 200
        assert _exec_params(self._mock_session, 0)["as_of_dt"] is None

    def test_valid_as_of_dt_bound_as_datetime(self):
        self._stub_stock()
        resp = client.get("/api/spreaderProd/roll_stock?co_id=1&as_of_dt=2026-08-20T14:00")
        assert resp.status_code == 200
        assert _exec_params(self._mock_session, 0)["as_of_dt"] == datetime(2026, 8, 20, 14, 0)

    def test_invalid_as_of_dt_400(self):
        resp = client.get("/api/spreaderProd/roll_stock?co_id=1&as_of_dt=garbage")
        assert resp.status_code == 400
        assert "as_of_dt" in resp.json()["detail"]

    def test_maturity_measured_against_as_of_dt(self):
        row = _mock_row(
            {
                "bin_id": 1,
                "bin_code": "B1",
                "entry_id_grp": 101,
                "item_id": 10,
                "item_name": "Jute",
                "item_id_2": None,
                "ratio_1": 1,
                "ratio_2": None,
                "quality_label": "Jute",
                "produced_rolls": 10,
                "issued_rolls": 2,
                "current_rolls": 8,
                "produced_kg": 500.0,
                "issued_kg": 100.0,
                "current_kg": 400.0,
                "avg_entry_dt": datetime(2026, 8, 20, 9, 0),  # 5h before the cutoff
            }
        )
        self._stub_stock([row])
        resp = client.get("/api/spreaderProd/roll_stock?co_id=1&as_of_dt=2026-08-20T14:00")
        assert resp.status_code == 200
        assert resp.json()["data"][0]["maturity_hrs"] == 5

    def test_quality_summary_accepts_as_of_dt(self):
        self._mock_session.execute.return_value.fetchall.return_value = []
        resp = client.get(
            "/api/spreaderProd/roll_stock_quality_summary?co_id=1&as_of_dt=2026-08-20T14:00"
        )
        assert resp.status_code == 200
        assert _exec_params(self._mock_session, 0)["as_of_dt"] == datetime(2026, 8, 20, 14, 0)

    def test_quality_summary_absent_binds_none(self):
        self._mock_session.execute.return_value.fetchall.return_value = []
        resp = client.get("/api/spreaderProd/roll_stock_quality_summary?co_id=1")
        assert resp.status_code == 200
        assert _exec_params(self._mock_session, 0)["as_of_dt"] is None

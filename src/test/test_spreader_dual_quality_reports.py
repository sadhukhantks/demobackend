"""Tests for dual-quality handling in jute production reports."""

from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db

client = TestClient(app)


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _prod_row(**over):
    base = {
        "spreader_prod_entry_id": 1, "entry_date": date(2026, 8, 14), "entry_time": 8,
        "entry_dt": None, "spell": "A1", "machine_id": 4, "machine_name": "SPREADER 1",
        "mech_code": "SP1", "item_id": 10, "item_name": "JTD5",
        "item_id_2": 20, "ratio_1": 2, "ratio_2": 1, "item_name_2": "ATD5",
        "quality_label": "JTD5/ATD5 (2:1)",
        "no_of_rolls": 3, "wt_per_roll": 65.0, "weight_kg": 195.0,
    }
    base.update(over)
    return _mock_row(base)


class TestProductionSummary:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def test_pivot_keyed_by_blend_label(self):
        self._mock_session.execute.return_value.fetchall.return_value = [
            _prod_row(),
            _prod_row(spreader_prod_entry_id=2, item_id=10, item_id_2=None, ratio_2=None,
                      item_name_2=None, quality_label="JTD5"),
        ]
        resp = client.get("/api/juteProductionReports/spreader_production_summary?co_id=1&report_date=2026-08-14")
        assert resp.status_code == 200
        data = resp.json()["data"]
        pivot = data["quality_pivot"]
        # keyed on the blend tuple, labelled with the server-built quality_label
        assert {p["entity_id"] for p in pivot} == {"10-20-2-1", "10-0-2-0"}
        assert {p["entity_name"] for p in pivot} == {"JTD5/ATD5 (2:1)", "JTD5"}
        assert data["totals"]["unique_items"] == 2

        # spreader_pivot still keys on machine_id (unchanged by the quality_label re-key)
        # and aggregates no_of_rolls across both rows (same machine, same shift): 3 + 3 = 6.
        spreader_pivot = data["spreader_pivot"]
        assert len(spreader_pivot) == 1
        assert spreader_pivot[0]["entity_id"] == 4
        assert spreader_pivot[0]["entity_name"] == "SPREADER 1"
        assert spreader_pivot[0]["Total"] == 6

    def test_pivot_keeps_null_label_and_same_name_items_apart(self):
        # a NULL label (orphaned item row) must still produce a pivot row, and two
        # distinct items sharing a name must not merge into one.
        self._mock_session.execute.return_value.fetchall.return_value = [
            _prod_row(item_id=10, item_id_2=None, ratio_1=1, ratio_2=None,
                      item_name_2=None, quality_label=None),
            _prod_row(spreader_prod_entry_id=2, item_id=11, item_id_2=None, ratio_1=1,
                      ratio_2=None, item_name="JTD5", item_name_2=None, quality_label="JTD5"),
            _prod_row(spreader_prod_entry_id=3, item_id=12, item_id_2=None, ratio_1=1,
                      ratio_2=None, item_name="JTD5", item_name_2=None, quality_label="JTD5"),
        ]
        resp = client.get("/api/juteProductionReports/spreader_production_summary?co_id=1&report_date=2026-08-14")
        assert resp.status_code == 200
        data = resp.json()["data"]
        pivot = data["quality_pivot"]
        assert len(pivot) == 3
        assert {p["entity_id"] for p in pivot} == {"10-0-1-0", "11-0-1-0", "12-0-1-0"}
        # NULL label falls back to the key itself
        assert next(p for p in pivot if p["entity_id"] == "10-0-1-0")["entity_name"] == "10-0-1-0"
        assert data["totals"]["unique_items"] == 3

    def test_item_filter_matches_second_component(self):
        self._mock_session.execute.return_value.fetchall.return_value = [_prod_row()]
        resp = client.get(
            "/api/juteProductionReports/spreader_production_summary"
            "?co_id=1&report_date=2026-08-14&items=20"
        )
        assert len(resp.json()["data"]["rows"]) == 1

    def test_item_filter_excludes_nonmatching(self):
        self._mock_session.execute.return_value.fetchall.return_value = [_prod_row()]
        resp = client.get(
            "/api/juteProductionReports/spreader_production_summary"
            "?co_id=1&report_date=2026-08-14&items=99"
        )
        assert len(resp.json()["data"]["rows"]) == 0

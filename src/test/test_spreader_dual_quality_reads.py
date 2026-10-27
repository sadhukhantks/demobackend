"""Tests for blend fields in spreader read endpoints."""

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


def _stock_row(**over):
    base = {
        "bin_id": 1, "bin_code": "B1", "entry_id_grp": 6,
        "item_id": 10, "item_name": "JTD5",
        "item_id_2": 20, "item_name_2": "ATD5", "ratio_1": 2, "ratio_2": 1,
        "quality_label": "JTD5/ATD5 (2:1)",
        "produced_rolls": 10, "issued_rolls": 0, "current_rolls": 10,
        "produced_kg": 650.0, "issued_kg": 0.0, "current_kg": 650.0,
        "avg_entry_dt": None,
    }
    base.update(over)
    return _mock_row(base)


class TestBase:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()


class TestRollStock(TestBase):
    def test_blend_fields_and_weighted_maturity(self):
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [_stock_row()],
            [_mock_row({"item_id": 10, "maturity_hours": 24}), _mock_row({"item_id": 20, "maturity_hours": 36})],
        ]
        resp = client.get("/api/spreaderProd/roll_stock?co_id=1")
        assert resp.status_code == 200
        row = resp.json()["data"][0]
        assert row["quality_label"] == "JTD5/ATD5 (2:1)"
        assert row["item_id_2"] == 20
        assert row["target_maturity_hrs"] == 28  # (24*2 + 36*1) / 3

    def test_pure_row_unchanged(self):
        self._mock_session.execute.return_value.fetchall.side_effect = [
            [_stock_row(item_id_2=None, item_name_2=None, ratio_2=None, quality_label="JTD5")],
            [_mock_row({"item_id": 10, "maturity_hours": 24})],
        ]
        resp = client.get("/api/spreaderProd/roll_stock?co_id=1")
        row = resp.json()["data"][0]
        assert row["quality_label"] == "JTD5"
        assert row["item_id_2"] is None
        assert row["target_maturity_hrs"] == 24


class TestQualitySummary(TestBase):
    def test_blend_own_row_with_component_weights(self):
        self._mock_session.execute.return_value.fetchall.return_value = [
            _stock_row(),
            _stock_row(entry_id_grp=7, current_rolls=5, current_kg=325.0),
            _stock_row(entry_id_grp=8, item_id=10, item_id_2=None, item_name_2=None,
                       ratio_2=None, quality_label="JTD5", current_rolls=4, current_kg=260.0),
        ]
        resp = client.get("/api/spreaderProd/roll_stock_quality_summary?co_id=1")
        data = resp.json()["data"]
        assert len(data) == 2  # blend row + pure row, not merged
        blend = next(d for d in data if d["item_id_2"] == 20)
        assert blend["closing_rolls"] == 15
        assert blend["closing_kg"] == 975.0
        assert blend["comp1_kg"] == 650.0   # 975 * 2/3
        assert blend["comp2_kg"] == 325.0
        pure = next(d for d in data if d["item_id_2"] is None)
        assert pure["comp1_kg"] == 260.0
        assert pure["comp2_kg"] == 0.0

    def test_summary_blend_with_null_ratio_2_does_not_500(self):
        # malformed import row: item_id_2 set but ratio_2 NULL
        self._mock_session.execute.return_value.fetchall.return_value = [
            # quality_label is NULL because the query builds it with MySQL CONCAT,
            # which returns NULL when ratio_2 is NULL -> falls through to blend_label
            _stock_row(ratio_1=2, ratio_2=None, current_rolls=6, current_kg=300.0,
                       quality_label=None),
        ]
        resp = client.get("/api/spreaderProd/roll_stock_quality_summary?co_id=1")
        assert resp.status_code == 200
        row = resp.json()["data"][0]
        assert row["quality_label"] == "JTD5/ATD5 (2:1)"  # label falls back to ratio_2=1
        assert row["comp1_kg"] == 200.0   # 300 * 2/(2+1), ratio_2 treated as 1
        assert row["comp2_kg"] == 100.0
        assert row["ratio_2"] is None     # stored ratio untouched

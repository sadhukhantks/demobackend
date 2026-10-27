"""Tests for dual-quality spreader entry create."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db

client = TestClient(app)


def _exec_params(mock_session, call_index: int) -> dict:
    return mock_session.execute.call_args_list[call_index][0][1]


BASE = {
    "co_id": 1,
    "branch_id": 2,
    "entry_date": "2026-08-14",
    "entry_time": 8,
    "spell": "A1",
    "machine_id": 4,
    "item_id": 10,
    "bin_id": 2,
    "no_of_rolls": 3,
    "wt_per_roll": 65.0,
}


class TestDualQualityEntryCreate:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    @patch("src.juteProduction.spreader_entry.assign_or_reuse_entry_id_grp")
    def test_old_payload_creates_pure_roll(self, mock_grp):
        mock_grp.return_value = (101, False)
        ins = MagicMock()
        ins.lastrowid = 9
        self._mock_session.execute.side_effect = [ins]
        resp = client.post("/api/spreaderProd/entry_create", json=BASE)
        assert resp.status_code == 200
        params = _exec_params(self._mock_session, 0)
        assert params["item_id"] == 10
        assert params["item_id_2"] is None
        assert params["ratio_1"] == 1
        assert params["ratio_2"] is None
        # blend passed to group assignment as canonical tuple
        assert mock_grp.call_args[0][3] == (10, None, 1, None)

    @patch("src.juteProduction.spreader_entry.assign_or_reuse_entry_id_grp")
    def test_blend_payload_canonicalized(self, mock_grp):
        mock_grp.return_value = (101, False)
        ins = MagicMock()
        ins.lastrowid = 9
        self._mock_session.execute.side_effect = [ins]
        body = {**BASE, "item_id": 20, "item_id_2": 10, "ratio_1": 1, "ratio_2": 2}
        resp = client.post("/api/spreaderProd/entry_create", json=body)
        assert resp.status_code == 200
        params = _exec_params(self._mock_session, 0)
        # dominant-first: (10, 20, 2, 1)
        assert params["item_id"] == 10
        assert params["item_id_2"] == 20
        assert params["ratio_1"] == 2
        assert params["ratio_2"] == 1

    def test_same_item_twice_rejected(self):
        body = {**BASE, "item_id_2": 10, "ratio_1": 2, "ratio_2": 1}
        resp = client.post("/api/spreaderProd/entry_create", json=body)
        assert resp.status_code == 422

    def test_ratio_2_without_item_2_rejected(self):
        body = {**BASE, "ratio_2": 1}
        resp = client.post("/api/spreaderProd/entry_create", json=body)
        assert resp.status_code == 422

    def test_item_2_without_ratio_2_rejected(self):
        body = {**BASE, "item_id_2": 20}
        resp = client.post("/api/spreaderProd/entry_create", json=body)
        assert resp.status_code == 422

    def test_zero_ratio_rejected(self):
        body = {**BASE, "item_id_2": 20, "ratio_1": 0, "ratio_2": 1}
        resp = client.post("/api/spreaderProd/entry_create", json=body)
        assert resp.status_code == 422

    def test_overflow_no_of_rolls_rejected(self):
        # past MySQL INT range: must fail validation, not reach the INSERT as a 500
        body = {**BASE, "no_of_rolls": 2147483648}
        resp = client.post("/api/spreaderProd/entry_create", json=body)
        assert resp.status_code == 422

    @patch("src.juteProduction.spreader_entry.assign_or_reuse_entry_id_grp")
    def test_blend_lock_violation_maps_to_400(self, mock_grp):
        mock_grp.side_effect = ValueError(
            "This bin already has an open group with a different quality: JTD5/ATD5 (2:1)."
        )
        resp = client.post("/api/spreaderProd/entry_create", json=BASE)
        assert resp.status_code == 400
        assert "JTD5/ATD5 (2:1)" in resp.json()["detail"]

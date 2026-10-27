"""Beaming production meter reading (opening/closing yard counter, like Drawing).

Rules: diff = close - open (counter wrap when close < open); with a reading the diff IS
act_prod_yards, kg = kg_per_cut × diff / laid_length, and act_cuts per beam is derived
as diff / laid_length / no_of_beam. Both readings or neither.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.juteProduction.beaming_entry import _apply_meter_cuts, _meter_reading
from src.juteProduction.services.beaming_rules import (
    compute_beaming_daily,
    compute_diff_meter,
    derive_act_cuts,
)

STANDARDS = {
    "ends": 272, "std_count": 13.0, "act_count": 13.0, "laid_length": 174.0,
    "std_cuts_per_beam": 82.0, "std_speed": 100.0, "target_speed": 110.0,
    "act_rpm": 0.0, "std_eff": 85.0, "target_eff": 90.0, "working_hours": 8.0,
    "dia": 12.0,
}


class TestDiffMeter:
    def test_plain_run(self):
        assert compute_diff_meter(1000, 15268, 0) == 14268

    def test_counter_wrap_uses_machine_limit(self):
        assert compute_diff_meter(99900, 50, 100000) == 150
        assert compute_diff_meter(9900, 50, 10000) == 150

    def test_no_wrap_limit_stays_negative(self):
        assert compute_diff_meter(500, 400, 0) == -100

    def test_derive_cuts_rounds_to_whole_cut(self):
        # 57072 yd / 174 laid / 4 beams = 82 cuts
        assert derive_act_cuts(57072, 174, 4) == 82
        assert derive_act_cuts(57200, 174, 4) == 82

    def test_derive_cuts_without_laid_length_is_zero(self):
        assert derive_act_cuts(57072, 0, 4) == 0


class TestComputeMeterBasis:
    def test_meter_diff_is_actual_yards(self):
        out = compute_beaming_daily({"act_cuts": 82, "no_of_beam": 4, "diff_meter": 57200}, STANDARDS)
        assert out["act_prod_yards"] == 57200
        assert out["act_prod_kg"] == round(out["kg_per_cut"] * 57200 / 174.0, 3)

    def test_no_meter_keeps_cuts_basis(self):
        out = compute_beaming_daily({"act_cuts": 82, "no_of_beam": 4}, STANDARDS)
        assert out["act_prod_yards"] == 174.0 * 82 * 4


class TestMeterReadingValidation:
    def test_neither_is_cuts_basis(self):
        assert _meter_reading(None, None) == {"open_meter": None, "close_meter": None, "diff_meter": None}

    def test_one_without_other_is_400(self):
        with pytest.raises(HTTPException) as exc:
            _meter_reading(100, None)
        assert exc.value.status_code == 400

    def test_zero_run_is_400(self):
        with pytest.raises(HTTPException):
            _meter_reading(500, 500)

    def test_pair_returns_diff(self):
        assert _meter_reading(1000, 1500)["diff_meter"] == 500

    def test_backwards_without_wrap_limit_is_400(self):
        with pytest.raises(HTTPException) as exc:
            _meter_reading(500, 400, lambda: 0.0)
        assert "Meter Wrap Limit" in exc.value.detail

    def test_backwards_with_wrap_limit_wraps(self):
        assert _meter_reading(9900, 50, lambda: 10000.0)["diff_meter"] == 150


class TestApplyMeterCuts:
    def test_meter_overrides_entered_cuts(self):
        inputs = {"act_cuts": 1, "no_of_beam": 4}
        _apply_meter_cuts(inputs, {"diff_meter": 57072}, {"laid_length": 174.0})
        assert inputs["act_cuts"] == 82

    def test_no_laid_length_keeps_entered_cuts(self):
        inputs = {"act_cuts": 80, "no_of_beam": 4}
        _apply_meter_cuts(inputs, {"diff_meter": 57072}, {"laid_length": 0})
        assert inputs["act_cuts"] == 80

    def test_no_cuts_at_all_is_400(self):
        with pytest.raises(HTTPException):
            _apply_meter_cuts({"act_cuts": 0, "no_of_beam": 4}, {"diff_meter": 57072}, {"laid_length": 0})


class TestMeterPrevStateEndpoint:
    def setup_method(self):
        from fastapi.testclient import TestClient
        from src.main import app
        from src.config.db import get_tenant_db
        from src.authorization.utils import get_current_user_with_refresh

        self.app = app
        self.session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.session
        self.client = TestClient(app)

    def teardown_method(self):
        self.app.dependency_overrides.clear()

    def test_prefills_last_close(self):
        row = MagicMock(); row.close_meter = 15268
        row.value = 10000  # resolve_param reads the meter_wrap standard first
        self.session.execute.return_value.fetchone.return_value = row
        r = self.client.get(
            "/api/beamingProd/meter_prev_state",
            params={"co_id": 1, "machine_id": 9, "spell_id": 110, "tran_date": "2026-10-24"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["open_meter"] == 15268
        assert r.json()["data"]["has_prev"] is True
        assert r.json()["data"]["meter_wrap_limit"] == 10000

    def test_no_history_is_zero(self):
        self.session.execute.return_value.fetchone.return_value = None  # no wrap set either
        r = self.client.get(
            "/api/beamingProd/meter_prev_state",
            params={"co_id": 1, "machine_id": 9, "spell_id": 110, "tran_date": "2026-10-24"},
        )
        assert r.json()["data"] == {"open_meter": 0.0, "has_prev": False, "meter_wrap_limit": 0.0}

    def test_missing_params_400(self):
        r = self.client.get("/api/beamingProd/meter_prev_state", params={"co_id": 1})
        assert r.status_code == 400

"""Tests for the Holiday & Attendance Incentive Eligibility validation rules."""

import pytest
from fastapi import HTTPException

from src.hrms.holidayIncEligibility import _validate


def _body(**overrides):
    body = {
        "eb_id": 12,
        "holiday_eligibility": "Y",
        "att_incn_eligibility": "N",
        "fn_att_inc_rate": 0,
        "mn_att_inc_rate": 0,
    }
    body.update(overrides)
    return body


class TestEligibilityValidation:
    def test_incentive_no_with_zero_rates_is_valid(self):
        values = _validate(_body())
        assert values["eb_id"] == 12
        assert values["att_incn_eligibility"] == "N"

    def test_incentive_yes_needs_a_rate(self):
        with pytest.raises(HTTPException) as exc:
            _validate(_body(att_incn_eligibility="Y"))
        assert exc.value.status_code == 400

    def test_incentive_no_rejects_a_rate(self):
        with pytest.raises(HTTPException) as exc:
            _validate(_body(fn_att_inc_rate=25))
        assert exc.value.status_code == 400

    def test_incentive_yes_with_monthly_rate_only_is_valid(self):
        values = _validate(_body(att_incn_eligibility="Y", mn_att_inc_rate="150.50"))
        assert float(values["mn_att_inc_rate"]) == 150.50

    def test_employee_is_required(self):
        with pytest.raises(HTTPException):
            _validate(_body(eb_id=None))

    def test_negative_rate_rejected(self):
        with pytest.raises(HTTPException):
            _validate(_body(att_incn_eligibility="Y", fn_att_inc_rate=-5))

    def test_flag_must_be_y_or_n(self):
        with pytest.raises(HTTPException):
            _validate(_body(holiday_eligibility="X"))

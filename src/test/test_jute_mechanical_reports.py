"""Tests for the maintenance reports — Jute Mechanical & Maintenance.

Covers ``src/juteMechanical/reports.py`` (prefix ``/api/mechMaintReports``).

``_metrics`` is the whole point of this file. Every one of the twelve reports
routes its numbers through it, so a drift there is a drift everywhere. The spec's
"Metric formulas" block is pinned here as an executable worked example::

    4 breakdowns / 240 downtime min / 30 days
      available_min    = 30 * 1440      = 43200
      mttr_min         = 240 / 4        = 60
      mtbf_min         = (43200-240)/4  = 10740
      availability_pct = 42960/43200*100 = 99.44

Equally important: EVERY zero-denominator path must return ``None`` rather than
raising. An empty date range, a machine with no failures, a report run before any
data exists — each must render an empty cell, never a 500. Those are asserted one
by one.

``_metrics`` and its supporting helpers are pure, so they are called directly.
The endpoint tests use ``app.dependency_overrides`` (``Depends(get_tenant_db)``
is bound at router-registration time, so a module-attribute patch would not
intercept it). Never touches a real database.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteMechanical import reports as rp

client = TestClient(app)


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


_UNSET = object()


def _exec_result(*, fetchall=_UNSET, fetchone=_UNSET, scalar=_UNSET):
    res = MagicMock()
    if fetchall is not _UNSET:
        res.fetchall.return_value = fetchall
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if scalar is not _UNSET:
        res.scalar.return_value = scalar
    return res


# Every report path, for the co_id gate sweep.
REPORT_PATHS = [
    "breakdown-register",
    "machine-summary",
    "reason-pareto",
    "date-machine-grid",
    "department-summary",
    "reliability",
    "reason-trend",
    "maintenance-due",
    "spares-consumption",
    "top-problem-machines",
    "machine-history",
    "open-breakdown-board",
]


# =============================================================================
# 1. _metrics — the locked metric set
# =============================================================================


class TestMetricsWorkedExample:
    """The spec's worked example, pinned exactly."""

    @pytest.fixture
    def metrics(self):
        return rp._metrics(count=4, downtime_min=240, nr_downtime_min=0, days=30)

    def test_breakdown_count(self, metrics):
        assert metrics["breakdown_count"] == 4

    def test_total_downtime(self, metrics):
        assert metrics["total_downtime_min"] == 240

    def test_days_in_range(self, metrics):
        assert metrics["days_in_range"] == 30

    def test_available_min_is_days_times_1440(self, metrics):
        assert metrics["available_min"] == 43200
        assert rp.AVAILABLE_MIN_PER_DAY == 1440.0

    def test_mttr(self, metrics):
        """MTTR = SUM(duration) / COUNT = 240 / 4."""
        assert metrics["mttr_min"] == 60

    def test_mtbf(self, metrics):
        """MTBF = (available - downtime) / COUNT = 42960 / 4."""
        assert metrics["mtbf_min"] == 10740

    def test_availability_pct(self, metrics):
        """(43200 - 240) / 43200 * 100, rounded to 2dp."""
        assert metrics["availability_pct"] == 99.44

    def test_unattributed_pct_is_zero_when_no_nr_downtime(self, metrics):
        assert metrics["unattributed_pct"] == 0.0

    def test_downtime_per_1000_running_hrs(self, metrics):
        # running_hrs = 42960 / 60 = 716; 240 * 1000 / 716 = 335.20
        assert metrics["downtime_per_1000_running_hrs"] == 335.2

    def test_unattributed_pct_from_the_nr_share(self):
        """60 of 240 downtime minutes sit on the 'NR' code -> 25%."""
        metrics = rp._metrics(count=4, downtime_min=240, nr_downtime_min=60, days=30)
        assert metrics["nr_downtime_min"] == 60
        assert metrics["unattributed_pct"] == 25.0


class TestMetricsZeroDenominators:
    """Every ratio must degrade to None, never raise and never fake a zero."""

    def test_zero_breakdowns_yields_none_mttr_and_mtbf(self):
        metrics = rp._metrics(count=0, downtime_min=0, nr_downtime_min=0, days=30)
        assert metrics["breakdown_count"] == 0
        assert metrics["mttr_min"] is None
        assert metrics["mtbf_min"] is None
        # availability is still computable: no downtime at all -> 100%
        assert metrics["availability_pct"] == 100.0

    def test_no_days_yields_none_available_mtbf_and_availability(self):
        """days=None (unbounded range, no data) -> no denominator to invent."""
        metrics = rp._metrics(count=4, downtime_min=240, nr_downtime_min=0, days=None)
        assert metrics["available_min"] is None
        assert metrics["mtbf_min"] is None
        assert metrics["availability_pct"] is None
        assert metrics["downtime_per_1000_running_hrs"] is None
        # MTTR does not depend on the range, so it survives.
        assert metrics["mttr_min"] == 60

    def test_zero_days_is_treated_as_no_denominator(self):
        metrics = rp._metrics(count=4, downtime_min=240, nr_downtime_min=0, days=0)
        assert metrics["available_min"] is None
        assert metrics["availability_pct"] is None

    def test_zero_total_downtime_yields_none_unattributed_pct(self):
        """0/0 for the NR share must be None, not a ZeroDivisionError."""
        metrics = rp._metrics(count=0, downtime_min=0, nr_downtime_min=0, days=30)
        assert metrics["unattributed_pct"] is None

    def test_everything_empty_returns_a_full_shape_of_nones(self):
        metrics = rp._metrics(count=None, downtime_min=None, nr_downtime_min=None, days=None)
        assert metrics["breakdown_count"] == 0
        assert metrics["total_downtime_min"] == 0
        assert metrics["mttr_min"] is None
        assert metrics["mtbf_min"] is None
        assert metrics["availability_pct"] is None
        assert metrics["unattributed_pct"] is None
        assert metrics["downtime_per_1000_running_hrs"] is None

    def test_full_outage_gives_zero_running_hours_not_a_crash(self):
        """Downtime equal to the whole window -> availability 0, ratio None."""
        metrics = rp._metrics(count=1, downtime_min=1440, nr_downtime_min=0, days=1)
        assert metrics["available_min"] == 1440
        assert metrics["availability_pct"] == 0.0
        assert metrics["mtbf_min"] == 0.0
        assert metrics["downtime_per_1000_running_hrs"] is None

    def test_every_returned_key_is_present_in_every_branch(self):
        expected = {
            "breakdown_count",
            "total_downtime_min",
            "nr_downtime_min",
            "days_in_range",
            "available_min",
            "mttr_min",
            "mtbf_min",
            "availability_pct",
            "unattributed_pct",
            "downtime_per_1000_running_hrs",
        }
        for args in [
            (4, 240, 60, 30),
            (0, 0, 0, 30),
            (4, 240, 0, None),
            (None, None, None, None),
        ]:
            assert set(rp._metrics(*args)) == expected

    def test_decimal_inputs_from_sql_are_handled(self):
        """SUM() comes back as Decimal from MySQL — it must not break the maths."""
        metrics = rp._metrics(
            count=4, downtime_min=Decimal("240.00"), nr_downtime_min=Decimal("0"), days=30
        )
        assert metrics["mttr_min"] == 60
        assert metrics["availability_pct"] == 99.44


class TestSafeDiv:
    def test_normal_division(self):
        assert rp._safe_div(10, 4) == 2.5

    def test_zero_denominator_returns_none(self):
        assert rp._safe_div(10, 0) is None

    def test_none_denominator_returns_none(self):
        assert rp._safe_div(10, None) is None

    def test_none_numerator_returns_none(self):
        assert rp._safe_div(None, 10) is None

    def test_non_numeric_returns_none(self):
        assert rp._safe_div("x", "y") is None


class TestDaysInRange:
    def test_uses_the_requested_range_inclusively(self):
        assert rp._days_in_range("2026-06-01", "2026-06-30", None, None) == 30

    def test_single_day_range_is_one_day(self):
        assert rp._days_in_range("2026-06-01", "2026-06-01", None, None) == 1

    def test_falls_back_to_the_observed_data_span(self):
        assert rp._days_in_range(
            None, None, date(2026, 6, 1), date(2026, 6, 10)
        ) == 10

    def test_narrows_datetimes_to_dates(self):
        assert rp._days_in_range(
            None, None, datetime(2026, 6, 1, 8, 0), datetime(2026, 6, 2, 23, 0)
        ) == 2

    def test_none_when_nothing_resolves(self):
        assert rp._days_in_range(None, None, None, None) is None

    def test_none_when_the_range_is_inverted(self):
        assert rp._days_in_range("2026-06-30", "2026-06-01", None, None) is None

    def test_rejects_a_bad_date_format(self):
        with pytest.raises(HTTPException) as exc:
            rp._days_in_range("30-06-2026", "2026-06-01", None, None)
        assert exc.value.status_code == 400


class TestPaging:
    def test_defaults(self):
        assert rp._paging(None, None, 100) == (100, 0)

    def test_offset_follows_the_page(self):
        assert rp._paging(3, 50, 100) == (50, 100)

    def test_limit_is_capped(self):
        limit, _ = rp._paging(1, 999999, 100)
        assert limit == rp.MAX_LIMIT

    def test_page_below_one_is_clamped(self):
        assert rp._paging(0, 10, 100) == (10, 0)
        assert rp._paging(-5, 10, 100) == (10, 0)


class TestMonthRange:
    def test_inclusive_month_span(self):
        assert rp._month_range("2026-01-15", "2026-03-02") == [
            "2026-01", "2026-02", "2026-03"
        ]

    def test_crosses_the_year_boundary(self):
        assert rp._month_range("2026-11-01", "2027-01-31") == [
            "2026-11", "2026-12", "2027-01"
        ]

    def test_single_month(self):
        assert rp._month_range("2026-06-01", "2026-06-30") == ["2026-06"]

    def test_empty_when_inverted_or_missing(self):
        assert rp._month_range("2026-06-30", "2026-01-01") == []
        assert rp._month_range(None, "2026-06-30") == []


class TestRequireCoId:
    def test_missing_co_id_raises_400(self):
        for value in (None, 0, ""):
            with pytest.raises(HTTPException) as exc:
                rp._require_co_id(value)
            assert exc.value.status_code == 400
            assert "co_id" in exc.value.detail

    def test_a_real_co_id_passes(self):
        rp._require_co_id(1)


# =============================================================================
# 2. Endpoint tests — dependency_overrides, never a real DB
# =============================================================================


class TestReportEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    @pytest.mark.parametrize("path", REPORT_PATHS)
    def test_every_report_requires_co_id(self, path):
        response = client.get(f"/api/mechMaintReports/{path}")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    def test_machine_history_requires_machine_id(self):
        response = client.get("/api/mechMaintReports/machine-history?co_id=1")
        assert response.status_code == 400
        assert "machine_id" in response.json()["detail"].lower()

    def test_machine_summary_returns_data_and_total_never_a_bare_list(self):
        """The house contract: an object with data + total, plus the metric block."""
        self._mock_session.execute.side_effect = [
            _exec_result(
                fetchall=[
                    _mock_row(
                        {
                            "machine_id": 10,
                            "machine_name": "SPR-1",
                            "mech_code": "S1",
                            "machine_type_id": 3,
                            "machine_type_name": "Spreader",
                            "dept_id": 50,
                            "dept_name": "Spreader Dept",
                            "branch_id": 1,
                            "criticality": "A",
                            "breakdown_count": 4,
                            "total_downtime_min": 240,
                            "nr_downtime_min": 0,
                            "nr_count": 0,
                            "grand_downtime_min": 480,
                            "total_response_min": 40,
                            "response_count": 4,
                            "open_count": 0,
                            "production_loss_qty": 12.5,
                            "first_breakdown_date": date(2026, 6, 1),
                            "last_breakdown_date": date(2026, 6, 30),
                        }
                    )
                ]
            ),
            _exec_result(scalar=1),          # _scalar_total
            _exec_result(                    # _fetch_summary
                fetchone=_mock_row(
                    {
                        "breakdown_count": 4,
                        "total_downtime_min": 240,
                        "nr_downtime_min": 0,
                        "nr_count": 0,
                        "machine_count": 1,
                        "open_count": 0,
                        "total_response_min": 40,
                        "response_count": 4,
                        "production_loss_qty": 12.5,
                        "first_breakdown_date": date(2026, 6, 1),
                        "last_breakdown_date": date(2026, 6, 30),
                    }
                )
            ),
        ]

        response = client.get(
            "/api/mechMaintReports/machine-summary"
            "?co_id=1&date_from=2026-06-01&date_to=2026-06-30"
        )

        assert response.status_code == 200
        payload = response.json()
        assert isinstance(payload, dict)
        assert set(["data", "total", "summary", "unattributed_pct"]) <= set(payload)
        assert payload["total"] == 1

        row = payload["data"][0]
        # The spec's worked example, end to end through the endpoint.
        assert row["breakdown_count"] == 4
        assert row["available_min"] == 43200
        assert row["mttr_min"] == 60
        assert row["mtbf_min"] == 10740
        assert row["availability_pct"] == 99.44
        assert row["days_in_range"] == 30
        assert row["pct_of_mill_downtime"] == 50.0     # 240 of 480
        assert row["avg_response_min"] == 10.0
        assert row["first_breakdown_date"] == "2026-06-01"

        assert payload["summary"]["mttr_min"] == 60
        assert payload["unattributed_pct"] == payload["summary"]["unattributed_pct"]

    def test_machine_summary_empty_result_still_returns_the_wrapper(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[]),
            _exec_result(scalar=0),
            _exec_result(fetchone=None),  # no metric row at all
        ]

        response = client.get("/api/mechMaintReports/machine-summary?co_id=1")

        assert response.status_code == 200
        payload = response.json()
        assert payload["data"] == []
        assert payload["total"] == 0
        # No data anywhere -> the ratios are None, not zero, and nothing raised.
        assert payload["summary"]["mttr_min"] is None
        assert payload["summary"]["availability_pct"] is None
        assert payload["summary"]["unattributed_pct"] is None

    def test_machine_summary_rejects_a_bad_date_format(self):
        response = client.get(
            "/api/mechMaintReports/machine-summary?co_id=1&date_from=01-06-2026"
        )
        assert response.status_code == 400
        assert "date_from" in response.json()["detail"]

    def test_breakdown_register_returns_data_and_total(self):
        self._mock_session.execute.side_effect = [
            _exec_result(
                fetchall=[
                    _mock_row(
                        {
                            "breakdown_id": 5,
                            "tran_date": date(2026, 6, 1),
                            "machine_id": 10,
                            "machine_name": "SPR-1",
                            "stop_datetime": datetime(2026, 6, 1, 8, 0),
                            "attended_datetime": None,
                            "restart_datetime": datetime(2026, 6, 1, 9, 0),
                            "duration_min": 60,
                            "response_min": None,
                            "reason_code": "RF",
                            "discipline": "MECHANICAL",
                            "downtime_type": "BREAKDOWN",
                            "status": "CLOSED",
                            "production_loss_qty": 3.5,
                        }
                    )
                ]
            ),
            _exec_result(scalar=1),
            _exec_result(
                fetchone=_mock_row(
                    {
                        "breakdown_count": 1,
                        "total_downtime_min": 60,
                        "nr_downtime_min": 0,
                        "first_breakdown_date": date(2026, 6, 1),
                        "last_breakdown_date": date(2026, 6, 1),
                    }
                )
            ),
        ]

        response = client.get("/api/mechMaintReports/breakdown-register?co_id=1")

        assert response.status_code == 200
        payload = response.json()
        assert isinstance(payload, dict)
        assert payload["total"] == 1
        row = payload["data"][0]
        assert row["breakdown_id"] == 5
        assert row["tran_date"] == "2026-06-01"
        assert row["duration_min"] == 60
        assert row["response_min"] is None
        assert row["downtime_type"] == "BREAKDOWN"
        # The register is not failure-filtered, but its summary block is.
        assert payload["summary"]["breakdown_count"] == 1

    def test_open_breakdown_board_returns_a_wrapped_object(self):
        self._mock_session.execute.return_value = _exec_result(
            fetchall=[
                _mock_row(
                    {
                        "breakdown_id": 6,
                        "machine_id": 10,
                        "machine_name": "SPR-1",
                        "stop_datetime": datetime(2026, 6, 1, 8, 0),
                        "attended_datetime": None,
                        "elapsed_min": 125,
                        "reason_code": "MT",
                        "status": "OPEN",
                    }
                )
            ],
            scalar=1,
        )

        response = client.get("/api/mechMaintReports/open-breakdown-board?co_id=1")

        assert response.status_code == 200
        payload = response.json()
        assert isinstance(payload, dict)
        assert "data" in payload and "total" in payload
        assert isinstance(payload["data"], list)

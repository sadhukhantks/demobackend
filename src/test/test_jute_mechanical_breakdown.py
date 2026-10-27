"""Tests for Breakdown Entry — Jute Mechanical & Maintenance.

Covers ``src/juteMechanical/breakdown_entry.py`` (prefix ``/api/mechBreakdown``).

Two layers, deliberately:

1. **Pure derived-field helpers** (``compute_tran_date``, ``compute_duration_min``,
   ``compute_response_min``, ``resolve_status``, ``resolve_component_id``,
   ``resolve_discipline``, ``validate_downtime_type``, ``validate_datetimes``,
   ``compute_spare_amount``) — imported and called directly. No DB, no HTTP, no
   mocking: these carry the module's real risk (spec "Derived fields") and the
   clock in ``validate_datetimes`` is injectable via ``now=`` so no test ever
   depends on the wall clock.

2. **Endpoint tests** using ``app.dependency_overrides`` for ``get_tenant_db`` and
   ``get_current_user_with_refresh`` — the same pattern as
   ``src/test/test_stoppage_entry.py``. The handlers take the session through
   ``Depends(get_tenant_db)``, which FastAPI binds at router-registration time,
   so patching the module attribute would NOT intercept it. The session is a
   MagicMock; rows expose ``._mapping`` for fetchall reads and plain attributes
   for fetchone reads.

Never touches a real database.
"""

from datetime import date, datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteMechanical import breakdown_entry as be
from src.juteMechanical.breakdown_query import (
    close_breakdown_query,
    confirm_breakdown_query,
    get_open_breakdowns_query,
    insert_breakdown_query,
    update_breakdown_query,
)
from src.juteMechanical.reportQueries import (
    get_open_breakdown_board_count_query,
    get_open_breakdown_board_query,
)
from src.juteMechanical.constants import (
    DISCIPLINE_ELECTRICAL,
    DISCIPLINE_MECHANICAL,
    DOWNTIME_BREAKDOWN,
    DOWNTIME_PLANNED_PM,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_UNDER_REPAIR,
)

client = TestClient(app)


# =============================================================================
# Row / result builders (mirrors test_stoppage_entry.py)
# =============================================================================


def _mock_row(mapping: dict):
    """A query row whose ._mapping is a plain dict (matches r._mapping usage)."""
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    """A fetchone() row exposing attributes (e.g. .machine_type_id, .branch_id)."""
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


_UNSET = object()


def _exec_result(*, fetchall=_UNSET, fetchone=_UNSET, lastrowid=_UNSET):
    """execute() result mock; the _UNSET sentinel honours an explicit fetchone=None."""
    res = MagicMock()
    if fetchall is not _UNSET:
        res.fetchall.return_value = fetchall
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if lastrowid is not _UNSET:
        res.lastrowid = lastrowid
    return res


def _machine(**over):
    """An active machine row as get_machine_for_validation_query returns it."""
    attrs = {
        "machine_id": 10,
        "machine_name": "SPR-1",
        "mech_code": "S1",
        "machine_type_id": 3,
        "machine_type_name": "Spreader",
        "dept_id": 50,
        "branch_id": 1,
    }
    attrs.update(over)
    return _attr_row(**attrs)


def _reason(**over):
    """An active reason row as get_reason_for_validation_query returns it."""
    attrs = {
        "reason_id": 7,
        "reason_code": "RF",
        "reason_desc": "Roller Fault",
        "machine_type_id": 3,
        "component_id": 42,
        "discipline": DISCIPLINE_MECHANICAL,
    }
    attrs.update(over)
    return _attr_row(**attrs)


# A stable "now" for every datetime rule; stop times sit safely in its past.
NOW = datetime(2026, 6, 1, 12, 0, 0)
STOP = datetime(2026, 6, 1, 8, 0, 0)


# =============================================================================
# 1. Pure derived-field helpers — no DB, no mocks
# =============================================================================


class TestComputeTranDate:
    def test_tran_date_is_the_stop_date(self):
        assert be.compute_tran_date(datetime(2026, 6, 1, 23, 59)) == date(2026, 6, 1)

    def test_tran_date_uses_stop_day_not_restart_day(self):
        """An incident spanning midnight still books on the day it stopped."""
        assert be.compute_tran_date(datetime(2026, 6, 1, 23, 30)) == date(2026, 6, 1)


class TestComputeDurationMin:
    def test_duration_is_none_while_open(self):
        """No restart -> the incident is still running, duration is NULL."""
        assert be.compute_duration_min(STOP, None) is None

    def test_duration_whole_minutes(self):
        assert be.compute_duration_min(STOP, datetime(2026, 6, 1, 9, 30)) == 90

    def test_duration_floors_partial_minutes(self):
        """90 seconds -> 1 minute (floor division, never rounding up)."""
        assert be.compute_duration_min(STOP, datetime(2026, 6, 1, 8, 1, 30)) == 1

    def test_duration_zero_when_restart_equals_stop(self):
        assert be.compute_duration_min(STOP, STOP) == 0

    def test_duration_spans_midnight(self):
        stop = datetime(2026, 6, 1, 23, 0)
        assert be.compute_duration_min(stop, datetime(2026, 6, 2, 1, 0)) == 120


class TestComputeResponseMin:
    def test_response_is_none_when_not_attended(self):
        assert be.compute_response_min(STOP, None) is None

    def test_response_whole_minutes(self):
        assert be.compute_response_min(STOP, datetime(2026, 6, 1, 8, 15)) == 15

    def test_response_zero_when_attended_immediately(self):
        assert be.compute_response_min(STOP, STOP) == 0


class TestResolveDurationMin:
    """Entered duration is an INDEPENDENT field — stored as typed, never checked."""

    def test_entered_value_wins_over_the_computed_one(self):
        assert be.resolve_duration_min(25, STOP, datetime(2026, 6, 1, 9, 30)) == 25

    def test_entered_value_kept_even_when_it_contradicts_the_timestamps(self):
        """User ruling: no cross-validation against stop/restart."""
        assert be.resolve_duration_min(999, STOP, datetime(2026, 6, 1, 8, 5)) == 999

    def test_derives_when_not_entered(self):
        assert be.resolve_duration_min(None, STOP, datetime(2026, 6, 1, 9, 30)) == 90

    def test_none_when_neither_entered_nor_restarted(self):
        assert be.resolve_duration_min(None, STOP, None) is None

    def test_entered_duration_without_a_restart(self):
        assert be.resolve_duration_min(45, STOP, None) == 45


class TestResolveStatus:
    def test_open_when_neither_datetime_present(self):
        assert be.resolve_status(None, None) == STATUS_OPEN

    def test_under_repair_when_attended_only(self):
        assert be.resolve_status(datetime(2026, 6, 1, 8, 5), None) == STATUS_UNDER_REPAIR

    def test_closed_when_restarted(self):
        assert be.resolve_status(None, datetime(2026, 6, 1, 9, 0)) == STATUS_CLOSED

    def test_restart_wins_over_attended(self):
        """A restart closes the incident whether or not attendance was logged."""
        assert (
            be.resolve_status(datetime(2026, 6, 1, 8, 5), datetime(2026, 6, 1, 9, 0))
            == STATUS_CLOSED
        )

    def test_closed_on_a_duration_alone(self):
        """A duration-only entry is a resolved incident, restart or no restart."""
        assert be.resolve_status(None, None, 30) == STATUS_CLOSED

    def test_duration_closes_over_attended(self):
        assert be.resolve_status(datetime(2026, 6, 1, 8, 5), None, 30) == STATUS_CLOSED

    def test_no_duration_leaves_the_datetime_rule_intact(self):
        assert be.resolve_status(None, None, None) == STATUS_OPEN
        assert (
            be.resolve_status(datetime(2026, 6, 1, 8, 5), None, None)
            == STATUS_UNDER_REPAIR
        )


class TestResolveComponentId:
    def test_body_value_wins(self):
        assert be.resolve_component_id(99, 42) == 99

    def test_falls_back_to_reason_component(self):
        assert be.resolve_component_id(None, 42) == 42

    def test_none_when_neither_supplied(self):
        assert be.resolve_component_id(None, None) is None

    def test_zero_is_an_explicit_value_not_a_fallback(self):
        """0 is falsy but not None — the check is `is not None`, so 0 must win."""
        assert be.resolve_component_id(0, 42) == 0


class TestResolveDiscipline:
    def test_body_value_wins(self):
        assert (
            be.resolve_discipline(DISCIPLINE_ELECTRICAL, DISCIPLINE_MECHANICAL)
            == DISCIPLINE_ELECTRICAL
        )

    def test_falls_back_to_reason_discipline(self):
        assert be.resolve_discipline(None, DISCIPLINE_ELECTRICAL) == DISCIPLINE_ELECTRICAL

    def test_defaults_to_mechanical_when_neither_supplied(self):
        assert be.resolve_discipline(None, None) == DISCIPLINE_MECHANICAL

    def test_normalises_case_and_whitespace(self):
        assert be.resolve_discipline("  electrical  ", None) == DISCIPLINE_ELECTRICAL

    def test_rejects_unknown_discipline(self):
        with pytest.raises(HTTPException) as exc:
            be.resolve_discipline("PLUMBING", None)
        assert exc.value.status_code == 400
        assert "discipline" in exc.value.detail.lower()

    def test_empty_string_body_falls_through_to_reason(self):
        """"" is falsy, so the reason's discipline is used rather than rejected."""
        assert be.resolve_discipline("", DISCIPLINE_ELECTRICAL) == DISCIPLINE_ELECTRICAL


class TestValidateDowntimeType:
    def test_defaults_to_breakdown_when_absent(self):
        assert be.validate_downtime_type(None) == DOWNTIME_BREAKDOWN
        assert be.validate_downtime_type("") == DOWNTIME_BREAKDOWN

    def test_normalises_case(self):
        assert be.validate_downtime_type("planned_pm") == DOWNTIME_PLANNED_PM

    def test_rejects_unknown_type(self):
        with pytest.raises(HTTPException) as exc:
            be.validate_downtime_type("TEA_BREAK")
        assert exc.value.status_code == 400
        assert "downtime_type" in exc.value.detail.lower()


class TestValidateDatetimes:
    """The incident timeline gate. `now` is injected — never the wall clock."""

    def test_open_incident_is_valid(self):
        be.validate_datetimes(STOP, None, None, now=NOW)

    def test_full_valid_timeline(self):
        be.validate_datetimes(
            STOP, datetime(2026, 6, 1, 8, 10), datetime(2026, 6, 1, 9, 0), now=NOW
        )

    def test_rejects_missing_stop(self):
        with pytest.raises(HTTPException) as exc:
            be.validate_datetimes(None, None, None, now=NOW)
        assert exc.value.status_code == 400
        assert "stop_datetime" in exc.value.detail

    def test_stop_ahead_of_the_server_clock_is_allowed(self):
        """Owner ruling: no future-time check — the server may run in another zone
        than the mill, so a genuine "now" can look hours ahead."""
        be.validate_datetimes(datetime(2026, 6, 1, 17, 30), None, None, now=NOW)

    def test_stop_exactly_now_is_allowed(self):
        be.validate_datetimes(NOW, None, None, now=NOW)

    def test_rejects_restart_before_stop(self):
        with pytest.raises(HTTPException) as exc:
            be.validate_datetimes(STOP, None, datetime(2026, 6, 1, 7, 0), now=NOW)
        assert exc.value.status_code == 400
        assert exc.value.detail == be.RESTART_BEFORE_STOP_MSG

    def test_restart_equal_to_stop_is_allowed(self):
        be.validate_datetimes(STOP, None, STOP, now=NOW)

    def test_rejects_attended_before_stop(self):
        with pytest.raises(HTTPException) as exc:
            be.validate_datetimes(STOP, datetime(2026, 6, 1, 7, 59), None, now=NOW)
        assert exc.value.status_code == 400
        assert exc.value.detail == be.ATTENDED_BEFORE_STOP_MSG

    def test_rejects_attended_after_restart(self):
        with pytest.raises(HTTPException) as exc:
            be.validate_datetimes(
                STOP, datetime(2026, 6, 1, 9, 30), datetime(2026, 6, 1, 9, 0), now=NOW
            )
        assert exc.value.status_code == 400
        assert exc.value.detail == be.ATTENDED_AFTER_RESTART_MSG

    def test_attended_on_the_boundaries_is_allowed(self):
        restart = datetime(2026, 6, 1, 9, 0)
        be.validate_datetimes(STOP, STOP, restart, now=NOW)
        be.validate_datetimes(STOP, restart, restart, now=NOW)

    def test_attended_without_restart_only_checks_the_lower_bound(self):
        """An open incident may be attended at any time at or after the stop."""
        be.validate_datetimes(STOP, datetime(2026, 6, 1, 11, 59), None, now=NOW)


class TestComputeSpareAmount:
    def test_explicit_amount_is_kept(self):
        assert be.compute_spare_amount(2.0, 10.0, 25.0) == 25.0

    def test_derived_from_qty_times_rate(self):
        assert be.compute_spare_amount(2.5, 10.0, None) == 25.0

    def test_none_when_qty_missing(self):
        assert be.compute_spare_amount(None, 10.0, None) is None

    def test_none_when_rate_missing(self):
        assert be.compute_spare_amount(2.0, None, None) is None

    def test_none_when_nothing_supplied(self):
        assert be.compute_spare_amount(None, None, None) is None

    def test_explicit_zero_amount_is_kept_not_recomputed(self):
        """0.0 is a typed total, not an absent one."""
        assert be.compute_spare_amount(2.0, 10.0, 0.0) == 0.0


class TestPickedMerge:
    """The model_fields_set merge that powers the partial PUT body."""

    def test_omitted_key_keeps_the_stored_value(self):
        body = be.BreakdownUpdate()
        assert be._picked(body, "remarks", "stored") == "stored"

    def test_explicit_null_clears_the_value(self):
        body = be.BreakdownUpdate(**{"remarks": None})
        assert be._picked(body, "remarks", "stored") is None

    def test_explicit_value_overrides(self):
        body = be.BreakdownUpdate(remarks="new")
        assert be._picked(body, "remarks", "stored") == "new"


# =============================================================================
# 2. Endpoint tests — dependency_overrides, never a real DB
# =============================================================================


class TestBreakdownEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    # ------------------------------------------------------------ co_id gate
    def test_entries_missing_co_id_returns_400(self):
        response = client.get("/api/mechBreakdown/entries")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    def test_open_breakdowns_missing_co_id_returns_400(self):
        response = client.get("/api/mechBreakdown/open_breakdowns")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    # ------------------------------------------------------------- reads
    def test_entries_returns_data_and_total_never_a_bare_list(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(total=1)),
            _exec_result(
                fetchall=[
                    _mock_row(
                        {
                            "breakdown_id": 5,
                            "tran_date": date(2026, 6, 1),
                            "machine_id": 10,
                            "machine_name": "SPR-1",
                            "stop_datetime": STOP,
                            "attended_datetime": None,
                            "restart_datetime": datetime(2026, 6, 1, 9, 0),
                            "duration_min": 60,
                            "response_min": None,
                            "reason_code": "RF",
                            "status": STATUS_CLOSED,
                            "production_loss_qty": 3.5,
                        }
                    )
                ]
            ),
        ]

        response = client.get("/api/mechBreakdown/entries?co_id=1")

        assert response.status_code == 200
        payload = response.json()
        assert isinstance(payload, dict)
        assert payload["total"] == 1
        row = payload["data"][0]
        assert row["breakdown_id"] == 5
        assert row["duration_min"] == 60
        assert row["response_min"] is None
        assert row["tran_date"] == "2026-06-01"
        assert row["production_loss_qty"] == 3.5

    def test_entries_rejects_unknown_status_filter(self):
        response = client.get("/api/mechBreakdown/entries?co_id=1&status=PENDING")
        assert response.status_code == 400
        assert "status" in response.json()["detail"].lower()

    def test_entry_by_id_not_found_returns_404(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.get("/api/mechBreakdown/entry_by_id/999?co_id=1")
        assert response.status_code == 404

    # ------------------------------------------------------------ create
    def test_entry_create_success_returns_breakdown_id(self):
        """Happy path: machine + reason resolve, insert returns the new id."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine()),   # _load_machine
            _exec_result(fetchone=_reason()),    # _load_reason
            _exec_result(lastrowid=501),         # insert
        ]
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "reason_id": 7,
            "stop_datetime": "2026-06-01T08:00:00",
            "restart_datetime": "2026-06-01T09:30:00",
            "attended_datetime": "2026-06-01T08:10:00",
        }

        response = client.post("/api/mechBreakdown/entry_create", json=payload)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["breakdown_id"] == 501
        assert data["duration_min"] == 90
        assert data["response_min"] == 10
        assert data["status"] == STATUS_CLOSED
        assert data["tran_date"] == "2026-06-01"
        assert data["branch_id"] == 1          # derived machine -> dept
        assert data["component_id"] == 42      # inherited from the reason
        assert data["discipline"] == DISCIPLINE_MECHANICAL
        assert data["downtime_type"] == DOWNTIME_BREAKDOWN
        self._mock_session.commit.assert_called_once()

    def test_entry_create_open_incident_has_no_duration(self):
        """No restart -> duration_min NULL and status OPEN."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_reason()),
            _exec_result(lastrowid=502),
        ]
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "reason_id": 7,
            "stop_datetime": "2026-06-01T08:00:00",
        }

        response = client.post("/api/mechBreakdown/entry_create", json=payload)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["duration_min"] is None
        assert data["response_min"] is None
        assert data["status"] == STATUS_OPEN

    def test_entry_create_restart_only_still_computes_the_duration(self):
        """Regression: with no duration_min in the body nothing changes."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_reason()),
            _exec_result(lastrowid=505),
        ]
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "reason_id": 7,
            "stop_datetime": "2026-06-01T08:00:00",
            "restart_datetime": "2026-06-01T09:30:00",
        }

        response = client.post("/api/mechBreakdown/entry_create", json=payload)

        assert response.status_code == 200
        assert response.json()["data"]["duration_min"] == 90
        insert_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert insert_params["duration_min"] == 90

    def test_entry_create_duration_only_closes_without_a_restart(self):
        """The new field: minutes typed by hand, no restart time keyed at all."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_reason()),
            _exec_result(lastrowid=506),
        ]
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "reason_id": 7,
            "stop_datetime": "2026-06-01T08:00:00",
            "duration_min": 35,
        }

        response = client.post("/api/mechBreakdown/entry_create", json=payload)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["duration_min"] == 35
        assert data["status"] == STATUS_CLOSED
        insert_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert insert_params["duration_min"] == 35
        assert insert_params["restart_datetime"] is None
        assert insert_params["status"] == STATUS_CLOSED

    def test_entry_create_entered_duration_beats_a_contradicting_restart(self):
        """User ruling: the typed figure is stored verbatim, never cross-validated."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_reason()),
            _exec_result(lastrowid=507),
        ]
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "reason_id": 7,
            "stop_datetime": "2026-06-01T08:00:00",
            "restart_datetime": "2026-06-01T09:30:00",  # would compute 90
            "duration_min": 20,
        }

        response = client.post("/api/mechBreakdown/entry_create", json=payload)

        assert response.status_code == 200
        assert response.json()["data"]["duration_min"] == 20
        insert_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert insert_params["duration_min"] == 20
        assert insert_params["restart_datetime"] is not None  # both are kept

    @pytest.mark.parametrize("bad", [0, -5, 1.5, "thirty"])
    def test_entry_create_rejects_a_non_positive_or_non_int_duration(self, bad):
        response = client.post(
            "/api/mechBreakdown/entry_create",
            json={
                "co_id": 1,
                "machine_id": 10,
                "reason_id": 7,
                "stop_datetime": "2026-06-01T08:00:00",
                "duration_min": bad,
            },
        )
        assert response.status_code == 422
        self._mock_session.commit.assert_not_called()

    def test_entry_create_rejects_reason_from_a_different_machine_type(self):
        """The gate that stops a Spreader code being logged against a Draw Head."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine(machine_type_id=8, machine_name="DH-2")),
            _exec_result(fetchone=_reason(machine_type_id=3, reason_code="SPR/RF")),
        ]
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "reason_id": 7,
            "stop_datetime": "2026-06-01T08:00:00",
        }

        response = client.post("/api/mechBreakdown/entry_create", json=payload)

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "SPR/RF" in detail
        assert "DH-2" in detail
        assert "machine type" in detail
        self._mock_session.commit.assert_not_called()

    def test_entry_create_accepts_a_global_reason_on_any_machine(self):
        """machine_type_id IS NULL codes (NR, MT, PP) are never type-gated."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine(machine_type_id=8)),
            _exec_result(
                fetchone=_reason(
                    machine_type_id=None, reason_code="NR", component_id=None
                )
            ),
            _exec_result(lastrowid=503),
        ]
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "reason_id": 7,
            "stop_datetime": "2026-06-01T08:00:00",
        }

        response = client.post("/api/mechBreakdown/entry_create", json=payload)

        assert response.status_code == 200
        assert response.json()["data"]["component_id"] is None

    def test_entry_create_rejects_unknown_machine(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        payload = {
            "co_id": 1,
            "machine_id": 999,
            "reason_id": 7,
            "stop_datetime": "2026-06-01T08:00:00",
        }

        response = client.post("/api/mechBreakdown/entry_create", json=payload)

        assert response.status_code == 400
        assert response.json()["detail"] == be.MACHINE_NOT_FOUND_MSG

    def test_entry_create_rejects_restart_before_stop(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_reason()),
        ]
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "reason_id": 7,
            "stop_datetime": "2026-06-01T09:00:00",
            "restart_datetime": "2026-06-01T08:00:00",
        }

        response = client.post("/api/mechBreakdown/entry_create", json=payload)

        assert response.status_code == 400
        assert response.json()["detail"] == be.RESTART_BEFORE_STOP_MSG

    def test_entry_create_body_missing_required_field_returns_422(self):
        """Pydantic rejects a body with no stop_datetime before any handler code."""
        response = client.post(
            "/api/mechBreakdown/entry_create",
            json={"co_id": 1, "machine_id": 10, "reason_id": 7},
        )
        assert response.status_code == 422

    def test_entry_create_inserts_spare_lines_with_derived_amount(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_reason()),
            _exec_result(lastrowid=504),
            _exec_result(),  # spare insert
        ]
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "reason_id": 7,
            "stop_datetime": "2026-06-01T08:00:00",
            "spares": [{"item_id": 77, "qty": 2, "rate": 12.5}],
        }

        response = client.post("/api/mechBreakdown/entry_create", json=payload)

        assert response.status_code == 200
        assert response.json()["data"]["spare_count"] == 1
        spare_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert spare_params["item_id"] == 77
        assert spare_params["amount"] == 25.0

    # -------------------------------------------------------------- edit
    def _existing(self, **over):
        attrs = {
            "breakdown_id": 5,
            "co_id": 1,
            "branch_id": 1,
            "machine_id": 10,
            "reason_id": 7,
            "spell_id": 2,
            "stop_datetime": STOP,
            "attended_datetime": None,
            "restart_datetime": datetime(2026, 6, 1, 9, 0),
            "duration_min": 60,
            "response_min": None,
            "component_id": 42,
            "discipline": DISCIPLINE_MECHANICAL,
            "downtime_type": DOWNTIME_BREAKDOWN,
            "attended_dept_id": None,
            "attended_by": "Fitter A",
            "reported_by": "Sup B",
            "root_cause": "worn bearing",
            "action_taken": "replaced",
            "preventive_action": None,
            "production_loss_qty": 4.0,
            "status": STATUS_CLOSED,
            "remarks": "stored remark",
        }
        attrs.update(over)
        return _attr_row(**attrs)

    def test_entry_edit_clearing_restart_reopens_and_nulls_duration(self):
        """An explicit null on restart_datetime REOPENS a closed incident.

        This is the merge contract's sharpest edge: `{"restart_datetime": null}`
        must clear the stored restart, drop duration_min back to None and move the
        status from CLOSED to OPEN — while every key NOT sent keeps its value.
        """
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),  # _load_active_breakdown
            _exec_result(fetchone=_machine()),        # _load_machine
            _exec_result(fetchone=_reason()),         # _load_reason
            _exec_result(),                           # update
        ]

        response = client.put(
            "/api/mechBreakdown/entry_edit/5?co_id=1",
            json={"restart_datetime": None},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["duration_min"] is None
        assert data["status"] == STATUS_OPEN

        update_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert update_params["restart_datetime"] is None
        assert update_params["duration_min"] is None
        assert update_params["status"] == STATUS_OPEN
        # Omitted keys preserve the stored values.
        assert update_params["remarks"] == "stored remark"
        assert update_params["attended_by"] == "Fitter A"
        assert update_params["root_cause"] == "worn bearing"
        assert update_params["spell_id"] == 2
        assert update_params["production_loss_qty"] == 4.0

    def test_entry_edit_explicit_null_clears_a_text_field(self):
        """`{"remarks": null}` clears the stored remark — omission would keep it."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_reason()),
            _exec_result(),
        ]

        response = client.put(
            "/api/mechBreakdown/entry_edit/5?co_id=1", json={"remarks": None}
        )

        assert response.status_code == 200
        update_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert update_params["remarks"] is None
        # ... while the untouched restart still yields the closed state.
        assert update_params["status"] == STATUS_CLOSED
        assert update_params["duration_min"] == 60

    def test_entry_edit_new_restart_recomputes_duration(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_reason()),
            _exec_result(),
        ]

        response = client.put(
            "/api/mechBreakdown/entry_edit/5?co_id=1",
            json={"restart_datetime": "2026-06-01T10:00:00"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["duration_min"] == 120
        assert data["status"] == STATUS_CLOSED

    def test_entry_edit_null_restart_and_null_duration_fully_reopens(self):
        """The full reopen body once duration_min can close an incident on its own."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_reason()),
            _exec_result(),
        ]

        response = client.put(
            "/api/mechBreakdown/entry_edit/5?co_id=1",
            json={"restart_datetime": None, "duration_min": None},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["duration_min"] is None
        assert data["status"] == STATUS_OPEN
        update_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert update_params["restart_datetime"] is None
        assert update_params["duration_min"] is None

    def test_entry_edit_null_duration_with_restart_kept_recomputes(self):
        """Clearing a manual duration falls back to the merged timestamps, not NULL."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing(duration_min=15)),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_reason()),
            _exec_result(),
        ]

        response = client.put(
            "/api/mechBreakdown/entry_edit/5?co_id=1", json={"duration_min": None}
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["duration_min"] == 60  # stored restart 09:00 - stop 08:00
        assert data["status"] == STATUS_CLOSED

    def test_entry_edit_stores_an_entered_duration_verbatim(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),
            _exec_result(fetchone=_machine()),
            _exec_result(fetchone=_reason()),
            _exec_result(),
        ]

        response = client.put(
            "/api/mechBreakdown/entry_edit/5?co_id=1", json={"duration_min": 7}
        )

        assert response.status_code == 200
        update_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert update_params["duration_min"] == 7  # not the 60 the timestamps imply
        assert update_params["status"] == STATUS_CLOSED

    def test_entry_edit_rejects_a_zero_duration(self):
        response = client.put(
            "/api/mechBreakdown/entry_edit/5?co_id=1", json={"duration_min": 0}
        )
        assert response.status_code == 422

    def test_entry_edit_not_found_returns_404(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.put(
            "/api/mechBreakdown/entry_edit/999?co_id=1", json={"remarks": "x"}
        )
        assert response.status_code == 404

    def test_entry_edit_missing_co_id_returns_400(self):
        response = client.put("/api/mechBreakdown/entry_edit/5", json={"remarks": "x"})
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    # ------------------------------------------------------------ delete
    def test_entry_delete_soft_deletes_and_cascades(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),
            _exec_result(),  # soft delete header
            _exec_result(),  # soft delete spares
        ]

        response = client.delete("/api/mechBreakdown/entry_delete/5?co_id=1")

        assert response.status_code == 200
        assert response.json()["data"]["breakdown_id"] == 5
        spare_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert spare_params["breakdown_id"] == 5
        self._mock_session.commit.assert_called_once()

    # ------------------------------------------------------------- close
    def test_entry_close_computes_duration_from_stored_stop(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing(restart_datetime=None)),
            _exec_result(),  # close update
        ]

        response = client.post(
            "/api/mechBreakdown/entry_close/5?co_id=1",
            json={"restart_datetime": "2026-06-01T08:45:00", "action_taken": "reset"},
        )

        assert response.status_code == 200
        close_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert close_params["duration_min"] == 45
        assert close_params["status"] == STATUS_CLOSED

    def test_entry_close_keeps_an_entered_duration(self):
        """An override on the quick close is stored as typed, restart still required."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing(restart_datetime=None)),
            _exec_result(),
        ]

        response = client.post(
            "/api/mechBreakdown/entry_close/5?co_id=1",
            json={"restart_datetime": "2026-06-01T08:45:00", "duration_min": 12},
        )

        assert response.status_code == 200
        assert response.json()["data"]["duration_min"] == 12
        close_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert close_params["duration_min"] == 12  # not the 45 the timestamps imply
        assert close_params["status"] == STATUS_CLOSED

    def test_entry_close_still_requires_a_restart_datetime(self):
        response = client.post(
            "/api/mechBreakdown/entry_close/5?co_id=1", json={"duration_min": 12}
        )
        assert response.status_code == 422

    # ----------------------------------------------- production reconfirm
    def test_entry_confirm_agreed_stores_confirmed_under_the_caller(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),
            _exec_result(),  # confirm update
        ]

        response = client.post(
            "/api/mechBreakdown/entry_confirm/5?co_id=1", json={"agreed": True}
        )

        assert response.status_code == 200
        assert response.json()["data"]["prod_confirm_status"] == "CONFIRMED"
        params = self._mock_session.execute.call_args_list[-1].args[1]
        assert params["prod_confirm_status"] == "CONFIRMED"
        assert params["prod_restart_datetime"] is None
        assert params["updated_by"] is not None  # the confirming user, from the token
        self._mock_session.commit.assert_called_once()

    def test_entry_confirm_agreed_ignores_a_stray_restart_time(self):
        """Agreeing means mechanical's time stands — no second time is stored."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),
            _exec_result(),
        ]

        response = client.post(
            "/api/mechBreakdown/entry_confirm/5?co_id=1",
            json={"agreed": True, "restart_datetime": "2026-06-01T09:30:00"},
        )

        assert response.status_code == 200
        params = self._mock_session.execute.call_args_list[-1].args[1]
        assert params["prod_restart_datetime"] is None

    def test_entry_confirm_dispute_stores_production_time_and_remark(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),
            _exec_result(),
        ]

        response = client.post(
            "/api/mechBreakdown/entry_confirm/5?co_id=1",
            json={
                "agreed": False,
                "restart_datetime": "2026-06-01T09:30:00",
                "remarks": "  machine ran only from 9:30  ",
            },
        )

        assert response.status_code == 200
        params = self._mock_session.execute.call_args_list[-1].args[1]
        assert params["prod_confirm_status"] == "DISPUTED"
        assert params["prod_restart_datetime"] == datetime(2026, 6, 1, 9, 30)
        assert params["prod_confirm_remarks"] == "machine ran only from 9:30"

    def test_entry_confirm_dispute_with_only_a_remark_is_allowed(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),
            _exec_result(),
        ]

        response = client.post(
            "/api/mechBreakdown/entry_confirm/5?co_id=1",
            json={"agreed": False, "remarks": "stopped again within minutes"},
        )

        assert response.status_code == 200
        params = self._mock_session.execute.call_args_list[-1].args[1]
        assert params["prod_confirm_status"] == "DISPUTED"
        assert params["prod_restart_datetime"] is None

    def test_entry_confirm_dispute_needs_a_remark_or_a_time(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),
        ]

        response = client.post(
            "/api/mechBreakdown/entry_confirm/5?co_id=1",
            json={"agreed": False, "remarks": "   "},
        )

        assert response.status_code == 400
        assert self._mock_session.execute.call_count == 1  # nothing written
        self._mock_session.commit.assert_not_called()

    def test_entry_confirm_rejects_a_production_time_before_the_stop(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=self._existing()),
        ]

        response = client.post(
            "/api/mechBreakdown/entry_confirm/5?co_id=1",
            json={"agreed": False, "restart_datetime": "2026-06-01T07:00:00"},
        )

        assert response.status_code == 400
        assert self._mock_session.execute.call_count == 1

    def test_entry_confirm_rejects_an_incident_that_is_not_closed(self):
        """There is no fix to reconfirm while the machine is still down."""
        self._mock_session.execute.side_effect = [
            _exec_result(
                fetchone=self._existing(
                    restart_datetime=None, duration_min=None, status=STATUS_OPEN
                )
            ),
        ]

        response = client.post(
            "/api/mechBreakdown/entry_confirm/5?co_id=1", json={"agreed": True}
        )

        assert response.status_code == 400
        assert self._mock_session.execute.call_count == 1

    def test_entry_confirm_not_found_returns_404(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.post(
            "/api/mechBreakdown/entry_confirm/999?co_id=1", json={"agreed": True}
        )
        assert response.status_code == 404


class TestWhoDidWhatStamps:
    """Reporter / fixer / production stamps are written by the SQL, not the client."""

    def test_insert_stamps_the_saving_user_as_reporter(self):
        sql = str(insert_breakdown_query())
        assert "reported_by_user_id, reported_date_time" in sql
        assert ":updated_by, NOW()," in sql

    def test_insert_stamps_the_fixer_only_when_keyed_closed(self):
        sql = str(insert_breakdown_query())
        assert "CASE WHEN :status = 'CLOSED' THEN :updated_by END" in sql

    def test_update_never_rewrites_the_reporter(self):
        sql = str(update_breakdown_query())
        assert "reported_by_user_id" not in sql
        assert "reported_date_time" not in sql

    def test_update_reads_the_stored_fix_before_overwriting_it(self):
        """MySQL applies SET left to right: the stamp CASEs compare against the
        STORED status / restart / duration, so they must precede those columns."""
        sql = str(update_breakdown_query())
        for stamp in ("fixed_by_user_id = CASE", "prod_confirm_remarks = CASE"):
            for overwritten in (
                "restart_datetime    = :restart_datetime",
                "duration_min        = :duration_min",
                "status              = :status",
            ):
                assert sql.index(stamp) < sql.index(overwritten)

    def test_update_clears_the_fixer_when_the_incident_reopens(self):
        assert "WHEN :status <> 'CLOSED' THEN NULL" in str(update_breakdown_query())

    def test_quick_close_stamps_the_fixer_and_resets_the_reconfirmation(self):
        sql = str(close_breakdown_query())
        assert "fixed_by_user_id       = :updated_by" in sql
        assert "prod_confirm_status    = NULL" in sql

    def test_confirm_touches_only_the_production_columns(self):
        sql = str(confirm_breakdown_query())
        assert "AND status = 'CLOSED'" in sql
        set_clause = sql.split("WHERE")[0]
        for untouched in ("restart_datetime ", "duration_min", "fixed_by_user_id"):
            assert untouched not in set_clause.replace("prod_restart_datetime", "")


class TestOpenBoardDefinition:
    """A duration-closed incident must leave every "currently open" query."""

    def test_open_breakdowns_excludes_duration_closed_incidents(self):
        sql = str(get_open_breakdowns_query())
        assert "b.restart_datetime IS NULL" in sql
        assert "b.duration_min IS NULL" in sql

    def test_open_breakdown_board_report_uses_the_same_definition(self):
        for sql in (
            str(get_open_breakdown_board_query()),
            str(get_open_breakdown_board_count_query()),
        ):
            assert "b.restart_datetime IS NULL" in sql
            assert "b.duration_min IS NULL" in sql

"""Tests for the maintenance masters — Jute Mechanical & Maintenance.

Covers ``src/juteMechanical/masters.py`` (prefix ``/api/mechMaintMasters``):
reason master, component master, machine-attribute master and PM-schedule master.

Two layers:

1. **Pure helpers** — the body-field coercers (``_opt_int``, ``_req_str``,
   ``_active_flag``, ``_opt_date``), the enum validators (``_validate_discipline``,
   ``_validate_frequency``, ``_validate_criticality``), the frequency resolution
   (``_resolve_freq_days``) and the two due-date derivations
   (``_next_overhaul_due`` / ``_next_pm_due``, which sit on masters.py's own
   ``_add_months``). No DB, no mocking.

2. **Endpoint tests** using ``app.dependency_overrides`` — the handlers take the
   session via ``Depends(get_tenant_db)``, bound at router-registration time, so
   patching the module attribute would not intercept it.

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
from src.juteMechanical import masters as ms
from src.juteMechanical.constants import (
    CRITICALITY_A,
    DISCIPLINE_ELECTRICAL,
    DISCIPLINE_MECHANICAL,
    DISCIPLINES,
    FREQ_MONTHLY,
    FREQ_QUARTERLY,
    PM_FREQUENCY_DAYS,
)

client = TestClient(app)


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


def _attr_row(**attrs):
    row = MagicMock()
    for k, v in attrs.items():
        setattr(row, k, v)
    return row


_UNSET = object()


def _exec_result(*, fetchall=_UNSET, fetchone=_UNSET, lastrowid=_UNSET):
    res = MagicMock()
    if fetchall is not _UNSET:
        res.fetchall.return_value = fetchall
    if fetchone is not _UNSET:
        res.fetchone.return_value = fetchone
    if lastrowid is not _UNSET:
        res.lastrowid = lastrowid
    return res


# =============================================================================
# 1. Pure helpers
# =============================================================================


class TestBodyCoercers:
    def test_opt_int_treats_none_and_blank_as_sql_null(self):
        assert ms._opt_int(None, "x") is None
        assert ms._opt_int("", "x") is None

    def test_opt_int_casts_a_numeric_string(self):
        assert ms._opt_int("7", "x") == 7

    def test_opt_int_rejects_garbage(self):
        with pytest.raises(HTTPException) as exc:
            ms._opt_int("abc", "machine_type_id")
        assert exc.value.status_code == 400
        assert "machine_type_id" in exc.value.detail

    def test_opt_int_keeps_zero(self):
        assert ms._opt_int(0, "x") == 0

    def test_req_int_rejects_a_missing_value(self):
        with pytest.raises(HTTPException) as exc:
            ms._req_int(None, "machine_id")
        assert exc.value.status_code == 400
        assert "required" in exc.value.detail

    def test_opt_str_trims_and_nulls_the_empty_string(self):
        assert ms._opt_str("  hello  ") == "hello"
        assert ms._opt_str("   ") is None
        assert ms._opt_str(None) is None

    def test_req_str_rejects_whitespace_only(self):
        with pytest.raises(HTTPException) as exc:
            ms._req_str("   ", "reason_code")
        assert exc.value.status_code == 400
        assert "reason_code" in exc.value.detail

    def test_active_flag_defaults_to_one(self):
        assert ms._active_flag(None) == 1
        assert ms._active_flag("") == 1

    def test_active_flag_normalises_truthiness(self):
        assert ms._active_flag(0) == 0
        assert ms._active_flag("0") == 0
        assert ms._active_flag(5) == 1

    def test_active_flag_falls_back_to_the_default_on_garbage(self):
        assert ms._active_flag("yes") == 1
        assert ms._active_flag("yes", default=0) == 0

    def test_opt_date_parses_iso_and_nulls_blanks(self):
        assert ms._opt_date("2026-06-01", "d") == date(2026, 6, 1)
        assert ms._opt_date("", "d") is None
        assert ms._opt_date(None, "d") is None

    def test_opt_date_narrows_a_datetime(self):
        assert ms._opt_date(datetime(2026, 6, 1, 8, 30), "d") == date(2026, 6, 1)

    def test_opt_date_rejects_a_bad_format(self):
        with pytest.raises(HTTPException) as exc:
            ms._opt_date("01-06-2026", "commissioning_date")
        assert exc.value.status_code == 400
        assert "commissioning_date" in exc.value.detail


class TestEnumValidators:
    def test_discipline_defaults_to_mechanical(self):
        assert ms._validate_discipline(None) == DISCIPLINE_MECHANICAL

    def test_discipline_normalises_case(self):
        assert ms._validate_discipline("electrical") == DISCIPLINE_ELECTRICAL

    def test_discipline_rejects_unknown(self):
        with pytest.raises(HTTPException) as exc:
            ms._validate_discipline("PLUMBING")
        assert exc.value.status_code == 400

    @pytest.mark.parametrize("value", DISCIPLINES)
    def test_every_discipline_constant_is_accepted(self, value):
        assert ms._validate_discipline(value) == value

    def test_frequency_is_required(self):
        with pytest.raises(HTTPException) as exc:
            ms._validate_frequency(None)
        assert exc.value.status_code == 400
        assert "frequency" in exc.value.detail

    def test_frequency_normalises_case(self):
        assert ms._validate_frequency("monthly") == FREQ_MONTHLY

    def test_frequency_rejects_unknown(self):
        with pytest.raises(HTTPException):
            ms._validate_frequency("FORTNIGHTLY")

    def test_criticality_is_optional(self):
        assert ms._validate_criticality(None) is None
        assert ms._validate_criticality("") is None

    def test_criticality_normalises_case(self):
        assert ms._validate_criticality("a") == CRITICALITY_A

    def test_criticality_rejects_unknown(self):
        with pytest.raises(HTTPException) as exc:
            ms._validate_criticality("Z")
        assert exc.value.status_code == 400


class TestResolveFreqDays:
    def test_explicit_freq_days_wins(self):
        assert ms._resolve_freq_days(45, FREQ_MONTHLY) == 45

    def test_resolves_from_the_frequency_map_when_omitted(self):
        assert ms._resolve_freq_days(None, FREQ_MONTHLY) == PM_FREQUENCY_DAYS[FREQ_MONTHLY]
        assert ms._resolve_freq_days("", FREQ_QUARTERLY) == 91

    def test_explicit_zero_is_kept(self):
        assert ms._resolve_freq_days(0, FREQ_MONTHLY) == 0


class TestMastersAddMonths:
    """masters.py carries its own _add_months — same clamping contract."""

    def test_jan_31_plus_1_clamps_to_feb_28(self):
        assert ms._add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_jan_31_plus_1_clamps_to_feb_29_in_a_leap_year(self):
        assert ms._add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)

    def test_mar_31_plus_1_clamps_to_apr_30(self):
        assert ms._add_months(date(2026, 3, 31), 1) == date(2026, 4, 30)

    def test_plus_12_months(self):
        assert ms._add_months(date(2026, 6, 15), 12) == date(2027, 6, 15)

    def test_plus_18_months(self):
        assert ms._add_months(date(2026, 6, 15), 18) == date(2027, 12, 15)

    def test_matches_the_overhaul_module_implementation(self):
        """Both copies must agree, or two screens would show different due dates."""
        from src.juteMechanical import overhaul as oh

        for base in (date(2026, 1, 31), date(2024, 2, 29), date(2026, 8, 31)):
            for months in (1, 3, 6, 12, 18):
                assert ms._add_months(base, months) == oh._add_months(base, months)


class TestDueDateDerivations:
    def test_next_overhaul_due_adds_months(self):
        assert ms._next_overhaul_due(date(2026, 6, 15), 12) == date(2027, 6, 15)

    def test_next_overhaul_due_clamps(self):
        assert ms._next_overhaul_due(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_next_overhaul_due_none_without_a_last_date(self):
        assert ms._next_overhaul_due(None, 12) is None

    def test_next_overhaul_due_none_without_an_interval(self):
        assert ms._next_overhaul_due(date(2026, 6, 15), None) is None
        assert ms._next_overhaul_due(date(2026, 6, 15), 0) is None

    def test_next_pm_due_adds_days(self):
        assert ms._next_pm_due(date(2026, 6, 1), 30) == date(2026, 7, 1)

    def test_next_pm_due_none_without_a_last_date(self):
        assert ms._next_pm_due(None, 30) is None

    def test_next_pm_due_none_without_an_interval(self):
        assert ms._next_pm_due(date(2026, 6, 1), None) is None
        assert ms._next_pm_due(date(2026, 6, 1), 0) is None


class TestSerializationHelpers:
    def test_serialize_row_stringifies_dates(self):
        row = ms._serialize_row({"d": date(2026, 6, 1), "n": 5, "s": "x"})
        assert row["d"] == "2026-06-01"
        assert row["n"] == 5
        assert row["s"] == "x"

    def test_serialize_row_floats_decimals(self):
        from decimal import Decimal

        row = ms._serialize_row({"amount": Decimal("12.50")})
        assert row["amount"] == 12.5
        assert isinstance(row["amount"], float)

    def test_enum_options_uses_code_and_label_keys(self):
        options = ms._enum_options(("A", "B"), {"A": "Alpha"})
        assert options == [{"code": "A", "label": "Alpha"}, {"code": "B", "label": "B"}]

    def test_paginate_slices_the_page(self):
        rows = list(range(25))
        assert ms._paginate(rows, page=1, limit=10) == list(range(10))
        assert ms._paginate(rows, page=3, limit=10) == list(range(20, 25))
        assert ms._paginate(rows, page=4, limit=10) == []


# =============================================================================
# 2. Endpoint tests — dependency_overrides, never a real DB
# =============================================================================


class TestMastersEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    # ------------------------------------------------------------ co_id gate
    def test_masters_setup_missing_co_id_returns_400(self):
        response = client.get("/api/mechMaintMasters/masters_setup")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    def test_reason_table_missing_co_id_returns_400(self):
        response = client.get("/api/mechMaintMasters/reason_table")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    def test_component_table_missing_co_id_returns_400(self):
        response = client.get("/api/mechMaintMasters/component_table")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    def test_reason_create_missing_co_id_returns_400(self):
        response = client.post(
            "/api/mechMaintMasters/reason_create",
            json={"reason_code": "RF", "reason_desc": "Roller Fault"},
        )
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    # ---------------------------------------------------------------- setup
    def test_masters_setup_returns_every_dropdown(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_mock_row({"machine_id": 10, "machine_name": "SPR-1"})]),
            _exec_result(fetchall=[_mock_row({"machine_type_id": 3, "machine_type_name": "Spreader"})]),
            _exec_result(fetchall=[_mock_row({"dept_id": 50, "dept_desc": "Spreader Dept"})]),
            _exec_result(fetchall=[_mock_row({"sub_dept_id": 9, "sub_dept_desc": "Fitters"})]),
            _exec_result(
                fetchall=[
                    _mock_row(
                        {
                            "spell_id": 7,
                            "spell_code": "A1",
                            "spell_name": "Shift A1",
                            "working_hours": 8.0,
                        }
                    ),
                    # duplicate spell_code must be dropped
                    _mock_row(
                        {
                            "spell_id": 8,
                            "spell_code": "A1",
                            "spell_name": "Shift A1 dup",
                            "working_hours": 8.0,
                        }
                    ),
                ]
            ),
            _exec_result(fetchall=[_mock_row({"component_id": 42, "component_name": "Roller"})]),
        ]

        response = client.get("/api/mechMaintMasters/masters_setup?co_id=1")

        assert response.status_code == 200
        data = response.json()["data"]
        for key in (
            "machines",
            "machine_types",
            "departments",
            "sub_departments",
            "spells",
            "components",
            "disciplines",
            "downtime_types",
            "criticalities",
            "pm_frequencies",
        ):
            assert key in data, f"masters_setup is missing {key}"
        assert len(data["spells"]) == 1  # de-duplicated by spell_code
        assert data["spells"][0]["working_hours"] == 8.0
        monthly = next(f for f in data["pm_frequencies"] if f["code"] == FREQ_MONTHLY)
        assert monthly["freq_days"] == 30

    # ------------------------------------------------------------ reason CRUD
    def test_reason_table_returns_data_and_total(self):
        self._mock_session.execute.side_effect = [
            _exec_result(
                fetchall=[
                    _mock_row(
                        {
                            "reason_id": 7,
                            "reason_code": "RF",
                            "reason_desc": "Roller Fault",
                            "machine_type_name": "Spreader",
                            "discipline": DISCIPLINE_MECHANICAL,
                            "active": 1,
                        }
                    )
                ]
            )
        ]

        response = client.get("/api/mechMaintMasters/reason_table?co_id=1")

        assert response.status_code == 200
        payload = response.json()
        assert isinstance(payload, dict)
        assert payload["total"] == 1
        assert payload["data"][0]["reason_code"] == "RF"

    def test_reason_create_success_returns_reason_id(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),   # dup check -> clear
            _exec_result(lastrowid=101),   # insert
        ]

        response = client.post(
            "/api/mechMaintMasters/reason_create",
            json={
                "co_id": 1,
                "reason_code": "RF",
                "reason_desc": "Roller Fault",
                "machine_type_id": 3,
                "discipline": "mechanical",
                "component_id": 42,
            },
        )

        assert response.status_code == 200
        assert response.json()["data"]["reason_id"] == 101
        insert_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert insert_params["discipline"] == DISCIPLINE_MECHANICAL
        assert insert_params["machine_type_id"] == 3
        self._mock_session.commit.assert_called_once()

    def test_reason_create_keeps_a_global_code_machine_type_null(self):
        """machine_type_id NULL is meaningful — it makes the code global."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),
            _exec_result(lastrowid=102),
        ]

        response = client.post(
            "/api/mechMaintMasters/reason_create",
            json={"co_id": 1, "reason_code": "NR", "reason_desc": "No Reason mentioned"},
        )

        assert response.status_code == 200
        insert_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert insert_params["machine_type_id"] is None

    def test_reason_create_duplicate_returns_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(reason_id=7, active=1)),
        ]

        response = client.post(
            "/api/mechMaintMasters/reason_create",
            json={"co_id": 1, "reason_code": "RF", "reason_desc": "Roller Fault",
                  "machine_type_id": 3},
        )

        assert response.status_code == 400
        assert "already exists" in response.json()["detail"]
        self._mock_session.commit.assert_not_called()

    def test_reason_create_duplicate_inactive_says_reactivate(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(reason_id=7, active=0)),
        ]

        response = client.post(
            "/api/mechMaintMasters/reason_create",
            json={"co_id": 1, "reason_code": "RF", "reason_desc": "Roller Fault"},
        )

        assert response.status_code == 400
        assert "reactivate" in response.json()["detail"]

    def test_reason_create_missing_reason_code_returns_400(self):
        response = client.post(
            "/api/mechMaintMasters/reason_create",
            json={"co_id": 1, "reason_desc": "Roller Fault"},
        )
        assert response.status_code == 400
        assert "reason_code" in response.json()["detail"]

    def test_reason_create_rejects_a_bad_discipline(self):
        response = client.post(
            "/api/mechMaintMasters/reason_create",
            json={"co_id": 1, "reason_code": "RF", "reason_desc": "x",
                  "discipline": "PLUMBING"},
        )
        assert response.status_code == 400
        assert "discipline" in response.json()["detail"].lower()

    def test_reason_by_id_not_found_returns_404(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.get("/api/mechMaintMasters/reason_by_id/999?co_id=1")
        assert response.status_code == 404

    # --------------------------------------------------------- component CRUD
    def test_component_create_duplicate_returns_400(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(component_id=42, active=1)),
        ]

        response = client.post(
            "/api/mechMaintMasters/component_create",
            json={"co_id": 1, "component_name": "Roller"},
        )

        assert response.status_code == 400
        assert "already exists" in response.json()["detail"].lower()

    def test_component_create_success(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=None),
            _exec_result(lastrowid=201),
        ]

        response = client.post(
            "/api/mechMaintMasters/component_create",
            json={"co_id": 1, "component_name": "Roller", "component_group": "Drive"},
        )

        assert response.status_code == 200
        assert response.json()["data"]["component_id"] == 201

    # ------------------------------------------------------ machine attribute
    def test_machine_attr_create_computes_both_due_dates(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(machine_id=10)),  # machine exists
            _exec_result(fetchone=None),                      # no existing attr row
            _exec_result(lastrowid=301),                      # insert
        ]

        response = client.post(
            "/api/mechMaintMasters/machine_attr_create",
            json={
                "co_id": 1,
                "machine_id": 10,
                "criticality": "A",
                "last_overhaul_date": "2026-01-31",
                "overhaul_freq_months": 1,
                "last_pm_date": "2026-06-01",
                "pm_freq_days": 30,
            },
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["machine_attr_id"] == 301
        assert data["created"] is True
        assert data["next_overhaul_due"] == "2026-02-28"  # clamped
        assert data["next_pm_due"] == "2026-07-01"

    def test_machine_attr_create_upserts_an_existing_row(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(machine_id=10)),
            _exec_result(fetchone=_attr_row(machine_attr_id=300)),  # already exists
            _exec_result(),                                          # update
        ]

        response = client.post(
            "/api/mechMaintMasters/machine_attr_create",
            json={"co_id": 1, "machine_id": 10, "criticality": "B"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["machine_attr_id"] == 300
        assert data["created"] is False

    def test_machine_attr_create_without_intervals_leaves_due_dates_null(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(machine_id=10)),
            _exec_result(fetchone=None),
            _exec_result(lastrowid=302),
        ]

        response = client.post(
            "/api/mechMaintMasters/machine_attr_create",
            json={"co_id": 1, "machine_id": 10},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["next_overhaul_due"] is None
        assert data["next_pm_due"] is None

    def test_machine_attr_create_rejects_unknown_machine(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.post(
            "/api/mechMaintMasters/machine_attr_create",
            json={"co_id": 1, "machine_id": 999},
        )
        assert response.status_code == 400
        assert "machine_id" in response.json()["detail"]

    def test_machine_attr_create_rejects_a_bad_criticality(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_attr_row(machine_id=10)),
        ]
        response = client.post(
            "/api/mechMaintMasters/machine_attr_create",
            json={"co_id": 1, "machine_id": 10, "criticality": "Z"},
        )
        assert response.status_code == 400

    # ----------------------------------------------------------- pm schedule
    def test_pm_schedule_create_resolves_freq_days_from_the_frequency(self):
        self._mock_session.execute.side_effect = [_exec_result(lastrowid=401)]

        response = client.post(
            "/api/mechMaintMasters/pm_schedule_create",
            json={
                "co_id": 1,
                "task_desc": "Grease bearings",
                "frequency": "MONTHLY",
                "machine_type_id": 3,
            },
        )

        assert response.status_code == 200
        assert response.json()["data"]["pm_schedule_id"] == 401
        insert_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert insert_params["freq_days"] == 30
        assert insert_params["frequency"] == FREQ_MONTHLY

    def test_pm_schedule_create_keeps_an_explicit_freq_days(self):
        self._mock_session.execute.side_effect = [_exec_result(lastrowid=402)]

        response = client.post(
            "/api/mechMaintMasters/pm_schedule_create",
            json={
                "co_id": 1,
                "task_desc": "Grease bearings",
                "frequency": "MONTHLY",
                "freq_days": 45,
            },
        )

        assert response.status_code == 200
        insert_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert insert_params["freq_days"] == 45

    def test_pm_schedule_create_requires_a_frequency(self):
        response = client.post(
            "/api/mechMaintMasters/pm_schedule_create",
            json={"co_id": 1, "task_desc": "Grease bearings"},
        )
        assert response.status_code == 400
        assert "frequency" in response.json()["detail"]

    def test_pm_schedule_create_rejects_unknown_machine(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.post(
            "/api/mechMaintMasters/pm_schedule_create",
            json={
                "co_id": 1,
                "task_desc": "Grease bearings",
                "frequency": "MONTHLY",
                "machine_id": 999,
            },
        )
        assert response.status_code == 400
        assert "machine_id" in response.json()["detail"]

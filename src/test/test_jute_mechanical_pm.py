"""Tests for Preventive Maintenance — Jute Mechanical & Maintenance.

Covers ``src/juteMechanical/pm.py`` (prefix ``/api/mechPM``).

The centrepiece is ``_resolve_freq_days``, the three-step resolution chain behind
``next_pm_due``:

    1. the machine's own ``jute_mech_machine_attr.pm_freq_days`` (the engineer's
       deliberate override for that machine),
    2. else the PM schedule template's resolved ``freq_days``,
    3. else the ``PM_FREQUENCY_DAYS`` map for the posted frequency,
    4. else None — the visit is recorded with NO next due date rather than an
       invented one.

It takes a session, so it is exercised against a MagicMock db built per case
rather than a real one, which keeps every step of the chain independently
assertable. ``_next_due_date`` is pure and tested directly.

Endpoint tests use ``app.dependency_overrides`` (``Depends(get_tenant_db)`` is
bound at router-registration time, so a module-attribute patch would not
intercept it). Never touches a real database.
"""

from datetime import date

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteMechanical import pm
from src.juteMechanical.constants import (
    FREQ_ANNUAL,
    FREQ_MONTHLY,
    FREQ_QUARTERLY,
    FREQ_WEEKLY,
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


def _machine_row(**over):
    mapping = {
        "machine_id": 10,
        "machine_name": "SPR-1",
        "mech_code": "S1",
        "machine_type_id": 3,
        "dept_id": 50,
        "branch_id": 1,
    }
    mapping.update(over)
    return _mock_row(mapping)


def _chain_db(attr=None, sched=None):
    """A db whose two _resolve_freq_days reads return the given mappings.

    ``None`` means "no row"; a dict becomes a row with that ``._mapping``.
    """
    db = MagicMock()
    db.execute.side_effect = [
        _exec_result(fetchone=None if attr is None else _mock_row(attr)),
        _exec_result(fetchone=None if sched is None else _mock_row(sched)),
    ]
    return db


# =============================================================================
# 1. _resolve_freq_days — the three-step chain
# =============================================================================


class TestResolveFreqDays:
    def test_step1_machine_attr_override_wins(self):
        """The machine's own pm_freq_days beats template and enum map alike."""
        db = _chain_db(attr={"pm_freq_days": 45}, sched={"freq_days": 7})

        result = pm._resolve_freq_days(db, 1, 10, 3, FREQ_MONTHLY)

        assert result == 45
        # Short-circuits: the template is never even read.
        assert db.execute.call_count == 1

    def test_step2_template_used_when_the_machine_has_no_override(self):
        db = _chain_db(attr={"pm_freq_days": None}, sched={"freq_days": 14})
        assert pm._resolve_freq_days(db, 1, 10, 3, FREQ_MONTHLY) == 14

    def test_step2_template_used_when_the_machine_has_no_attr_row_at_all(self):
        db = _chain_db(attr=None, sched={"freq_days": 14})
        assert pm._resolve_freq_days(db, 1, 10, 3, FREQ_MONTHLY) == 14

    def test_step3_enum_map_used_when_neither_resolves(self):
        db = _chain_db(attr=None, sched=None)
        assert pm._resolve_freq_days(db, 1, 10, 3, FREQ_MONTHLY) == 30
        assert PM_FREQUENCY_DAYS[FREQ_MONTHLY] == 30

    def test_step3_enum_map_used_when_the_template_freq_days_is_blank(self):
        db = _chain_db(attr=None, sched={"freq_days": None})
        assert pm._resolve_freq_days(db, 1, 10, 3, FREQ_QUARTERLY) == 91

    def test_step4_none_when_nothing_resolves_and_no_frequency_given(self):
        """No override, no template, no frequency -> no invented interval."""
        db = _chain_db(attr=None, sched=None)
        assert pm._resolve_freq_days(db, 1, 10, 3, None) is None

    def test_zero_attr_days_falls_through_rather_than_meaning_zero(self):
        """0 days is not a usable interval — the chain must keep looking."""
        db = _chain_db(attr={"pm_freq_days": 0}, sched={"freq_days": 7})
        assert pm._resolve_freq_days(db, 1, 10, 3, FREQ_MONTHLY) == 7

    @pytest.mark.parametrize(
        "frequency,expected",
        [
            ("DAILY", 1),
            (FREQ_WEEKLY, 7),
            (FREQ_MONTHLY, 30),
            (FREQ_QUARTERLY, 91),
            ("HALF_YEARLY", 182),
            (FREQ_ANNUAL, 365),
        ],
    )
    def test_every_frequency_maps_to_its_documented_day_count(self, frequency, expected):
        db = _chain_db(attr=None, sched=None)
        assert pm._resolve_freq_days(db, 1, 10, 3, frequency) == expected


class TestNextDueDate:
    """pm_date + freq_days, with None meaning 'not computable'."""

    def test_adds_the_interval(self):
        assert pm._next_due_date(date(2026, 6, 1), 30) == date(2026, 7, 1)

    def test_none_when_no_interval_resolved(self):
        assert pm._next_due_date(date(2026, 6, 1), None) is None

    def test_none_when_the_interval_is_zero(self):
        assert pm._next_due_date(date(2026, 6, 1), 0) is None

    def test_crosses_a_year_boundary(self):
        assert pm._next_due_date(date(2026, 12, 20), 30) == date(2027, 1, 19)

    def test_annual_interval_over_a_leap_year(self):
        assert pm._next_due_date(date(2024, 1, 1), 365) == date(2024, 12, 31)


class TestValidateFrequency:
    def test_none_passes_through(self):
        assert pm._validate_frequency(None) is None

    def test_blank_normalises_to_none(self):
        assert pm._validate_frequency("   ") is None

    def test_normalises_case(self):
        assert pm._validate_frequency("monthly") == FREQ_MONTHLY

    def test_rejects_unknown_frequency(self):
        with pytest.raises(HTTPException) as exc:
            pm._validate_frequency("FORTNIGHTLY")
        assert exc.value.status_code == 400
        assert exc.value.detail == pm.BAD_FREQUENCY_MSG


class TestValidateResult:
    def test_accepts_the_three_results(self):
        assert pm._validate_result("ok", 0) == "OK"
        assert pm._validate_result("NOT_OK", 0) == "NOT_OK"
        assert pm._validate_result("na", 0) == "NA"

    def test_rejects_anything_else_naming_the_row(self):
        with pytest.raises(HTTPException) as exc:
            pm._validate_result("MAYBE", 2)
        assert exc.value.status_code == 400
        assert "Task 3" in exc.value.detail

    def test_rejects_an_empty_result(self):
        with pytest.raises(HTTPException) as exc:
            pm._validate_result(None, 0)
        assert exc.value.status_code == 400


# =============================================================================
# 2. Endpoint tests — dependency_overrides, never a real DB
# =============================================================================


class TestPmEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    # ------------------------------------------------------------ co_id gate
    def test_pm_create_setup_missing_co_id_returns_400(self):
        response = client.get("/api/mechPM/pm_create_setup")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    def test_pm_checklist_missing_co_id_returns_400(self):
        response = client.get("/api/mechPM/pm_checklist?machine_id=10")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    def test_pm_checklist_missing_machine_id_returns_400(self):
        response = client.get("/api/mechPM/pm_checklist?co_id=1")
        assert response.status_code == 400
        assert "machine_id" in response.json()["detail"].lower()

    def test_pm_execution_table_missing_co_id_returns_400(self):
        response = client.get("/api/mechPM/pm_execution_table")
        assert response.status_code == 400
        assert "co_id" in response.json()["detail"].lower()

    # ---------------------------------------------------------------- setup
    def test_pm_create_setup_returns_enums_with_freq_days(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchall=[_machine_row()]),
        ]

        response = client.get("/api/mechPM/pm_create_setup?co_id=1")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["machines"][0]["machine_id"] == 10
        monthly = next(f for f in data["pm_frequencies"] if f["code"] == FREQ_MONTHLY)
        assert monthly["freq_days"] == 30
        assert {r["code"] for r in data["pm_results"]} == {"OK", "NOT_OK", "NA"}

    def test_pm_checklist_returns_tasks_and_total(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine_row()),  # _machine_context
            _exec_result(
                fetchall=[
                    _mock_row(
                        {
                            "pm_schedule_id": 4,
                            "task_desc": "Grease bearings",
                            "seq_no": 1,
                            "frequency": FREQ_MONTHLY,
                        }
                    )
                ]
            ),
        ]

        response = client.get("/api/mechPM/pm_checklist?co_id=1&machine_id=10")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["data"]["machine_name"] == "SPR-1"
        assert payload["data"]["tasks"][0]["task_desc"] == "Grease bearings"

    def test_pm_checklist_rejects_unknown_frequency(self):
        response = client.get(
            "/api/mechPM/pm_checklist?co_id=1&machine_id=10&frequency=FORTNIGHTLY"
        )
        assert response.status_code == 400
        assert response.json()["detail"] == pm.BAD_FREQUENCY_MSG

    def test_pm_checklist_rejects_unknown_machine(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.get("/api/mechPM/pm_checklist?co_id=1&machine_id=999")
        assert response.status_code == 400
        assert response.json()["detail"] == pm.UNKNOWN_MACHINE_MSG

    # --------------------------------------------------------------- create
    def _payload(self, **over):
        payload = {
            "co_id": 1,
            "machine_id": 10,
            "pm_date": "2026-06-01",
            "frequency": "MONTHLY",
            "done_by": "Fitter A",
        }
        payload.update(over)
        return payload

    def test_pm_execution_create_success_with_derived_next_due(self):
        """attr override 45 days -> next_due_date = pm_date + 45."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine_row()),                      # machine ctx
            _exec_result(fetchone=_mock_row({"pm_freq_days": 45})),     # resolve step1
            _exec_result(lastrowid=401),                                # insert header
            _exec_result(fetchone=_mock_row({"last_pm_date": date(2026, 6, 1)})),
            _exec_result(fetchone=_mock_row({"pm_freq_days": 45})),     # resolve again
            _exec_result(),                                             # attr upsert
        ]

        response = client.post("/api/mechPM/pm_execution_create", json=self._payload())

        assert response.status_code == 200
        assert response.json()["data"]["pm_exec_id"] == 401
        self._mock_session.commit.assert_called_once()

        insert_params = self._mock_session.execute.call_args_list[2].args[1]
        assert insert_params["next_due_date"] == date(2026, 7, 16)  # 1 Jun + 45
        assert insert_params["branch_id"] == 1                      # machine -> dept
        assert insert_params["frequency"] == FREQ_MONTHLY

        attr_params = self._mock_session.execute.call_args_list[-1].args[1]
        assert attr_params["last_pm_date"] == date(2026, 6, 1)
        assert attr_params["next_pm_due"] == date(2026, 7, 16)

    def test_pm_execution_create_falls_back_to_the_enum_map(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine_row()),
            _exec_result(fetchone=None),  # no attr row
            _exec_result(fetchone=None),  # no template row
            _exec_result(lastrowid=402),
            _exec_result(fetchone=_mock_row({"last_pm_date": date(2026, 6, 1)})),
            _exec_result(fetchone=None),
            _exec_result(fetchone=None),
            _exec_result(),
        ]

        response = client.post("/api/mechPM/pm_execution_create", json=self._payload())

        assert response.status_code == 200
        insert_params = self._mock_session.execute.call_args_list[3].args[1]
        assert insert_params["next_due_date"] == date(2026, 7, 1)  # 1 Jun + 30

    def test_pm_execution_create_without_frequency_leaves_next_due_null(self):
        """No frequency and nothing configured -> no next due date at all."""
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine_row()),
            _exec_result(fetchone=None),
            _exec_result(fetchone=None),
            _exec_result(lastrowid=403),
            _exec_result(fetchone=_mock_row({"last_pm_date": date(2026, 6, 1)})),
            _exec_result(fetchone=None),
            _exec_result(fetchone=None),
            _exec_result(),
        ]

        response = client.post(
            "/api/mechPM/pm_execution_create", json=self._payload(frequency=None)
        )

        assert response.status_code == 200
        insert_params = self._mock_session.execute.call_args_list[3].args[1]
        assert insert_params["next_due_date"] is None
        assert insert_params["frequency"] is None

    def test_pm_execution_create_rejects_unknown_frequency(self):
        response = client.post(
            "/api/mechPM/pm_execution_create",
            json=self._payload(frequency="FORTNIGHTLY"),
        )
        assert response.status_code == 400
        assert response.json()["detail"] == pm.BAD_FREQUENCY_MSG

    def test_pm_execution_create_rejects_unknown_machine(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.post("/api/mechPM/pm_execution_create", json=self._payload())
        assert response.status_code == 400
        assert response.json()["detail"] == pm.UNKNOWN_MACHINE_MSG

    def test_pm_execution_create_snapshots_posted_task_desc(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine_row()),
            _exec_result(fetchone=_mock_row({"pm_freq_days": 30})),
            _exec_result(lastrowid=404),
            _exec_result(),  # task insert
            _exec_result(fetchone=_mock_row({"last_pm_date": date(2026, 6, 1)})),
            _exec_result(fetchone=_mock_row({"pm_freq_days": 30})),
            _exec_result(),
        ]

        response = client.post(
            "/api/mechPM/pm_execution_create",
            json=self._payload(
                tasks=[{"task_desc": "Grease bearings", "result": "OK"}]
            ),
        )

        assert response.status_code == 200
        task_params = self._mock_session.execute.call_args_list[3].args[1]
        assert task_params["task_desc"] == "Grease bearings"
        assert task_params["result"] == "OK"

    def test_pm_execution_create_snapshots_task_desc_from_the_template(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine_row()),
            _exec_result(fetchone=_mock_row({"pm_freq_days": 30})),
            _exec_result(lastrowid=405),
            _exec_result(fetchone=_mock_row({"task_desc": "Check oil level"})),
            _exec_result(),  # task insert
            _exec_result(fetchone=_mock_row({"last_pm_date": date(2026, 6, 1)})),
            _exec_result(fetchone=_mock_row({"pm_freq_days": 30})),
            _exec_result(),
        ]

        response = client.post(
            "/api/mechPM/pm_execution_create",
            json=self._payload(tasks=[{"pm_schedule_id": 9, "result": "NOT_OK"}]),
        )

        assert response.status_code == 200
        task_params = self._mock_session.execute.call_args_list[4].args[1]
        assert task_params["task_desc"] == "Check oil level"
        assert task_params["result"] == "NOT_OK"

    def test_pm_execution_create_rejects_a_task_with_no_description(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine_row()),
            _exec_result(fetchone=_mock_row({"pm_freq_days": 30})),
            _exec_result(lastrowid=406),
        ]

        response = client.post(
            "/api/mechPM/pm_execution_create",
            json=self._payload(tasks=[{"result": "OK"}]),
        )

        assert response.status_code == 400
        assert "task_desc" in response.json()["detail"]
        self._mock_session.commit.assert_not_called()

    def test_pm_execution_create_rejects_a_bad_task_result(self):
        self._mock_session.execute.side_effect = [
            _exec_result(fetchone=_machine_row()),
            _exec_result(fetchone=_mock_row({"pm_freq_days": 30})),
            _exec_result(lastrowid=407),
        ]

        response = client.post(
            "/api/mechPM/pm_execution_create",
            json=self._payload(tasks=[{"task_desc": "x", "result": "MAYBE"}]),
        )

        assert response.status_code == 400
        assert "Task 1" in response.json()["detail"]

    def test_pm_execution_create_body_missing_required_field_returns_422(self):
        response = client.post(
            "/api/mechPM/pm_execution_create", json={"co_id": 1, "machine_id": 10}
        )
        assert response.status_code == 422

    def test_pm_execution_create_rejects_negative_downtime(self):
        response = client.post(
            "/api/mechPM/pm_execution_create", json=self._payload(downtime_min=-5)
        )
        assert response.status_code == 422

    # ----------------------------------------------------------------- edit
    def test_pm_execution_edit_not_found_returns_404(self):
        self._mock_session.execute.side_effect = [_exec_result(fetchone=None)]
        response = client.put(
            "/api/mechPM/pm_execution_edit/999", json=self._payload()
        )
        assert response.status_code == 404


class TestPmMachineAttrFeedback:
    """last_pm_date / next_pm_due come from MAX(pm_date), never the saved row."""

    def test_clears_both_columns_when_no_live_pm_remains(self):
        db = MagicMock()
        db.execute.side_effect = [
            _exec_result(fetchone=_mock_row({"last_pm_date": None})),
            _exec_result(),  # clear
        ]

        pm._write_machine_attr_feedback(
            db, co_id=1, machine_id=10, machine_type_id=3,
            frequency=FREQ_MONTHLY, user_id=1,
        )

        assert db.execute.call_count == 2

    def test_upserts_the_recomputed_dates(self):
        db = MagicMock()
        db.execute.side_effect = [
            _exec_result(fetchone=_mock_row({"last_pm_date": date(2026, 6, 1)})),
            _exec_result(fetchone=_mock_row({"pm_freq_days": 7})),
            _exec_result(),  # upsert
        ]

        pm._write_machine_attr_feedback(
            db, co_id=1, machine_id=10, machine_type_id=3,
            frequency=FREQ_MONTHLY, user_id=1,
        )

        params = db.execute.call_args_list[-1].args[1]
        assert params["last_pm_date"] == date(2026, 6, 1)
        assert params["next_pm_due"] == date(2026, 6, 8)

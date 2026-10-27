"""Tests for the EJM fortnight-wise Efficiency Code API (ejm_fne_eff_code_type)."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms.ejmFneEffCodeType import (
    EDITABLE_STATUSES,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_DRAFT,
    STATUS_OPEN,
    STATUS_PENDING,
    STATUS_REJECTED,
    TRANSITIONS,
    _clean_eff_pc,
    get_fne_eff_code_list_query,
    get_fne_status_query,
)
from src.main import app

client = TestClient(app)

PREFIX = "/api/hrmsMasters"


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    for k, v in mapping.items():
        setattr(row, k, v)
    return row


# ─── Queries ────────────────────────────────────────────────────────


class TestFneQueries:
    def test_list_query_binds_fortnight_and_matches_code_value(self):
        sql = str(get_fne_eff_code_list_query())
        assert isinstance(get_fne_eff_code_list_query(), type(text("")))
        assert ":forthnig_id" in sql
        # eff_code stores the code VALUE, not ejm_eff_code_type's primary key.
        assert "CAST(e.effocode AS UNSIGNED)" in sql

    def test_status_query_binds_fortnight(self):
        assert ":forthnig_id" in str(get_fne_status_query())


# ─── Transition table ───────────────────────────────────────────────


class TestTransitions:
    def test_full_happy_path(self):
        assert TRANSITIONS["open"][STATUS_DRAFT] == STATUS_OPEN
        assert TRANSITIONS["send-for-approval"][STATUS_OPEN] == STATUS_PENDING
        assert TRANSITIONS["approve"][STATUS_PENDING] == STATUS_APPROVED

    def test_reject_and_cancel_land_on_their_own_statuses(self):
        assert TRANSITIONS["reject"][STATUS_PENDING] == STATUS_REJECTED
        assert TRANSITIONS["cancel"][STATUS_DRAFT] == STATUS_CANCELLED

    def test_reopen_goes_back_to_draft_from_cancel_and_open_from_reject(self):
        assert TRANSITIONS["reopen"][STATUS_CANCELLED] == STATUS_DRAFT
        assert TRANSITIONS["reopen"][STATUS_REJECTED] == STATUS_OPEN

    def test_approved_is_terminal(self):
        assert all(STATUS_APPROVED not in froms for froms in TRANSITIONS.values())

    def test_only_draft_and_open_are_editable(self):
        assert set(EDITABLE_STATUSES) == {STATUS_DRAFT, STATUS_OPEN}


class TestCleanEffPc:
    def test_blank_is_none(self):
        for raw in (None, "", "  "):
            assert _clean_eff_pc(raw) is None

    def test_parses(self):
        assert _clean_eff_pc("82.50") == Decimal("82.50")

    def test_rejects_text_and_overflow(self):
        for raw in ("abc", "10000"):
            with pytest.raises(HTTPException) as exc:
                _clean_eff_pc(raw)
            assert exc.value.status_code == 400


# ─── Endpoints ──────────────────────────────────────────────────────


class TestFneEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._db
        yield
        app.dependency_overrides.clear()

    def test_table_requires_forthnight(self):
        response = client.get(f"{PREFIX}/get_ejm_fne_eff_code_type")
        assert response.status_code == 400

    def test_empty_fortnight_reads_as_draft_and_editable(self):
        self._db.execute.return_value.fetchall.return_value = []
        self._db.execute.return_value.fetchone.return_value = _mock_row(
            {"status_id": None, "row_count": 0}
        )
        body = client.get(f"{PREFIX}/get_ejm_fne_eff_code_type?forthnig_id=5").json()
        assert body["status_id"] == STATUS_DRAFT
        assert body["editable"] is True
        assert body["row_count"] == 0

    def test_approved_fortnight_is_not_editable(self):
        self._db.execute.return_value.fetchall.return_value = []
        self._db.execute.return_value.fetchone.return_value = _mock_row(
            {"status_id": STATUS_APPROVED, "row_count": 4}
        )
        body = client.get(f"{PREFIX}/get_ejm_fne_eff_code_type?forthnig_id=5").json()
        assert body["status_id"] == STATUS_APPROVED
        assert body["editable"] is False

    def test_save_rejects_locked_fortnight(self):
        self._db.execute.return_value.fetchone.return_value = _mock_row(
            {"status_id": STATUS_APPROVED, "row_count": 3}
        )
        response = client.post(f"{PREFIX}/ejm_fne_eff_code_type_save", json={
            "forthnig_id": 5,
            "lines": [{"eff_code": 43, "eff_pc": 80}],
        })
        assert response.status_code == 400
        assert "Approved" in response.json()["detail"]

    def test_save_rejects_duplicate_codes(self):
        self._db.execute.return_value.fetchone.return_value = _mock_row(
            {"status_id": STATUS_DRAFT, "row_count": 0}
        )
        response = client.post(f"{PREFIX}/ejm_fne_eff_code_type_save", json={
            "forthnig_id": 5,
            "lines": [{"eff_code": 43, "eff_pc": 80}, {"eff_code": 43, "eff_pc": 90}],
        })
        assert response.status_code == 400
        assert "more than once" in response.json()["detail"]

    def test_status_rejects_unknown_action(self):
        response = client.post(f"{PREFIX}/ejm_fne_eff_code_type_status",
                               json={"forthnig_id": 5, "action": "teleport"})
        assert response.status_code == 400

    def test_status_rejects_illegal_transition(self):
        self._db.execute.return_value.fetchone.return_value = _mock_row(
            {"status_id": STATUS_APPROVED, "row_count": 3}
        )
        response = client.post(f"{PREFIX}/ejm_fne_eff_code_type_status",
                               json={"forthnig_id": 5, "action": "approve"})
        assert response.status_code == 400
        assert "Approved" in response.json()["detail"]

    def test_status_rejects_empty_fortnight(self):
        self._db.execute.return_value.fetchone.return_value = _mock_row(
            {"status_id": None, "row_count": 0}
        )
        response = client.post(f"{PREFIX}/ejm_fne_eff_code_type_status",
                               json={"forthnig_id": 5, "action": "open"})
        assert response.status_code == 400

    def test_status_open_moves_draft_to_open(self):
        self._db.execute.return_value.fetchone.return_value = _mock_row(
            {"status_id": STATUS_DRAFT, "row_count": 3}
        )
        body = client.post(f"{PREFIX}/ejm_fne_eff_code_type_status",
                           json={"forthnig_id": 5, "action": "open"}).json()
        assert body["status_id"] == STATUS_OPEN

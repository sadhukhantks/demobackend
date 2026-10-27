"""Tests for the EJM Efficiency Code Type API (ejm_eff_code_type)."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms.ejmEffCodeType import (
    _assert_unique,
    _clean_eff_pc,
    _clean_effocode,
    get_ejm_eff_code_type_by_id_query,
    get_ejm_eff_code_type_list_query,
)
from src.main import app

client = TestClient(app)

PREFIX = "/api/hrmsMasters"


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    row.cnt = mapping.get("cnt", 0)
    return row


# ─── Query functions ────────────────────────────────────────────────


class TestEffCodeQueries:
    def test_list_query_has_search_bind(self):
        sql = get_ejm_eff_code_type_list_query()
        assert isinstance(sql, type(text("")))
        assert ":search" in str(sql)

    def test_by_id_query_has_bind(self):
        assert ":eff_code_type_id" in str(get_ejm_eff_code_type_by_id_query())


# ─── Field cleaning ─────────────────────────────────────────────────


class TestCleanEffocode:
    def test_blank_rejected(self):
        for body in ({}, {"effocode": ""}, {"effocode": "   "}, {"effocode": None}):
            with pytest.raises(HTTPException) as exc:
                _clean_effocode(body)
            assert exc.value.status_code == 400

    def test_too_long_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _clean_effocode({"effocode": "12345678901"})
        assert exc.value.status_code == 400

    def test_trimmed(self):
        assert _clean_effocode({"effocode": "  43 "}) == "43"


class TestCleanEffPc:
    def test_empty_is_none(self):
        for body in ({}, {"eff_pc": ""}, {"eff_pc": None}, {"eff_pc": "  "}):
            assert _clean_eff_pc(body) is None

    def test_parses_number(self):
        assert _clean_eff_pc({"eff_pc": "82.50"}) == Decimal("82.50")
        assert _clean_eff_pc({"eff_pc": 100}) == Decimal("100")

    def test_non_numeric_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _clean_eff_pc({"eff_pc": "abc"})
        assert exc.value.status_code == 400

    def test_out_of_decimal_6_2_range_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _clean_eff_pc({"eff_pc": "10000"})
        assert exc.value.status_code == 400


class TestAssertUnique:
    def test_duplicate_raises_400(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _mock_row({"cnt": 1})
        with pytest.raises(HTTPException) as exc:
            _assert_unique(db, "43")
        assert exc.value.status_code == 400

    def test_edit_excludes_own_row(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _mock_row({"cnt": 0})
        _assert_unique(db, "43", eff_code_type_id=7)
        assert db.execute.call_args[0][1]["eff_code_type_id"] == 7


# ─── Endpoints ──────────────────────────────────────────────────────


class TestEffCodeEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._mock_session = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._mock_session
        yield
        app.dependency_overrides.clear()

    def test_table_returns_paginated(self):
        rows = [
            _mock_row({"eff_code_type_id": i, "effocode": str(i), "code_details": "x", "eff_pc": 80})
            for i in range(1, 6)
        ]
        self._mock_session.execute.return_value.fetchall.return_value = rows

        response = client.get(f"{PREFIX}/get_ejm_eff_code_type_table?page=2&limit=2")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 5
        assert [r["eff_code_type_id"] for r in body["data"]] == [3, 4]

    def test_by_id_missing_returns_404(self):
        self._mock_session.execute.return_value.fetchone.return_value = None
        response = client.get(f"{PREFIX}/get_ejm_eff_code_type_by_id/99")
        assert response.status_code == 404

    def test_create_rejects_blank_code(self):
        response = client.post(f"{PREFIX}/ejm_eff_code_type_create", json={"effocode": ""})
        assert response.status_code == 400

    def test_edit_missing_row_returns_404(self):
        # duplicate check + row lookup both go through execute(); row lookup is None
        self._mock_session.execute.return_value.fetchone.return_value = None
        response = client.put(f"{PREFIX}/ejm_eff_code_type_edit/99", json={"effocode": "43"})
        assert response.status_code == 404

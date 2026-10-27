"""Tests for the EJM piece-rate / occupation API (ejm_piece_occupation_rate)."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms.ejmPieceOccupationRate import (
    _assert_free,
    _clean_occu_code,
    _clean_rate,
    _resolve_designation,
    get_piece_occupation_designation_query,
    get_piece_occupation_rate_by_id_query,
    get_piece_occupation_rate_list_query,
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


class TestPieceRateQueries:
    def test_list_query_binds_search(self):
        sql = get_piece_occupation_rate_list_query()
        assert isinstance(sql, type(text("")))
        assert ":search" in str(sql)

    def test_by_id_query_uses_surrogate_id(self):
        assert ":p_occu_id" in str(get_piece_occupation_rate_by_id_query())

    def test_designation_query_is_branch_scoped_and_active_only(self):
        sql = str(get_piece_occupation_designation_query())
        assert ":branch_id" in sql
        assert "g.active = 1" in sql


# ─── Field cleaning ─────────────────────────────────────────────────


class TestCleanOccuCode:
    def test_blank_rejected(self):
        for body in ({}, {"occu_code": ""}, {"occu_code": "  "}):
            with pytest.raises(HTTPException) as exc:
                _clean_occu_code(body)
            assert exc.value.status_code == 400

    def test_too_long_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _clean_occu_code({"occu_code": "123"})
        assert exc.value.status_code == 400

    def test_numeric_is_zero_padded_to_char_2(self):
        assert _clean_occu_code({"occu_code": "1"}) == "01"
        assert _clean_occu_code({"occu_code": " 2 "}) == "02"
        assert _clean_occu_code({"occu_code": "43"}) == "43"


class TestCleanRate:
    def test_required(self):
        for body in ({}, {"f_b_rate": ""}, {"f_b_rate": None}):
            with pytest.raises(HTTPException) as exc:
                _clean_rate(body)
            assert exc.value.status_code == 400

    def test_keeps_six_decimals(self):
        assert _clean_rate({"f_b_rate": "3.303060"}) == Decimal("3.303060")

    def test_rejects_text_and_overflow(self):
        for raw in ("abc", "10000"):
            with pytest.raises(HTTPException) as exc:
                _clean_rate({"f_b_rate": raw})
            assert exc.value.status_code == 400


class TestResolveDesignation:
    def test_missing_designation_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _resolve_designation(MagicMock(), {})
        assert exc.value.status_code == 400

    def test_unknown_designation_rejected(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        with pytest.raises(HTTPException) as exc:
            _resolve_designation(db, {"desig_id": 999})
        assert exc.value.status_code == 400

    def test_department_without_legacy_code_rejected(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _mock_row(
            {"designation_id": 71, "dept_id": 21, "dept_code": ""}
        )
        with pytest.raises(HTTPException) as exc:
            _resolve_designation(db, {"desig_id": 71})
        assert "legacy department code" in exc.value.detail

    def test_returns_dept_from_designation(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _mock_row(
            {"designation_id": 71, "dept_id": 21, "dept_code": "08"}
        )
        assert _resolve_designation(db, {"desig_id": 71}) == (71, 21, "08")


class TestAssertFree:
    def test_duplicate_dept_occu_pair_rejected(self):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = [
            _mock_row({"p_occu_id": 7, "dept_code": "08", "occu_code": "01", "desig_id": 71})
        ]
        with pytest.raises(HTTPException) as exc:
            _assert_free(db, "08", "01", 999)
        assert "already exists" in exc.value.detail

    def test_designation_already_rated_rejected(self):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = [
            _mock_row({"p_occu_id": 7, "dept_code": "08", "occu_code": "01", "desig_id": 71})
        ]
        with pytest.raises(HTTPException) as exc:
            _assert_free(db, "09", "05", 71)
        assert "already has a rate" in exc.value.detail

    def test_editing_own_row_is_allowed(self):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = [
            _mock_row({"p_occu_id": 7, "dept_code": "08", "occu_code": "01", "desig_id": 71})
        ]
        _assert_free(db, "08", "01", 71, p_occu_id=7)


# ─── Endpoints ──────────────────────────────────────────────────────


class TestPieceRateEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._db
        yield
        app.dependency_overrides.clear()

    def test_designations_require_branch(self):
        response = client.get(f"{PREFIX}/get_ejm_piece_occupation_designations")
        assert response.status_code == 400

    def test_table_paginates(self):
        rows = [
            _mock_row({
                "p_occu_id": i, "dept_code": "0%d" % i, "occu_code": "01",
                "f_b_rate": Decimal("3.3"), "dept_id": 1, "dept_desc": "D",
                "desig_id": i, "desig": "G",
            })
            for i in range(1, 6)
        ]
        self._db.execute.return_value.fetchall.return_value = rows
        body = client.get(f"{PREFIX}/get_ejm_piece_occupation_rate_table?page=2&limit=2").json()
        assert body["total"] == 5
        assert [r["p_occu_id"] for r in body["data"]] == [3, 4]

    def test_by_id_missing_returns_404(self):
        self._db.execute.return_value.fetchone.return_value = None
        response = client.get(f"{PREFIX}/get_ejm_piece_occupation_rate_by_id/99")
        assert response.status_code == 404

    def test_create_rejects_blank_occu_code(self):
        response = client.post(f"{PREFIX}/ejm_piece_occupation_rate_create",
                               json={"desig_id": 71, "occu_code": "", "f_b_rate": 3.3})
        assert response.status_code == 400

    def test_create_rejects_missing_rate(self):
        response = client.post(f"{PREFIX}/ejm_piece_occupation_rate_create",
                               json={"desig_id": 71, "occu_code": "01"})
        assert response.status_code == 400

"""Tests for the EJM wages quality-rate API (ejm_wages_quality_rate)."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms.ejmWagesQualityRate import (
    _assert_free,
    _clean_int,
    _clean_rate,
    get_wages_quality_rate_list_query,
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


class TestCleanRate:
    def test_required(self):
        for body in ({}, {"quality_rate": ""}, {"quality_rate": None}):
            with pytest.raises(HTTPException) as exc:
                _clean_rate(body)
            assert exc.value.status_code == 400

    def test_keeps_six_decimals(self):
        assert _clean_rate({"quality_rate": "3.303060"}) == Decimal("3.303060")

    def test_rejects_text_and_overflow(self):
        for raw in ("abc", "10000"):
            with pytest.raises(HTTPException) as exc:
                _clean_rate({"quality_rate": raw})
            assert exc.value.status_code == 400


class TestCleanInt:
    def test_missing_and_invalid_rejected(self):
        for body in ({}, {"dept_id": ""}, {"dept_id": "abc"}):
            with pytest.raises(HTTPException) as exc:
                _clean_int(body, "dept_id", "Department")
            assert exc.value.status_code == 400

    def test_numeric_string_accepted(self):
        assert _clean_int({"dept_id": "21"}, "dept_id", "Department") == 21


class TestAssertFree:
    def test_duplicate_pair_rejected(self):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = [
            _mock_row({"wages_quality_rate_id": 7})
        ]
        with pytest.raises(HTTPException) as exc:
            _assert_free(db, 21, 5)
        assert "already has a rate" in exc.value.detail

    def test_editing_own_row_is_allowed(self):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = [
            _mock_row({"wages_quality_rate_id": 7})
        ]
        _assert_free(db, 21, 5, record_id=7)


class TestQualityRateEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._db
        yield
        app.dependency_overrides.clear()

    def test_list_query_is_branch_scoped(self):
        sql = str(get_wages_quality_rate_list_query())
        assert ":branch_id" in sql and ":search" in sql

    def test_table_paginates(self):
        rows = [
            _mock_row({
                "wages_quality_rate_id": i, "dept_id": 21, "dept_desc": "WEAVING",
                "quality_id": i, "q_code": "Q%d" % i, "quality": "Q",
                "quality_rate": Decimal("3.3"),
            })
            for i in range(1, 6)
        ]
        self._db.execute.return_value.fetchall.return_value = rows
        body = client.get(f"{PREFIX}/get_ejm_wages_quality_rate_table?page=2&limit=2").json()
        assert body["total"] == 5
        assert [r["wages_quality_rate_id"] for r in body["data"]] == [3, 4]

    def test_by_id_missing_returns_404(self):
        self._db.execute.return_value.fetchone.return_value = None
        assert client.get(f"{PREFIX}/get_ejm_wages_quality_rate_by_id/99").status_code == 404

    def test_create_rejects_missing_rate(self):
        response = client.post(f"{PREFIX}/ejm_wages_quality_rate_create",
                               json={"dept_id": 21, "quality_id": 5})
        assert response.status_code == 400

    def test_create_rejects_missing_department(self):
        response = client.post(f"{PREFIX}/ejm_wages_quality_rate_create",
                               json={"quality_id": 5, "quality_rate": 3.3})
        assert response.status_code == 400

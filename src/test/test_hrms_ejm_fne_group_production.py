"""Tests for the EJM fortnightly machine-group production API (ejm_fne_group_production)."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms import ejmFneGroupProduction as mod
from src.main import app

client = TestClient(app)
PREFIX = "/api/hrmsMasters"

MACHINE = {"machine_id": 7, "machine_name": "BEAMING-01", "dept_id": 20,
           "dept_desc": "BEAMING(EJM)", "machine_type_name": "Beaming"}


def _row(**kw):
    r = MagicMock()
    r._mapping = kw
    for k, v in kw.items():
        setattr(r, k, v)
    return r


class TestCleanDecimal:
    def test_blank_is_none_and_parses(self):
        assert mod._clean_decimal("", "Production") is None
        assert mod._clean_decimal("12.345", "Production") == Decimal("12.345")

    def test_rejects_text_and_overflow(self):
        for raw in ("abc", "10000000"):
            with pytest.raises(HTTPException):
                mod._clean_decimal(raw, "Production")


class TestMachineQuery:
    def test_only_beaming_and_press_types(self):
        sql = str(mod.get_group_machines_query())
        assert "'%BEAM%'" in sql and "'%PRESS%'" in sql


class TestSave:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        # Every fetchall() returns the machine row and the shift row; the code only reads its own fields.
        self.db.execute.return_value.fetchall.return_value = [_row(**MACHINE, shift_id=8)]
        self.db.execute.return_value.fetchone.return_value = None  # no duplicate
        self.db.execute.return_value.lastrowid = 55
        with patch.object(mod, "_assert_fortnight_exists"), \
             patch.object(mod, "_quality_options_by_dept", return_value={20: [{"value": 5, "label": "Q5"}]}):
            yield
        app.dependency_overrides.clear()

    def _post(self, **over):
        body = {"period_id": 1, "branch_id": 29, "machine_id": 7, "shift_id": 8,
                "quality_id": 5, "production": "10.5", **over}
        return client.post(f"{PREFIX}/ejm_fne_group_production_save", json=body)

    def test_insert_returns_new_id(self):
        r = self._post()
        assert r.status_code == 200 and r.json()["grp_prod_id"] == 55

    def test_rejects_non_group_machine(self):
        assert self._post(machine_id=999).status_code == 400

    def test_rejects_shift_of_another_branch(self):
        assert self._post(shift_id=999).status_code == 400

    def test_rejects_quality_of_another_department(self):
        assert self._post(quality_id=999).status_code == 400

    def test_rejects_duplicate(self):
        self.db.execute.return_value.fetchone.return_value = _row(grp_prod_id=3)
        r = self._post()
        assert r.status_code == 400 and "already has a row" in r.json()["detail"]

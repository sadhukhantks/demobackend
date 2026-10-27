"""Tests for the EJM fortnightly group machine hours API (ejm_wages_group_mc_hours)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms import ejmFneGroupMachineHours as mod
from src.main import app

client = TestClient(app)
PREFIX = "/api/hrmsMasters"


def _row(**kw):
    r = MagicMock()
    r._mapping = kw
    for k, v in kw.items():
        setattr(r, k, v)
    return r


class TestSave:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        # One fetchall() row serves as both the machine and the shift lookup.
        self.db.execute.return_value.fetchall.return_value = [
            _row(machine_id=7, dept_id=20, machine_name="BM-1", machine_type_name="Beaming",
                 dept_desc="BEAMING(EJM)", shift_id=8)]
        self.db.execute.return_value.fetchone.return_value = None
        self.db.execute.return_value.lastrowid = 41
        with patch.object(mod, "_assert_fortnight_exists"):
            yield
        app.dependency_overrides.clear()

    def _post(self, **over):
        body = {"period_id": 1, "branch_id": 29, "machine_id": 7, "shift_id": 8,
                "mc_hours": "96", "idle_hours": "4.5", **over}
        return client.post(f"{PREFIX}/ejm_group_mc_hours_save", json=body)

    def test_insert_copies_dept_from_machine(self):
        r = self._post()
        assert r.status_code == 200 and r.json()["grp_mc_hrs_id"] == 41
        insert_params = self.db.execute.call_args_list[-1].args[1]
        assert insert_params["dept_id"] == 20

    def test_rejects_non_group_machine(self):
        assert self._post(machine_id=999).status_code == 400

    def test_rejects_shift_of_another_branch(self):
        assert self._post(shift_id=999).status_code == 400

    def test_rejects_bad_hours(self):
        assert self._post(mc_hours="abc").status_code == 400

    def test_rejects_duplicate(self):
        self.db.execute.return_value.fetchone.return_value = _row(grp_mc_hrs_id=3)
        r = self._post()
        assert r.status_code == 400 and "already has a row" in r.json()["detail"]

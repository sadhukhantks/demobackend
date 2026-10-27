"""Tests for the EJM fortnightly wages quality target API (ejm_wages_fne_qual_target)."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.hrms import ejmWagesFneQualTarget as mod
from src.main import app

client = TestClient(app)

PREFIX = "/api/hrmsMasters"
# ejm_wages_quality_rate rows of the department (value = wages_quality_rate_id).
OPTIONS = [{"value": 5, "label": "Q5 - Hessian"}, {"value": 6, "label": "Q6 - Sacking"},
           {"value": 7, "label": "Q7 - Hands sewers"}]
PROCESSES = ["Handsewer", "Hemming", "Heracle", "Press"]


def _row(**kw):
    r = MagicMock()
    for k, v in kw.items():
        setattr(r, k, v)
    return r


class TestCleanEffPc:
    def test_blank_is_none(self):
        for raw in (None, "", "  "):
            assert mod._clean_eff_pc(raw) is None

    def test_parses(self):
        assert mod._clean_eff_pc("82.50") == Decimal("82.50")

    def test_rejects_text_and_overflow(self):
        for raw in ("abc", "10000"):
            with pytest.raises(HTTPException) as exc:
                mod._clean_eff_pc(raw)
            assert exc.value.status_code == 400


class TestEndpoints:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        with patch.object(mod, "_assert_department", return_value=21), \
             patch.object(mod, "_rate_options", return_value=OPTIONS), \
             patch.object(mod, "_assert_fortnight_exists"), \
             patch.object(mod, "_link_processes", return_value=PROCESSES),              patch.object(mod, "_ran_quality_ids", return_value=None) as self.ran:
            yield
        app.dependency_overrides.clear()

    def test_grid_returns_only_saved_rows_plus_options(self):
        self.db.execute.return_value.fetchall.return_value = [
            _row(fne_qual_traget_id=9, quality_id=6, process=None, eff_pc=Decimal("80.00"), status_id=3)]
        body = client.get(f"{PREFIX}/get_ejm_fne_qual_target_grid?period_id=1&dept_id=21").json()
        assert [r["quality_id"] for r in body["data"]] == [6]
        assert body["data"][0]["quality_label"] == "Q6 - Sacking"
        assert body["data"][0]["status_id"] == mod.STATUS_APPROVED
        assert [q["value"] for q in body["qualities"]] == [5, 6, 7]
        assert body["processes"] == PROCESSES

    def test_grid_options_list_ran_qualities_first(self):
        self.ran.return_value = {6}
        self.db.execute.return_value.fetchall.return_value = []
        body = client.get(f"{PREFIX}/get_ejm_fne_qual_target_grid?period_id=1&dept_id=21").json()
        assert [(q["value"], q["ran"]) for q in body["qualities"]] == [(6, True), (5, False), (7, False)]

    def test_table_paginates_and_requires_branch(self):
        assert client.get(f"{PREFIX}/get_ejm_fne_qual_target_table").status_code == 400
        rows = []
        for i in range(1, 6):
            r = _row(period_id=i, dept_id=21)
            r._mapping = {"period_id": i, "dept_id": 21}
            rows.append(r)
        self.db.execute.return_value.fetchall.return_value = rows
        body = client.get(f"{PREFIX}/get_ejm_fne_qual_target_table?branch_id=87&page=2&limit=2").json()
        assert body["total"] == 5 and [r["period_id"] for r in body["data"]] == [3, 4]

    def test_grid_requires_period(self):
        assert client.get(f"{PREFIX}/get_ejm_fne_qual_target_grid?dept_id=21").status_code == 400

    def _save(self, saved_rows, **body):
        self.db.execute.return_value.fetchall.return_value = saved_rows
        self.db.execute.return_value.fetchone.return_value = _row(next_id=10)
        return client.post(f"{PREFIX}/ejm_fne_qual_target_save",
                           json={"period_id": 1, "dept_id": 21, **body})

    def test_save_skips_blank_lines(self):
        r = self._save([], lines=[{"quality_id": 5, "eff_pc": 80}, {"quality_id": 6, "eff_pc": ""}])
        assert r.status_code == 200 and r.json()["saved"] == 1

    def test_save_rejects_quality_of_another_department(self):
        r = self._save([], lines=[{"quality_id": 99, "eff_pc": 80}])
        assert r.status_code == 400

    def test_approved_row_is_locked(self):
        r = self._save([_row(fne_qual_traget_id=9, quality_id=5, process=None, status_id=3)],
                       lines=[{"quality_id": 5, "eff_pc": 80}])
        assert r.status_code == 400 and "locked" in r.json()["detail"]

    def test_approve_needs_a_value(self):
        r = self._save([], approve=True, lines=[{"quality_id": 5, "eff_pc": ""}])
        assert r.status_code == 400

    def test_process_is_optional_but_must_be_known(self):
        assert self._save([], lines=[{"quality_id": 7, "eff_pc": 80}]).status_code == 200
        r = self._save([], lines=[{"quality_id": 7, "process": "Calender", "eff_pc": 80}])
        assert r.status_code == 400

    def test_same_quality_saves_once_per_process(self):
        r = self._save([], lines=[{"quality_id": 7, "process": "Hemming", "eff_pc": 80},
                                  {"quality_id": 7, "process": "Heracle", "eff_pc": 60},
                                  {"quality_id": 7, "eff_pc": 70}])
        assert r.status_code == 200 and r.json()["saved"] == 3
        r = self._save([], lines=[{"quality_id": 7, "process": "Hemming", "eff_pc": 80},
                                  {"quality_id": 7, "process": "Hemming", "eff_pc": 60}])
        assert r.status_code == 400

    def test_modify_changes_process_by_id(self):
        r = self._save([_row(fne_qual_traget_id=9, quality_id=7, process=None, status_id=21)],
                       lines=[{"id": 9, "quality_id": 7, "process": "Hemming", "eff_pc": 80}])
        assert r.status_code == 200
        upd = [c for c in self.db.execute.call_args_list if "UPDATE" in str(c.args[0])][0]
        assert upd.args[1]["process"] == "Hemming" and upd.args[1]["id"] == 9
        # ...but not onto a pair another row already holds
        r = self._save([_row(fne_qual_traget_id=9, quality_id=7, process=None, status_id=21),
                        _row(fne_qual_traget_id=10, quality_id=7, process="Hemming", status_id=21)],
                       lines=[{"id": 9, "quality_id": 7, "process": "Hemming", "eff_pc": 80}])
        assert r.status_code == 400

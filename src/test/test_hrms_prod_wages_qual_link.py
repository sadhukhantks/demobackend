"""Tests for the Production Quality <-> Wages Quality link master."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.hrms.prodWagesQualLink import quality_type_for

client = TestClient(app)

PREFIX = "/api/hrmsMasters"


def _mock_row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    for k, v in mapping.items():
        setattr(row, k, v)
    return row


class TestQualityTypeFor:
    @pytest.mark.parametrize("name,expected", [
        ("WEAVING(EJM)", "weaving"),
        ("COP WINDING(EJM)", "winding"),
        ("Spool Winding", "winding"),
        ("FINISHING dev", "finishing"),
        ("Beaming", "beaming"),
        ("BATCHING(EJM)", None),
        (None, None),
    ])
    def test_derives_type_from_department_name(self, name, expected):
        assert quality_type_for(name) == expected


class TestEndpoints:
    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self._db
        yield
        app.dependency_overrides.clear()

    def test_sack_sewing_uses_finishing_qualities(self):
        from src.hrms.prodWagesQualLink import quality_type_for
        assert quality_type_for("SACK SEWING(EJM)") == "finishing"

    def test_setup_requires_co_id(self):
        assert client.get(f"{PREFIX}/prod_wages_link_setup").status_code == 400

    def test_setup_keeps_only_typed_departments(self):
        self._db.execute.return_value.fetchall.side_effect = [
            [_mock_row({"dept_id": 21, "dept_desc": "WEAVING(EJM)", "branch_id": 2, "co_id": 1}),
             _mock_row({"dept_id": 15, "dept_desc": "BATCHING(EJM)", "branch_id": 2, "co_id": 1})],
            [],  # no branch rated in ejm_wages_quality_rate
            [_mock_row({"q_code_id": 7, "q_code": "6501", "quality": "UPTO 22LBS", "master_dept_id": 21,
                        "branch_id": 2})],
            [],
        ]
        data = client.get(f"{PREFIX}/prod_wages_link_setup?co_id=1").json()["data"]
        assert data["departments"] == [{"value": "21", "label": "WEAVING(EJM)", "quality_type": "weaving"}]
        assert data["wages_qualities"] == [{"value": "7", "label": "6501 - UPTO 22LBS", "dept_id": "21"}]

    def test_setup_ejm_branch_uses_rate_table_not_master(self):
        self._db.execute.return_value.fetchall.side_effect = [
            [_mock_row({"dept_id": 20, "dept_desc": "BEAMING(EJM)", "branch_id": 29, "co_id": 1})],
            [_mock_row({"branch_id": 29})],
            [_mock_row({"q_code_id": 7, "q_code": "6501", "quality": "X", "master_dept_id": 20, "branch_id": 29}),
             _mock_row({"q_code_id": 8, "q_code": "3502", "quality": "B TWILL", "master_dept_id": 347,
                        "branch_id": 87})],
            [_mock_row({"wages_quality_rate_id": 3, "dept_id": 20, "quality_code": "037",
                        "quality_desc": "HESS 9X8", "quality_rate": Decimal("0.127100")})],
        ]
        data = client.get(f"{PREFIX}/prod_wages_link_setup?co_id=1").json()["data"]
        assert data["wages_qualities"] == [
            {"value": "8", "label": "3502 - B TWILL", "dept_id": "347"},
            {"value": "3", "label": "037 - HESS 9X8 (0.127100)", "dept_id": "20"},
        ]

    def test_qualities_rejects_untyped_department(self):
        self._db.execute.return_value.fetchone.return_value = _mock_row(
            {"dept_id": 15, "dept_desc": "BATCHING(EJM)", "branch_id": 2, "co_id": 1})
        resp = client.get(f"{PREFIX}/prod_wages_link_qualities?dept_id=15")
        assert resp.status_code == 400

    def test_qualities_uses_weaving_master_for_weaving_dept(self):
        self._db.execute.return_value.fetchone.return_value = _mock_row(
            {"dept_id": 21, "dept_desc": "WEAVING(EJM)", "branch_id": 2, "co_id": 1})
        self._db.execute.return_value.fetchall.return_value = [
            _mock_row({"weaving_quality_id": 1, "weaving_quality_name": "B-Twill A BB", "weaving_quality_code": "X"}),
        ]
        data = client.get(f"{PREFIX}/prod_wages_link_qualities?dept_id=21").json()["data"]
        assert data["quality_type"] == "weaving"
        assert data["qualities"] == [{"value": "1", "label": "X - B-Twill A BB"}]

    def test_table_labels_prod_quality_by_department_type(self):
        self._db.execute.return_value.fetchall.return_value = [_mock_row({
            "link_prod_wages_id": 5, "prod_quality_id": 1, "wages_quality_id": 7, "branch_id": 2,
            "dept_id": 20, "active": 1, "dept_desc": "BEAMING(EJM)", "branch_name": "Mill",
            "wages_q_code": "6501", "wages_quality": "UPTO 22LBS",
            "weaving_quality_name": "WRONG", "bm_quality_code": "BM1", "bm_quality_name": "272-13/272",
            "finishing_item_name": None, "winding_item_name": None,
        })]
        data = client.get(f"{PREFIX}/get_prod_wages_link_table?co_id=1").json()
        assert data["total"] == 1
        assert data["data"][0]["prod_quality_name"] == "BM1 - 272-13/272"
        assert data["data"][0]["wages_quality_name"] == "6501 - UPTO 22LBS"

    def test_qualities_beaming_falls_back_to_item_name(self):
        self._db.execute.return_value.fetchone.return_value = _mock_row(
            {"dept_id": 20, "dept_desc": "BEAMING(EJM)", "branch_id": 29, "co_id": 1})
        self._db.execute.return_value.fetchall.return_value = [
            _mock_row({"bm_quality_id": 16, "bm_quality_code": "1698", "bm_quality_name": None,
                       "item_name": "HESSIAN WARP"}),
        ]
        data = client.get(f"{PREFIX}/prod_wages_link_qualities?dept_id=20").json()["data"]
        assert data["qualities"] == [{"value": "16", "label": "1698 - HESSIAN WARP"}]

    def test_table_labels_ejm_rate_when_rate_matches_dept(self):
        self._db.execute.return_value.fetchall.return_value = [_mock_row({
            "link_prod_wages_id": 5, "prod_quality_id": 1, "wages_quality_id": 3, "branch_id": 29,
            "dept_id": 20, "active": 1, "dept_desc": "BEAMING(EJM)", "branch_name": "EJM",
            "wages_q_code": "6501", "wages_quality": "WRONG",
            "ejm_quality_code": "037", "ejm_quality_desc": "HESS 9X8", "ejm_quality_rate": Decimal("0.127100"),
            "bm_quality_code": "BM1", "bm_quality_name": "Q",
        })]
        data = client.get(f"{PREFIX}/get_prod_wages_link_table?co_id=1").json()
        assert data["data"][0]["wages_quality_name"] == "037 - HESS 9X8 (0.127100)"

    def test_create_requires_all_three_ids(self):
        resp = client.post(f"{PREFIX}/prod_wages_link_create", json={"dept_id": 21})
        assert resp.status_code == 400
        assert "Production quality" in resp.json()["detail"]

    def test_create_rejects_rate_from_another_dept_on_ejm_branch(self):
        self._db.execute.return_value.fetchone.side_effect = [
            _mock_row({"dept_id": 20, "dept_desc": "BEAMING(EJM)", "branch_id": 29, "co_id": 1}),
            None,
        ]
        self._db.execute.return_value.fetchall.return_value = [_mock_row({"branch_id": 29})]
        resp = client.post(f"{PREFIX}/prod_wages_link_create",
                           json={"dept_id": 20, "prod_quality_id": 1, "wages_quality_id": 99})
        assert resp.status_code == 400
        assert "not rated" in resp.json()["detail"]

    def test_create_rejects_unknown_process(self):
        resp = client.post(f"{PREFIX}/prod_wages_link_create",
                           json={"dept_id": 21, "prod_quality_id": 1, "wages_quality_id": 7, "process": "Cutting"})
        assert resp.status_code == 400
        assert "Process" in resp.json()["detail"]

    def test_duplicate_check_is_per_process(self):
        self._db.execute.return_value.fetchone.side_effect = [
            _mock_row({"dept_id": 22, "dept_desc": "FINISHING(EJM)", "branch_id": 2, "co_id": 1}),
            _mock_row({"ok": 1}),
            _mock_row({"cnt": 1}),
        ]
        self._db.execute.return_value.fetchall.return_value = []
        resp = client.post(f"{PREFIX}/prod_wages_link_create",
                           json={"dept_id": 22, "prod_quality_id": 1, "wages_quality_id": 7, "process": "Hemming"})
        assert resp.status_code == 400
        dup_params = self._db.execute.call_args_list[-1][0][1]
        assert dup_params["process"] == "Hemming"

    def test_create_rejects_duplicate(self):
        self._db.execute.return_value.fetchone.side_effect = [
            _mock_row({"dept_id": 21, "dept_desc": "WEAVING(EJM)", "branch_id": 2, "co_id": 1}),
            _mock_row({"ok": 1}),
            _mock_row({"cnt": 1}),
        ]
        self._db.execute.return_value.fetchall.return_value = []
        resp = client.post(f"{PREFIX}/prod_wages_link_create",
                           json={"dept_id": 21, "prod_quality_id": 1, "wages_quality_id": 7})
        assert resp.status_code == 400
        assert "already linked" in resp.json()["detail"]

    def test_create_copies_branch_from_department(self):
        self._db.execute.return_value.fetchone.side_effect = [
            _mock_row({"dept_id": 21, "dept_desc": "WEAVING(EJM)", "branch_id": 2, "co_id": 1}),
            _mock_row({"ok": 1}),
            _mock_row({"cnt": 0}),
        ]
        self._db.execute.return_value.fetchall.return_value = []

        def _refresh(record):
            record.link_prod_wages_id = 9
        self._db.refresh.side_effect = _refresh

        resp = client.post(f"{PREFIX}/prod_wages_link_create",
                           json={"dept_id": 21, "prod_quality_id": 1, "wages_quality_id": 7})
        assert resp.status_code == 200
        added = self._db.add.call_args[0][0]
        assert added.branch_id == 2
        assert added.process is None  # blank process stored as NULL
        assert added.active == 1
        assert resp.json()["link_prod_wages_id"] == 9

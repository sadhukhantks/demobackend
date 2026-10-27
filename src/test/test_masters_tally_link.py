"""Tests for the Tally Link Master (src/masters/tallyLink.py)."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db
from src.main import app
from src.masters.tallyLink import clean_link, clean_tally_name
from src.models.mst import TallyLinkMst

client = TestClient(app)
BASE = "/api/tallyLinkMaster"


def _row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


class TestValidation:
    def test_godown_forces_ref_zero(self):
        assert clean_link("godown", 55) == ("GODOWN", 0)

    def test_ledger_types_are_company_level(self):
        for t in ("PURCHASE_LEDGER", "purchase_claim", "TDS_194Q", "SALES_LEDGER", "SALES_CLAIM", "ROUND_OFF"):
            assert clean_link(t, 7) == (t.upper(), 0)

    def test_party_needs_ref(self):
        with pytest.raises(HTTPException) as e:
            clean_link("PARTY", None)
        assert e.value.status_code == 400
        with pytest.raises(HTTPException):
            clean_link("ITEM", 0)

    def test_party_ok(self):
        assert clean_link(" party ", 7) == ("PARTY", 7)

    def test_destination_types_need_a_record(self):
        assert clean_link("destination", 29) == ("DESTINATION", 29)
        assert clean_link("DESTINATION_PARTY", 31) == ("DESTINATION_PARTY", 31)
        for t in ("DESTINATION", "DESTINATION_PARTY"):
            with pytest.raises(HTTPException):
                clean_link(t, 0)

    def test_bad_type(self):
        with pytest.raises(HTTPException):
            clean_link("LEDGER", 1)

    def test_tally_name_trimmed_and_bounded(self):
        assert clean_tally_name("  Jagrati Trade  ") == "Jagrati Trade"
        for bad in ("", "   ", None, "x" * 256):
            with pytest.raises(HTTPException):
                clean_tally_name(bad)


class TestEndpoints:
    @pytest.fixture(autouse=True)
    def overrides(self):
        self.db = MagicMock()
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 1}
        app.dependency_overrides[get_tenant_db] = lambda: self.db
        yield
        app.dependency_overrides.clear()

    def _existing(self, record):
        self.db.query.return_value.filter.return_value.first.return_value = record

    def test_list_requires_co_id(self):
        assert client.get(f"{BASE}/list").status_code == 400

    def test_list_binds_filters(self):
        self.db.execute.return_value.fetchall.return_value = [
            _row({"tally_link_id": 1, "link_type": "GODOWN", "erp_name": "Company default", "tally_name": "Main"})
        ]
        r = client.get(f"{BASE}/list?co_id=2&link_type=godown&search=Ma")
        assert r.status_code == 200, r.text
        assert r.json()["data"][0]["tally_name"] == "Main"
        params = self.db.execute.call_args[0][1]
        assert params == {"co_id": 2, "link_type": "GODOWN", "search": "%Ma%"}

    def test_list_no_filters_binds_none(self):
        self.db.execute.return_value.fetchall.return_value = []
        client.get(f"{BASE}/list?co_id=2")
        assert self.db.execute.call_args[0][1] == {"co_id": 2, "link_type": None, "search": None}

    def test_setup_returns_types_and_pickers(self):
        self.db.execute.return_value.fetchall.return_value = [_row({"ref_id": 9, "erp_name": "X"})]
        r = client.get(f"{BASE}/setup?co_id=2")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["link_types"][:3] == ["PARTY", "ITEM", "GODOWN"]
        assert "ROUND_OFF" in d["link_types"]
        assert d["ledger_defaults"]["TDS_194Q"] == "TDS ON PURCHASE OF GOODS (194Q)"
        assert d["parties"] == [{"ref_id": 9, "erp_name": "X"}]

    def test_setup_includes_branches_and_destination_types(self):
        self.db.execute.return_value.fetchall.return_value = [_row({"ref_id": 29, "erp_name": "FACTORY"})]
        d = client.get(f"{BASE}/setup?co_id=2").json()["data"]
        assert d["branches"] == [{"ref_id": 29, "erp_name": "FACTORY"}]
        assert d["link_types"][-2:] == ["DESTINATION", "DESTINATION_PARTY"]

    def test_create_destination_validates_own_branch(self):
        self.db.execute.return_value.fetchall.return_value = []
        r = client.post(f"{BASE}/create", json={"co_id": 2, "link_type": "DESTINATION", "ref_id": 87, "tally_name": "15/16 B.T. Road"})
        assert r.status_code == 400
        assert "branch does not belong" in r.json()["detail"]
        self.db.add.assert_not_called()
        assert "FROM branch_mst" in str(self.db.execute.call_args[0][0])

    def test_create_destination(self):
        self.db.execute.return_value.fetchall.return_value = [_row({"ref_id": 29, "erp_name": "FACTORY"})]
        self._existing(None)
        r = client.post(f"{BASE}/create", json={"co_id": 2, "link_type": "DESTINATION", "ref_id": 29, "tally_name": " 15/16 B.T. Road "})
        assert r.status_code == 200, r.text
        added = self.db.add.call_args[0][0]
        assert (added.link_type, added.ref_id, added.tally_name) == ("DESTINATION", 29, "15/16 B.T. Road")

    def test_create_destination_party_validates_party(self):
        self.db.execute.return_value.fetchall.return_value = []
        r = client.post(f"{BASE}/create", json={"co_id": 2, "link_type": "DESTINATION_PARTY", "ref_id": 31, "tally_name": "Golabari"})
        assert r.status_code == 400
        assert "party does not belong" in r.json()["detail"]
        assert "FROM party_mst" in str(self.db.execute.call_args[0][0])

    def test_create_godown(self):
        self._existing(None)
        r = client.post(f"{BASE}/create", json={"co_id": 2, "link_type": "GODOWN", "ref_id": 99, "tally_name": " Main Godown "})
        assert r.status_code == 200, r.text
        added = self.db.add.call_args[0][0]
        assert (added.co_id, added.link_type, added.ref_id, added.tally_name, added.active) == (2, "GODOWN", 0, "Main Godown", 1)
        self.db.execute.assert_not_called()  # no ERP lookup for GODOWN
        self.db.commit.assert_called_once()

    def test_create_ledger_name(self):
        self._existing(None)
        r = client.post(f"{BASE}/create", json={"co_id": 2, "link_type": "TDS_194Q", "tally_name": "TDS 194Q Payable"})
        assert r.status_code == 200, r.text
        added = self.db.add.call_args[0][0]
        assert (added.link_type, added.ref_id, added.tally_name) == ("TDS_194Q", 0, "TDS 194Q Payable")
        self.db.execute.assert_not_called()

    def test_create_party_of_other_company_rejected(self):
        self.db.execute.return_value.fetchall.return_value = []
        r = client.post(f"{BASE}/create", json={"co_id": 2, "link_type": "PARTY", "ref_id": 5, "tally_name": "A"})
        assert r.status_code == 400
        assert "does not belong" in r.json()["detail"]
        self.db.add.assert_not_called()

    def test_create_active_duplicate_rejected(self):
        self.db.execute.return_value.fetchall.return_value = [_row({"ref_id": 5, "erp_name": "A"})]
        self._existing(TallyLinkMst(tally_link_id=3, co_id=2, link_type="PARTY", ref_id=5, tally_name="Old", active=1))
        r = client.post(f"{BASE}/create", json={"co_id": 2, "link_type": "PARTY", "ref_id": 5, "tally_name": "New"})
        assert r.status_code == 400
        assert "already mapped" in r.json()["detail"]

    def test_create_revives_inactive_duplicate(self):
        self.db.execute.return_value.fetchall.return_value = [_row({"ref_id": 5, "erp_name": "A"})]
        old = TallyLinkMst(tally_link_id=3, co_id=2, link_type="ITEM", ref_id=5, tally_name="Old", active=0)
        self._existing(old)
        r = client.post(f"{BASE}/create", json={"co_id": 2, "link_type": "ITEM", "ref_id": 5, "tally_name": "New"})
        assert r.status_code == 200, r.text
        assert r.json() == {"data": {"tally_link_id": 3}}
        assert (old.tally_name, old.active) == ("New", 1)
        self.db.add.assert_not_called()

    def test_create_race_is_friendly_400(self):
        self._existing(None)
        self.db.commit.side_effect = IntegrityError("stmt", {}, Exception("dup"))
        r = client.post(f"{BASE}/create", json={"co_id": 2, "link_type": "GODOWN", "tally_name": "Main"})
        assert r.status_code == 400

    def test_update_soft_delete(self):
        rec = TallyLinkMst(tally_link_id=3, co_id=2, link_type="PARTY", ref_id=5, tally_name="Old", active=1)
        self.db.get.return_value = rec
        r = client.post(f"{BASE}/update", json={"co_id": 2, "tally_link_id": 3, "tally_name": "Old", "active": 0})
        assert r.status_code == 200, r.text
        assert rec.active == 0

    def test_update_other_company_404(self):
        self.db.get.return_value = TallyLinkMst(tally_link_id=3, co_id=9, link_type="PARTY", ref_id=5, tally_name="X", active=1)
        r = client.post(f"{BASE}/update", json={"co_id": 2, "tally_link_id": 3, "tally_name": "Y"})
        assert r.status_code == 404

    def test_update_bad_active(self):
        r = client.post(f"{BASE}/update", json={"co_id": 2, "tally_link_id": 3, "tally_name": "Y", "active": 2})
        assert r.status_code == 400

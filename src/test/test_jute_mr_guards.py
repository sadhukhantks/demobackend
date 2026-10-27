"""Guards added 2026-09-03 (jute transfer chain review, decisions D1-D4)."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.juteProcurement import mr as mr_module

client = TestClient(app)


@pytest.fixture
def mock_db():
    db = MagicMock()
    app.dependency_overrides[get_tenant_db] = lambda: db
    app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
    yield db
    app.dependency_overrides.clear()


def _row(**cols):
    m = MagicMock()
    m._mapping = cols
    for k, v in cols.items():
        setattr(m, k, v)
    return m


class TestRequirePartyComplete:
    def test_missing_party_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            mr_module.require_mr_party_complete(None, 5)
        assert exc.value.status_code == 400
        assert "Party" in exc.value.detail

    def test_missing_branch_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            mr_module.require_mr_party_complete("123", None)
        assert exc.value.status_code == 400
        assert "branch" in exc.value.detail.lower()

    def test_complete_passes(self):
        assert mr_module.require_mr_party_complete("123", 5) is None


class TestPendingMrPartyGuard:
    def test_pending_blocked_without_branch(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _row(
            jute_mr_id=1, status_id=1, party_id="123", party_branch_id=None,
            src_jute_mr_id=None,
        )
        r = client.post("/api/juteMR/pending_mr", json={"mr_id": "1", "branch_id": "29"})
        assert r.status_code == 400
        assert "branch" in r.json()["detail"].lower()
        mock_db.commit.assert_not_called()


class TestApproveMrPartyGuard:
    def test_approve_blocked_without_party(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _row(
            jute_mr_id=1, status_id=1, party_id=None, party_branch_id=None,
            branch_id=29, branch_mr_no=None, jute_mr_date=None,
        )
        r = client.post("/api/juteMR/approve_mr", json={"mr_id": "1", "branch_id": "29", "mr_date": "2026-09-03"})
        assert r.status_code == 400
        assert "Party" in r.json()["detail"]


class TestChangeStatusPartyGuard:
    def test_change_status_to_approved_needs_party(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _row(
            jute_mr_id=1, status_id=1, party_id="123", party_branch_id=None,
        )
        r = client.put("/api/juteMR/change_status/1?co_id=2", json={"new_status_id": 3})
        assert r.status_code == 400


class TestAssertMrEditable:
    def test_pending_is_frozen(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(is_hop=0, child_count=0)
        with pytest.raises(HTTPException) as exc:
            mr_module.assert_mr_editable(db, 1, status_id=13)
        assert exc.value.status_code == 409

    def test_approved_is_frozen(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(is_hop=0, child_count=0)
        with pytest.raises(HTTPException):
            mr_module.assert_mr_editable(db, 1, status_id=3)

    def test_open_root_with_children_is_frozen(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(is_hop=0, child_count=2)
        with pytest.raises(HTTPException) as exc:
            mr_module.assert_mr_editable(db, 1, status_id=1)
        assert "juteTransfer" in exc.value.detail

    def test_open_unchained_passes(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(is_hop=0, child_count=0)
        assert mr_module.assert_mr_editable(db, 1, status_id=1) is None

    def test_tolerates_tenant_without_chain_columns(self):
        db = MagicMock()
        db.execute.side_effect = Exception("Unknown column 'src_jute_mr_id'")
        assert mr_module.assert_mr_editable(db, 1, status_id=1) is None
        db.rollback.assert_called()


class TestRejectCancelGuards:
    def test_reject_blocked_when_issues_exist(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            _row(jute_mr_id=1, status_id=1),          # status check
            _row(cnt=2, wt=500.0),                    # live_issue_summary
        ]
        r = client.post("/api/juteMR/reject_mr", json={"mr_id": "1", "branch_id": "29", "reason": "x"})
        assert r.status_code == 409
        assert "issue" in r.json()["detail"].lower()

    def test_cancel_blocked_when_chain_children(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            _row(jute_mr_id=1, status_id=1),
            _row(cnt=0, wt=0.0),
            _row(is_hop=0, child_count=1),
        ]
        r = client.post("/api/juteMR/cancel_mr", json={"mr_id": "1", "branch_id": "29"})
        assert r.status_code == 409
        assert "chain" in r.json()["detail"].lower()


class TestRecomputeMrMoney:
    def test_writes_line_total_price_and_header(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = _row(total_amount=50000.0, claim_amount=500.0)
        out = mr_module.recompute_mr_money(db, 1)
        assert out["net_total"] == 49500.0
        sqls = " ".join(str(c.args[0]) for c in db.execute.call_args_list)
        assert "total_price" in sqls and "net_total" in sqls


class TestApproveKeepsBillPassNo:
    def test_existing_bill_pass_no_is_kept(self, mock_db):
        # Adjusted from the plan's literal mock list: recompute_mr_money re-runs
        # calculate_mr_amounts internally, and approve_mr also reads the doc-number
        # prefixes after commit — both need a mocked row (plan A5 step 4 note).
        mock_db.execute.return_value.fetchone.side_effect = [
            _row(jute_mr_id=1, status_id=1, party_id="123", party_branch_id=5, branch_id=29,
                 branch_mr_no=50, jute_mr_date=None, bill_pass_no=80),
            _row(total_amount=1000.0, claim_amount=0.0),   # calculate_mr_amounts (tds calc)
            _row(cumulative_value=0.0),                    # cumulative
            _row(total_amount=1000.0, claim_amount=0.0),   # calculate_mr_amounts (inside recompute_mr_money)
            _row(po_id=None),                              # PO settlement hook (settle_after_mr_change)
            _row(co_prefix="AB", branch_prefix="CD"),      # doc-number prefix lookup
        ]
        r = client.post("/api/juteMR/approve_mr", json={"mr_id": "1", "branch_id": "29", "mr_date": "2026-09-03"})
        assert r.status_code == 200
        sqls = " ".join(str(c.args[0]) for c in mock_db.execute.call_args_list)
        assert "MAX(bill_pass_no)" not in sqls


class TestReopenMr:
    def _mr(self, status_id=13, qc_check=1):
        return _row(jute_mr_id=1, status_id=status_id, branch_id=29, qc_check=qc_check)

    def test_requires_edit_access(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._mr()
        with patch.object(mr_module, "_user_has_edit_access", return_value=False):
            r = client.post("/api/juteMR/reopen_mr", json={"mr_id": "1", "branch_id": "29", "menu_id": 719, "target": "open"})
        assert r.status_code == 403

    def test_blocked_when_sold_onward(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [self._mr(), _row(is_hop=0, child_count=1)]
        with patch.object(mr_module, "_user_has_edit_access", return_value=True):
            r = client.post("/api/juteMR/reopen_mr", json={"mr_id": "1", "branch_id": "29", "menu_id": 719, "target": "open"})
        assert r.status_code == 409

    def test_reopen_for_qc_clears_qc_and_numbers_and_warns(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            self._mr(3), _row(is_hop=0, child_count=0), _row(cnt=3, wt=1200.0),
            _row(po_id=None),  # PO settlement hook (settle_after_mr_change)
        ]
        with patch.object(mr_module, "_user_has_edit_access", return_value=True):
            r = client.post("/api/juteMR/reopen_mr", json={"mr_id": "1", "branch_id": "29", "menu_id": 719, "target": "qc"})
        assert r.status_code == 200
        body = r.json()
        assert body["status_id"] == 1 and body["issues"]["count"] == 3 and body["warning"]
        sqls = " ".join(str(c.args[0]) for c in mock_db.execute.call_args_list)
        assert "qc_check = 0" in sqls and "bill_pass_no = NULL" in sqls and "branch_mr_no" not in sqls.split("SET",1)[1]

    # TKT-000229 (2026-10-16): an Open MR whose QC is done can be rolled back to QC.
    def test_open_mr_with_qc_done_rolls_back_to_qc_only(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            self._mr(1, qc_check=1), _row(is_hop=0, child_count=0), _row(cnt=0, wt=0.0),
        ]
        with patch.object(mr_module, "_user_has_edit_access", return_value=True), \
             patch.object(mr_module, "settle_after_mr_change") as settle:
            r = client.post("/api/juteMR/reopen_mr", json={"mr_id": "1", "branch_id": "29", "menu_id": 719, "target": "qc"})
        assert r.status_code == 200
        body = r.json()
        assert body["status_id"] == 1 and body["from_status_id"] == 1 and body["warning"] is None
        update = [str(c.args[0]) for c in mock_db.execute.call_args_list if "UPDATE jute_mr" in str(c.args[0])][0]
        assert "qc_check = 0" in update
        assert "status_id" not in update.split("SET", 1)[1] and "bill_pass_no" not in update and "total_amount" not in update
        settle.assert_not_called()  # still a receipt: settlement untouched

    def test_open_mr_cannot_be_reopened_for_edit(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._mr(1, qc_check=1)
        with patch.object(mr_module, "_user_has_edit_access", return_value=True):
            r = client.post("/api/juteMR/reopen_mr", json={"mr_id": "1", "branch_id": "29", "menu_id": 719, "target": "open"})
        assert r.status_code == 400 and "already Open" in r.json()["detail"]

    def test_open_mr_without_qc_has_nothing_to_roll_back(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = self._mr(1, qc_check=0)
        with patch.object(mr_module, "_user_has_edit_access", return_value=True):
            r = client.post("/api/juteMR/reopen_mr", json={"mr_id": "1", "branch_id": "29", "menu_id": 719, "target": "qc"})
        assert r.status_code == 400 and "not been completed" in r.json()["detail"]


class TestReturnMr:
    def test_return_from_open(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            _row(jute_mr_id=1, status_id=1, is_returned=0),
            _row(po_id=None),  # PO settlement hook (settle_after_mr_change)
        ]
        r = client.post("/api/juteMR/return_mr", json={"mr_id": "1", "branch_id": "29", "reason": "no settlement"})
        # Returned is a flag now: the status is unchanged, so the MR stays approvable/transferable.
        assert r.status_code == 200 and r.json()["status_id"] == 1 and r.json()["is_returned"] is True
        sqls = " ".join(str(c.args[0]) for c in mock_db.execute.call_args_list)
        assert "is_returned = :flag" in sqls and "status_id =" not in sqls.split("SET", 1)[1].split("WHERE", 1)[0]

    def test_return_refused_once_approved(self, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _row(jute_mr_id=1, status_id=3, is_returned=0)
        r = client.post("/api/juteMR/return_mr", json={"mr_id": "1", "branch_id": "29", "reason": "x"})
        assert r.status_code == 400

    def test_unreturn_clears_flag(self, mock_db):
        mock_db.execute.return_value.fetchone.side_effect = [
            _row(jute_mr_id=1, status_id=13, is_returned=1), _row(po_id=None)]
        r = client.post("/api/juteMR/unreturn_mr", json={"mr_id": "1", "branch_id": "29", "reason": ""})
        assert r.status_code == 200 and r.json()["is_returned"] is False

    def test_retained_material_is_stock_and_issuable(self):
        """Owner ruling 2026-10-03: Returned is a flag; the retained part is stock."""
        from src.juteProcurement.constants import (
            JUTE_MR_ISSUABLE_STATUSES, JUTE_MR_STOCK_EXCLUDED_STATUSES)
        assert 48 not in JUTE_MR_STOCK_EXCLUDED_STATUSES
        assert 48 in JUTE_MR_ISSUABLE_STATUSES  # legacy rows until the flag migration is run

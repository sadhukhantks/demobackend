# src/test/test_procurement_sr_gst_context_guard.py
"""Regression: save_sr must refuse a taxable SR whose place-of-supply cannot be
resolved, instead of skipping the proc_gst write silently.

sls br-87 bug (2026-08-11): `proc_inward.supplier_branch_id` NULL + supplier with
2-3 active party branches => no supplier state => the whole GST block was skipped
and the SR persisted with zero tax. Manual DR/CR notes built on such an SR then
inherited zero tax (notes 332/333 on sls).
"""

from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh

client = TestClient(app)


def _override_auth():
    return {"user_id": 1}


def _make_mock_db_override(mock_session):
    def _override():
        yield mock_session
    return _override


def _mock_row(mapping):
    row = MagicMock()
    row._mapping = mapping
    return row


class TestSaveSrGstContextGuard:
    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = _override_auth
        self.mock_session = MagicMock()
        app.dependency_overrides[get_tenant_db] = _make_mock_db_override(self.mock_session)

    def teardown_method(self):
        app.dependency_overrides.pop(get_current_user_with_refresh, None)
        app.dependency_overrides.pop(get_tenant_db, None)

    def _script(self, supplier_branch_id, supplier_state):
        """execute() results up to the GST guard (one line item, no addl charges)."""
        header_row = MagicMock()
        header_row.branch_id = 87
        header_row.sr_no = "LCPL/SR/26-27/000265"
        header_row.sr_status = 21
        supplier_state_calls = (
            [MagicMock(fetchone=MagicMock(return_value=(supplier_state,)))]
            if supplier_branch_id else []
        )
        return [
            MagicMock(),                                                # update inward dtl
            MagicMock(),                                                # delete addl-charge gst
            MagicMock(),                                                # delete addl charges
            MagicMock(fetchone=MagicMock(return_value=header_row)),     # branch/sr_no/sr_status
            MagicMock(),                                                # update inward header
            MagicMock(fetchone=MagicMock(return_value=(106,))),         # co_id from branch
            MagicMock(fetchone=MagicMock(return_value=_mock_row({"india_gst": 1}))),
            MagicMock(fetchone=MagicMock(return_value=_mock_row({      # inward_info
                "supplier_branch_id": supplier_branch_id,
                "bill_branch_id": 87, "ship_branch_id": 87,
            }))),
            *supplier_state_calls,                                      # supplier state
            MagicMock(fetchone=MagicMock(return_value=(29,))),          # shipping state
        ]

    def _post(self, tax_percentage):
        return client.post("/api/storesReceipt/save_sr", json={
            "inward_id": 18616,
            "sr_date": "2026-08-07",
            "line_items": [{
                "inward_dtl_id": 42236,
                "accepted_rate": 800.0,
                "amount": 2400.0,
                "tax_percentage": tax_percentage,
            }],
        })

    def test_rejects_taxable_sr_when_supplier_branch_missing(self):
        self.mock_session.execute.side_effect = self._script(
            supplier_branch_id=None, supplier_state=None,
        )
        resp = self._post(18.0)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "Supplier State (GST)" in detail          # names the exact SR field
        assert "NOT saved" in detail
        self.mock_session.commit.assert_not_called()
        self.mock_session.rollback.assert_called_once()  # line updates discarded

    def test_rejects_taxable_sr_when_supplier_branch_has_no_state(self):
        self.mock_session.execute.side_effect = self._script(
            supplier_branch_id=38482, supplier_state=None,
        )
        resp = self._post(18.0)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "no State configured" in detail
        assert "Party Master" in detail
        self.mock_session.commit.assert_not_called()

    def test_allows_zero_tax_sr_without_supplier_state(self):
        """Genuinely tax-free lines must still save — the guard is tax-scoped."""
        self.mock_session.execute.side_effect = self._script(
            supplier_branch_id=None, supplier_state=None,
        )
        resp = self._post(0.0)
        assert resp.status_code == 200, resp.json()
        self.mock_session.commit.assert_called_once()

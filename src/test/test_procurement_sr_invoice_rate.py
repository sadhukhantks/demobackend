# src/test/test_procurement_sr_invoice_rate.py
"""Tests that save_sr persists per-line invoice_rate (spec 2026-08-08-drcr-note-rework)."""

import pytest
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


class TestSaveSRInvoiceRate:
    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = _override_auth
        self.mock_session = MagicMock()
        app.dependency_overrides[get_tenant_db] = _make_mock_db_override(self.mock_session)

    def teardown_method(self):
        app.dependency_overrides.pop(get_current_user_with_refresh, None)
        app.dependency_overrides.pop(get_tenant_db, None)

    def _line_update_params(self):
        """Return the params dict of the proc_inward_dtl UPDATE issued by save_sr."""
        for c in self.mock_session.execute.call_args_list:
            if len(c[0]) > 1 and isinstance(c[0][1], dict) and "accepted_rate" in c[0][1]:
                return c[0][1]
        return None

    def test_save_sr_persists_invoice_rate(self):
        resp = client.post("/api/storesReceipt/save_sr", json={
            "inward_id": 7,
            "sr_date": "2026-08-08",
            "line_items": [{
                "inward_dtl_id": 16,
                "accepted_rate": 90.0,
                "invoice_rate": 100.0,
                "amount": 900.0,
            }],
        })
        assert resp.status_code == 200
        params = self._line_update_params()
        assert params is not None
        assert params["invoice_rate"] == 100.0

    def test_save_sr_invoice_rate_optional_defaults_none(self):
        resp = client.post("/api/storesReceipt/save_sr", json={
            "inward_id": 7,
            "sr_date": "2026-08-08",
            "line_items": [{
                "inward_dtl_id": 16,
                "accepted_rate": 90.0,
                "amount": 900.0,
            }],
        })
        assert resp.status_code == 200
        params = self._line_update_params()
        assert params is not None
        assert params["invoice_rate"] is None

    def test_save_sr_rejects_negative_invoice_rate(self):
        """A negative rate would mint a negative-value debit note - reject at the boundary."""
        resp = client.post("/api/storesReceipt/save_sr", json={
            "inward_id": 7,
            "sr_date": "2026-08-08",
            "line_items": [{
                "inward_dtl_id": 16,
                "accepted_rate": 90.0,
                "invoice_rate": -1.0,
                "amount": 900.0,
            }],
        })
        assert resp.status_code == 422


def _mock_row(mapping):
    row = MagicMock()
    row._mapping = mapping
    return row


class TestGetSRInvoiceRatePrefill:
    """GET /storesReceipt/get_sr_by_inward_id/{id} invoice_rate prefill gate
    (src/procurement/sr.py get_sr_by_inward_id, ~line 335): stored value wins,
    else PO rate for PO-linked lines, else None for PO-less lines.
    """

    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = _override_auth
        self.mock_session = MagicMock()
        app.dependency_overrides[get_tenant_db] = _make_mock_db_override(self.mock_session)

    def teardown_method(self):
        app.dependency_overrides.pop(get_current_user_with_refresh, None)
        app.dependency_overrides.pop(get_tenant_db, None)

    def _build_header_row(self):
        """Mirrors test_procurement_sr_warehouse.py's header row fixture."""
        return _mock_row({
            "inward_id": 10,
            "inward_sequence_no": 1,
            "inward_date": None,
            "branch_id": 1,
            "branch_name": "Main",
            "supplier_id": None,
            "supplier_name": "Supplier A",
            "supplier_state_id": 1,
            "supplier_state_name": "Maharashtra",
            "bill_branch_id": 1,
            "billing_branch_name": "Main",
            "billing_state_id": 1,
            "billing_state_name": "Maharashtra",
            "ship_branch_id": 1,
            "shipping_branch_name": "Main",
            "shipping_state_id": 1,
            "shipping_state_name": "Maharashtra",
            "india_gst": 0,
            "inspection_check": True,
            "co_prefix": "CO",
            "branch_prefix": "BR",
            "sr_no": None,
            "sr_date": None,
            "sr_status": 21,
            "sr_status_name": "Draft",
            "inspection_date": None,
            "challan_no": None,
            "challan_date": None,
            "invoice_date": None,
            "invoice_amount": 0,
            "invoice_recvd_date": None,
            "vehicle_number": None,
            "driver_name": None,
            "driver_contact_no": None,
            "consignment_no": None,
            "consignment_date": None,
            "ewaybillno": None,
            "ewaybill_date": None,
            "despatch_remarks": None,
            "receipts_remarks": None,
            "sr_remarks": None,
            "gross_amount": 0,
            "net_amount": 0,
        })

    def _dtl_row(self, po_dtl_id, po_id, po_rate, invoice_rate):
        return _mock_row({
            "inward_dtl_id": 100,
            "inward_id": 10,
            "po_dtl_id": po_dtl_id,
            "po_id": po_id,
            "item_id": 1,
            "item_code": "ITM001",
            "item_name": "Test Item",
            "item_grp_id": 1,
            "item_grp_code": "GRP1",
            "item_grp_name": "Group 1",
            "item_make_id": None,
            "item_make_name": None,
            "accepted_item_make_id": None,
            "accepted_item_make_name": None,
            "uom_id": 1,
            "uom_name": "KG",
            "approved_qty": 10,
            "rejected_qty": 0,
            "rate": 100,
            "accepted_rate": 100,
            "invoice_rate": invoice_rate,
            "amount": 1000,
            "discount_mode": None,
            "discount_value": None,
            "discount_amount": None,
            "remarks": None,
            "warehouse_id": None,
            "warehouse_name": None,
            "warehouse_path": None,
            "po_rate": po_rate,
            "tax_percentage": 18,
        })

    def _get_line_items(self, dtl_row):
        """Drive GET /get_sr_by_inward_id/10 with a single dtl_row, mirroring
        test_procurement_sr_warehouse.py's call-sequence mock (header, dtl,
        warehouse, addl charges master, existing addl charges, then generic
        empty results for whatever else get_sr_by_inward_id queries)."""
        header_row = self._build_header_row()
        call_count = {"n": 0}

        def side_effect(query, params=None):
            call_count["n"] += 1
            result = MagicMock()
            n = call_count["n"]
            if n == 1:
                result.fetchone.return_value = header_row
                result.fetchall.return_value = [header_row]
            elif n == 2:
                result.fetchone.return_value = dtl_row
                result.fetchall.return_value = [dtl_row]
            else:
                result.fetchone.return_value = None
                result.fetchall.return_value = []
                result.scalar.return_value = 0
            return result

        self.mock_session.execute.side_effect = side_effect

        response = client.get("/api/storesReceipt/get_sr_by_inward_id/10?co_id=1")
        assert response.status_code == 200, response.text
        return response.json()["line_items"]

    def test_po_linked_line_stored_null_prefills_po_rate(self):
        """PO-linked line (po_dtl_id set), stored invoice_rate NULL → prefilled from po_rate."""
        dtl_row = self._dtl_row(po_dtl_id=50, po_id=5, po_rate=100, invoice_rate=None)
        line_items = self._get_line_items(dtl_row)
        assert line_items[0]["invoice_rate"] == 100

    def test_po_less_line_stored_null_stays_none(self):
        """PO-less line (po_dtl_id None), stored invoice_rate NULL → stays None, no fallback."""
        dtl_row = self._dtl_row(po_dtl_id=None, po_id=None, po_rate=None, invoice_rate=None)
        line_items = self._get_line_items(dtl_row)
        assert line_items[0]["invoice_rate"] is None

    def test_stored_invoice_rate_wins_over_po_rate(self):
        """Stored invoice_rate (88) beats the po_rate (100) prefill."""
        dtl_row = self._dtl_row(po_dtl_id=50, po_id=5, po_rate=100, invoice_rate=88)
        line_items = self._get_line_items(dtl_row)
        assert line_items[0]["invoice_rate"] == 88

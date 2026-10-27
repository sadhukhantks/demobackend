# src/test/test_procurement_sr_last_warehouse.py
"""
Regression tests for TKT-000194: "last warehouse details of the materials are
not displayed during SR creation" — the SR line-items table showed a blank
warehouse dropdown with no memory of where an item was stored last time.

Validates that:
1. get_last_warehouse_by_item_for_branch_query() is a well-formed windowed
   query (recency-ranked per item, scoped to branch, self-exclusion).
2. GET /storesReceipt/get_sr_by_inward_id/{id} enriches each line with
   last_warehouse_id / last_warehouse_name / last_warehouse_path.
"""

from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.procurement.query import get_last_warehouse_by_item_for_branch_query

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


class TestLastWarehouseQuery:
    """Tests for the get_last_warehouse_by_item_for_branch_query SQL function."""

    def test_returns_text_object(self):
        result = get_last_warehouse_by_item_for_branch_query()
        assert isinstance(result, type(text("")))

    def test_query_windows_by_item_partitioned_recency(self):
        sql_str = str(get_last_warehouse_by_item_for_branch_query())
        assert "PARTITION BY pid.item_id" in sql_str
        assert "ROW_NUMBER()" in sql_str
        assert "rn = 1" in sql_str

    def test_query_scopes_to_branch_and_excludes_current_inward(self):
        sql_str = str(get_last_warehouse_by_item_for_branch_query())
        assert ":branch_id" in sql_str
        assert ":exclude_inward_id" in sql_str

    def test_query_only_considers_rows_with_a_warehouse(self):
        sql_str = str(get_last_warehouse_by_item_for_branch_query())
        assert "pid.warehouse_id IS NOT NULL" in sql_str


class TestSREndpointLastWarehouse:
    """Tests for last-warehouse enrichment in GET /storesReceipt/get_sr_by_inward_id/{id}."""

    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = _override_auth
        self.mock_session = MagicMock()
        app.dependency_overrides[get_tenant_db] = _make_mock_db_override(self.mock_session)

    def teardown_method(self):
        app.dependency_overrides.pop(get_current_user_with_refresh, None)
        app.dependency_overrides.pop(get_tenant_db, None)

    def _build_header_row(self):
        return _mock_row({
            "inward_id": 10,
            "inward_sequence_no": 1,
            "inward_date": None,
            "branch_id": 1,
            "branch_name": "Main",
            "supplier_id": 5,
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
            "india_gst": 1,
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

    def _dtl_row(self, item_id=1, warehouse_id=None):
        return _mock_row({
            "inward_dtl_id": 100,
            "inward_id": 10,
            "po_dtl_id": 50,
            "po_id": 5,
            "item_id": item_id,
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
            "invoice_rate": None,
            "amount": 1000,
            "discount_mode": None,
            "discount_value": None,
            "discount_amount": None,
            "remarks": None,
            "warehouse_id": warehouse_id,
            "warehouse_name": None,
            "warehouse_path": None,
            "po_rate": 100,
            "tax_percentage": 18,
        })

    def _run(self, dtl_row, last_wh_rows):
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
            elif n == 3:
                # last-warehouse-by-item lookup
                result.fetchone.return_value = last_wh_rows[0] if last_wh_rows else None
                result.fetchall.return_value = last_wh_rows
            else:
                result.fetchone.return_value = None
                result.fetchall.return_value = []
                result.scalar.return_value = 0
            return result

        self.mock_session.execute.side_effect = side_effect

        response = client.get("/api/storesReceipt/get_sr_by_inward_id/10?co_id=1")
        assert response.status_code == 200, response.text
        return response.json()["line_items"]

    def test_line_gets_last_warehouse_when_item_has_history(self):
        dtl_row = self._dtl_row(item_id=1, warehouse_id=None)
        last_wh_row = _mock_row({
            "item_id": 1,
            "warehouse_id": 7,
            "warehouse_name": "Shelf 2",
            "warehouse_path": "Main Store-Shelf 2",
        })
        line_items = self._run(dtl_row, [last_wh_row])
        assert line_items[0]["last_warehouse_id"] == 7
        assert line_items[0]["last_warehouse_name"] == "Shelf 2"
        assert line_items[0]["last_warehouse_path"] == "Main Store-Shelf 2"

    def test_line_has_no_last_warehouse_when_item_never_received_before(self):
        dtl_row = self._dtl_row(item_id=2, warehouse_id=None)
        # Lookup returns a match for a *different* item only.
        last_wh_row = _mock_row({
            "item_id": 1,
            "warehouse_id": 7,
            "warehouse_name": "Shelf 2",
            "warehouse_path": "Main Store-Shelf 2",
        })
        line_items = self._run(dtl_row, [last_wh_row])
        assert line_items[0]["last_warehouse_id"] is None
        assert line_items[0]["last_warehouse_name"] is None
        assert line_items[0]["last_warehouse_path"] is None

    def test_no_history_at_all_leaves_fields_none(self):
        dtl_row = self._dtl_row(item_id=1, warehouse_id=None)
        line_items = self._run(dtl_row, [])
        assert line_items[0]["last_warehouse_id"] is None

"""
Tests for PUT /api/salesOrder/update_sales_order_rates.

Govt Sacking rates are notified by the government AFTER the sales order is
approved, so approved Govt Sacking orders get a narrow, rate-only edit path.
Every other invoice type stays frozen post-approval, and the edit is an
UPDATE-in-place: sales_order_dtl ids must survive because delivery orders and
invoices reference them.

Covers:
  1. Success on an approved Govt Sacking order — only rate/amount columns
     move, the detail id is preserved, no delete/reinsert.
  2. GST row updated when it exists, inserted when the line has none.
  3. 400 when the order is not Govt Sacking.
  4. 400 when the order is not approved.
  5. 404 when the order is missing / inactive.
  6. 400 when an items[] line references a foreign sales_order_dtl_id.
  7. Response is wrapped in {"data": {...}}.
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.sales.constants import INVOICE_TYPE_IDS, SALES_STATUS_IDS


client = TestClient(app)

SALES_ORDER_ID = 77
DTL_ID_WITH_GST = 501
DTL_ID_WITHOUT_GST = 502


def _row(mapping: dict):
    r = MagicMock()
    r._mapping = mapping
    return r


def _make_session(invoice_type=INVOICE_TYPE_IDS["GOVT_SKG"],
                  status_id=SALES_STATUS_IDS["APPROVED"],
                  order_found=True):
    """MagicMock Session recording every executed statement + params."""
    session = MagicMock()
    executed: list[dict] = []

    def fake_execute(query, params=None):
        sql = " ".join(str(query).lower().split())
        executed.append({"sql": sql, "params": params or {}})
        result = MagicMock()
        result.lastrowid = 1

        if "from sales_order where" in sql:
            result.fetchone.return_value = (
                _row({
                    "sales_order_id": SALES_ORDER_ID,
                    "invoice_type": invoice_type,
                    "status_id": status_id,
                })
                if order_found else None
            )
            return result

        if "from sales_order_dtl_gst" in sql and sql.startswith("select"):
            result.fetchall.return_value = [_row({"sales_order_dtl_id": DTL_ID_WITH_GST})]
            return result

        if "from sales_order_dtl where" in sql and sql.startswith("select"):
            result.fetchall.return_value = [
                _row({"sales_order_dtl_id": DTL_ID_WITH_GST, "item_id": 900}),
                _row({"sales_order_dtl_id": DTL_ID_WITHOUT_GST, "item_id": 901}),
            ]
            return result

        if "from item_mst" in sql:
            result.fetchall.return_value = [
                _row({"item_id": 900, "rr": 2}),
                _row({"item_id": 901, "rr": 2}),
            ]
            return result

        result.fetchone.return_value = None
        result.fetchall.return_value = []
        return result

    session.execute.side_effect = fake_execute
    session._executed = executed
    return session


def _payload(dtl_id=DTL_ID_WITH_GST, **overrides):
    body = {
        "sales_order_id": SALES_ORDER_ID,
        "gross_amount": 10999.99,
        "net_amount": 10999.99,
        "items": [
            {
                "sales_order_dtl_id": dtl_id,
                "rate": 1234.567,
                "discounted_rate": None,
                "discount_amount": None,
                "net_amount": 9999.99,
                "total_amount": 10999.99,
                "gst": {
                    "igst_amount": 0,
                    "cgst_amount": 500,
                    "sgst_amount": 500,
                    "igst_percent": 0,
                    "cgst_percent": 5,
                    "sgst_percent": 5,
                    "gst_total": 1000,
                },
            }
        ],
    }
    body.update(overrides)
    return body


def _calls(session, needle):
    return [e for e in session._executed if needle in e["sql"]]


class _Base:
    invoice_type = INVOICE_TYPE_IDS["GOVT_SKG"]
    status_id = SALES_STATUS_IDS["APPROVED"]
    order_found = True

    @pytest.fixture(autouse=True)
    def setup_overrides(self):
        self._session = _make_session(self.invoice_type, self.status_id, self.order_found)
        app.dependency_overrides[get_current_user_with_refresh] = lambda: {"user_id": 7}
        app.dependency_overrides[get_tenant_db] = lambda: self._session
        yield
        app.dependency_overrides.clear()

    def _put(self, payload):
        return client.put("/api/salesOrder/update_sales_order_rates", json=payload)


class TestApprovedGovtSkgRateUpdate(_Base):
    def test_updates_only_rate_and_amount_columns_in_place(self):
        response = self._put(_payload())

        assert response.status_code == 200, response.text
        assert response.json() == {
            "data": {"sales_order_id": SALES_ORDER_ID, "updated_lines": 1}
        }

        dtl_updates = _calls(self._session, "update sales_order_dtl set")
        assert len(dtl_updates) == 1, "expected exactly one in-place line UPDATE"
        sql, params = dtl_updates[0]["sql"], dtl_updates[0]["params"]

        # Detail id survives — downstream DO / invoice rows reference it.
        assert params["sales_order_dtl_id"] == DTL_ID_WITH_GST
        assert params["sales_order_id"] == SALES_ORDER_ID

        # Rate rounded to item_mst.rate_rounding (2), amounts to 2 decimals.
        assert params["rate"] == 1234.57
        assert params["net_amount"] == 9999.99
        assert params["total_amount"] == 10999.99
        assert params["discounted_rate"] is None
        assert params["discount_amount"] is None

        # Nothing outside the rate/amount whitelist is touched.
        for frozen in ("quantity", "item_id", "uom_id", "status_id", "approval_level", "active"):
            assert frozen not in sql, f"{frozen} must not be written by the rate-only update"

        # No delete/reinsert anywhere.
        assert not _calls(self._session, "insert into sales_order_dtl (")
        assert not _calls(self._session, "delete from sales_order_dtl")
        assert not any("active = 0" in e["sql"] for e in self._session._executed)

        self._session.commit.assert_called_once()

    def test_updates_existing_gst_row(self):
        self._put(_payload(dtl_id=DTL_ID_WITH_GST))

        gst_updates = _calls(self._session, "update sales_order_dtl_gst set")
        assert len(gst_updates) == 1
        params = gst_updates[0]["params"]
        assert params["sales_order_dtl_id"] == DTL_ID_WITH_GST
        assert params["cgst_amount"] == 500
        assert params["sgst_percent"] == 5
        assert params["gst_total"] == 1000
        assert not _calls(self._session, "insert into sales_order_dtl_gst")

    def test_inserts_gst_row_when_line_has_none(self):
        self._put(_payload(dtl_id=DTL_ID_WITHOUT_GST))

        assert len(_calls(self._session, "insert into sales_order_dtl_gst")) == 1
        assert not _calls(self._session, "update sales_order_dtl_gst set")

    def test_updates_header_totals_only(self):
        self._put(_payload())

        header_updates = _calls(self._session, "update sales_order set")
        assert len(header_updates) == 1
        sql, params = header_updates[0]["sql"], header_updates[0]["params"]
        assert params["gross_amount"] == 10999.99
        assert params["net_amount"] == 10999.99
        for frozen in ("status_id", "party_id", "branch_id", "invoice_type", "approval_level"):
            assert frozen not in sql, f"{frozen} must not be written by the rate-only update"

    def test_rejects_foreign_detail_id(self):
        response = self._put(_payload(dtl_id=999999))

        assert response.status_code == 400, response.text
        assert "999999" in response.json()["detail"]
        assert not _calls(self._session, "update sales_order_dtl set")
        self._session.commit.assert_not_called()

    def test_requires_at_least_one_item(self):
        response = self._put(_payload(items=[]))
        assert response.status_code == 400, response.text


class TestNonGovtSkgRejected(_Base):
    invoice_type = INVOICE_TYPE_IDS["REGULAR"]

    def test_rejects_non_govt_sacking_order(self):
        response = self._put(_payload())

        assert response.status_code == 400, response.text
        assert "Govt Sacking" in response.json()["detail"]
        assert not _calls(self._session, "update sales_order_dtl set")
        self._session.commit.assert_not_called()


class TestNonApprovedRejected(_Base):
    status_id = SALES_STATUS_IDS["OPEN"]

    def test_rejects_unapproved_order(self):
        response = self._put(_payload())

        assert response.status_code == 400, response.text
        assert "approved" in response.json()["detail"].lower()
        assert not _calls(self._session, "update sales_order_dtl set")
        self._session.commit.assert_not_called()


class TestMissingOrder(_Base):
    order_found = False

    def test_missing_or_inactive_order_404s(self):
        response = self._put(_payload())

        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "Sales order not found or inactive"
        self._session.commit.assert_not_called()

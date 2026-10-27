# src/test/test_procurement_sr_approve.py
"""
Tests for POST /storesReceipt/approve_sr endpoint.

Validates that debit_credit_note_id is correctly passed when auto-creating
DRCR notes during SR approval.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, call, patch
from datetime import datetime, date
from sqlalchemy import text

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


class TestApproveSREndpoint:
    """Tests for the approve_sr endpoint."""

    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = _override_auth
        self.mock_session = MagicMock()
        app.dependency_overrides[get_tenant_db] = _make_mock_db_override(self.mock_session)

    def teardown_method(self):
        app.dependency_overrides.pop(get_current_user_with_refresh, None)
        app.dependency_overrides.pop(get_tenant_db, None)

    def test_approve_sr_with_rejected_qty_passes_debit_credit_note_id(self):
        """
        When SR has rejected quantities, the auto-created debit note detail
        must include debit_credit_note_id linking it to the header.
        """
        # Setup: line item with rejected qty
        line_row = _mock_row({
            "inward_dtl_id": 16,
            "po_rate": 200.0,
            "rate": 200.0,
            "invoice_rate": 200.0,
            "accepted_rate": 200.0,
            "rejected_qty": 2.0,
            "approved_qty": 8.0,
        })

        # Mock LAST_INSERT_ID result for the note header
        note_id_row = MagicMock()
        note_id_row.id = 42

        # Header row used for SR-no minting check (sr_no already set so no mint)
        header_row = MagicMock()
        header_row.branch_id = 1
        header_row.sr_no = "SR-EXISTING"
        header_row.sr_date = date(2025, 1, 1)

        # Configure mock: first call returns line items, second is GST-prep inward_info
        # lookup (None to disable india_gst), third inserts header, etc.
        self.mock_session.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[line_row])),  # get line items
            MagicMock(fetchone=MagicMock(return_value=None)),  # GST prep: inward_info lookup (co_id=None → india_gst False)
            MagicMock(),  # insert note header
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),  # LAST_INSERT_ID for note header
            MagicMock(),  # insert detail line
            MagicMock(fetchone=MagicMock(return_value=header_row)),  # header SELECT for sr_no minting
            MagicMock(),  # update SR status
        ]

        response = client.post(
            "/api/storesReceipt/approve_sr",
            json={"inward_id": 7},
        )

        assert response.status_code == 200

        # Verify the detail insert call includes debit_credit_note_id
        detail_insert_call = self.mock_session.execute.call_args_list[4]
        detail_params = detail_insert_call[0][1]
        assert "debit_credit_note_id" in detail_params
        assert detail_params["debit_credit_note_id"] == 42

    def test_approve_sr_with_rate_increase_creates_credit_note_with_note_id(self):
        """
        When SR has a rate increase (accepted_rate > po_rate), a credit note
        detail is created with debit_credit_note_id.
        """
        line_row = _mock_row({
            "inward_dtl_id": 20,
            "po_rate": 100.0,
            "rate": 100.0,
            "invoice_rate": 100.0,
            "accepted_rate": 120.0,
            "rejected_qty": 0,
            "approved_qty": 10.0,
        })

        note_id_row = MagicMock()
        note_id_row.id = 55

        header_row = MagicMock()
        header_row.branch_id = 1
        header_row.sr_no = "SR-EXISTING"
        header_row.sr_date = date(2025, 1, 1)

        self.mock_session.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[line_row])),  # get line items
            MagicMock(fetchone=MagicMock(return_value=None)),  # GST prep: inward_info lookup (india_gst False)
            MagicMock(),  # insert credit note header
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),  # LAST_INSERT_ID
            MagicMock(),  # insert detail line
            MagicMock(fetchone=MagicMock(return_value=header_row)),  # header SELECT for sr_no minting
            MagicMock(),  # update SR status
        ]

        response = client.post(
            "/api/storesReceipt/approve_sr",
            json={"inward_id": 7},
        )

        assert response.status_code == 200

        # Verify credit note detail includes debit_credit_note_id
        detail_insert_call = self.mock_session.execute.call_args_list[4]
        detail_params = detail_insert_call[0][1]
        assert "debit_credit_note_id" in detail_params
        assert detail_params["debit_credit_note_id"] == 55

    def test_approve_sr_no_drcr_when_rates_match_and_no_rejection(self):
        """
        When there's no rate difference and no rejected qty,
        no DRCR note should be created.
        """
        line_row = _mock_row({
            "inward_dtl_id": 30,
            "po_rate": 100.0,
            "rate": 100.0,
            "invoice_rate": 100.0,
            "accepted_rate": 100.0,
            "rejected_qty": 0,
            "approved_qty": 10.0,
        })

        header_row = MagicMock()
        header_row.branch_id = 1
        header_row.sr_no = "SR-EXISTING"
        header_row.sr_date = date(2025, 1, 1)

        self.mock_session.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[line_row])),  # get line items
            MagicMock(fetchone=MagicMock(return_value=None)),  # GST prep: inward_info lookup
            MagicMock(fetchone=MagicMock(return_value=header_row)),  # header SELECT for sr_no minting
            MagicMock(),  # update SR status (no DRCR inserts)
        ]

        response = client.post(
            "/api/storesReceipt/approve_sr",
            json={"inward_id": 7},
        )

        assert response.status_code == 200
        # 4 execute calls: get line items + GST-prep inward_info + header SELECT + update SR status
        assert self.mock_session.execute.call_count == 4

    def _approve(self, line_rows, extra_side_effects_after_lines=None):
        """POST approve_sr with mocked line rows. Returns response."""
        header_row = MagicMock()
        header_row.branch_id = 1
        header_row.sr_no = "SR-EXISTING"
        header_row.sr_date = date(2025, 1, 1)
        side = [
            MagicMock(fetchall=MagicMock(return_value=line_rows)),   # line items
            MagicMock(fetchone=MagicMock(return_value=None)),        # GST prep inward_info (india_gst off)
        ]
        if extra_side_effects_after_lines:
            side.extend(extra_side_effects_after_lines)
        side.extend([
            MagicMock(fetchone=MagicMock(return_value=header_row)),  # header for sr_no minting
            MagicMock(),                                             # update SR status
        ])
        self.mock_session.execute.side_effect = side
        return client.post("/api/storesReceipt/approve_sr", json={"inward_id": 7})

    def test_poless_line_without_invoice_rate_creates_no_note(self):
        """Regression: PO-less line (po_rate None) + blank invoice rate must NOT create any note."""
        line_row = _mock_row({
            "inward_dtl_id": 30, "po_rate": None, "rate": None, "invoice_rate": None,
            "accepted_rate": 84.75, "rejected_qty": 0, "approved_qty": 10.0,
        })
        response = self._approve([line_row])
        assert response.status_code == 200
        assert response.json()["drcr_created"] is False

    def test_invoice_rate_above_accepted_creates_debit_note(self):
        """Invoice 100 vs accepted 90 - supplier billed high - debit note, rate 10."""
        note_id_row = MagicMock(); note_id_row.id = 61
        line_row = _mock_row({
            "inward_dtl_id": 31, "po_rate": None, "rate": None, "invoice_rate": 100.0,
            "accepted_rate": 90.0, "rejected_qty": 0, "approved_qty": 5.0,
        })
        response = self._approve([line_row], extra_side_effects_after_lines=[
            MagicMock(),                                             # insert note header
            MagicMock(fetchone=MagicMock(return_value=note_id_row)), # LAST_INSERT_ID
            MagicMock(),                                             # insert dtl
        ])
        assert response.status_code == 200
        assert response.json()["drcr_created"] is True
        dtl_params = self.mock_session.execute.call_args_list[4][0][1]
        assert dtl_params["debit_credit_note_id"] == 61
        assert dtl_params["rate"] == pytest.approx(10.0)
        hdr_params = self.mock_session.execute.call_args_list[2][0][1]
        assert hdr_params["adjustment_type"] == 1

    def test_invoice_rate_below_accepted_creates_credit_note(self):
        """Invoice 80 vs accepted 90 - we owe supplier - credit note, rate 10."""
        note_id_row = MagicMock(); note_id_row.id = 62
        line_row = _mock_row({
            "inward_dtl_id": 32, "po_rate": None, "rate": None, "invoice_rate": 80.0,
            "accepted_rate": 90.0, "rejected_qty": 0, "approved_qty": 5.0,
        })
        response = self._approve([line_row], extra_side_effects_after_lines=[
            MagicMock(),
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),
            MagicMock(),
        ])
        assert response.status_code == 200
        assert response.json()["drcr_created"] is True
        hdr_params = self.mock_session.execute.call_args_list[2][0][1]
        assert hdr_params["adjustment_type"] == 2

    def test_rate_diff_within_epsilon_creates_no_note(self):
        line_row = _mock_row({
            "inward_dtl_id": 33, "po_rate": 100.0, "rate": 100.0, "invoice_rate": 100.004,
            "accepted_rate": 100.0, "rejected_qty": 0, "approved_qty": 5.0,
        })
        response = self._approve([line_row])
        assert response.status_code == 200
        assert response.json()["drcr_created"] is False

    def test_rejection_note_rate_falls_back_invoice_then_po(self):
        """Rejected qty debit note uses invoice_rate when present."""
        note_id_row = MagicMock(); note_id_row.id = 63
        line_row = _mock_row({
            "inward_dtl_id": 34, "po_rate": 95.0, "rate": 95.0, "invoice_rate": 100.0,
            "accepted_rate": 100.0, "rejected_qty": 3.0, "approved_qty": 7.0,
        })
        response = self._approve([line_row], extra_side_effects_after_lines=[
            MagicMock(),
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),
            MagicMock(),
        ])
        assert response.status_code == 200
        dtl_params = self.mock_session.execute.call_args_list[4][0][1]
        assert dtl_params["rate"] == pytest.approx(100.0)
        assert dtl_params["quantity"] == pytest.approx(3.0)

    def test_rejection_note_rate_falls_back_to_po_rate_when_no_invoice_rate(self):
        """No invoice rate captured -> rejection note prices at po_rate."""
        note_id_row = MagicMock(); note_id_row.id = 64
        line_row = _mock_row({
            "inward_dtl_id": 35, "po_rate": 95.0, "rate": 95.0, "invoice_rate": None,
            "accepted_rate": 100.0, "rejected_qty": 3.0, "approved_qty": 7.0,
        })
        response = self._approve([line_row], extra_side_effects_after_lines=[
            MagicMock(),
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),
            MagicMock(),
        ])
        assert response.status_code == 200
        dtl_params = self.mock_session.execute.call_args_list[4][0][1]
        assert dtl_params["rate"] == pytest.approx(95.0)
        # No invoice rate -> rejection note only, no rate-diff line
        # (lines + gst-prep + note hdr + LAST_INSERT_ID + dtl + header SELECT + status update)
        assert self.mock_session.execute.call_count == 7

    def test_rejection_note_rate_falls_back_to_inward_rate_before_accepted_rate(self):
        """PO-less line, no invoice rate, but the inward line carries its own rate ->
        the rejection note prices at the inward rate, not the accepted rate."""
        note_id_row = MagicMock(); note_id_row.id = 68
        line_row = _mock_row({
            "inward_dtl_id": 39, "po_rate": None, "rate": 100.0, "invoice_rate": None,
            "accepted_rate": 80.0, "rejected_qty": 5.0, "approved_qty": 5.0,
        })
        response = self._approve([line_row], extra_side_effects_after_lines=[
            MagicMock(),
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),
            MagicMock(),
        ])
        assert response.status_code == 200
        dtl_params = self.mock_session.execute.call_args_list[4][0][1]
        assert dtl_params["rate"] == pytest.approx(100.0)

    def test_rejection_note_rate_falls_back_to_accepted_rate_when_no_invoice_or_po_rate(self):
        """PO-less line, no invoice rate -> rejection note prices at accepted_rate."""
        note_id_row = MagicMock(); note_id_row.id = 65
        line_row = _mock_row({
            "inward_dtl_id": 36, "po_rate": None, "rate": None, "invoice_rate": None,
            "accepted_rate": 100.0, "rejected_qty": 3.0, "approved_qty": 7.0,
        })
        response = self._approve([line_row], extra_side_effects_after_lines=[
            MagicMock(),
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),
            MagicMock(),
        ])
        assert response.status_code == 200
        dtl_params = self.mock_session.execute.call_args_list[4][0][1]
        assert dtl_params["rate"] == pytest.approx(100.0)

    def test_negative_invoice_rate_treated_as_no_baseline(self):
        """Defensive gate: a negative invoice_rate must never price a note nor
        drive a rate diff (Pydantic rejects it at save, this pins the read side)."""
        note_id_row = MagicMock(); note_id_row.id = 66
        line_row = _mock_row({
            "inward_dtl_id": 37, "po_rate": 95.0, "rate": 95.0, "invoice_rate": -50.0,
            "accepted_rate": 100.0, "rejected_qty": 3.0, "approved_qty": 7.0,
        })
        response = self._approve([line_row], extra_side_effects_after_lines=[
            MagicMock(),
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),
            MagicMock(),
        ])
        assert response.status_code == 200
        dtl_params = self.mock_session.execute.call_args_list[4][0][1]
        assert dtl_params["rate"] == pytest.approx(95.0)
        hdr_params = self.mock_session.execute.call_args_list[2][0][1]
        assert hdr_params["gross_amount"] > 0
        # Rejection note only - the negative rate must not create a rate-diff line
        assert self.mock_session.execute.call_count == 7

    def test_rate_diff_just_above_epsilon_creates_note(self):
        """Upper side of the dead band: 0.01 diff still bills."""
        note_id_row = MagicMock(); note_id_row.id = 67
        line_row = _mock_row({
            "inward_dtl_id": 38, "po_rate": 100.0, "rate": 100.0, "invoice_rate": 100.01,
            "accepted_rate": 100.0, "rejected_qty": 0, "approved_qty": 5.0,
        })
        response = self._approve([line_row], extra_side_effects_after_lines=[
            MagicMock(),
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),
            MagicMock(),
        ])
        assert response.status_code == 200
        assert response.json()["drcr_created"] is True
        dtl_params = self.mock_session.execute.call_args_list[4][0][1]
        assert dtl_params["rate"] == pytest.approx(0.01)


class TestInsertDrcrNoteDtlQuery:
    """Tests for the insert_drcr_note_dtl query function."""

    def test_query_contains_debit_credit_note_id_bind(self):
        """The INSERT query must include :debit_credit_note_id bind parameter."""
        from src.procurement.query import insert_drcr_note_dtl
        query = insert_drcr_note_dtl()
        sql_str = str(query)
        assert ":debit_credit_note_id" in sql_str
        assert "debit_credit_note_id" in sql_str

    def test_query_contains_all_required_binds(self):
        """Query should have all expected bind parameters."""
        from src.procurement.query import insert_drcr_note_dtl
        query = insert_drcr_note_dtl()
        sql_str = str(query)
        expected_binds = [
            ":debit_credit_note_id",
            ":inward_dtl_id",
            ":debitnote_type",
            ":quantity",
            ":rate",
            ":discount_mode",
            ":discount_value",
            ":discount_amount",
            ":updated_by",
            ":updated_date_time",
        ]
        for bind in expected_binds:
            assert bind in sql_str, f"Missing bind parameter {bind}"

    def test_query_returns_text_object(self):
        """Query function should return a sqlalchemy text object."""
        from src.procurement.query import insert_drcr_note_dtl
        result = insert_drcr_note_dtl()
        assert isinstance(result, type(text("")))


class TestDrcrNoteDtlModel:
    """Tests for the DrcrNoteDtl ORM model."""

    def test_model_has_debit_credit_note_id_column(self):
        """ORM model should include debit_credit_note_id FK column."""
        from src.models.procurement import DrcrNoteDtl
        mapper = DrcrNoteDtl.__table__
        column_names = [col.name for col in mapper.columns]
        assert "debit_credit_note_id" in column_names

    def test_model_debit_credit_note_id_is_not_nullable(self):
        """debit_credit_note_id should be a required (NOT NULL) column."""
        from src.models.procurement import DrcrNoteDtl
        col = DrcrNoteDtl.__table__.columns["debit_credit_note_id"]
        assert col.nullable is False

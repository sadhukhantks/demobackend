"""Tests for manual DRCR note endpoints: prefill, eligible list, create hardening."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from datetime import date

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


class TestDrcrManualEndpoints:
    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = _override_auth
        self.mock_session = MagicMock()
        app.dependency_overrides[get_tenant_db] = _make_mock_db_override(self.mock_session)

    def teardown_method(self):
        app.dependency_overrides.pop(get_current_user_with_refresh, None)
        app.dependency_overrides.pop(get_tenant_db, None)

    def test_prefill_queries_use_real_schema_tables(self):
        """The rewritten queries must not reference legacy con_* / *_master tables."""
        from src.procurement.query import (
            get_inward_header_for_drcr_query,
            get_inward_dtl_for_drcr_query,
            get_eligible_inwards_for_drcr_query,
        )
        for q in (get_inward_header_for_drcr_query(),
                  get_inward_dtl_for_drcr_query(),
                  get_eligible_inwards_for_drcr_query()):
            sql = str(q)
            for bad in ("branch_master", "con_supplier_master", "con_company_info",
                        "con_item_master", "con_item_group", "con_item_make", "con_uom_master"):
                assert bad not in sql, f"legacy table {bad} still referenced"

    def test_prefill_rejects_unapproved_sr(self):
        header = _mock_row({
            "inward_id": 7, "inward_sequence_no": 5, "inward_date": date(2026, 8, 1),
            "branch_id": 2, "branch_name": "Factory", "branch_prefix": "FA",
            "supplier_id": 9, "supplier_name": "ACME", "sr_no": None, "sr_date": None,
            "sr_status": 1, "challan_no": None, "challan_date": None, "co_prefix": "EM",
        })
        self.mock_session.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=header)),
        ]
        resp = client.get("/api/drcrNote/get_inward_for_drcr_note/7")
        assert resp.status_code == 400
        assert "approved" in resp.json()["detail"].lower()

    def test_prefill_success_returns_invoice_rate_and_po_rate_and_scopes_co_id(self):
        """200 path: line_items carry invoice_rate/po_rate (FE Task 8 interface);
        header query must be scoped by co_id (cross-company guard)."""
        header = _mock_row({
            "inward_id": 7, "inward_sequence_no": 5, "inward_date": date(2026, 8, 1),
            "branch_id": 2, "branch_name": "Factory", "branch_prefix": "FA",
            "supplier_id": 9, "supplier_name": "ACME", "sr_no": "12", "sr_date": date(2026, 8, 2),
            "sr_status": 3, "challan_no": None, "challan_date": None, "co_prefix": "EM",
        })
        dtl_row = _mock_row({
            "inward_dtl_id": 101, "item_id": 55, "item_code": "IT1", "item_name": "Item One",
            "item_grp_name": "Group A", "full_item_code": "GA/IT1", "uom_id": 3, "uom_name": "KG",
            "inward_qty": 10.0, "rejected_qty": 0.0, "approved_qty": 10.0,
            "rate": 90.0, "accepted_rate": 90.0, "invoice_rate": 88.0, "po_rate": 100.0,
            "hsn_code": "1234",
        })
        self.mock_session.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=header)),
            MagicMock(fetchall=MagicMock(return_value=[dtl_row])),
        ]
        resp = client.get("/api/drcrNote/get_inward_for_drcr_note/7?co_id=1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["line_items"][0]["invoice_rate"] == 88.0
        assert body["line_items"][0]["po_rate"] == 100.0

        # co_id must reach the header query bind params (cross-company guard)
        header_call_args = self.mock_session.execute.call_args_list[0].args
        header_params = header_call_args[1]
        assert header_params.get("inward_id") == 7
        assert header_params.get("co_id") == 1

    def test_eligible_inwards_returns_wrapped_data(self):
        row = _mock_row({
            "inward_id": 7, "inward_sequence_no": 5, "inward_date": date(2026, 8, 1),
            "sr_no": 12, "sr_date": date(2026, 8, 2), "supplier_id": 9,
            "supplier_name": "ACME", "branch_id": 2, "branch_prefix": "FA", "co_prefix": "EM",
        })
        self.mock_session.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[row])),
        ]
        resp = client.get("/api/drcrNote/get_eligible_inwards?co_id=1")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["data"], list)
        assert body["data"][0]["inward_id"] == 7
        assert body["data"][0]["supplier_name"] == "ACME"

    def test_create_rejects_unapproved_sr(self):
        sr_row = MagicMock()
        sr_row.sr_status = 1
        self.mock_session.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=sr_row)),  # sr_status check
        ]
        resp = client.post("/api/drcrNote/create_drcr_note", json={
            "inward_id": 7,
            "note_date": "2026-08-08",
            "adjustment_type": 1,
            "line_items": [{"inward_dtl_id": 16, "debitnote_type": 2, "quantity": 5, "rate": 10}],
        })
        assert resp.status_code == 400
        assert "approved" in resp.json()["detail"].lower()
        self.mock_session.commit.assert_not_called()

    def test_create_rejects_line_from_another_inward(self):
        """A line_items[].inward_dtl_id that does not belong to inward_id must 400."""
        sr_row = MagicMock()
        sr_row.sr_status = 3
        self.mock_session.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=sr_row)),              # sr_status check
            MagicMock(fetchall=MagicMock(return_value=[(16,), (17,)])),      # owned inward dtl ids
        ]
        resp = client.post("/api/drcrNote/create_drcr_note", json={
            "inward_id": 7,
            "note_date": "2026-08-08",
            "adjustment_type": 1,
            "line_items": [
                {"inward_dtl_id": 16, "debitnote_type": 2, "quantity": 5, "rate": 10},
                {"inward_dtl_id": 999, "debitnote_type": 2, "quantity": 5, "rate": 10},
            ],
        })
        assert resp.status_code == 400
        assert "999" in resp.json()["detail"]
        assert "belong" in resp.json()["detail"].lower()
        self.mock_session.commit.assert_not_called()
        # nothing beyond the two guard SELECTs ran — no header/detail insert
        assert self.mock_session.execute.call_count == 2

    def test_create_rejects_unknown_adjustment_type(self):
        """adjustment_type must be 1 (Debit) or 2 (Credit) — 3 is unclassifiable."""
        resp = client.post("/api/drcrNote/create_drcr_note", json={
            "inward_id": 7,
            "note_date": "2026-08-08",
            "adjustment_type": 3,
            "line_items": [{"inward_dtl_id": 16, "debitnote_type": 2, "quantity": 5, "rate": 10}],
        })
        assert resp.status_code == 422
        self.mock_session.commit.assert_not_called()

    def test_create_inserts_gst_when_india_gst(self):
        sr_row = MagicMock()
        sr_row.sr_status = 3
        note_id_row = MagicMock(); note_id_row.id = 71
        dtl_id_row = MagicMock(); dtl_id_row.id = 501
        gst_ctx_row = _mock_row({"supplier_branch_id": 4, "ship_branch_id": 2, "co_id": 1})
        cfg_row = _mock_row({"india_gst": 1})
        self.mock_session.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=sr_row)),        # sr_status check
            MagicMock(fetchall=MagicMock(return_value=[(16,)])),       # owned inward dtl ids
            MagicMock(fetchone=MagicMock(return_value=gst_ctx_row)),   # inward_info
            MagicMock(fetchone=MagicMock(return_value=cfg_row)),       # co_config
            MagicMock(fetchone=MagicMock(return_value=(29,))),         # supplier state
            MagicMock(fetchone=MagicMock(return_value=(29,))),         # ship state
            MagicMock(fetchall=MagicMock(return_value=[               # proc_gst tax pcts
                _mock_row({"proc_inward_dtl": 16, "tax_pct": 18.0}),
            ])),
            MagicMock(fetchall=MagicMock(return_value=[               # expected tax per line
                _mock_row({"inward_dtl_id": 16, "item_name": "Item One", "expected_tax_pct": 18.0}),
            ])),
            MagicMock(),                                               # insert header
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),   # LAST_INSERT_ID header
            MagicMock(),                                               # insert dtl
            MagicMock(fetchone=MagicMock(return_value=dtl_id_row)),    # LAST_INSERT_ID dtl
            MagicMock(),                                               # insert gst
        ]
        resp = client.post("/api/drcrNote/create_drcr_note", json={
            "inward_id": 7,
            "note_date": "2026-08-08",
            "adjustment_type": 1,
            "line_items": [{"inward_dtl_id": 16, "debitnote_type": 2, "quantity": 5, "rate": 10}],
        })
        assert resp.status_code == 200, resp.json()
        gst_params = self.mock_session.execute.call_args_list[-1][0][1]
        assert "cgst_amount" in gst_params
        assert gst_params["drcr_note_dtl_id"] == 501

    def _gst_context_side_effect(self, supplier_branch_id, supplier_state, proc_gst_rows,
                                 expected_tax_pct=18.0, sr_status=3):
        """Common execute() script up to (and including) the tax-context guard."""
        sr_row = MagicMock()
        sr_row.sr_status = 3
        # No supplier branch → the handler skips the supplier-state SELECT entirely.
        supplier_state_calls = (
            [MagicMock(fetchone=MagicMock(return_value=(supplier_state,)))]
            if supplier_branch_id else []
        )
        return [
            MagicMock(fetchone=MagicMock(return_value=sr_row)),        # sr_status check
            MagicMock(fetchall=MagicMock(return_value=[(16,)])),       # owned inward dtl ids
            MagicMock(fetchone=MagicMock(return_value=_mock_row({     # inward_info
                "supplier_branch_id": supplier_branch_id, "ship_branch_id": 2, "co_id": 1,
            }))),
            MagicMock(fetchone=MagicMock(return_value=_mock_row({"india_gst": 1}))),  # co_config
            *supplier_state_calls,                                     # supplier state
            MagicMock(fetchone=MagicMock(return_value=(29,))),         # ship state
            MagicMock(fetchall=MagicMock(return_value=proc_gst_rows)),  # proc_gst tax pcts
            MagicMock(fetchall=MagicMock(return_value=[               # expected tax per line
                _mock_row({"inward_dtl_id": 16, "item_name": "V BELT B-80",
                           "expected_tax_pct": expected_tax_pct,
                           "sr_status": sr_status}),
            ])),
        ]

    def _post_note(self):
        return client.post("/api/drcrNote/create_drcr_note", json={
            "inward_id": 7,
            "note_date": "2026-08-10",
            "adjustment_type": 1,
            "line_items": [{"inward_dtl_id": 16, "debitnote_type": 2, "quantity": 1, "rate": 750}],
        })

    def test_create_rejects_taxable_line_when_supplier_branch_missing(self):
        """sls br-87 bug: NULL supplier_branch_id used to mint a zero-tax note silently."""
        self.mock_session.execute.side_effect = self._gst_context_side_effect(
            supplier_branch_id=None, supplier_state=None, proc_gst_rows=[],
        )
        resp = self._post_note()
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "Supplier State (GST)" in detail        # names the exact SR field
        assert "NOT created" in detail
        self.mock_session.commit.assert_not_called()

    def test_create_honours_saved_zero_tax_sr(self):
        """TKT-000126: states resolve, the SR is saved, and it carries no
        proc_gst row — that is a DELIBERATE zero-tax SR (the user zeroed the
        Tax % against a no-GST invoice; save_sr only writes proc_gst when
        tax > 0). The note must be allowed with zero tax, not 400 on the
        item-master seed."""
        note_id_row = MagicMock(); note_id_row.id = 71
        self.mock_session.execute.side_effect = self._gst_context_side_effect(
            supplier_branch_id=4, supplier_state=29, proc_gst_rows=[],
        ) + [
            MagicMock(),                                               # insert header
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),   # LAST_INSERT_ID header
            MagicMock(),                                               # insert dtl
        ]
        resp = self._post_note()
        assert resp.status_code == 200, resp.json()
        self.mock_session.commit.assert_called_once()

    def test_create_rejects_taxable_line_when_sr_not_saved(self):
        """No saved SR decision to honour: taxable seed + no proc_gst still 400s."""
        self.mock_session.execute.side_effect = self._gst_context_side_effect(
            supplier_branch_id=4, supplier_state=29, proc_gst_rows=[], sr_status=None,
        )
        resp = self._post_note()
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "V BELT B-80" in detail                 # names the offending item
        assert "Stores Receipt" in detail
        self.mock_session.commit.assert_not_called()

    def test_create_allows_genuinely_tax_free_line(self):
        """Item with 0% tax and no proc_gst row is legitimate — must not be blocked."""
        note_id_row = MagicMock(); note_id_row.id = 71
        self.mock_session.execute.side_effect = self._gst_context_side_effect(
            supplier_branch_id=4, supplier_state=29, proc_gst_rows=[], expected_tax_pct=0.0,
        ) + [
            MagicMock(),                                               # insert header
            MagicMock(fetchone=MagicMock(return_value=note_id_row)),   # LAST_INSERT_ID header
            MagicMock(),                                               # insert dtl
        ]
        resp = self._post_note()
        assert resp.status_code == 200, resp.json()
        self.mock_session.commit.assert_called_once()

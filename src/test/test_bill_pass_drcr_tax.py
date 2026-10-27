"""
Regression tests: DR/CR notes adjust the bill TAX-INCLUSIVE.

`drcr_note.gross_amount` / `net_amount` are stored TAX-EXCLUSIVE (see
create_drcr_note in src/procurement/drcr_note.py — it writes the GST into
drcr_note_dtl_gst separately). The bill-pass readers used to aggregate
`SUM(COALESCE(net_amount, gross_amount, 0))` with no GST and then subtract that
from the tax-INCLUSIVE sr_total, so net_payable was short by exactly the note's
tax.

Real case (sls, inward_id 18636):
    SR   taxable 32,588.98 + IGST 5,866.02 = 38,455.00
    DN331 taxable 2,618.76 + IGST   471.38 =  3,090.14
    shown 35,836.24  (wrong — DN taken tax-exclusive)
    correct 35,364.86

Covers:
- get_bill_pass_list_query() / get_bill_pass_by_id_query() join
  drcr_note_dtl_gst and add the GST term into dr_total / cr_total.
- GET /api/billPass/get_bill_pass_by_id/{id} exposes the note-level and
  line-level tax columns.
- The arithmetic invariant with the real sls numbers.
"""

import re
from datetime import date
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.main import app
from src.config.db import get_tenant_db
from src.authorization.utils import get_current_user_with_refresh
from src.procurement.query import (
    get_bill_pass_list_query,
    get_bill_pass_by_id_query,
    get_bill_pass_drcr_notes_query,
    get_bill_pass_drcr_note_lines_query,
)


client = TestClient(app)

# Real sls figures (inward_id 18636 / DN 331).
SR_TAXABLE = 32588.98
SR_IGST = 5866.02
SR_TOTAL = 38455.00
DN_TAXABLE = 2618.76
DN_IGST = 471.38
DN_TOTAL = 3090.14
CORRECT_NET_PAYABLE = 35364.86
BUGGY_NET_PAYABLE = 35836.24  # what the tax-exclusive dr_total produced


def _norm(sql) -> str:
    """Whitespace-normalised SQL text — keeps the assertions off exact layout."""
    return " ".join(str(sql).split())


def _expr_of(sql_norm: str, alias: str, opener: str = "SUM(") -> str:
    """
    The expression inside the `opener ... )` that is aliased `AS <alias>`.

    Paren-balanced rather than regex-greedy, so it cannot silently swallow
    unrelated SELECT-list items (which would make the assertions vacuous).
    """
    for match in re.finditer(re.escape(opener), sql_norm):
        depth, i = 1, match.end()
        while depth and i < len(sql_norm):
            if sql_norm[i] == "(":
                depth += 1
            elif sql_norm[i] == ")":
                depth -= 1
            i += 1
        if depth:
            continue
        tail = sql_norm[i:].lstrip()
        if re.match(r"AS " + alias + r"\b", tail, re.IGNORECASE):
            return sql_norm[match.end():i - 1]
    raise AssertionError(f"no `{opener}...) AS {alias}` in query")


def _sum_expr(sql_norm: str, alias: str) -> str:
    return _expr_of(sql_norm, alias)


# =============================================================================
# 1. SQL shape — the GST term must be part of dr_total / cr_total
# =============================================================================

class TestBillPassQueriesAddNoteGst:
    def test_list_query_joins_drcr_note_dtl_gst(self):
        sql = _norm(get_bill_pass_list_query())
        assert "drcr_note_dtl_gst" in sql
        assert "dng.drcr_note_dtl_id = dnd.drcr_note_dtl_id" in sql

    def test_detail_query_joins_drcr_note_dtl_gst(self):
        sql = _norm(get_bill_pass_by_id_query())
        assert "drcr_note_dtl_gst" in sql
        assert "dng.drcr_note_dtl_id = dnd.drcr_note_dtl_id" in sql

    def test_list_query_dr_and_cr_totals_include_gst(self):
        """Fails against the old `SUM(COALESCE(net_amount, gross_amount, 0))` form."""
        sql = _norm(get_bill_pass_list_query())
        for alias in ("dr_total", "cr_total"):
            expr = _sum_expr(sql, alias)
            assert "dn.net_amount" in expr
            assert "gst" in expr, f"{alias} is tax-exclusive: {expr}"

    def test_detail_query_dr_and_cr_totals_include_gst(self):
        sql = _norm(get_bill_pass_by_id_query())
        for alias in ("dr_total", "cr_total"):
            expr = _sum_expr(sql, alias)
            assert "dn.net_amount" in expr
            assert "gst" in expr, f"{alias} is tax-exclusive: {expr}"

    def test_note_gst_aggregate_sums_all_three_taxes(self):
        for query in (get_bill_pass_list_query(), get_bill_pass_by_id_query()):
            sql = _norm(query)
            expr = _sum_expr(sql, "gst")
            assert "dng.cgst_amount" in expr
            assert "dng.sgst_amount" in expr
            assert "dng.igst_amount" in expr

    def test_note_gst_aggregate_ignores_inactive_gst_rows(self):
        for query in (get_bill_pass_list_query(), get_bill_pass_by_id_query()):
            assert "(dng.active = 1 OR dng.active IS NULL)" in _norm(query)

    def test_notes_query_exposes_tax_inclusive_total(self):
        sql = _norm(get_bill_pass_drcr_notes_query())
        assert "AS tax_amount" in sql
        total = _expr_of(sql, "total_amount", opener="(")
        assert "dn.net_amount" in total
        assert "g.gst" in total, f"note total is tax-exclusive: {total}"

    def test_note_lines_query_exposes_tax_inclusive_line_total(self):
        sql = _norm(get_bill_pass_drcr_note_lines_query())
        assert "AS tax_amount" in sql
        assert "AS line_total" in sql
        line_total = _expr_of(sql, "line_total", opener="(")
        assert "dng.cgst_amount" in line_total
        assert "dng.sgst_amount" in line_total
        assert "dng.igst_amount" in line_total


# =============================================================================
# 2. Endpoint response surface + arithmetic invariant
# =============================================================================

def _row(mapping):
    row = MagicMock()
    row._mapping = mapping
    return row


def _override_auth():
    return {"user_id": 1}


def _header_row(**overrides):
    base = {
        "inward_id": 18636,
        "inward_sequence_no": 1,
        "inward_date": date(2025, 4, 15),
        "bill_pass_no": "SR/HO/IN/0001",
        "bill_pass_date": date(2025, 4, 16),
        "invoice_date": None,
        "invoice_amount": None,
        "invoice_no": None,
        "invoice_recvd_date": None,
        "invoice_due_date": None,
        "supplier_id": 77,
        "supplier_name": "Acme Suppliers",
        "branch_id": 1,
        "branch_name": "Head Office",
        "branch_prefix": "HO",
        "co_id": 1,
        "co_prefix": "ACME",
        "sr_status": 3,
        "billpass_status": 0,
        "sr_status_name": "Approved",
        "sr_remarks": None,
        "challan_no": None,
        "challan_date": None,
        "round_off_value": 0,
        "gross_amount": SR_TAXABLE,
        "net_amount": SR_TOTAL,
        "sr_taxable": SR_TAXABLE,
        "sr_tax": SR_IGST,
        "sr_cgst": 0.0,
        "sr_sgst": 0.0,
        "sr_igst": SR_IGST,
        "sr_total": SR_TOTAL,
        "sr_line_count": 1,
        "dr_total": 0.0,
        "dr_count": 0,
        "cr_total": 0.0,
        "cr_count": 0,
        "additional_net": 0.0,
        "additional_cgst": 0.0,
        "additional_sgst": 0.0,
        "additional_igst": 0.0,
        "additional_tax": 0.0,
        "additional_total": 0.0,
        "additional_count": 0,
        "net_payable": SR_TOTAL,
    }
    base.update(overrides)
    return _row(base)


def _note_row(note_id, adjustment_type, taxable, cgst=0.0, sgst=0.0, igst=0.0):
    """A drcr_note row as get_bill_pass_drcr_notes_query() returns it."""
    return _row({
        "debit_credit_note_id": note_id,
        "note_date": date(2025, 4, 20),
        "adjustment_type": adjustment_type,
        "note_type_name": "Debit Note" if adjustment_type == 1 else "Credit Note",
        "remarks": "short supply",
        # gross/net are TAX-EXCLUSIVE by design
        "gross_amount": taxable,
        "net_amount": taxable,
        "cgst_amount": cgst,
        "sgst_amount": sgst,
        "igst_amount": igst,
        "tax_amount": round(cgst + sgst + igst, 2),
        "total_amount": round(taxable + cgst + sgst + igst, 2),
        "status_id": 3,
        "status_name": "Approved",
        "line_count": 1,
    })


def _note_line_row(note_id, dtl_id, qty, rate, cgst=0.0, sgst=0.0, igst=0.0):
    taxable = round(qty * rate, 2)
    return _row({
        "drcr_note_dtl_id": dtl_id,
        "debit_credit_note_id": note_id,
        "inward_dtl_id": 5001,
        "debitnote_type": 1,
        "adjustment_reason": "Quantity Rejection",
        "quantity": qty,
        "rate": rate,
        "discount_mode": None,
        "discount_value": None,
        "discount_amount": 0,
        "line_amount": taxable,
        "cgst_amount": cgst,
        "sgst_amount": sgst,
        "igst_amount": igst,
        "tax_amount": round(cgst + sgst + igst, 2),
        "line_total": round(taxable + cgst + sgst + igst, 2),
        "item_name": "MS Plate",
        "item_code": "MSP-01",
        "full_item_code": "RM/MSP-01",
        "po_id": None,
        "po_no": None,
        "po_date": None,
        "branch_prefix": "HO",
        "co_prefix": "ACME",
    })


class TestBillPassDetailNoteTax:
    def setup_method(self):
        app.dependency_overrides[get_current_user_with_refresh] = _override_auth
        self.mock_session = MagicMock()
        app.dependency_overrides[get_tenant_db] = lambda: self.mock_session

    def teardown_method(self):
        app.dependency_overrides.pop(get_current_user_with_refresh, None)
        app.dependency_overrides.pop(get_tenant_db, None)

    def _wire(self, header, notes=(), note_lines=(), additional=()):
        """The 5 db.execute(...) calls of get_bill_pass_by_id, in order."""
        self.mock_session.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=header)),
            MagicMock(fetchall=MagicMock(return_value=[])),
            MagicMock(fetchall=MagicMock(return_value=list(notes))),
            MagicMock(fetchall=MagicMock(return_value=list(note_lines))),
            MagicMock(fetchall=MagicMock(return_value=list(additional))),
        ]

    def test_debit_note_exposes_tax_columns(self):
        self._wire(
            header=_header_row(dr_total=DN_TOTAL, dr_count=1,
                               net_payable=CORRECT_NET_PAYABLE),
            notes=[_note_row(331, 1, DN_TAXABLE, igst=DN_IGST)],
        )

        resp = client.get("/api/billPass/get_bill_pass_by_id/18636")

        assert resp.status_code == 200, resp.text
        notes = resp.json()["debit_notes"]
        assert len(notes) == 1
        dn = notes[0]
        assert dn["net_amount"] == DN_TAXABLE       # still tax-exclusive
        assert dn["cgst_amount"] == 0.0
        assert dn["sgst_amount"] == 0.0
        assert dn["igst_amount"] == DN_IGST
        assert dn["tax_amount"] == DN_IGST
        assert dn["total_amount"] == DN_TOTAL       # what actually adjusts the bill
        assert dn["total_amount"] == round(dn["net_amount"] + dn["tax_amount"], 2)

    def test_credit_note_exposes_tax_columns(self):
        self._wire(
            header=_header_row(cr_total=1180.0, cr_count=1,
                               net_payable=SR_TOTAL + 1180.0),
            notes=[_note_row(332, 2, 1000.0, cgst=90.0, sgst=90.0)],
        )

        resp = client.get("/api/billPass/get_bill_pass_by_id/18636")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["debit_notes"] == []
        cn = body["credit_notes"][0]
        assert cn["cgst_amount"] == 90.0
        assert cn["sgst_amount"] == 90.0
        assert cn["igst_amount"] == 0.0
        assert cn["tax_amount"] == 180.0
        assert cn["total_amount"] == 1180.0

    def test_note_lines_expose_tax_columns(self):
        self._wire(
            header=_header_row(dr_total=DN_TOTAL, dr_count=1,
                               net_payable=CORRECT_NET_PAYABLE),
            notes=[_note_row(331, 1, DN_TAXABLE, igst=DN_IGST)],
            note_lines=[_note_line_row(331, 9001, qty=2.0, rate=1309.38, igst=DN_IGST)],
        )

        resp = client.get("/api/billPass/get_bill_pass_by_id/18636")

        assert resp.status_code == 200, resp.text
        lines = resp.json()["debit_notes"][0]["lines"]
        assert len(lines) == 1
        line = lines[0]
        assert line["line_amount"] == DN_TAXABLE     # tax-exclusive
        assert line["cgst_amount"] == 0.0
        assert line["sgst_amount"] == 0.0
        assert line["igst_amount"] == DN_IGST
        assert line["tax_amount"] == DN_IGST
        assert line["line_total"] == DN_TOTAL
        assert line["line_total"] == round(line["line_amount"] + line["tax_amount"], 2)

    def test_net_payable_uses_tax_inclusive_note_totals_real_case(self):
        """sls inward_id 18636 / DN 331 — was 35,836.24, must be 35,364.86."""
        self._wire(
            header=_header_row(dr_total=DN_TOTAL, dr_count=1,
                               net_payable=CORRECT_NET_PAYABLE),
            notes=[_note_row(331, 1, DN_TAXABLE, igst=DN_IGST)],
            note_lines=[_note_line_row(331, 9001, qty=2.0, rate=1309.38, igst=DN_IGST)],
        )

        resp = client.get("/api/billPass/get_bill_pass_by_id/18636")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        s = body["summary"]
        dn = body["debit_notes"][0]

        # The header aggregate must agree with the per-note total (both tax-inclusive).
        assert s["dr_total"] == dn["total_amount"] == DN_TOTAL
        assert s["dr_total"] != DN_TAXABLE, "dr_total is tax-exclusive again"

        expected = round(
            s["sr_total"]
            - (DN_TAXABLE + DN_IGST)
            + s["cr_total"]
            + s["additional_net"]
            + s["additional_tax"],
            2,
        )
        assert expected == CORRECT_NET_PAYABLE
        assert round(s["net_payable"], 2) == expected
        assert round(s["net_payable"], 2) != BUGGY_NET_PAYABLE

    def test_net_payable_invariant_with_dr_cr_and_additional(self):
        """net = sr - (dr_taxable+dr_gst) + (cr_taxable+cr_gst) + add_net + add_tax."""
        cr_taxable, cr_gst = 1000.0, 180.0
        add_net, add_tax = 500.0, 90.0
        expected = round(
            SR_TOTAL - DN_TOTAL + (cr_taxable + cr_gst) + add_net + add_tax, 2
        )
        self._wire(
            header=_header_row(
                dr_total=DN_TOTAL, dr_count=1,
                cr_total=cr_taxable + cr_gst, cr_count=1,
                additional_net=add_net, additional_tax=add_tax,
                additional_cgst=45.0, additional_sgst=45.0,
                additional_total=add_net + add_tax, additional_count=1,
                net_payable=expected,
            ),
            notes=[
                _note_row(331, 1, DN_TAXABLE, igst=DN_IGST),
                _note_row(332, 2, cr_taxable, cgst=90.0, sgst=90.0),
            ],
        )

        resp = client.get("/api/billPass/get_bill_pass_by_id/18636")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        s = body["summary"]
        assert s["dr_total"] == body["debit_notes"][0]["total_amount"]
        assert s["cr_total"] == body["credit_notes"][0]["total_amount"]
        assert round(s["net_payable"], 2) == expected

"""
Regression tests for src/accounting/posting_service.py::_recipe_drcr_note.

`drcr_note.net_amount` / `gross_amount` are stored TAX-EXCLUSIVE (see
create_drcr_note in src/procurement/drcr_note.py — the GST is written into
drcr_note_dtl_gst separately). The recipe used to assume the opposite and
derive `base_amount = net_amount - cgst - sgst - igst`, which under-debited the
party ledger and under-credited the material ledger by the note's GST.

Correct shape:
    base_amount  = net_amount                       -> MATERIAL leg + GST summary taxable
    party_amount = base_amount + cgst + sgst + igst -> CREDITOR leg, bill refs, settlement

Real case (sls, inward_id 18636 / DN 331): taxable 2,618.76 + IGST 471.38
= 3,090.14 moving on the supplier account.

The recipe is exercised directly with a mocked Session; the SQL-writing helpers
(_write_voucher / _insert_gst_summary / _insert_bill_ref) and the ledger lookups
are patched so the assertions are about the amounts, not the inserts.
"""

from contextlib import ExitStack
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.accounting import posting_service
from src.accounting.constants import LINE_TYPES, SOURCE_DOC_TYPES


DR = posting_service.DR
CR = posting_service.CR

# Real sls figures (DN 331 against inward 18636).
TAXABLE = 2618.76
IGST = 471.38
PARTY_TOTAL = 3090.14
PURCHASE_PENDING = 38455.00        # the SR bill ref, tax-inclusive
PENDING_AFTER = 35364.86           # 38455.00 - 3090.14
PENDING_IF_TAX_EXCLUSIVE = 35836.24  # what the old recipe would have left

PARTY_LEDGER_ID = 901
LEDGER_IDS = {
    LINE_TYPES["MATERIAL"]: 910,
    LINE_TYPES["CGST_INPUT"]: 911,
    LINE_TYPES["SGST_INPUT"]: 912,
    LINE_TYPES["IGST_INPUT"]: 913,
}


def _row(mapping: dict):
    """A stand-in for a SQLAlchemy Row (code does row._mapping[...])."""
    row = MagicMock()
    row._mapping = mapping
    return row


def _hdr(adjustment_type=1, net_amount=TAXABLE, **overrides):
    base = {
        "debit_credit_note_id": 331,
        "adjustment_type": adjustment_type,
        "inward_id": 18636,
        "note_date": date(2025, 4, 20),
        "net_amount": net_amount,
        "gross_amount": net_amount,
        "status_id": posting_service.STATUS_APPROVED,
        "supplier_id": 77,
        "branch_id": 1,
        "invoice_no": "INV-9",
        "co_id": 1,
    }
    base.update(overrides)
    return base


def _gst(cgst=0.0, sgst=0.0, igst=0.0):
    return {"cgst": cgst, "sgst": sgst, "igst": igst}


def _make_session(hdr, gst, purchase_ref=None, credit_days=None, log=None):
    """MagicMock Session that answers _recipe_drcr_note's SELECTs by SQL text."""
    def _execute(sql, params=None):
        text_ = " ".join(str(sql).split())
        if log is not None:
            log.append((text_, params))
        result = MagicMock()
        if "FROM drcr_note_dtl dnd" in text_:
            result.fetchone.return_value = _row(gst)
        elif "FROM drcr_note dn" in text_:
            result.fetchone.return_value = _row(hdr)
        elif "FROM acc_bill_ref br" in text_:
            result.fetchone.return_value = _row(purchase_ref) if purchase_ref else None
        elif "pp.credit_days" in text_:
            result.fetchone.return_value = (
                _row({"credit_days": credit_days}) if credit_days is not None else None
            )
        else:
            result.fetchone.return_value = None
        return result

    session = MagicMock()
    session.execute.side_effect = _execute
    return session


def _post(hdr, gst, purchase_ref=None, credit_days=None):
    """Run the recipe against mocked infrastructure; return what it produced."""
    log = []
    session = _make_session(hdr, gst, purchase_ref, credit_days, log)
    captured = {"entry": None, "voucher": None, "gst_summary": [], "bill_refs": [], "log": log}

    def _fake_write_voucher(db, **kwargs):
        captured["entry"] = kwargs["entry"]
        captured["voucher"] = kwargs
        total_dr = posting_service._assert_balanced(
            kwargs["entry"], kwargs["source_doc_type"], kwargs["source_doc_id"]
        )
        return 555, "DN/2025/0001", 777, total_dr

    def _fake_bill_ref(db, **kwargs):
        captured["bill_refs"].append(kwargs)
        return 888

    with ExitStack() as stack:
        stack.enter_context(patch.object(
            posting_service, "_get_party_ledger", return_value=PARTY_LEDGER_ID))
        stack.enter_context(patch.object(
            posting_service, "_require_ledger",
            side_effect=lambda db, co_id, doc_type, line_type: LEDGER_IDS[line_type]))
        stack.enter_context(patch.object(
            posting_service, "_write_voucher", side_effect=_fake_write_voucher))
        stack.enter_context(patch.object(
            posting_service, "_insert_gst_summary",
            side_effect=lambda db, **kw: captured["gst_summary"].append(kw)))
        stack.enter_context(patch.object(
            posting_service, "_insert_bill_ref", side_effect=_fake_bill_ref))

        voucher_id, message, extra = posting_service._recipe_drcr_note(
            session, co_id=1, note_id=hdr["debit_credit_note_id"], user_id=1,
            settings={}, mode=posting_service.MODE_AUTO_APPROVED,
        )

    captured["voucher_id"] = voucher_id
    captured["message"] = message
    return captured


def _leg(entry, line_type):
    legs = [leg for leg in entry if leg["line_type"] == LINE_TYPES[line_type]]
    assert len(legs) <= 1, f"duplicate {line_type} legs"
    return legs[0] if legs else None


def _totals(entry):
    return (
        round(sum(leg["amount"] for leg in entry if leg["dr_cr"] == DR), 2),
        round(sum(leg["amount"] for leg in entry if leg["dr_cr"] == CR), 2),
    )


def _params_for(log, needle):
    for sql, params in log:
        if needle in sql:
            return params
    raise AssertionError(f"no execute() carrying {needle!r}")


class TestDebitNotePosting:
    def test_party_leg_is_tax_inclusive_material_leg_is_taxable(self):
        out = _post(_hdr(adjustment_type=1), _gst(igst=IGST))
        entry = out["entry"]

        party = _leg(entry, "CREDITOR")
        assert party["dr_cr"] == DR
        assert party["amount"] == PARTY_TOTAL
        assert party["ledger_id"] == PARTY_LEDGER_ID
        assert party["is_party_leg"] is True

        material = _leg(entry, "MATERIAL")
        assert material["dr_cr"] == CR
        assert material["amount"] == TAXABLE
        assert material["amount"] != PARTY_TOTAL

        igst = _leg(entry, "IGST_INPUT")
        assert igst["dr_cr"] == CR
        assert igst["amount"] == IGST

        assert _leg(entry, "CGST_INPUT") is None
        assert _leg(entry, "SGST_INPUT") is None

    def test_voucher_balances(self):
        out = _post(_hdr(adjustment_type=1), _gst(igst=IGST))
        total_dr, total_cr = _totals(out["entry"])
        assert total_dr == total_cr == PARTY_TOTAL

    def test_gst_summary_taxable_is_the_base_not_the_party_amount(self):
        out = _post(_hdr(adjustment_type=1), _gst(igst=IGST))
        assert len(out["gst_summary"]) == 1
        summary = out["gst_summary"][0]
        assert summary["taxable"] == TAXABLE
        assert summary["igst"] == IGST
        assert summary["cgst"] == 0.0
        assert summary["sgst"] == 0.0

    def test_against_bill_ref_is_tax_inclusive(self):
        out = _post(_hdr(adjustment_type=1), _gst(igst=IGST))
        refs = [r for r in out["bill_refs"] if r["ref_type"] == "AGAINST"]
        assert len(refs) == 1
        assert refs[0]["amount"] == PARTY_TOTAL
        assert refs[0]["status"] == "CLOSED"

    def test_intra_state_note_splits_cgst_and_sgst_legs(self):
        out = _post(_hdr(adjustment_type=1), _gst(cgst=235.69, sgst=235.69))
        entry = out["entry"]
        assert _leg(entry, "CREDITOR")["amount"] == round(TAXABLE + 235.69 + 235.69, 2)
        assert _leg(entry, "CGST_INPUT") == {
            "line_type": LINE_TYPES["CGST_INPUT"], "dr_cr": CR, "amount": 235.69,
            "ledger_id": LEDGER_IDS[LINE_TYPES["CGST_INPUT"]],
            "narration": "CGST Input adjustment",
        }
        assert _leg(entry, "SGST_INPUT")["amount"] == 235.69
        assert _leg(entry, "IGST_INPUT") is None
        total_dr, total_cr = _totals(entry)
        assert total_dr == total_cr


class TestCreditNotePosting:
    def test_sides_are_mirrored(self):
        out = _post(_hdr(adjustment_type=2), _gst(igst=IGST))
        entry = out["entry"]

        party = _leg(entry, "CREDITOR")
        assert party["dr_cr"] == CR
        assert party["amount"] == PARTY_TOTAL

        material = _leg(entry, "MATERIAL")
        assert material["dr_cr"] == DR
        assert material["amount"] == TAXABLE

        igst = _leg(entry, "IGST_INPUT")
        assert igst["dr_cr"] == DR
        assert igst["amount"] == IGST

        total_dr, total_cr = _totals(entry)
        assert total_dr == total_cr == PARTY_TOTAL

    def test_new_bill_ref_amount_and_pending_are_tax_inclusive(self):
        out = _post(_hdr(adjustment_type=2), _gst(igst=IGST))
        refs = out["bill_refs"]
        assert len(refs) == 1
        ref = refs[0]
        assert ref["ref_type"] == "NEW"
        assert ref["status"] == "OPEN"
        assert ref["amount"] == PARTY_TOTAL
        assert ref["pending_amount"] == PARTY_TOTAL

    def test_credit_note_does_not_settle_a_purchase_bill(self):
        out = _post(_hdr(adjustment_type=2), _gst(igst=IGST),
                    purchase_ref={"acc_bill_ref_id": 4242, "pending": PURCHASE_PENDING})
        assert all("acc_bill_settlement" not in sql for sql, _ in out["log"])
        assert all("UPDATE acc_bill_ref" not in sql for sql, _ in out["log"])


class TestNoteWithoutGst:
    def test_party_equals_material_equals_net_amount(self):
        out = _post(_hdr(adjustment_type=1, net_amount=1500.0), _gst())
        entry = out["entry"]

        assert _leg(entry, "CREDITOR")["amount"] == 1500.0
        assert _leg(entry, "MATERIAL")["amount"] == 1500.0
        for line_type in ("CGST_INPUT", "SGST_INPUT", "IGST_INPUT"):
            assert _leg(entry, line_type) is None
        assert len(entry) == 2

        total_dr, total_cr = _totals(entry)
        assert total_dr == total_cr == 1500.0

    def test_no_gst_summary_row_is_written(self):
        out = _post(_hdr(adjustment_type=1, net_amount=1500.0), _gst())
        assert out["gst_summary"] == []


class TestDebitNoteSettlement:
    def _settle(self):
        return _post(
            _hdr(adjustment_type=1), _gst(igst=IGST),
            purchase_ref={"acc_bill_ref_id": 4242, "pending": PURCHASE_PENDING},
        )

    def test_settled_amount_is_tax_inclusive(self):
        params = _params_for(self._settle()["log"], "acc_bill_settlement")
        assert params["settled_amount"] == PARTY_TOTAL
        # :bill_ref_id = the note's own AGAINST ref, :against_ref_id = the purchase bill
        assert params["bill_ref_id"] == 888
        assert params["against_ref_id"] == 4242

    def test_purchase_bill_pending_drops_by_the_tax_inclusive_amount(self):
        params = _params_for(self._settle()["log"], "UPDATE acc_bill_ref")
        assert params["bill_ref_id"] == 4242
        assert params["pending_amount"] == PENDING_AFTER
        assert params["pending_amount"] != PENDING_IF_TAX_EXCLUSIVE
        assert params["status"] == "PARTIAL"

    def test_full_settlement_closes_the_bill(self):
        out = _post(
            _hdr(adjustment_type=1), _gst(igst=IGST),
            purchase_ref={"acc_bill_ref_id": 4242, "pending": PARTY_TOTAL},
        )
        params = _params_for(out["log"], "UPDATE acc_bill_ref")
        assert params["pending_amount"] == 0.0
        assert params["status"] == "CLOSED"

    def test_no_purchase_voucher_means_no_settlement(self):
        out = _post(_hdr(adjustment_type=1), _gst(igst=IGST), purchase_ref=None)
        assert all("acc_bill_settlement" not in sql for sql, _ in out["log"])
        # the AGAINST ref is still recorded
        assert [r["ref_type"] for r in out["bill_refs"]] == ["AGAINST"]

    def test_settlement_looks_up_the_purchase_voucher_bill_ref(self):
        params = _params_for(self._settle()["log"], "FROM acc_bill_ref br")
        assert params["purchase_doc_type"] == SOURCE_DOC_TYPES["PROC_BILLPASS"]
        assert params["inward_id"] == 18636


class TestGuards:
    def test_zero_net_amount_is_rejected(self):
        with pytest.raises(posting_service.PostingError, match="net amount is zero"):
            _post(_hdr(adjustment_type=1, net_amount=0), _gst(igst=IGST))

    def test_unapproved_note_is_rejected(self):
        with pytest.raises(posting_service.PostingError, match="not approved"):
            _post(_hdr(adjustment_type=1, status_id=1), _gst(igst=IGST))

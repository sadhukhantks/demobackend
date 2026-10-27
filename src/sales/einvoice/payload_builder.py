"""
Builds the INV-01 (schema v1.1) JSON payload for Generate IRN from an existing
sales invoice — reads only; never mutates the base sales tables.

Field mapping (ERP column -> INV-01 field) is documented side-by-side in
docs/einvoice/INTEGRATION_NOTES.md. Validation failures are collected and
returned together so a developer sees every data gap in one pass instead of
fixing them one 400 at a time.
"""

import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from src.sales.einvoice.constants import (
    DOC_NO_MAX_LEN,
    DOC_TYP_INVOICE,
    EINV_DATE_FORMAT,
    EINV_SCHEMA_VERSION,
    SUP_TYP_B2B,
    TAX_SCHEME_GST,
)
from src.sales.einvoice.query import (
    get_invoice_header_for_einvoice,
    get_invoice_lines_for_einvoice,
)

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3}$")
DOC_NO_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/-]*$")


def _round2(value) -> float:
    """IRP validates amounts to 2 decimals; round half-up like the portal does."""
    if value is None:
        value = 0
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _fmt_date(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime(EINV_DATE_FORMAT)
    return str(value)


def _clean_gstin(gstin) -> str | None:
    if not gstin:
        return None
    cleaned = str(gstin).strip().upper()
    return cleaned or None


def _pincode(value) -> int | None:
    try:
        pin = int(value)
    except (TypeError, ValueError):
        return None
    return pin if 100000 <= pin <= 999999 else None


def build_invoice_payload(db: Session, invoice_id: int) -> tuple[dict | None, list[str]]:
    """Return (payload, errors). payload is None when blocking errors exist.

    Only status-3 (Approved) invoices should be submitted for real; the caller
    enforces that — this builder just reports what it sees so the payload can
    be previewed at any status.
    """
    errors: list[str] = []

    header_row = db.execute(
        get_invoice_header_for_einvoice(), {"invoice_id": int(invoice_id)}
    ).fetchone()
    if header_row is None:
        return None, [f"Invoice {invoice_id} not found"]
    header = dict(header_row._mapping)

    line_rows = db.execute(
        get_invoice_lines_for_einvoice(), {"invoice_id": int(invoice_id)}
    ).fetchall()
    lines = [dict(r._mapping) for r in line_rows]
    if not lines:
        errors.append("Invoice has no line items")

    # --- Seller (our branch) ---------------------------------------------
    seller_gstin = _clean_gstin(header.get("seller_gstin"))
    if not seller_gstin or not GSTIN_RE.match(seller_gstin):
        errors.append(
            f"Seller GSTIN missing/invalid on branch_mst.gst_no for branch {header.get('branch_id')}: {seller_gstin!r}"
        )
    seller_pin = _pincode(header.get("seller_pincode"))
    if seller_pin is None:
        errors.append("Seller PIN code missing/invalid (branch_mst.branch_zipcode must be 6 digits)")
    seller_stcd = str(header.get("seller_state_code") or "").strip()
    if not seller_stcd:
        errors.append("Seller state code missing (state_mst.state_code for the branch state)")

    # --- Buyer (billing party branch) ------------------------------------
    buyer_gstin = _clean_gstin(header.get("buyer_gstin"))
    if not buyer_gstin or not GSTIN_RE.match(buyer_gstin):
        errors.append(
            f"Buyer GSTIN missing/invalid on party_branch_mst.gst_no for billing_to_id {header.get('billing_to_id')}: {buyer_gstin!r}"
        )
    buyer_pin = _pincode(header.get("buyer_pincode"))
    if buyer_pin is None:
        errors.append("Buyer PIN code missing/invalid (party_branch_mst.zip_code must be 6 digits)")
    buyer_stcd = str(header.get("buyer_state_code") or "").strip()
    if not buyer_stcd:
        errors.append("Buyer state code missing (state_mst.state_code for the billing party state)")

    # --- Document details -------------------------------------------------
    doc_no = str(header.get("invoice_no") or "").strip()
    if not doc_no:
        errors.append("Invoice number is empty — open/approve the invoice first")
    elif len(doc_no) > DOC_NO_MAX_LEN or not DOC_NO_RE.match(doc_no):
        errors.append(
            f"Invoice number {doc_no!r} violates IRP DocDtls.No rules (max 16 chars, alphanumeric//-, no leading 0/./-)"
        )
    doc_dt = _fmt_date(header.get("invoice_date"))
    if not doc_dt:
        errors.append("Invoice date is empty")

    # --- Line items -------------------------------------------------------
    item_list = []
    total_assessable = Decimal("0")
    total_cgst = Decimal("0")
    total_sgst = Decimal("0")
    total_igst = Decimal("0")
    # intra_inter_state: this ERP stores 1 = intra-state (CGST+SGST), else IGST
    is_intra = header.get("intra_inter_state") == 1

    for idx, line in enumerate(lines, start=1):
        hsn = str(line.get("hsn_code") or "").strip()
        if not (4 <= len(hsn) <= 8 and hsn.isdigit()):
            errors.append(f"Line {idx}: HSN code missing/invalid ({hsn!r}) — must be 4-8 digits")

        qty = line.get("quantity") or 0
        assessable = Decimal(str(line.get("amount_without_tax") or 0))
        cgst = Decimal(str(line.get("cgst_amount") or 0))
        sgst = Decimal(str(line.get("sgst_amount") or 0))
        igst = Decimal(str(line.get("igst_amount") or 0))
        gst_rate = line.get("tax_percentage")
        if gst_rate is None:
            gst_rate = (line.get("cgst_percentage") or 0) + (line.get("sgst_percentage") or 0) \
                if is_intra else (line.get("igst_percentage") or 0)

        total_assessable += assessable
        total_cgst += cgst
        total_sgst += sgst
        total_igst += igst

        item_total = assessable + cgst + sgst + igst
        item_list.append({
            "SlNo": str(idx),
            "PrdDesc": (line.get("item_name") or "")[:300],
            "IsServc": "N",
            "HsnCd": hsn,
            "Qty": _round2(qty),
            "Unit": _uqc(line.get("uom_name")),
            "UnitPrice": _round2(line.get("rate")),
            "TotAmt": _round2(assessable + Decimal(str(line.get("discount_amount") or 0))),
            "Discount": _round2(line.get("discount_amount")),
            "AssAmt": _round2(assessable),
            "GstRt": _round2(gst_rate),
            "IgstAmt": _round2(igst),
            "CgstAmt": _round2(cgst),
            "SgstAmt": _round2(sgst),
            "CesRt": 0,
            "CesAmt": 0,
            "CesNonAdvlAmt": 0,
            "OthChrg": 0,
            "TotItemVal": _round2(item_total),
        })

    round_off = Decimal(str(header.get("round_off") or 0))
    tot_inv_val = total_assessable + total_cgst + total_sgst + total_igst + round_off

    payload = {
        "Version": EINV_SCHEMA_VERSION,
        "TranDtls": {
            "TaxSch": TAX_SCHEME_GST,
            "SupTyp": SUP_TYP_B2B,
            "RegRev": "N",
            "IgstOnIntra": "N",
        },
        "DocDtls": {
            "Typ": DOC_TYP_INVOICE,
            "No": doc_no,
            "Dt": doc_dt,
        },
        "SellerDtls": {
            "Gstin": seller_gstin,
            "LglNm": header.get("seller_name") or "",
            "Addr1": (header.get("seller_address1") or "")[:100],
            "Addr2": (header.get("seller_address2") or "")[:100] or None,
            "Loc": header.get("seller_state_name") or "",
            "Pin": seller_pin,
            "Stcd": seller_stcd,
            "Ph": str(header.get("seller_phone") or "") or None,
            "Em": header.get("seller_email") or None,
        },
        "BuyerDtls": {
            "Gstin": buyer_gstin,
            "LglNm": header.get("buyer_name") or "",
            "Pos": buyer_stcd,
            "Addr1": (header.get("buyer_address1") or "")[:100],
            "Addr2": (header.get("buyer_address2") or "")[:100] or None,
            "Loc": header.get("buyer_state_name") or "",
            "Pin": buyer_pin,
            "Stcd": buyer_stcd,
            "Ph": str(header.get("buyer_phone") or "") or None,
        },
        "ValDtls": {
            "AssVal": _round2(total_assessable),
            "CgstVal": _round2(total_cgst),
            "SgstVal": _round2(total_sgst),
            "IgstVal": _round2(total_igst),
            "CesVal": 0,
            "StCesVal": 0,
            "Discount": 0,
            "OthChrg": 0,
            "RndOffAmt": _round2(round_off),
            "TotInvVal": _round2(tot_inv_val),
        },
        "ItemList": item_list,
    }

    # Ship-to differs from bill-to -> ShipDtls block
    ship_gstin = _clean_gstin(header.get("ship_gstin"))
    if header.get("shipping_to_id") and header.get("shipping_to_id") != header.get("billing_to_id"):
        ship_pin = _pincode(header.get("ship_pincode"))
        ship_stcd = str(header.get("ship_state_code") or "").strip()
        if ship_pin is None or not ship_stcd:
            errors.append("Ship-to selected but its PIN/state code is missing on party_branch_mst")
        payload["ShipDtls"] = {
            "Gstin": ship_gstin,
            "LglNm": header.get("buyer_name") or "",
            "Addr1": (header.get("ship_address1") or "")[:100],
            "Addr2": (header.get("ship_address2") or "")[:100] or None,
            "Loc": header.get("ship_state_name") or "",
            "Pin": ship_pin,
            "Stcd": ship_stcd,
        }

    # Drop optional keys with None values — the IRP rejects nulls for optionals
    payload = _strip_nones(payload)

    if errors:
        return None, errors
    return payload, []


# Common jute-trade UOM names -> GST UQC codes; anything unmapped passes through
# uppercased (the IRP rejects unknown codes, surfacing the gap loudly in sandbox).
_UQC_MAP = {
    "KGS": "KGS", "KG": "KGS", "KILOGRAM": "KGS", "KILOGRAMS": "KGS",
    "MT": "TON", "TON": "TON", "TONNE": "TON", "TONNES": "TON", "METRIC TON": "TON",
    "QTL": "QTL", "QUINTAL": "QTL",
    "PCS": "PCS", "PIECE": "PCS", "PIECES": "PCS", "NOS": "NOS", "NO": "NOS", "NUMBER": "NOS",
    "MTR": "MTR", "METER": "MTR", "METRE": "MTR", "METERS": "MTR",
    "BALE": "BAL", "BALES": "BAL", "BAL": "BAL",
    "BAG": "BAG", "BAGS": "BAG",
    "ROLL": "ROL", "ROLLS": "ROL",
    "SET": "SET", "SETS": "SET",
    "LTR": "LTR", "LITRE": "LTR", "LITER": "LTR",
    "SQM": "SQM", "SQ MTR": "SQM",
    "YDS": "YDS", "YARD": "YDS", "YARDS": "YDS",
}


def _uqc(uom_name) -> str:
    name = str(uom_name or "").strip().upper()
    return _UQC_MAP.get(name, name or "OTH")


def _strip_nones(obj):
    if isinstance(obj, dict):
        return {k: _strip_nones(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nones(v) for v in obj]
    return obj

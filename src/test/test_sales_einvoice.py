"""Tests for the GST e-invoice integration (src/sales/einvoice/).

Covers the INV-01 payload builder (mapping + validation collection), the
crypto helpers (AES round-trip, RSA envelope), and the EInvoiceClient
transport behavior against a mocked httpx layer.
"""

import base64
import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.sales.einvoice import constants as C
from src.sales.einvoice.client import EInvoiceClient, EInvoiceError, _redact
from src.sales.einvoice.payload_builder import build_invoice_payload, _uqc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(mapping: dict):
    row = MagicMock()
    row._mapping = mapping
    return row


GOOD_HEADER = {
    "invoice_id": 101, "invoice_no": 42, "invoice_date": date(2026, 8, 1),
    "invoice_type": 1, "invoice_amount": 1180.0, "round_off": 0,
    "tax_amount": 180.0, "intra_inter_state": 1, "status_id": 3,
    "branch_id": 4, "party_id": 7, "billing_to_id": 9, "shipping_to_id": 9,
    "vehicle_no": None, "transporter_id": None, "transporter_name": None,
    "transporter_doc_no": None, "transporter_doc_date": None,
    "co_id": 1,
    "seller_name": "Empire Jute", "seller_gstin": "19AABCE1234F1Z5",
    "seller_address1": "Mill Road", "seller_address2": None,
    "seller_pincode": 700001, "seller_state_name": "West Bengal",
    "seller_state_code": "19", "seller_phone": "9830000000",
    "seller_email": "mill@example.com",
    "buyer_name": "Buyer Traders", "buyer_gstin": "19AABCB9876K1Z2",
    "buyer_address1": "Market St", "buyer_address2": None,
    "buyer_pincode": 700002, "buyer_state_name": "West Bengal",
    "buyer_state_code": "19", "buyer_phone": "9830000001",
    "ship_gstin": None, "ship_address1": None, "ship_address2": None,
    "ship_pincode": None, "ship_state_name": None, "ship_state_code": None,
}

GOOD_LINE = {
    "invoice_line_item_id": 1, "item_id": 5, "hsn_code": "530710",
    "quantity": 100.0, "rate": 10.0, "discount_amount": 0,
    "amount_without_tax": 1000.0, "total_amount": 1180.0,
    "item_name": "Jute Yarn 8lbs", "uom_name": "KGS",
    "tax_percentage": 18, "cgst_amount": 90.0, "sgst_amount": 90.0,
    "igst_amount": 0, "cgst_percentage": 9, "sgst_percentage": 9,
    "igst_percentage": 0, "line_tax_amount": 180.0,
}


def _db_returning(header, lines):
    db = MagicMock()
    header_result = MagicMock()
    header_result.fetchone.return_value = _row(header) if header else None
    lines_result = MagicMock()
    lines_result.fetchall.return_value = [_row(l) for l in lines]
    db.execute.side_effect = [header_result, lines_result]
    return db


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

class TestPayloadBuilder:
    def test_happy_path_builds_valid_inv01(self):
        db = _db_returning(GOOD_HEADER, [GOOD_LINE])
        payload, errors = build_invoice_payload(db, 101)
        assert errors == []
        assert payload["Version"] == "1.1"
        assert payload["TranDtls"] == {"TaxSch": "GST", "SupTyp": "B2B", "RegRev": "N", "IgstOnIntra": "N"}
        assert payload["DocDtls"] == {"Typ": "INV", "No": "42", "Dt": "01/08/2026"}
        assert payload["SellerDtls"]["Gstin"] == "19AABCE1234F1Z5"
        assert payload["SellerDtls"]["Pin"] == 700001
        assert payload["BuyerDtls"]["Pos"] == "19"
        assert len(payload["ItemList"]) == 1
        item = payload["ItemList"][0]
        assert item["HsnCd"] == "530710"
        assert item["Unit"] == "KGS"
        assert item["AssAmt"] == 1000.0
        assert item["CgstAmt"] == 90.0
        assert item["TotItemVal"] == 1180.0
        assert payload["ValDtls"]["TotInvVal"] == 1180.0
        # bill-to == ship-to → no ShipDtls block
        assert "ShipDtls" not in payload

    def test_missing_invoice_returns_error(self):
        db = _db_returning(None, [])
        payload, errors = build_invoice_payload(db, 999)
        assert payload is None
        assert "not found" in errors[0]

    def test_collects_all_validation_errors_at_once(self):
        bad_header = dict(GOOD_HEADER, seller_gstin=None, buyer_pincode=12,
                          invoice_no=None, seller_state_code=None)
        bad_line = dict(GOOD_LINE, hsn_code="ab")
        db = _db_returning(bad_header, [bad_line])
        payload, errors = build_invoice_payload(db, 101)
        assert payload is None
        joined = " | ".join(errors)
        assert "Seller GSTIN" in joined
        assert "Buyer PIN" in joined
        assert "Invoice number is empty" in joined
        assert "Seller state code" in joined
        assert "HSN code" in joined

    def test_doc_no_format_rules(self):
        header = dict(GOOD_HEADER, invoice_no="0INVALIDLEADZERO1")
        db = _db_returning(header, [GOOD_LINE])
        payload, errors = build_invoice_payload(db, 101)
        assert payload is None
        assert any("DocDtls.No" in e for e in errors)

    def test_round_off_included_in_total(self):
        header = dict(GOOD_HEADER, round_off=0.4)
        db = _db_returning(header, [GOOD_LINE])
        payload, errors = build_invoice_payload(db, 101)
        assert errors == []
        assert payload["ValDtls"]["RndOffAmt"] == 0.4
        assert payload["ValDtls"]["TotInvVal"] == 1180.4

    def test_ship_to_differs_adds_shipdtls(self):
        header = dict(GOOD_HEADER, shipping_to_id=11, ship_gstin="19AABCS1111A1Z9",
                      ship_address1="Depot Rd", ship_pincode=700099,
                      ship_state_name="West Bengal", ship_state_code="19")
        db = _db_returning(header, [GOOD_LINE])
        payload, errors = build_invoice_payload(db, 101)
        assert errors == []
        assert payload["ShipDtls"]["Gstin"] == "19AABCS1111A1Z9"
        assert payload["ShipDtls"]["Pin"] == 700099

    def test_uqc_mapping(self):
        assert _uqc("Kgs") == "KGS"
        assert _uqc("Metric Ton") == "TON"
        assert _uqc("bales") == "BAL"
        assert _uqc(None) == "OTH"
        assert _uqc("WEIRDUNIT") == "WEIRDUNIT"


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------

class TestCrypto:
    def test_aes_roundtrip(self):
        crypto = pytest.importorskip("cryptography")  # noqa: F841
        from src.sales.einvoice import crypto_utils
        key = crypto_utils.generate_app_key()
        assert len(key) == 32
        payload = json.dumps({"hello": "irp"}).encode("utf-8")
        encrypted = crypto_utils.aes_encrypt_b64(payload, key)
        assert crypto_utils.aes_decrypt(encrypted, key) == payload

    def test_rsa_encrypt_produces_b64(self):
        pytest.importorskip("cryptography")
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat,
        )
        from src.sales.einvoice import crypto_utils
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        ).decode("utf-8")
        token = crypto_utils.rsa_encrypt_b64(b"secret", pem)
        base64.b64decode(token)  # must be valid base64


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def _plain_config(**overrides):
    config = {
        "irp_provider": "NIC", "env": "SANDBOX",
        "api_base_url": "https://sandbox.example",
        "auth_path": "/auth", "generate_irn_path": "/invoice",
        "cancel_irn_path": "/cancel", "get_irn_path": "/irn/{irn}",
        "encryption_mode": "PLAIN",
        "client_id": "cid", "client_secret": "csec",
        "api_username": "user1", "api_password": "Pass@12",
        "gstin": "19AABCE1234F1Z5", "public_key_pem": None,
    }
    config.update(overrides)
    return config


class TestClient:
    def test_incomplete_config_raises_helpful_error(self):
        # IRIS ships no default base URL (console-only), so both gaps must be reported
        client = EInvoiceClient(_plain_config(client_id=None, api_base_url=None,
                                              irp_provider="IRIS"))
        with pytest.raises(EInvoiceError) as exc:
            client._require_ready()
        assert "client_id" in str(exc.value)
        assert "api_base_url" in str(exc.value)

    def test_nic_mode_requires_public_key(self):
        client = EInvoiceClient(_plain_config(encryption_mode="NIC"))
        with pytest.raises(EInvoiceError) as exc:
            client._require_ready()
        assert "public_key_pem" in str(exc.value)

    @patch("src.sales.einvoice.client.EInvoiceClient._post")
    def test_plain_auth_success_caches_token(self, mock_post):
        from src.sales.einvoice.client import _token_cache
        _token_cache._entries.clear()
        mock_post.return_value = (200, {"Status": "1", "Data": {"AuthToken": "tok123"}})
        logs = []
        client = EInvoiceClient(_plain_config(), log_sink=logs.append)
        auth = client.authenticate(force=True)
        assert auth["token"] == "tok123"
        assert logs[-1]["success"] == 1
        # second call served from cache — no extra HTTP hit
        client.authenticate()
        assert mock_post.call_count == 1

    @patch("src.sales.einvoice.client.EInvoiceClient._post")
    def test_auth_failure_raises_with_irp_error(self, mock_post):
        from src.sales.einvoice.client import _token_cache
        _token_cache._entries.clear()
        mock_post.return_value = (
            200,
            {"Status": "0", "ErrorDetails": [{"ErrorCode": "1005", "ErrorMessage": "Invalid Client-Id/Secret"}]},
        )
        logs = []
        client = EInvoiceClient(_plain_config(), log_sink=logs.append)
        with pytest.raises(EInvoiceError) as exc:
            client.authenticate(force=True)
        assert exc.value.error_code == "1005"
        assert logs[-1]["success"] == 0

    @patch("src.sales.einvoice.client.EInvoiceClient._post")
    def test_generate_irn_plain_mode(self, mock_post):
        from src.sales.einvoice.client import _token_cache
        _token_cache._entries.clear()
        mock_post.side_effect = [
            (200, {"Status": "1", "Data": {"AuthToken": "tok123"}}),
            (200, {"Status": "1", "Data": {"Irn": "a" * 64, "AckNo": 112010036563, "AckDt": "2026-08-01 11:00:00"}}),
        ]
        client = EInvoiceClient(_plain_config())
        result = client.generate_irn({"Version": "1.1"})
        assert result["Irn"] == "a" * 64
        # generate call carried the AuthToken header
        headers = mock_post.call_args_list[1][0][1]
        assert headers["AuthToken"] == "tok123"

    @patch("src.sales.einvoice.client.EInvoiceClient._post")
    def test_generate_401_clears_token_cache(self, mock_post):
        from src.sales.einvoice.client import _token_cache
        _token_cache._entries.clear()
        mock_post.side_effect = [
            (200, {"Status": "1", "Data": {"AuthToken": "tok123"}}),
            (401, {"Status": "0", "ErrorDetails": [{"ErrorCode": "AUTH", "ErrorMessage": "expired"}]}),
        ]
        client = EInvoiceClient(_plain_config())
        with pytest.raises(EInvoiceError):
            client.generate_irn({"Version": "1.1"})
        key = (client.gstin, client.env, client.provider)
        assert _token_cache.get(key) is None

    def test_redact_hides_secrets(self):
        redacted = _redact({"Password": "x", "nested": [{"client_secret": "y", "ok": 1}]})
        assert redacted["Password"] == "***"
        assert redacted["nested"][0]["client_secret"] == "***"
        assert redacted["nested"][0]["ok"] == 1

    def test_env_var_overrides_config(self, monkeypatch):
        monkeypatch.setenv(C.ENVVAR_BASE_URL, "https://override.example")
        client = EInvoiceClient(_plain_config())
        assert client.base_url == "https://override.example"

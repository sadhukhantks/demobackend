"""
Constants for the GST e-invoice (IRP) integration.

Provider defaults below follow the GSTN e-invoice standard API contract
(published for NIC at einv-apisandbox.nic.in; IRIS IRP's core APIs follow the
same standard — token valid 6 h, SEK-based AES-256/ECB/PKCS7 payload
encryption). The IRIS IRP sandbox base URL/paths are issued inside its
Developer Console after sandbox signup and MUST be confirmed there — set them
per GSTIN in einv_gstin_config (api_base_url / *_path columns) or via the
EINVOICE_* env vars. See docs/einvoice/SANDBOX_SETUP_GUIDE.md.
"""

import os

# ---------------------------------------------------------------------------
# Environments / providers
# ---------------------------------------------------------------------------
ENV_SANDBOX = "SANDBOX"
ENV_PRODUCTION = "PRODUCTION"

PROVIDER_IRIS = "IRIS"  # einvoice6.gst.gov.in
PROVIDER_NIC = "NIC"    # einvoice1.gst.gov.in / einv-apisandbox.nic.in

# Payload transport: NIC = e-invoice standard RSA/AES SEK flow; PLAIN = plain JSON over TLS
ENCRYPTION_NIC = "NIC"
ENCRYPTION_PLAIN = "PLAIN"

# ---------------------------------------------------------------------------
# IRN lifecycle statuses (einv_irn_registry.einv_status)
# ---------------------------------------------------------------------------
EINV_STATUS_PENDING = "PENDING"
EINV_STATUS_GENERATED = "GENERATED"
EINV_STATUS_CANCELLED = "CANCELLED"
EINV_STATUS_FAILED = "FAILED"

# ---------------------------------------------------------------------------
# API names (einv_api_log.api_name)
# ---------------------------------------------------------------------------
API_AUTH = "AUTH"
API_GENERATE_IRN = "GENERATE_IRN"
API_CANCEL_IRN = "CANCEL_IRN"
API_GET_IRN = "GET_IRN"

# ---------------------------------------------------------------------------
# Provider endpoint defaults.
# NIC sandbox paths are publicly documented at einv-apisandbox.nic.in.
# IRIS values ship empty on purpose — copy the exact base URL + paths from the
# IRIS IRP Developer Console (Sandbox section) into einv_gstin_config or the
# EINVOICE_* env vars; the client refuses to call an empty base URL.
# ---------------------------------------------------------------------------
PROVIDER_DEFAULTS = {
    PROVIDER_NIC: {
        ENV_SANDBOX: {
            "base_url": "https://einv-apisandbox.nic.in",
            "auth_path": "/eivital/v1.04/auth",
            "generate_irn_path": "/eicore/v1.03/Invoice",
            "cancel_irn_path": "/eicore/v1.03/Invoice/Cancel",
            "get_irn_path": "/eicore/v1.03/Invoice/irn/{irn}",
        },
        ENV_PRODUCTION: {
            # Production NIC access is via GSP/direct whitelisting - fill from onboarding letter.
            "base_url": "",
            "auth_path": "/eivital/v1.04/auth",
            "generate_irn_path": "/eicore/v1.03/Invoice",
            "cancel_irn_path": "/eicore/v1.03/Invoice/Cancel",
            "get_irn_path": "/eicore/v1.03/Invoice/irn/{irn}",
        },
    },
    PROVIDER_IRIS: {
        ENV_SANDBOX: {
            "base_url": "",  # from IRIS IRP Developer Console after sandbox signup
            "auth_path": "",
            "generate_irn_path": "",
            "cancel_irn_path": "",
            "get_irn_path": "",
        },
        ENV_PRODUCTION: {
            "base_url": "",
            "auth_path": "",
            "generate_irn_path": "",
            "cancel_irn_path": "",
            "get_irn_path": "",
        },
    },
}

# Env-var overrides (highest precedence, then einv_gstin_config row, then defaults above).
ENVVAR_BASE_URL = "EINVOICE_BASE_URL"
ENVVAR_AUTH_PATH = "EINVOICE_AUTH_PATH"
ENVVAR_GENERATE_PATH = "EINVOICE_GENERATE_IRN_PATH"
ENVVAR_CANCEL_PATH = "EINVOICE_CANCEL_IRN_PATH"
ENVVAR_GET_PATH = "EINVOICE_GET_IRN_PATH"
ENVVAR_CLIENT_ID = "EINVOICE_CLIENT_ID"
ENVVAR_CLIENT_SECRET = "EINVOICE_CLIENT_SECRET"
ENVVAR_API_USERNAME = "EINVOICE_API_USERNAME"
ENVVAR_API_PASSWORD = "EINVOICE_API_PASSWORD"
ENVVAR_PUBLIC_KEY_PATH = "EINVOICE_PUBLIC_KEY_PATH"


def env_override(name: str) -> str | None:
    """Return a non-empty env var value or None."""
    value = os.environ.get(name, "").strip()
    return value or None


# ---------------------------------------------------------------------------
# INV-01 schema constants (Version 1.1)
# ---------------------------------------------------------------------------
EINV_SCHEMA_VERSION = "1.1"
TAX_SCHEME_GST = "GST"

# Supply type codes (TranDtls.SupTyp)
SUP_TYP_B2B = "B2B"        # registered buyer, domestic
SUP_TYP_SEZWP = "SEZWP"    # SEZ with payment of tax
SUP_TYP_SEZWOP = "SEZWOP"  # SEZ without payment of tax
SUP_TYP_EXPWP = "EXPWP"    # export with payment of tax
SUP_TYP_EXPWOP = "EXPWOP"  # export without payment of tax
SUP_TYP_DEXP = "DEXP"      # deemed export

# Document types (DocDtls.Typ)
DOC_TYP_INVOICE = "INV"
DOC_TYP_CREDIT_NOTE = "CRN"
DOC_TYP_DEBIT_NOTE = "DBN"

# DocDtls.No rules: max 16 chars, alphanumeric plus / and -, no leading 0/./-
DOC_NO_MAX_LEN = 16

# Dates in the INV-01 payload use dd/mm/yyyy
EINV_DATE_FORMAT = "%d/%m/%Y"

# Auth token lifetime per the e-invoice standard: 6 hours. Refresh a little early.
TOKEN_LIFETIME_SECONDS = 6 * 60 * 60
TOKEN_REFRESH_MARGIN_SECONDS = 15 * 60

# IRN cancellation window after generation (per GSTN rules)
CANCEL_WINDOW_HOURS = 24

# Cancel reason codes accepted by the IRP
CANCEL_REASON_DUPLICATE = "1"
CANCEL_REASON_DATA_ENTRY_MISTAKE = "2"
CANCEL_REASON_ORDER_CANCELLED = "3"
CANCEL_REASON_OTHERS = "4"

# Keys that must never be written to einv_api_log.request_json
REDACT_KEYS = frozenset({"password", "Password", "client_secret", "client-secret", "AppKey", "Sek", "app_key"})

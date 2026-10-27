"""Minimal sync client for the WhatsApp Cloud API (Meta Graph API).

Credentials (read at CALL time, so a rotated token needs no restart) come from
the tenant DB: the active row of `whatsapp_config` (phone_number_id,
access_token, api_version). Every send function takes the tenant session as `db`.

Fallback when no `db` is passed or the tenant has no active row:
  WHATSAPP_ACCESS_TOKEN      required
  WHATSAPP_PHONE_NUMBER_ID   required (the sender's phone-number id, not the number)
  WHATSAPP_API_VERSION       optional, default v23.0

These are taken from the process environment. If a required one is missing there,
the server-only file /root/projects/vowerp3/whatsapp.env (path overridable with
WHATSAPP_ENV_FILE) is read with python-dotenv. Environment values win over the file.

Free-form messages (send_text / send_document) are only delivered inside the
24-hour customer-service window, i.e. after the recipient has messaged the
business number. Business-initiated messages outside it must use an approved
template (send_template).

The access token is never put into an exception message or a log line.
"""

import logging
import os
import re

import httpx
from dotenv import dotenv_values

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v23.0"
DEFAULT_ENV_FILE = "/root/projects/vowerp3/whatsapp.env"
REQUIRED_VARS = ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")
TIMEOUT_SECONDS = 60


class WhatsAppConfigError(Exception):
    """Credentials are not configured. Message names the missing variable(s)."""


class WhatsAppApiError(Exception):
    """Meta rejected the call (non-2xx) or the request never got through.

    Carries the HTTP status (None for a network failure) and Meta's error fields.
    The message is safe to surface to an API caller.
    """

    def __init__(self, message: str, status_code: int | None = None, error: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error = error or {}
        self.code = self.error.get("code")
        self.error_subcode = self.error.get("error_subcode")
        self.fbtrace_id = self.error.get("fbtrace_id")
        self.meta_message = self.error.get("message")


def _load_db_config(db) -> dict | None:
    """The tenant's active whatsapp_config row, or None when there is none."""
    from src.common.whatsapp.query import get_active_config

    row = db.execute(get_active_config()).mappings().first()
    if not row:
        return None
    token = (row.get("access_token") or "").strip()
    phone_number_id = (row.get("phone_number_id") or "").strip()
    missing = [n for n, v in (("access_token", token), ("phone_number_id", phone_number_id)) if not v]
    if missing:
        raise WhatsAppConfigError(
            "WhatsApp is not configured: whatsapp_config row has empty " + ", ".join(missing) + "."
        )
    return {
        "token": token,
        "phone_number_id": phone_number_id,
        "api_version": (row.get("api_version") or DEFAULT_API_VERSION).strip(),
    }


def _load_config(db=None) -> dict:
    """Resolve credentials now: tenant whatsapp_config row, else environment, else the env file."""
    if db is not None:
        cfg = _load_db_config(db)
        if cfg:
            return cfg
    values = {k: os.environ.get(k) for k in (*REQUIRED_VARS, "WHATSAPP_API_VERSION")}
    env_file = os.environ.get("WHATSAPP_ENV_FILE") or DEFAULT_ENV_FILE
    if any(not values[k] for k in REQUIRED_VARS) and os.path.isfile(env_file):
        file_values = dotenv_values(env_file)
        for key in values:
            if not values[key]:
                values[key] = file_values.get(key)
    missing = [k for k in REQUIRED_VARS if not (values[k] or "").strip()]
    if missing:
        raise WhatsAppConfigError(
            "WhatsApp is not configured: missing " + ", ".join(missing)
            + ". Add an active row to the tenant's whatsapp_config table"
            + f" (or set them in the environment / {env_file})."
        )
    return {
        "token": values["WHATSAPP_ACCESS_TOKEN"].strip(),
        "phone_number_id": values["WHATSAPP_PHONE_NUMBER_ID"].strip(),
        "api_version": (values["WHATSAPP_API_VERSION"] or DEFAULT_API_VERSION).strip(),
    }


def is_configured(db=None) -> bool:
    try:
        _load_config(db)
        return True
    except WhatsAppConfigError:
        return False


def normalize_phone(raw) -> str:
    """Digits only, with country code, no '+'.

    "+91 84201 10041" -> "918420110041"; a bare 10-digit number (or 0 + 10
    digits) is taken as Indian and gets "91". Raises ValueError otherwise-invalid.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    if not 11 <= len(digits) <= 15:
        raise ValueError(f"Invalid phone number: {raw!r} (need country code + number)")
    return digits


def _http_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def _post(path: str, *, json: dict | None = None, data: dict | None = None, files: dict | None = None, db=None) -> dict:
    cfg = _load_config(db)
    url = f"{GRAPH_BASE_URL}/{cfg['api_version']}/{cfg['phone_number_id']}/{path}"
    headers = {"Authorization": f"Bearer {cfg['token']}"}
    try:
        with _http_client() as http:
            resp = http.post(url, headers=headers, json=json, data=data, files=files)
    except httpx.HTTPError as exc:
        # str(exc) holds at most the URL (no token: that travels in a header)
        raise WhatsAppApiError(f"WhatsApp request failed ({path}): {type(exc).__name__}: {exc}") from exc

    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not 200 <= resp.status_code < 300:
        err = body.get("error") if isinstance(body, dict) else None
        if not isinstance(err, dict):
            err = {"message": (resp.text or "")[:300]}
        detail = (err.get("error_data") or {}).get("details") if isinstance(err.get("error_data"), dict) else None
        msg = (
            f"WhatsApp API error (HTTP {resp.status_code}, code {err.get('code')}"
            f"{', subcode ' + str(err.get('error_subcode')) if err.get('error_subcode') else ''}): "
            f"{err.get('message')}"
            f"{' — ' + detail if detail else ''}"
            f"{' [fbtrace_id ' + str(err.get('fbtrace_id')) + ']' if err.get('fbtrace_id') else ''}"
        )
        logger.warning("%s", msg)
        raise WhatsAppApiError(msg, status_code=resp.status_code, error=err)
    return body


def message_id(response: dict) -> str | None:
    """The wamid of a /messages response, if present."""
    try:
        return response["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


def send_text(to, body: str, db=None) -> dict:
    return _post("messages", db=db, json={
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_phone(to),
        "type": "text",
        "text": {"preview_url": False, "body": body},
    })


def upload_media(pdf_bytes: bytes, filename: str, mime: str = "application/pdf", db=None) -> str:
    """Upload a file to Meta and return its media id (valid for 30 days)."""
    body = _post(
        "media",
        data={"messaging_product": "whatsapp", "type": mime},
        files={"file": (filename, pdf_bytes, mime)},
        db=db,
    )
    media_id = body.get("id") if isinstance(body, dict) else None
    if not media_id:
        raise WhatsAppApiError("WhatsApp media upload returned no media id", status_code=200)
    return str(media_id)


def send_document(to, pdf_bytes: bytes, filename: str, caption: str | None = None, db=None) -> dict:
    """Free-form document message: upload the PDF, then send it by media id."""
    to = normalize_phone(to)
    document = {"id": upload_media(pdf_bytes, filename, db=db), "filename": filename}
    if caption:
        document["caption"] = caption
    return _post("messages", db=db, json={
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "document",
        "document": document,
    })


def send_template(
    to,
    template_name: str,
    language_code: str = "en",
    body_params: list[str] | None = None,
    document: tuple[bytes, str] | None = None,
    db=None,
) -> dict:
    """Template message. `document` = (pdf_bytes, filename) fills a DOCUMENT header."""
    to = normalize_phone(to)
    components = []
    if document is not None:
        pdf_bytes, filename = document
        components.append({
            "type": "header",
            "parameters": [{
                "type": "document",
                "document": {"id": upload_media(pdf_bytes, filename, db=db), "filename": filename},
            }],
        })
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        })
    template = {"name": template_name, "language": {"code": language_code or "en"}}
    if components:
        template["components"] = components
    return _post("messages", db=db, json={
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": template,
    })

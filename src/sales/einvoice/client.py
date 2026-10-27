"""
HTTP client for the IRP e-invoice APIs (sandbox-first).

Transport modes (einv_gstin_config.encryption_mode):
  NIC   — the e-invoice standard flow: auth request carries the password and a
          random AppKey RSA-encrypted with the IRP public key; the auth
          response returns AuthToken + Sek (AES key, itself AES-encrypted with
          the AppKey); every later payload is AES-256/ECB/PKCS7 encrypted with
          the Sek. IRIS IRP core APIs follow this same standard.
  PLAIN — plain JSON over TLS, for IRP endpoints that accept it.

Token cache is process-local, keyed by (gstin, env, provider), guarded by a
lock — handlers run concurrently in FastAPI's threadpool. Cached entries carry
their own expiry (6 h per the standard, refreshed 15 min early).

Every call is logged to einv_api_log with secrets redacted.
"""

import json
import threading
import time

import httpx

from src.sales.einvoice import constants as C
from src.sales.einvoice import crypto_utils as crypto


class EInvoiceError(Exception):
    """Raised for IRP-side failures; message is safe to surface to the API caller."""

    def __init__(self, message: str, error_code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.http_status = http_status


class _TokenCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[tuple, dict] = {}

    def get(self, key: tuple) -> dict | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry["expires_at"] > time.time() + C.TOKEN_REFRESH_MARGIN_SECONDS:
                return entry
            return None

    def put(self, key: tuple, token: str, sek: bytes | None):
        with self._lock:
            self._entries[key] = {
                "token": token,
                "sek": sek,
                "expires_at": time.time() + C.TOKEN_LIFETIME_SECONDS,
            }

    def clear(self, key: tuple):
        with self._lock:
            self._entries.pop(key, None)


_token_cache = _TokenCache()


def _redact(obj):
    if isinstance(obj, dict):
        return {
            k: ("***" if k in C.REDACT_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class EInvoiceClient:
    """One client per resolved credential config (einv_gstin_config row + env overrides)."""

    def __init__(self, config: dict, log_sink=None):
        """
        config keys: gstin, irp_provider, env, api_base_url, auth_path,
        generate_irn_path, cancel_irn_path, get_irn_path, encryption_mode,
        client_id, client_secret, api_username, api_password, public_key_pem.

        log_sink: optional callable(dict) that persists one einv_api_log row —
        keeps the client free of DB session ownership.
        """
        self.config = config
        self.log_sink = log_sink
        self.provider = (config.get("irp_provider") or C.PROVIDER_IRIS).upper()
        self.env = (config.get("env") or C.ENV_SANDBOX).upper()
        defaults = C.PROVIDER_DEFAULTS.get(self.provider, {}).get(self.env, {})

        self.base_url = (
            C.env_override(C.ENVVAR_BASE_URL)
            or config.get("api_base_url")
            or defaults.get("base_url")
            or ""
        ).rstrip("/")
        self.paths = {
            C.API_AUTH: C.env_override(C.ENVVAR_AUTH_PATH) or config.get("auth_path") or defaults.get("auth_path") or "",
            C.API_GENERATE_IRN: C.env_override(C.ENVVAR_GENERATE_PATH) or config.get("generate_irn_path") or defaults.get("generate_irn_path") or "",
            C.API_CANCEL_IRN: C.env_override(C.ENVVAR_CANCEL_PATH) or config.get("cancel_irn_path") or defaults.get("cancel_irn_path") or "",
            C.API_GET_IRN: C.env_override(C.ENVVAR_GET_PATH) or config.get("get_irn_path") or defaults.get("get_irn_path") or "",
        }
        self.client_id = C.env_override(C.ENVVAR_CLIENT_ID) or config.get("client_id")
        self.client_secret = C.env_override(C.ENVVAR_CLIENT_SECRET) or config.get("client_secret")
        self.username = C.env_override(C.ENVVAR_API_USERNAME) or config.get("api_username")
        self.password = C.env_override(C.ENVVAR_API_PASSWORD) or config.get("api_password")
        self.gstin = config.get("gstin")
        self.encryption_mode = (config.get("encryption_mode") or C.ENCRYPTION_NIC).upper()
        self.public_key_pem = config.get("public_key_pem")
        if not self.public_key_pem:
            key_path = C.env_override(C.ENVVAR_PUBLIC_KEY_PATH)
            if key_path:
                with open(key_path, "r", encoding="utf-8") as fh:
                    self.public_key_pem = fh.read()

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    def _require_ready(self):
        missing = []
        if not self.base_url:
            missing.append("api_base_url (copy from the IRP developer console — see docs/einvoice/)")
        if not self.paths[C.API_AUTH]:
            missing.append("auth_path")
        if not self.client_id:
            missing.append("client_id")
        if not self.client_secret:
            missing.append("client_secret")
        if not self.username:
            missing.append("api_username")
        if not self.password:
            missing.append("api_password")
        if not self.gstin:
            missing.append("gstin")
        if self.encryption_mode == C.ENCRYPTION_NIC and not self.public_key_pem:
            missing.append("public_key_pem (download from the IRP sandbox portal)")
        if missing:
            raise EInvoiceError(
                "e-invoice config incomplete: missing " + ", ".join(missing)
            )

    def _log(self, api_name: str, *, http_status=None, success=0, error_code=None,
             error_message=None, request_json=None, response_json=None, duration_ms=None):
        if self.log_sink is None:
            return
        self.log_sink({
            "api_name": api_name,
            "http_status": http_status,
            "success": int(success),
            "error_code": error_code,
            "error_message": (error_message or "")[:1000] or None,
            "request_json": json.dumps(_redact(request_json)) if request_json is not None else None,
            "response_json": json.dumps(response_json) if response_json is not None else None,
            "duration_ms": duration_ms,
        })

    def _http_headers(self, auth_token: str | None = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "client_id": self.client_id or "",
            "client_secret": self.client_secret or "",
            "gstin": self.gstin or "",
            "user_name": self.username or "",
        }
        if auth_token:
            headers["AuthToken"] = auth_token
        return headers

    def _post(self, url: str, headers: dict, body: dict) -> tuple[int, dict]:
        with httpx.Client(timeout=60) as http:
            response = http.post(url, headers=headers, json=body)
        try:
            parsed = response.json()
        except ValueError:
            parsed = {"raw": response.text[:2000]}
        return response.status_code, parsed

    def _get(self, url: str, headers: dict) -> tuple[int, dict]:
        with httpx.Client(timeout=60) as http:
            response = http.get(url, headers=headers)
        try:
            parsed = response.json()
        except ValueError:
            parsed = {"raw": response.text[:2000]}
        return response.status_code, parsed

    @staticmethod
    def _extract_error(parsed: dict) -> tuple[str | None, str]:
        """IRP errors arrive as ErrorDetails: [{ErrorCode, ErrorMessage}, ...]."""
        details = parsed.get("ErrorDetails") or parsed.get("errorDetails") or []
        if isinstance(details, list) and details:
            first = details[0] or {}
            code = str(first.get("ErrorCode") or first.get("error_code") or "") or None
            messages = "; ".join(
                str(d.get("ErrorMessage") or d.get("error_message") or "") for d in details if d
            )
            return code, messages or json.dumps(parsed)[:500]
        return None, json.dumps(parsed)[:500]

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def authenticate(self, force: bool = False) -> dict:
        """Return {token, sek} — cached for 6 h per the e-invoice standard."""
        self._require_ready()
        cache_key = (self.gstin, self.env, self.provider)
        if not force:
            cached = _token_cache.get(cache_key)
            if cached:
                return cached

        url = self.base_url + self.paths[C.API_AUTH]
        started = time.monotonic()

        if self.encryption_mode == C.ENCRYPTION_NIC:
            app_key = crypto.generate_app_key()
            auth_payload = {
                "UserName": self.username,
                "Password": crypto.rsa_encrypt_b64(self.password.encode("utf-8"), self.public_key_pem),
                "AppKey": crypto.rsa_encrypt_b64(app_key, self.public_key_pem),
                "ForceRefreshAccessToken": bool(force),
            }
            body = {"Data": crypto.b64_of(json.dumps(auth_payload).encode("utf-8"))}
            log_request = {"UserName": self.username, "Password": "***", "AppKey": "***",
                           "ForceRefreshAccessToken": bool(force)}
        else:
            app_key = None
            body = {
                "UserName": self.username,
                "Password": self.password,
                "ForceRefreshAccessToken": bool(force),
            }
            log_request = _redact(body)

        try:
            status, parsed = self._post(url, self._http_headers(), body)
        except httpx.HTTPError as exc:
            self._log(C.API_AUTH, error_message=f"network error: {exc}", request_json=log_request,
                      duration_ms=int((time.monotonic() - started) * 1000))
            raise EInvoiceError(f"IRP auth network error: {exc}") from exc

        duration = int((time.monotonic() - started) * 1000)
        data = parsed.get("Data") or parsed.get("data") or {}
        if status == 200 and str(parsed.get("Status", parsed.get("status", ""))) in ("1", "Success", "success", "200"):
            token = data.get("AuthToken") or data.get("authToken")
            sek = None
            sek_b64 = data.get("Sek") or data.get("sek")
            if sek_b64 and app_key is not None:
                sek = crypto.decrypt_sek(sek_b64, app_key)
            if not token:
                self._log(C.API_AUTH, http_status=status, error_message="no AuthToken in response",
                          request_json=log_request, response_json=parsed, duration_ms=duration)
                raise EInvoiceError("IRP auth succeeded but returned no AuthToken", http_status=status)
            _token_cache.put(cache_key, token, sek)
            self._log(C.API_AUTH, http_status=status, success=1, request_json=log_request,
                      response_json={"AuthToken": "***", "TokenExpiry": data.get("TokenExpiry")},
                      duration_ms=duration)
            return {"token": token, "sek": sek}

        error_code, error_message = self._extract_error(parsed)
        self._log(C.API_AUTH, http_status=status, error_code=error_code, error_message=error_message,
                  request_json=log_request, response_json=parsed, duration_ms=duration)
        raise EInvoiceError(f"IRP auth failed: {error_message}", error_code=error_code, http_status=status)

    # ------------------------------------------------------------------
    # Core APIs
    # ------------------------------------------------------------------
    def _call_encrypted(self, api_name: str, url: str, payload: dict) -> dict:
        """POST a core API using the transport dictated by encryption_mode."""
        auth = self.authenticate()
        started = time.monotonic()

        if self.encryption_mode == C.ENCRYPTION_NIC:
            if not auth.get("sek"):
                raise EInvoiceError("No SEK from auth — cannot encrypt payload in NIC mode")
            body = {"Data": crypto.aes_encrypt_b64(json.dumps(payload).encode("utf-8"), auth["sek"])}
        else:
            body = payload

        try:
            status, parsed = self._post(url, self._http_headers(auth["token"]), body)
        except httpx.HTTPError as exc:
            self._log(api_name, error_message=f"network error: {exc}", request_json=payload,
                      duration_ms=int((time.monotonic() - started) * 1000))
            raise EInvoiceError(f"IRP network error: {exc}") from exc

        duration = int((time.monotonic() - started) * 1000)

        # 401/403 → token likely stale; drop cache so the next attempt re-auths
        if status in (401, 403):
            _token_cache.clear((self.gstin, self.env, self.provider))

        if status == 200 and str(parsed.get("Status", parsed.get("status", ""))) in ("1", "Success", "success", "200"):
            data = parsed.get("Data") or parsed.get("data")
            if isinstance(data, str) and self.encryption_mode == C.ENCRYPTION_NIC:
                data = json.loads(crypto.aes_decrypt(data, auth["sek"]).decode("utf-8"))
            self._log(api_name, http_status=status, success=1, request_json=payload,
                      response_json=data, duration_ms=duration)
            return data if isinstance(data, dict) else {"data": data}

        error_code, error_message = self._extract_error(parsed)
        self._log(api_name, http_status=status, error_code=error_code, error_message=error_message,
                  request_json=payload, response_json=parsed, duration_ms=duration)
        raise EInvoiceError(f"IRP call failed: {error_message}", error_code=error_code, http_status=status)

    def generate_irn(self, invoice_payload: dict) -> dict:
        """Submit an INV-01 payload; returns {Irn, AckNo, AckDt, SignedInvoice, SignedQRCode, Status, ...}."""
        self._require_ready()
        if not self.paths[C.API_GENERATE_IRN]:
            raise EInvoiceError("generate_irn_path not configured — copy it from the IRP developer console")
        url = self.base_url + self.paths[C.API_GENERATE_IRN]
        return self._call_encrypted(C.API_GENERATE_IRN, url, invoice_payload)

    def cancel_irn(self, irn: str, reason_code: str, remarks: str) -> dict:
        """Cancel an IRN (allowed within 24 h of generation per GSTN rules)."""
        self._require_ready()
        if not self.paths[C.API_CANCEL_IRN]:
            raise EInvoiceError("cancel_irn_path not configured — copy it from the IRP developer console")
        url = self.base_url + self.paths[C.API_CANCEL_IRN]
        payload = {"Irn": irn, "CnlRsn": reason_code, "CnlRem": remarks}
        return self._call_encrypted(C.API_CANCEL_IRN, url, payload)

    def get_irn(self, irn: str) -> dict:
        """Fetch IRN details from the IRP."""
        self._require_ready()
        if not self.paths[C.API_GET_IRN]:
            raise EInvoiceError("get_irn_path not configured — copy it from the IRP developer console")
        auth = self.authenticate()
        url = self.base_url + self.paths[C.API_GET_IRN].format(irn=irn)
        started = time.monotonic()
        try:
            status, parsed = self._get(url, self._http_headers(auth["token"]))
        except httpx.HTTPError as exc:
            self._log(C.API_GET_IRN, error_message=f"network error: {exc}",
                      duration_ms=int((time.monotonic() - started) * 1000))
            raise EInvoiceError(f"IRP network error: {exc}") from exc
        duration = int((time.monotonic() - started) * 1000)
        if status == 200 and str(parsed.get("Status", parsed.get("status", ""))) in ("1", "Success", "success", "200"):
            data = parsed.get("Data") or parsed.get("data")
            if isinstance(data, str) and self.encryption_mode == C.ENCRYPTION_NIC and auth.get("sek"):
                data = json.loads(crypto.aes_decrypt(data, auth["sek"]).decode("utf-8"))
            self._log(C.API_GET_IRN, http_status=status, success=1, response_json=data, duration_ms=duration)
            return data if isinstance(data, dict) else {"data": data}
        error_code, error_message = self._extract_error(parsed)
        self._log(C.API_GET_IRN, http_status=status, error_code=error_code,
                  error_message=error_message, response_json=parsed, duration_ms=duration)
        raise EInvoiceError(f"IRP get-IRN failed: {error_message}", error_code=error_code, http_status=status)

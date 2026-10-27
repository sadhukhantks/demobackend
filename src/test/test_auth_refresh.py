"""Silent access-token refresh + logout.

Regression tests for the phone "session expired and did not renew" bug:

* an expired access token must be reissued from the *tenant* user_mst refresh
  token for portal users (verify-session used to look only in con_user_master);
* the reissued cookie must be persistent (Max-Age) so a phone-browser restart
  does not drop it;
* ``verify_access_token`` must refresh too, not just decode;
* console tokens keep their org claims across a refresh;
* ``/logout`` expires the httpOnly cookies.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient

from src.authorization import utils as auth_utils
from src.authorization.utils import (
    ALGORITHM,
    AUTH_COOKIE_MAX_AGE_SECONDS,
    SECRET_KEY,
    create_access_token,
    create_refresh_token,
    get_current_user_with_refresh,
    verify_access_token,
)
from src.main import app

client = TestClient(app)

EXPIRED = timedelta(minutes=-5)


def _expired_token(payload: dict) -> str:
    return create_access_token(payload, expires_delta=EXPIRED)


def _cookie_headers(response: Response) -> list[str]:
    return [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]


def _access_cookie(headers: list[str]) -> str:
    matches = [h for h in headers if h.startswith("access_token=")]
    assert len(matches) == 1, headers
    return matches[0]


def _cookie_value(cookie_header: str) -> str:
    return cookie_header.split(";", 1)[0].split("=", 1)[1]


# --------------------------------------------------------------------------
# get_current_user_with_refresh / verify_access_token
# --------------------------------------------------------------------------


def test_valid_token_is_returned_without_touching_db():
    token = create_access_token({"user_id": 7, "type": "portal"})
    response = Response()
    with patch.object(auth_utils, "_fetch_refresh_token") as fetch:
        payload = get_current_user_with_refresh(MagicMock(), response, token)
    fetch.assert_not_called()
    assert payload["user_id"] == 7
    assert _cookie_headers(response) == []


def test_expired_portal_token_refreshes_from_tenant_and_sets_persistent_cookie():
    token = _expired_token({"user_id": 7, "type": "portal"})
    response = Response()
    request = MagicMock()
    with patch.object(
        auth_utils, "_fetch_refresh_token", return_value=create_refresh_token({"user_id": 7})
    ) as fetch:
        payload = get_current_user_with_refresh(request, response, token)

    # portal tokens must be looked up with token_type == "portal" (tenant user_mst)
    fetch.assert_called_once_with(7, "portal", request)
    assert payload == {"user_id": 7, "type": "portal"}

    cookie = _access_cookie(_cookie_headers(response))
    assert f"Max-Age={AUTH_COOKIE_MAX_AGE_SECONDS}" in cookie
    assert "HttpOnly" in cookie
    new_payload = jwt.decode(_cookie_value(cookie), SECRET_KEY, algorithms=[ALGORITHM])
    assert new_payload["user_id"] == 7
    assert new_payload["type"] == "portal"


def test_expired_console_token_keeps_org_claims():
    token = _expired_token({"user_id": 501, "con_org_id": 3, "subdomain": "dev3"})
    response = Response()
    request = MagicMock()
    with patch.object(
        auth_utils, "_fetch_refresh_token", return_value=create_refresh_token({"user_id": 501})
    ) as fetch:
        payload = get_current_user_with_refresh(request, response, token)

    fetch.assert_called_once_with(501, None, request)
    assert payload == {"user_id": 501, "con_org_id": 3, "subdomain": "dev3"}
    new_payload = jwt.decode(
        _cookie_value(_access_cookie(_cookie_headers(response))), SECRET_KEY, algorithms=[ALGORITHM]
    )
    assert new_payload["con_org_id"] == 3
    assert new_payload["subdomain"] == "dev3"


def test_expired_token_without_stored_refresh_token_is_rejected():
    token = _expired_token({"user_id": 7, "type": "portal"})
    with patch.object(auth_utils, "_fetch_refresh_token", return_value=None):
        with pytest.raises(HTTPException) as exc:
            get_current_user_with_refresh(MagicMock(), Response(), token)
    assert exc.value.status_code == 401


def test_expired_token_with_expired_refresh_token_is_rejected():
    token = _expired_token({"user_id": 7, "type": "portal"})
    stale = create_refresh_token({"user_id": 7}, expires_delta=EXPIRED)
    with patch.object(auth_utils, "_fetch_refresh_token", return_value=stale):
        with pytest.raises(HTTPException) as exc:
            get_current_user_with_refresh(MagicMock(), Response(), token)
    assert exc.value.status_code == 401
    assert exc.value.detail == "Refresh token expired"


def test_missing_cookie_is_403():
    with pytest.raises(HTTPException) as exc:
        get_current_user_with_refresh(MagicMock(), Response(), None)
    assert exc.value.status_code == 403


def test_verify_access_token_refreshes_like_get_current_user_with_refresh():
    token = _expired_token({"user_id": 7, "type": "portal"})
    response = Response()
    with patch.object(
        auth_utils, "_fetch_refresh_token", return_value=create_refresh_token({"user_id": 7})
    ):
        payload = verify_access_token(MagicMock(), response, token)
    assert payload["user_id"] == 7
    assert _access_cookie(_cookie_headers(response))


# --------------------------------------------------------------------------
# /api/authRoutes/verify-session
# --------------------------------------------------------------------------


def test_verify_session_refreshes_portal_token_from_tenant():
    token = _expired_token({"user_id": 7, "type": "portal"})
    with patch.object(
        auth_utils, "_fetch_refresh_token", return_value=create_refresh_token({"user_id": 7})
    ) as fetch:
        res = client.get("/api/authRoutes/verify-session", cookies={"access_token": token})

    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True}
    fetch.assert_called_once()
    assert fetch.call_args.args[:2] == (7, "portal")
    assert "access_token" in res.cookies
    assert res.cookies["access_token"] != token


def test_verify_session_console_org_mismatch_is_401_and_sets_no_cookie():
    token = _expired_token({"user_id": 501, "con_org_id": 3, "subdomain": "dev3"})
    with patch.object(
        auth_utils, "_fetch_refresh_token", return_value=create_refresh_token({"user_id": 501})
    ):
        res = client.get(
            "/api/authRoutes/verify-session",
            cookies={"access_token": token},
            headers={"X-Subdomain": "sls"},
        )
    assert res.status_code == 401
    assert "access_token" not in res.cookies


def test_verify_session_without_cookie_is_401():
    # fresh client: the module-level one keeps cookies set by earlier tests
    res = TestClient(app).get("/api/authRoutes/verify-session")
    assert res.status_code == 401


# --------------------------------------------------------------------------
# /api/authRoutes/logout
# --------------------------------------------------------------------------


def test_logout_expires_auth_cookies():
    res = client.post("/api/authRoutes/logout")
    assert res.status_code == 200
    set_cookies = res.headers.get_list("set-cookie")
    names = {c.split("=", 1)[0] for c in set_cookies}
    assert {"access_token", "portal_permission_token"} <= names
    for c in set_cookies:
        assert "Max-Age=0" in c

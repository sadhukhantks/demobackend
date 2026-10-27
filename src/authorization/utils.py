"""JWT utility helpers used across authentication flows."""

from datetime import datetime, timedelta, timezone
import os
from typing import Any, Dict, Optional

import jwt
from fastapi import Cookie, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

# Password hashing context shared across auth flows
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT configuration
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))
# The access-token cookie deliberately outlives the JWT inside it: an expired JWT is
# what lets us find the user's stored refresh token and reissue silently. Without a
# max-age it is a session cookie, which mobile browsers drop whenever the OS kills the
# browser, and then nothing is left to refresh from.
AUTH_COOKIE_MAX_AGE_SECONDS = REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
AUTH_COOKIE_NAMES = ("access_token", "portal_permission_token")
# Claims copied from an expired access token into its replacement.
_TOKEN_CONTEXT_CLAIMS = ("type", "con_org_id", "subdomain")

security = HTTPBearer(auto_error=False)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = _now_utc() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = _now_utc() + (expires_delta or timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def decode_access_token(token: str) -> Dict[str, Any]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:  # pragma: no cover - fast error path
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:  # pragma: no cover - fast error path
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    if "sub" not in payload and "user_id" not in payload:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    return payload


def verify_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> Dict[str, Any]:
    if not credentials:
        raise HTTPException(status_code=403, detail="Not authenticated")
    return decode_access_token(credentials.credentials)


def verify_access_token(
    request: Request,
    response: Response,
    access_token: Optional[str] = Cookie(None, alias="access_token"),
) -> Dict[str, Any]:
    """Cookie-auth dependency.

    Historically this only decoded the cookie, so any endpoint using it returned
    401 the moment the 30-minute access token lapsed even though the user's
    refresh token was still valid. It now behaves exactly like
    ``get_current_user_with_refresh`` so every cookie-protected endpoint
    silently reissues the access token.
    """
    return get_current_user_with_refresh(request, response, access_token)


def _refresh_cookie_settings() -> Dict[str, Any]:
    env_value = os.getenv("ENV", "development")
    return {
        "secure": env_value == "production",
        "domain": ".vowerp.co.in" if env_value == "production" else None,
        "samesite": "None" if env_value == "production" else "Lax",
    }


def set_auth_cookie(response: Response, key: str, value: str) -> None:
    """Set an httpOnly auth cookie that persists for the refresh-token lifetime."""
    cfg = _refresh_cookie_settings()
    response.set_cookie(
        key=key,
        value=value,
        max_age=AUTH_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=cfg["secure"],
        samesite=cfg["samesite"],
        path="/",
        domain=cfg["domain"],
    )


def set_access_token_cookie(response: Response, token: str) -> None:
    set_auth_cookie(response, "access_token", token)


def clear_auth_cookies(response: Response) -> None:
    """Expire every auth cookie for this browser (used by logout)."""
    cfg = _refresh_cookie_settings()
    for key in AUTH_COOKIE_NAMES + ("refresh_token",):
        response.delete_cookie(key=key, path="/", domain=cfg["domain"])


def _fetch_refresh_token(
    user_id: int,
    token_type: Optional[str],
    request: Request,
) -> Optional[str]:
    from src.config.db import (
        Session as DBSession,
        default_engine,
        extract_subdomain_from_request,
        get_tenant_engine,
    )

    if token_type == "portal":
        subdomain = extract_subdomain_from_request(request)
        tenant_db = subdomain if subdomain else None
        if not tenant_db or tenant_db == "default":
            return None

        TenantSession = sessionmaker(autocommit=False, autoflush=False, bind=get_tenant_engine(tenant_db))

        with TenantSession() as session:
            row = session.execute(
                text(
                    "SELECT refresh_token FROM user_mst "
                    "WHERE user_id = :user_id AND active = 1"
                ),
                {"user_id": user_id},
            ).fetchone()
        return row[0] if row and row[0] else None

    with DBSession(default_engine) as session:
        row = session.execute(
            text(
                "SELECT refresh_token FROM con_user_master "
                "WHERE con_user_id = :user_id AND active = 1"
            ),
            {"user_id": user_id},
        ).fetchone()
    return row[0] if row and row[0] else None


def _decode_refresh_token(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:  # pragma: no cover - fast error path
        raise HTTPException(status_code=401, detail="Refresh token expired") from exc
    except jwt.InvalidTokenError as exc:  # pragma: no cover - fast error path
        raise HTTPException(status_code=401, detail="Invalid refresh token") from exc


def get_current_user_with_refresh(
    request: Request,
    response: Response,
    access_token: Optional[str] = Cookie(None, alias="access_token"),
) -> Dict[str, Any]:
    """Decode the access-token cookie; if it has expired, reissue it.

    Returns the token payload. On a refresh the returned payload is the payload
    of the *new* token (user_id plus any type / con_org_id / subdomain claims
    the expired token carried) and the new cookie is attached to ``response``.
    """
    if not access_token:
        raise HTTPException(status_code=403, detail="No access token cookie provided")

    try:
        return jwt.decode(access_token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        expired_payload = jwt.decode(
            access_token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"verify_exp": False},
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    return refresh_access_token(request, response, expired_payload)


def refresh_access_token(
    request: Request,
    response: Response,
    expired_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Issue a fresh access token for an expired one if the stored refresh token is valid.

    Portal tokens (``type == "portal"``) are looked up in the tenant's
    ``user_mst``; console tokens in ``vowconsole3.con_user_master``.
    """
    user_id = expired_payload.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    token_type = expired_payload.get("type")
    stored_refresh_token = _fetch_refresh_token(user_id, token_type, request)
    if not stored_refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token found")
    _decode_refresh_token(stored_refresh_token)

    token_payload: Dict[str, Any] = {"user_id": user_id}
    for claim in _TOKEN_CONTEXT_CLAIMS:
        if expired_payload.get(claim) is not None:
            token_payload[claim] = expired_payload[claim]

    set_access_token_cookie(response, create_access_token(token_payload))
    return token_payload

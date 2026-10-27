from fastapi import APIRouter, Request, Response, HTTPException, Header, Depends  # Add Header to the imports
from starlette.status import HTTP_401_UNAUTHORIZED
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.sql import text
from sqlalchemy.orm import Session
from pydantic import BaseModel
import os
from src.authorization.utils import (
    decode_access_token,
    get_current_user_with_refresh,
    clear_auth_cookies,
    create_access_token,
    verify_password,
    get_password_hash,
    SECRET_KEY,
    ALGORITHM
)
from src.authorization.auth import login_user, login_user_console
from src.authorization.query import validate_subdomain_query
from src.config.db import default_engine, extract_subdomain_from_request
from datetime import datetime
import jwt

from src.authorization.models import LoginRequest
common_router = APIRouter()


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


@common_router.post("/login")
def login_route(request: Request, login_data: LoginRequest):
    """Login Route (calls login_user from auth.py)"""
    print("\n=== Starting Login Process ===")
    print(f"🔄 Received login request for user: {login_data.username}")
    print(f"🔍 Login type: {login_data.logintype}")
    print(f"🔒 Remember me: {login_data.rememberMe}")

    # Extract subdomain
    subdomain = request.headers.get("X-Subdomain", "default")
    print(f"🌐 Using subdomain: {subdomain}")
    
    # Print all headers for debugging
    print("\n📋 Request Headers:")
    for header, value in request.headers.items():
        print(f"   {header}: {value}")
    
    try:
        result = login_user(
            request,
            login_data.username,
            login_data.password,
            login_data.logintype,
            subdomain
        )
        print("✅ Login function completed successfully")
        return result
    except Exception as e:
        print(f"❌ Login error in router: {str(e)}")
        print(f"Error type: {type(e).__name__}")
        raise

@common_router.post("/loginconsole")
def login_console_route(request: Request, login_data: LoginRequest):
    """Login Route (calls login_user_console from auth.py)"""
    print("Login User")

    # For console login, prefer frontend-provided subdomain from browser hostname; fallback to request extraction.
    header_subdomain = (request.headers.get("X-Subdomain") or "").strip().lower()
    subdomain = header_subdomain if header_subdomain else extract_subdomain_from_request(request)
    print(f"DEBUG: Extracted Subdomain = {subdomain}")  # Debugging
    print('hhhsub=====',subdomain)
    return login_user_console(
        request,
        login_data.username,
        login_data.password,
        login_data.logintype,
        subdomain  # ✅ Pass extracted subdomain
    )




@common_router.post("/change-password")
def change_password(
    request: Request,
    response: Response,
    payload: ChangePasswordRequest,
    token_data: dict = Depends(get_current_user_with_refresh),
):
    """Self-service password change for the currently logged-in user.

    Works for both personas based on the token's `type` field:
      * type == "portal"  -> tenant DB `user_mst` (resolved from subdomain, same
        mechanism as login_user()).
      * otherwise (console/admin) -> `vowconsole3.con_user_master` via the default
        engine, same mechanism as login_user_console().
    """
    try:
        user_id = token_data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=403, detail="User ID not found in token")

        current_password = (payload.current_password or "").strip()
        new_password = (payload.new_password or "").strip()
        confirm_password = (payload.confirm_password or "").strip()

        # Validate all three fields non-empty
        if not current_password or not new_password or not confirm_password:
            raise HTTPException(
                status_code=400,
                detail="current_password, new_password and confirm_password are all required",
            )

        # New password must match its confirmation
        if new_password != confirm_password:
            raise HTTPException(
                status_code=400,
                detail="New password and confirm password do not match",
            )

        # Reject no-op change
        if new_password == current_password:
            raise HTTPException(
                status_code=400,
                detail="New password must be different from current password",
            )

        token_type = token_data.get("type")

        if token_type == "portal":
            # Resolve tenant DB exactly like login_user() does.
            subdomain = extract_subdomain_from_request(request)
            if not subdomain or subdomain == "default":
                raise HTTPException(status_code=400, detail="Unable to resolve tenant for portal user")

            tenant_url = (
                f"mysql+pymysql://{os.getenv('DATABASE_USER')}:{os.getenv('DATABASE_PASSWORD')}@"
                f"{os.getenv('DATABASE_HOST')}:{os.getenv('DATABASE_PORT')}/{subdomain}"
            )
            tenant_engine = create_engine(tenant_url, pool_pre_ping=True)

            with Session(tenant_engine) as session:
                row = session.execute(
                    text(
                        "SELECT user_id, password FROM user_mst "
                        "WHERE user_id = :user_id AND active = TRUE"
                    ),
                    {"user_id": user_id},
                ).fetchone()

                if not row:
                    raise HTTPException(status_code=404, detail="User not found")

                stored_hash = row.password
                if not stored_hash or not verify_password(current_password, stored_hash):
                    raise HTTPException(status_code=400, detail="Current password is incorrect")

                session.execute(
                    text("UPDATE user_mst SET password = :password WHERE user_id = :user_id"),
                    {"password": get_password_hash(new_password), "user_id": user_id},
                )
                session.commit()
        else:
            # Console / tenant-admin user lives in vowconsole3.con_user_master.
            with Session(default_engine) as session:
                row = session.execute(
                    text(
                        "SELECT con_user_id, con_user_login_password FROM con_user_master "
                        "WHERE con_user_id = :user_id AND active = 1"
                    ),
                    {"user_id": user_id},
                ).fetchone()

                if not row:
                    raise HTTPException(status_code=404, detail="User not found")

                stored_hash = row.con_user_login_password
                if not stored_hash or not verify_password(current_password, stored_hash):
                    raise HTTPException(status_code=400, detail="Current password is incorrect")

                session.execute(
                    text(
                        "UPDATE con_user_master SET con_user_login_password = :password "
                        "WHERE con_user_id = :user_id"
                    ),
                    {"password": get_password_hash(new_password), "user_id": user_id},
                )
                session.commit()

        return {"data": {"message": "Password changed successfully"}}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error changing password: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@common_router.get("/validate-subdomain")
def validate_subdomain(request: Request, subdomain: str = ""):
    """
    Validate if a subdomain corresponds to an active organisation in con_org_master.
    'admin' is always valid (control desk).
    Returns { valid: true/false }
    """
    subdomain = subdomain.strip().lower()
    if not subdomain:
        return {"valid": False}

    # 'admin' is the control desk — always valid
    if subdomain == "admin":
        return {"valid": True}

    try:
        with Session(default_engine) as session:
            query = validate_subdomain_query()
            result = session.execute(query, {"subdomain": subdomain}).fetchone()
            is_valid = result is not None and result[0] > 0
            return {"valid": is_valid}
    except Exception as e:
        print(f"Error validating subdomain: {str(e)}")
        return {"valid": False}


@common_router.get("/valid-origins")
def get_valid_origins(request: Request):
    """
    Returns all active organisation shortnames from con_org_master.
    Used by CORS middleware to dynamically allow origins.
    """
    try:
        with Session(default_engine) as session:
            result = session.execute(
                text("""
                    SELECT LOWER(TRIM(con_org_shortname)) as shortname
                    FROM vowconsole3.con_org_master
                    WHERE active = 1
                      AND con_org_shortname IS NOT NULL
                      AND TRIM(con_org_shortname) != ''
                """)
            ).fetchall()
            shortnames = [row[0] for row in result]
            return {"data": shortnames}
    except Exception as e:
        print(f"Error fetching valid origins: {str(e)}")
        return {"data": []}


@common_router.get("/protected")
def protected_route(request: Request, authorization: str = Header(None)):
    """Protected Route"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")

    token = authorization.split(" ")[1]
    username = decode_access_token(token)  # Decode the token to get the username
    return {"message": f"Hello, {username}"}

@common_router.get("/verify-session")
def verify_session(request: Request, response: Response):
    """
    Verify session validity.
    Returns 200 { ok: true } if valid, 401 if not.
    Also validates that the user's org matches the current subdomain.

    An expired access token is refreshed through the same tenant-aware path as
    every other endpoint (``get_current_user_with_refresh``): portal tokens are
    checked against the tenant's ``user_mst`` refresh token, console tokens
    against ``con_user_master``. The reissued cookie rides on the 200 response.
    """
    access_token = request.cookies.get("access_token")
    if not access_token:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="No access token")

    try:
        payload = get_current_user_with_refresh(request, response, access_token)
    except HTTPException as exc:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail=exc.detail) from exc
    except Exception as exc:  # DB down, tenant unresolved, ...
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED) from exc

    # --- Org/subdomain validation for console sessions ---
    # Only console tokens carry "subdomain" (portal tokens have "type": "portal").
    token_subdomain = payload.get("subdomain")
    if token_subdomain is not None:
        current_subdomain = extract_subdomain_from_request(request).strip().lower()
        # Also check X-Subdomain header (frontend sends browser hostname)
        header_sub = (request.headers.get("X-Subdomain") or "").strip().lower()
        resolved_current = header_sub if header_sub and header_sub != "default" else current_subdomain
        if not resolved_current or resolved_current == "default":
            resolved_current = "default"
        if token_subdomain != resolved_current:
            print(f"Session org mismatch: token={token_subdomain}, current={resolved_current}")
            # Raising discards the refreshed cookie attached to `response`.
            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Session not valid for this organization")

    return {"ok": True}


@common_router.post("/logout")
def logout(response: Response):
    """
    Expire this browser's auth cookies. The cookies are httpOnly, so the
    frontend cannot clear them itself. The stored refresh token is left in
    place so the user's other devices stay signed in.
    """
    clear_auth_cookies(response)
    return {"ok": True}

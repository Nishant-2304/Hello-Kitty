"""Google auth: verify the ID token from the frontend, store the user.

The frontend uses Google Identity Services (GIS) to:
  1. Get an `id_token` (a JWT signed by Google) — used to identify the user.
  2. Get an `access_token` (with prompt=consent) — used as the Gmail
     credentials when the backend creates drafts. The access token is
     short-lived (~1 hour); the user re-signs when it expires.

Flow:
  POST /auth/google  body={id_token, access_token, username, phone, linkedin}
  -> verify id_token via Google's tokeninfo endpoint
  -> upsert into users table (token stored in users.access_token)
  -> set session cookie
  -> return {user_id, email, name, username}

The session cookie persists for 30 days. The Gmail access token persists
in the DB and is rotated on every sign-in. /run uses the stored access
token directly to call gmail.drafts().create().

In dev mode (no GOOGLE_OAUTH_CLIENT_ID in .env), the verification step
is relaxed: /auth/dev-signin accepts an email directly. Production
refuses dev-signin (returns 404).
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
import httpx
from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

import db

# Load .env when this module is imported.
load_dotenv()

# --- Config ---------------------------------------------------------------

CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()

DEV_MODE = not (CLIENT_ID and CLIENT_SECRET)

if not DEV_MODE and not SESSION_SECRET:
    SESSION_SECRET = secrets.token_urlsafe(32)

SESSION_COOKIE = "hk_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

_serializer = URLSafeSerializer(SESSION_SECRET or "dev-secret", salt="hk-session")

# Google endpoints
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


# --- Identity verification -------------------------------------------------
#
# The Google Identity Services token client (used in the frontend) only
# hands back an access_token, not an id_token. So we verify the access_token
# by calling Google's userinfo endpoint with it. That endpoint returns the
# user's Google profile (sub, email, name, picture) — same fields an ID
# token would have given us.
#
# If the frontend ever does have an id_token (e.g. someone wires up GIS's
# id client), we accept that too via verify_google_id_token().

GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


async def fetch_google_user_via_access_token(access_token: str) -> dict[str, Any]:
    """Use an access token to fetch the user's identity from Google.

    Returns a dict with at minimum: sub, email. Also includes name, picture
    if Google provided them. The `sub` is the stable Google account id and
    is what we use as the primary key in our users table.
    """
    if not access_token:
        raise HTTPException(status_code=400, detail="access_token is required")

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code != 200:
        raise HTTPException(
            status_code=401,
            detail=f"Google userinfo returned {r.status_code} — access token may be invalid",
        )
    return r.json()


async def verify_google_id_token(id_token: str) -> dict[str, Any]:
    """Verify a Google ID token and return its claims.

    Uses Google's tokeninfo endpoint (HTTPS, signed by Google). Returns
    the standard claims: sub, email, email_verified, name, picture, etc.
    """
    if not id_token:
        raise HTTPException(status_code=400, detail="id_token is required")

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            GOOGLE_TOKENINFO_URL, params={"id_token": id_token}
        )
    if r.status_code != 200:
        raise HTTPException(
            status_code=401, detail="Invalid Google ID token"
        )

    claims = r.json()

    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(status_code=401, detail="Bad token issuer")
    if claims.get("exp"):
        exp = int(claims["exp"])
        if exp < datetime.now(timezone.utc).timestamp():
            raise HTTPException(status_code=401, detail="Token expired")
    if CLIENT_ID and claims.get("aud") != CLIENT_ID:
        raise HTTPException(status_code=401, detail="Token audience mismatch")

    return claims


# --- Session cookie -------------------------------------------------------

def set_session(response: Response, user_id: str) -> None:
    """Sign the user_id and set it as a cookie on the response."""
    token = _serializer.dumps(user_id)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=False,  # set True in prod (HTTPS)
        samesite="lax",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def read_session(request: Request) -> str | None:
    """Read the user_id from the session cookie. None if invalid/missing."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return _serializer.loads(raw)
    except BadSignature:
        return None


async def get_current_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency: returns the logged-in user, or 401."""
    user_id = read_session(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not signed in")
    user = await db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def exchange_code_for_tokens(code: str) -> dict[str, Any]:
    """Exchange OAuth authorization code for access and refresh tokens.

    In dev mode (no CLIENT_ID/CLIENT_SECRET), returns fake credentials.
    """
    if not CLIENT_ID or not CLIENT_SECRET:
        return {
            "access_token": "dev-fake-token",
            "refresh_token": "dev-fake-refresh-token",
            "expires_in": 3600,
            "id_token": None,
        }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": "postmessage",
                "grant_type": "authorization_code",
            },
        )
    if r.status_code != 200:
        raise HTTPException(
            status_code=401,
            detail=f"Google token exchange failed: {r.status_code} {r.text}",
        )
    return r.json()


async def refresh_access_token(user: dict[str, Any]) -> str:
    """Refresh the user's access token using their refresh_token.

    If in dev mode or no refresh token, return current access token.
    Stores the new access token and its expiry in the database.
    """
    refresh_token = user.get("refresh_token")
    if not refresh_token or not CLIENT_ID or not CLIENT_SECRET:
        # Dev mode or missing token
        return user.get("access_token", "")

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code != 200:
        raise RuntimeError(f"Google token refresh failed: {r.status_code} {r.text}")

    resp = r.json()
    new_access_token = resp["access_token"]
    expires_in = resp.get("expires_in", 3600)

    from datetime import timedelta
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ).isoformat()

    # Save the updated token back to the database
    async with db.get_db() as database:
        await database.execute(
            "UPDATE users SET access_token = ?, access_expires_at = ? WHERE id = ?",
            (new_access_token, expires_at, user["id"]),
        )
        await database.commit()

    return new_access_token


def is_dev_mode() -> bool:
    return DEV_MODE

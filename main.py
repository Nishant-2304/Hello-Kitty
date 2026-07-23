"""FastAPI app: the /run orchestrator + per-user Google auth.

Endpoints:
  POST /auth/google   Sign in. Body: {id_token, access_token, expires_in,
                                      username, phone, linkedin}.
                      Sets session cookie. Returns the user dict.
  POST /auth/signout  Clear session cookie.
  GET  /me            Returns the current user (used by the frontend on load).
  GET  /me/next-batch Preview the next 10 rows that /run would claim.
  POST /run           Claim -> draft -> verify -> push -> mark. Returns summary.
  GET  /healthz       Liveness probe.

Run with:
    uvicorn main:app --reload --port 8000

Note: services.llm is intentionally not imported — that file is left in
place for the personalisation pass (later PR).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import auth
import db
import services.claims as claims
import services.templates as templates
from services.scheduler import start_email_scheduler

app = FastAPI(title="Hello Kitty", version="0.3.0")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(start_email_scheduler())


# Permissive CORS for dev with credential support.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Pydantic shapes ------------------------------------------------------

class SignInBody(BaseModel):
    """Body for /auth/google.

    code:         optional OAuth authorization code (new flow).
    access_token: optional (legacy/dev flow).
    id_token:     optional. If present, used instead of access_token.
    expires_in:   seconds until access_token expires. Default 3600.
    username:     optional handle for "CONTACTED BY" column.
    """
    code: Optional[str] = None
    access_token: Optional[str] = None
    id_token: Optional[str] = None
    expires_in: int = 3600
    username: str = Field(default="", max_length=64)
    phone: str = Field(default="", max_length=64)
    linkedin: str = Field(default="", max_length=256)


class SignInResponse(BaseModel):
    id: str
    email: str
    name: str
    username: str
    is_new_user: bool
    dev_mode: bool


# --- Endpoints ------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "dev_mode": auth.is_dev_mode()}


@app.post("/auth/google", response_model=SignInResponse)
async def sign_in(body: SignInBody, response: Response) -> SignInResponse:
    # Exchange code for tokens if code is provided
    if body.code:
        tokens = await auth.exchange_code_for_tokens(body.code)
        access_token = tokens["access_token"]
        refresh_token = tokens.get("refresh_token", "")
        expires_in = tokens.get("expires_in", 3600)
        id_token = tokens.get("id_token")
    else:
        # Fallback to access_token from body (dev / legacy flow)
        if not body.access_token:
            raise HTTPException(status_code=400, detail="Either code or access_token must be provided")
        access_token = body.access_token
        refresh_token = ""
        expires_in = body.expires_in
        id_token = body.id_token

    # Identity verification
    if id_token:
        claims_data = await auth.verify_google_id_token(id_token)
    else:
        claims_data = await auth.fetch_google_user_via_access_token(access_token)

    google_sub = claims_data["sub"]
    email = claims_data["email"]
    name = claims_data.get("name") or claims_data.get("given_name") or email
    user_handle = body.username.strip() or name.split()[0].lower() or email.split("@")[0]

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ).isoformat()

    user = await db.upsert_user(
        google_sub=google_sub,
        email=email,
        name=name,
        username=user_handle,
        phone=body.phone,
        linkedin=body.linkedin,
        access_token=access_token,
        access_expires_at=expires_at,
        refresh_token=refresh_token,
    )
    auth.set_session(response, user["id"])
    return SignInResponse(
        id=user["id"],
        email=user["email"],
        name=user["name"],
        username=user["username"],
        is_new_user=False,
        dev_mode=auth.is_dev_mode(),
    )


@app.post("/auth/signout")
async def sign_out(response: Response) -> dict[str, Any]:
    auth.clear_session(response)
    return {"ok": True}


class DevSignInBody(BaseModel):
    """DEV ONLY. Skips Google token verification. Refuses in prod."""
    email: str
    name: str = "Dev User"
    username: str = "dev"
    phone: str = ""
    linkedin: str = ""


@app.post("/auth/dev-signin", response_model=SignInResponse, include_in_schema=False)
async def dev_sign_in(body: DevSignInBody, response: Response) -> SignInResponse:
    """Dev-only sign-in: skips Google. Refuses unless auth.is_dev_mode()."""
    if not auth.is_dev_mode():
        raise HTTPException(status_code=404, detail="Not found")
    user = await db.upsert_user(
        google_sub=f"dev|{body.email}",
        email=body.email,
        name=body.name,
        username=body.username,
        phone=body.phone,
        linkedin=body.linkedin,
        access_token="dev-fake-token",
        access_expires_at="2099-12-31T00:00:00+00:00",
    )
    auth.set_session(response, user["id"])
    return SignInResponse(
        id=user["id"],
        email=user["email"],
        name=user["name"],
        username=user["username"],
        is_new_user=False,
        dev_mode=True,
    )


@app.get("/me")
async def me(user: dict[str, Any] = Depends(auth.get_current_user)) -> dict[str, Any]:
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "username": user["username"],
        "phone": user.get("phone", ""),
        "linkedin": user.get("linkedin", ""),
        "is_new_user": False,
    }


class ProfileUpdateBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    phone: str = Field(default="", max_length=64)
    linkedin: str = Field(default="", max_length=256)


@app.patch("/me")
async def update_me(
    body: ProfileUpdateBody,
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    updated = await db.update_user_profile(
        google_sub=user["id"],
        username=body.username,
        phone=body.phone,
        linkedin=body.linkedin,
    )
    return {
        "id": updated["id"],
        "email": updated["email"],
        "name": updated["name"],
        "username": updated["username"],
        "phone": updated.get("phone", ""),
        "linkedin": updated.get("linkedin", ""),
        "is_new_user": False,
    }


@app.get("/me/next-batch")
async def next_batch(
    _user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    rows = await claims.preview_next_batch()
    return {
        "count": len(rows),
        "rows": [
            {
                "company": r.get("COMPANY NAME") or r.get("COMAPANY NAME") or "",
                "email": r.get("EMAIL ID") or r.get("EMAIL") or "",
                "category": r.get("CATEGORY") or r.get("CATEGORIES") or r.get("TYPE") or "",
                "status": r.get("STATUS", ""),
            }
            for r in rows
        ],
    }


@app.post("/run")
async def run_endpoint(
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    result = await claims.run(user)
    return result.to_dict()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)

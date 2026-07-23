"""Gmail drafts: per-user OAuth, using access tokens stored in the DB.

The flow: when the user signs in via Google, GIS hands us an
access_token. We store it in the users table along with its
expiry. /run reads the token and uses it to call gmail.drafts().create().

If the token is expired (rare — only if the user re-runs /run >1hr
after their last sign-in), we don't refresh it. The user re-signs
in. The access token is short-lived for security, but in practice
our /run completes in seconds, well within the 1-hour window.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
from typing import Any

import httpx

# Each user gets their own Gmail client, scoped to gmail.compose.
SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def _is_expired(user: dict[str, Any]) -> bool:
    """True if the stored access token has passed its expiry."""
    exp = user.get("access_expires_at", "")
    if not exp:
        return False  # unknown — assume not expired
    try:
        return datetime.fromisoformat(exp) <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


async def create_draft(
    user: dict[str, Any],
    *,
    to: str,
    subject: str,
    body: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Create a Gmail draft in the user's account.

    In dev mode (no client_id/client_secret), returns a fake response.
    Production uses the stored access token directly.
    """
    if not client_id or not client_secret:
        # Dev mode: don't actually call Gmail.
        return {
            "id": f"dev-draft-{hash((to, subject)) & 0xFFFF:04x}",
            "to": to,
            "subject": subject,
            "body_preview": body[:60],
        }

    if _is_expired(user):
        raise RuntimeError(
            "Gmail access token expired. Please sign in again to refresh it."
        )

    access_token = user["access_token"]

    message = MIMEMultipart()
    message["to"] = to
    message["subject"] = subject

    # Attach text body
    message.attach(MIMEText(body, "plain"))

    # Attach PDF brochure if it exists
    pdf_path = Path(__file__).parent.parent / "CodeChef-VIT graVITas'26 Brochure.pdf"
    if pdf_path.exists():
        try:
            with open(pdf_path, "rb") as f:
                attachment = MIMEApplication(f.read(), _subtype="pdf")
            attachment.add_header(
                "Content-Disposition",
                "attachment",
                filename=pdf_path.name
            )
            message.attach(attachment)
        except Exception as attach_err:
            import logging
            logging.getLogger("hellokitty").error("Failed to attach brochure PDF: %s", attach_err)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"message": {"raw": raw}},
        )
    if r.status_code >= 300:
        raise RuntimeError(f"Gmail drafts.create failed: {r.status_code} {r.text}")
    return r.json()


async def send_email(
    user: dict[str, Any],
    *,
    to: str,
    subject: str,
    body: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Send an email from the user's Gmail account directly.

    In dev mode (no client_id/client_secret), returns a fake response.
    Production uses the stored access token directly.
    """
    if not client_id or not client_secret:
        # Dev mode: don't actually call Gmail.
        return {
            "id": f"dev-msg-{hash((to, subject)) & 0xFFFF:04x}",
            "to": to,
            "subject": subject,
        }

    if _is_expired(user):
        raise RuntimeError(
            "Gmail access token expired. Access token must be refreshed."
        )

    access_token = user["access_token"]

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"raw": raw},
        )
    if r.status_code >= 300:
        raise RuntimeError(f"Gmail messages.send failed: {r.status_code} {r.text}")
    return r.json()


async def send_draft(
    user: dict[str, Any],
    *,
    draft_id: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Send an existing Gmail draft by its draft_id.

    In dev mode (no client_id/client_secret), returns a fake response.
    Production uses the stored access token directly.
    """
    if not client_id or not client_secret:
        # Dev mode: don't actually call Gmail.
        return {
            "id": f"dev-msg-{hash(draft_id) & 0xFFFF:04x}",
            "draft_id": draft_id,
        }

    if _is_expired(user):
        raise RuntimeError(
            "Gmail access token expired. Access token must be refreshed."
        )

    access_token = user["access_token"]

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"id": draft_id},
        )
    if r.status_code >= 300:
        raise RuntimeError(f"Gmail drafts.send failed: {r.status_code} {r.text}")
    return r.json()

"""SQLite persistence for the bot. One table: users.

Why SQLite: zero config, file-based, perfect for 10 users. If we ever
scale beyond that, swap the connection string for Postgres + asyncpg —
the API stays the same.

Threading: we use aiosqlite. The FastAPI app is async, so all DB ops
must be `await`ed. We open one connection per request via aiosqlite's
connection-per-call pattern (no long-lived connection pool needed at
this scale).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from aiosqlite import Connection, connect

DB_PATH = Path(__file__).parent / "hellokitty.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,        -- Google's `sub` claim, stable per user
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,           -- Google profile name (real name)
    username      TEXT NOT NULL,           -- chosen by user at sign-in
    phone         TEXT NOT NULL DEFAULT '',
    linkedin      TEXT NOT NULL DEFAULT '',
    access_token  TEXT NOT NULL,           -- Gmail access token (short-lived, rotated on sign-in)
    access_expires_at TEXT NOT NULL DEFAULT '',  -- ISO 8601 UTC, empty if unknown
    refresh_token TEXT NOT NULL DEFAULT '',  -- Gmail refresh token (long-lived)
    created_at    TEXT NOT NULL            -- ISO 8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS scheduled_emails (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    sheet_row     INTEGER NOT NULL,
    to_email      TEXT NOT NULL,
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    draft_id      TEXT NOT NULL DEFAULT '', -- Gmail draft ID
    scheduled_at  TEXT NOT NULL,         -- ISO 8601 UTC timestamp
    status        TEXT NOT NULL DEFAULT 'pending', -- pending, sent, failed
    error         TEXT,
    created_at    TEXT NOT NULL,         -- ISO 8601 UTC timestamp
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""


async def _ensure_schema() -> None:
    """Create the schema if it doesn't exist. Idempotent."""
    async with connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
        # Migration: Add refresh_token column to users table if not exists
        try:
            await db.execute("ALTER TABLE users ADD COLUMN refresh_token TEXT NOT NULL DEFAULT ''")
            await db.commit()
        except Exception:
            # Column already exists
            pass
        # Migration: Add draft_id column to scheduled_emails table if not exists
        try:
            await db.execute("ALTER TABLE scheduled_emails ADD COLUMN draft_id TEXT NOT NULL DEFAULT ''")
            await db.commit()
        except Exception:
            # Column already exists
            pass


@asynccontextmanager
async def get_db() -> AsyncIterator[Connection]:
    """Yield a connection, run the query, close.

    Usage:
        async with get_db() as db:
            await db.execute("INSERT ...", (...))
            await db.commit()
    """
    # Always run CREATE TABLE IF NOT EXISTS — it's idempotent and cheap,
    # and lets us recover if a table was dropped out-of-band.
    await _ensure_schema()
    async with connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


async def upsert_user(
    *,
    google_sub: str,
    email: str,
    name: str,
    username: str,
    phone: str,
    linkedin: str,
    access_token: str,
    access_expires_at: str = "",
    refresh_token: str = "",
) -> dict[str, Any]:
    """Insert or update a user. Returns the user row as a dict.

    `google_sub` is the primary key (stable per Google account). On
    update we rotate the access_token (it expires hourly) and let the
    user keep their username/phone/linkedin unless they pass new ones.
    `email` and `name` come from Google, so we trust them.
    """
    from datetime import datetime, timezone
    created_at = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO users (
                id, email, name, username, phone, linkedin, access_token, access_expires_at, refresh_token, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                email = excluded.email,
                name = excluded.name,
                username = excluded.username,
                phone = excluded.phone,
                linkedin = excluded.linkedin,
                access_token = excluded.access_token,
                access_expires_at = excluded.access_expires_at,
                refresh_token = CASE WHEN excluded.refresh_token != '' THEN excluded.refresh_token ELSE users.refresh_token END
            """,
            (google_sub, email, name, username, phone, linkedin, access_token, access_expires_at, refresh_token, created_at),
        )
        await db.commit()
        async with db.execute(
            "SELECT * FROM users WHERE id = ?", (google_sub,)
        ) as cur:
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


async def get_user_by_id(google_sub: str) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM users WHERE id = ?", (google_sub,)
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


async def update_user_profile(
    *,
    google_sub: str,
    username: str,
    phone: str,
    linkedin: str,
) -> dict[str, Any]:
    """Update only username/phone/linkedin. Does NOT touch the access_token
    or any Google identity fields. Used by PATCH /me during the
    first-time profile setup flow.
    """
    async with get_db() as db:
        await db.execute(
            """
            UPDATE users SET username = ?, phone = ?, linkedin = ?
            WHERE id = ?
            """,
            (username, phone, linkedin, google_sub),
        )
        await db.commit()
        async with db.execute(
            "SELECT * FROM users WHERE id = ?", (google_sub,)
        ) as cur:
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

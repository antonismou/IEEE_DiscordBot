from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    discord_id  INTEGER PRIMARY KEY,
    email       TEXT UNIQUE,
    full_name   TEXT NOT NULL,
    method      TEXT NOT NULL CHECK (method IN ('email', 'manual')),
    verified_by INTEGER,
    note        TEXT,
    consent_at  TEXT,
    verified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_codes (
    discord_id INTEGER PRIMARY KEY,
    email      TEXT NOT NULL,
    full_name  TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS send_log (
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_items (
    feed_id   TEXT NOT NULL,
    item_id   TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    PRIMARY KEY (feed_id, item_id)
);
CREATE TABLE IF NOT EXISTS feed_state (
    feed_id        TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_feeds (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    url        TEXT NOT NULL,
    channel_id INTEGER NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Create the file as 0600 first so the -wal/-shm files SQLite derives from it are private too.
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn

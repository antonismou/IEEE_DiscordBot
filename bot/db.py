from __future__ import annotations

import logging
import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class DataDirError(Exception):
    """The data directory or database file cannot be written."""


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
    discord_id INTEGER NOT NULL,
    email_hash TEXT NOT NULL,
    sent_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS send_log_user ON send_log (discord_id, sent_at);
CREATE INDEX IF NOT EXISTS send_log_email ON send_log (email_hash, sent_at);
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
    parent_existed = path.parent.exists()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create the file as 0600 first so the -wal/-shm files SQLite derives from it are private too.
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        os.chmod(path, 0o600)  # also tightens a database file that already existed
    except PermissionError as exc:
        raise DataDirError(
            f"Cannot write to {path.parent}: {exc.strerror}. If the bot runs in Docker, the folder must be owned by "
            f"uid 1000: sudo chown -R 1000:1000 {path.parent}"
        ) from exc
    if parent_existed and stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        log.warning(
            "%s is accessible by other users; consider `chmod 700 %s` (the database file itself is 0600)",
            path.parent, path.parent,
        )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA secure_delete=ON")  # deleted personal data must not linger in free pages
    conn.executescript(SCHEMA)
    return conn

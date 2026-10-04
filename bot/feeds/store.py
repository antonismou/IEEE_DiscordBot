from __future__ import annotations

import sqlite3
from datetime import datetime

from bot.config import FeedConfig
from bot.db import to_iso


class FeedExists(Exception):
    pass


class SeenStore:
    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    def is_initialized(self, feed_id: str) -> bool:
        row = self._c.execute("SELECT 1 FROM feed_state WHERE feed_id = ?", (feed_id,)).fetchone()
        return row is not None

    def initialize(self, feed_id: str, item_ids: list[str], now: datetime) -> None:
        with self._c:
            self._insert_seen(feed_id, item_ids, now)
            self._c.execute(
                "INSERT OR IGNORE INTO feed_state (feed_id, initialized_at) VALUES (?, ?)", (feed_id, to_iso(now))
            )

    def seen_ids(self, feed_id: str) -> set[str]:
        rows = self._c.execute("SELECT item_id FROM seen_items WHERE feed_id = ?", (feed_id,)).fetchall()
        return {row["item_id"] for row in rows}

    def mark_seen(self, feed_id: str, item_ids: list[str], now: datetime) -> None:
        with self._c:
            self._insert_seen(feed_id, item_ids, now)

    def _insert_seen(self, feed_id: str, item_ids: list[str], now: datetime) -> None:
        self._c.executemany(
            "INSERT OR IGNORE INTO seen_items (feed_id, item_id, posted_at) VALUES (?, ?, ?)",
            [(feed_id, item_id, to_iso(now)) for item_id in item_ids],
        )


class CustomFeedRepo:
    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    def add(self, feed: FeedConfig) -> None:
        try:
            with self._c:
                self._c.execute(
                    "INSERT INTO custom_feeds (id, title, url, channel_id) VALUES (?, ?, ?, ?)",
                    (feed.id, feed.title, feed.url, feed.channel_id),
                )
        except sqlite3.IntegrityError:
            raise FeedExists(feed.id) from None

    def remove(self, feed_id: str) -> bool:
        with self._c:
            cursor = self._c.execute("DELETE FROM custom_feeds WHERE id = ?", (feed_id,))
        return cursor.rowcount > 0

    def all(self) -> list[FeedConfig]:
        rows = self._c.execute("SELECT id, title, url, channel_id FROM custom_feeds ORDER BY id").fetchall()
        return [FeedConfig(r["id"], r["title"], r["url"], r["channel_id"]) for r in rows]

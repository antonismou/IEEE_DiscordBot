from __future__ import annotations

import sqlite3

OFFICER_LOG = "officer-log"  # reserved topic: where the bot reports problems to officers


class ChannelRepo:
    """Which Discord channel each topic (e.g. "ai-ml") posts to. Set with /channel set."""

    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    def get(self, topic: str) -> int | None:
        row = self._c.execute("SELECT channel_id FROM channel_bindings WHERE topic = ?", (topic,)).fetchone()
        return row["channel_id"] if row else None

    def set(self, topic: str, channel_id: int) -> None:
        with self._c:
            self._c.execute(
                "INSERT INTO channel_bindings (topic, channel_id) VALUES (?, ?) "
                "ON CONFLICT(topic) DO UPDATE SET channel_id = excluded.channel_id",
                (topic, channel_id),
            )

    def clear(self, topic: str) -> bool:
        with self._c:
            cursor = self._c.execute("DELETE FROM channel_bindings WHERE topic = ?", (topic,))
        return cursor.rowcount > 0

    def all(self) -> dict[str, int]:
        rows = self._c.execute("SELECT topic, channel_id FROM channel_bindings").fetchall()
        return {row["topic"]: row["channel_id"] for row in rows}


def topics_for(feeds) -> list[str]:
    """Every topic a channel can be chosen for: the feeds' topics plus the officer log."""
    names = {feed.topic for feed in feeds if feed.topic}
    names.add(OFFICER_LOG)
    return sorted(names)

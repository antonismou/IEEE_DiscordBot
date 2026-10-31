from __future__ import annotations

import random as _random
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from bot.db import from_iso, to_iso


@dataclass(frozen=True)
class Quote:
    id: int
    text: str
    author_id: int
    author_name: str
    message_url: str
    quoted_by: int
    quoted_at: datetime


def _row_to_quote(row: sqlite3.Row) -> Quote:
    return Quote(
        id=row["id"],
        text=row["text"],
        author_id=row["author_id"],
        author_name=row["author_name"],
        message_url=row["message_url"],
        quoted_by=row["quoted_by"],
        quoted_at=from_iso(row["quoted_at"]),
    )


class QuoteRepo:
    """Messages members saved with /quoteit, so the bot can bring one back at random later."""

    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    def add(
        self,
        *,
        text: str,
        author_id: int,
        author_name: str,
        message_url: str,
        quoted_by: int,
        quoted_at: datetime,
    ) -> Quote:
        with self._c:
            cur = self._c.execute(
                "INSERT INTO quotes (text, author_id, author_name, message_url, quoted_by, quoted_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (text, author_id, author_name, message_url, quoted_by, to_iso(quoted_at)),
            )
        return self._get(cur.lastrowid)

    def random(self, rng: _random.Random | None = None) -> Quote | None:
        ids = [row["id"] for row in self._c.execute("SELECT id FROM quotes ORDER BY id")]
        if not ids:
            return None
        return self._get((rng or _random).choice(ids))

    def _get(self, quote_id: int) -> Quote:
        row = self._c.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        return _row_to_quote(row)

from __future__ import annotations

import random as _random
import sqlite3
from collections.abc import Callable
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

    def random(
        self, rng: _random.Random | None = None, *, accept: Callable[[Quote], bool] | None = None
    ) -> Quote | None:
        """A random quote, or None. With `accept`, only a quote it returns True for (tried in random order)."""
        ids = [row["id"] for row in self._c.execute("SELECT id FROM quotes ORDER BY id")]
        if not ids:
            return None
        rng = rng or _random
        if accept is None:
            return self._get(rng.choice(ids))
        rng.shuffle(ids)
        for quote_id in ids:
            quote = self._get(quote_id)
            if accept(quote):
                return quote
        return None

    def delete_by_user(self, user_id: int) -> int:
        """Forget a member: delete what they said; keep quotes they made of others but drop who made them."""
        with self._c:
            deleted = self._c.execute("DELETE FROM quotes WHERE author_id = ?", (user_id,)).rowcount
            self._c.execute("UPDATE quotes SET quoted_by = 0 WHERE quoted_by = ?", (user_id,))
        return deleted

    def _get(self, quote_id: int) -> Quote:
        row = self._c.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        return _row_to_quote(row)

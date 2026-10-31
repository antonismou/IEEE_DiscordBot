from __future__ import annotations

import random
from datetime import datetime, timezone

from bot.quotes.store import Quote, QuoteRepo

WHEN = datetime(2026, 10, 31, 12, 30, tzinfo=timezone.utc)


def _add(repo: QuoteRepo, text: str = "hello world", author_id: int = 1) -> Quote:
    return repo.add(
        text=text,
        author_id=author_id,
        author_name="Alice",
        message_url="https://discord.com/channels/1/2/3",
        quoted_by=99,
        quoted_at=WHEN,
    )


def test_add_returns_the_stored_quote(conn):
    repo = QuoteRepo(conn)
    quote = _add(repo)
    assert isinstance(quote.id, int)
    assert quote == Quote(
        id=quote.id,
        text="hello world",
        author_id=1,
        author_name="Alice",
        message_url="https://discord.com/channels/1/2/3",
        quoted_by=99,
        quoted_at=WHEN,
    )
    assert repo.random() == quote


def test_random_is_none_when_empty(conn):
    assert QuoteRepo(conn).random() is None


def test_random_returns_a_saved_quote(conn):
    repo = QuoteRepo(conn)
    added = [_add(repo, text=f"quote {i}", author_id=i) for i in range(3)]
    ids = {q.id for q in added}
    assert repo.random().id in ids
    first = repo.random(random.Random(1))
    second = repo.random(random.Random(1))
    assert first.id in ids
    assert first == second
    assert first in added

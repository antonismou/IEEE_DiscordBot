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


def test_delete_by_user_removes_their_quotes_and_anonymises_what_they_quoted(conn):
    repo = QuoteRepo(conn)
    theirs = repo.add(text="mine", author_id=7, author_name="Zed", message_url="u1", quoted_by=99, quoted_at=WHEN)
    quoted = repo.add(text="yours", author_id=8, author_name="Ana", message_url="u2", quoted_by=7, quoted_at=WHEN)
    other = repo.add(text="other", author_id=8, author_name="Ana", message_url="u3", quoted_by=99, quoted_at=WHEN)
    assert repo.delete_by_user(7) == 1
    rows = {r["id"]: (r["author_id"], r["quoted_by"]) for r in conn.execute("SELECT * FROM quotes")}
    assert rows == {quoted.id: (8, 0), other.id: (8, 99)}
    assert theirs.id not in rows
    assert repo.delete_by_user(7) == 0


def test_random_only_returns_accepted_quotes(conn):
    repo = QuoteRepo(conn)
    added = [_add(repo, text=f"q{i}") for i in range(6)]
    for seed in range(20):
        assert repo.random(random.Random(seed), accept=lambda q: q.text == "q3") == added[3]
    assert repo.random(accept=lambda q: False) is None

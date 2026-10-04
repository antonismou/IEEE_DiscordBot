from datetime import datetime, timezone

import pytest

from bot.config import FeedConfig
from bot.feeds.store import CustomFeedRepo, FeedExists, SeenStore

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def test_initialize_marks_items_seen(conn):
    store = SeenStore(conn)
    assert store.is_initialized("f") is False
    store.initialize("f", ["a", "b"], NOW)
    assert store.is_initialized("f") is True
    assert store.seen_ids("f") == {"a", "b"}
    assert store.seen_ids("other") == set()


def test_mark_seen_is_idempotent(conn):
    store = SeenStore(conn)
    store.mark_seen("f", ["a"], NOW)
    store.mark_seen("f", ["a", "b"], NOW)
    assert store.seen_ids("f") == {"a", "b"}


def test_custom_feeds_add_list_remove(conn):
    repo = CustomFeedRepo(conn)
    feed = FeedConfig("my-feed", "My Feed", "https://x/feed", 123)
    repo.add(feed)
    assert repo.all() == [feed]
    with pytest.raises(FeedExists):
        repo.add(feed)
    assert repo.remove("my-feed") is True
    assert repo.remove("my-feed") is False
    assert repo.all() == []


def test_forget_clears_one_feed_so_a_re_added_feed_starts_fresh(conn):
    store = SeenStore(conn)
    store.initialize("f", ["a", "b"], NOW)
    store.initialize("g", ["c"], NOW)
    store.forget("f")
    assert store.is_initialized("f") is False and store.seen_ids("f") == set()
    assert store.is_initialized("g") is True and store.seen_ids("g") == {"c"}

import asyncio
from datetime import datetime, timezone

import pytest

from bot.config import FeedConfig
from bot.feeds.parse import FeedError
from bot.feeds.service import process_feed
from bot.feeds.store import SeenStore

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
FEED = FeedConfig("robotics-tr", "IEEE Transactions on Robotics", "https://x/feed", 77)


def feed_xml(ids, *, oldest_first=False):
    ids = list(ids)
    entries = []
    for i, item_id in enumerate(ids):
        # ids later in the list are newer: pubDate grows with the numeric suffix
        day = 1 + int(str(item_id).split("-")[-1]) if "-" in str(item_id) else 1 + i
        entries.append(
            f"<item><title>Paper {item_id}</title><link>https://x/{item_id}</link><guid>{item_id}</guid>"
            f"<pubDate>Mon, {day:02d} Sep 2026 10:00:00 GMT</pubDate></item>"
        )
    if not oldest_first:
        entries.reverse()
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>' + "".join(entries) + "</channel></rss>").encode()


class Harness:
    def __init__(self, conn, content):
        self.store = SeenStore(conn)
        self.content = content
        self.sent = []
        self.fail_on_call = None

    def fetch(self, url):
        if isinstance(self.content, Exception):
            raise self.content
        return self.content

    async def send(self, channel_id, embeds, content):
        if self.fail_on_call is not None and len(self.sent) + 1 == self.fail_on_call:
            raise RuntimeError("discord is down")
        self.sent.append((channel_id, [e.title for e in embeds], content))

    def run(self, max_items=10):
        return asyncio.run(process_feed(
            FEED, fetch=self.fetch, store=self.store, send=self.send, max_items=max_items, clock=lambda: NOW
        ))


def titles(h):
    return [t for _, batch, _ in h.sent for t in batch]


def test_first_run_posts_nothing_then_posts_only_new_items(conn):
    h = Harness(conn, feed_xml(["p-1", "p-2", "p-3"]))
    assert h.run() == 0 and h.sent == []
    h.content = feed_xml(["p-1", "p-2", "p-3", "p-4"])
    assert h.run() == 1
    assert titles(h) == ["Paper p-4"] and h.sent[0][0] == 77


def test_unchanged_feed_posts_nothing_even_after_many_polls(conn):
    h = Harness(conn, feed_xml(["p-1", "p-2"]))
    h.run()
    for _ in range(3):
        assert h.run() == 0
    assert h.sent == []


def test_cap_posts_newest_in_chronological_order_and_notes_overflow(conn):
    h = Harness(conn, feed_xml(["p-1"]))
    h.run()
    h.content = feed_xml([f"p-{i}" for i in range(1, 15)])      # 13 new items
    assert h.run(max_items=10) == 10
    assert titles(h) == [f"Paper p-{i}" for i in range(5, 15)]  # newest 10, oldest of them first
    assert "+3" in h.sent[-1][2] and FEED.title in h.sent[-1][2]
    assert h.run(max_items=10) == 0                              # overflow was marked seen


def test_oldest_first_feeds_still_keep_the_newest(conn):
    h = Harness(conn, feed_xml(["p-1"], oldest_first=True))
    h.run()
    h.content = feed_xml([f"p-{i}" for i in range(1, 6)], oldest_first=True)
    assert h.run(max_items=2) == 2
    assert titles(h) == ["Paper p-4", "Paper p-5"]


def test_duplicate_ids_in_a_feed_are_posted_once(conn):
    h = Harness(conn, feed_xml(["p-1"]))
    h.run()
    h.content = feed_xml(["p-1", "p-2", "p-2"])
    assert h.run() == 1


def test_html_error_page_raises_and_does_not_initialise(conn):
    h = Harness(conn, b"<html><body>418</body></html>")
    with pytest.raises(FeedError):
        h.run()
    assert h.store.is_initialized(FEED.id) is False
    h.content = feed_xml(["p-1", "p-2"])
    assert h.run() == 0                       # still treated as the first run
    assert h.store.is_initialized(FEED.id) is True


def test_fetch_errors_propagate_and_change_nothing(conn):
    h = Harness(conn, FeedError("HTTP 418"))
    with pytest.raises(FeedError, match="418"):
        h.run()
    assert h.store.seen_ids(FEED.id) == set()


def test_send_failure_keeps_items_unseen_and_they_are_retried(conn):
    h = Harness(conn, feed_xml(["p-1"]))
    h.run()
    h.content = feed_xml(["p-1", "p-2"])
    h.fail_on_call = 1
    with pytest.raises(RuntimeError):
        h.run()
    assert h.store.seen_ids(FEED.id) == {"p-1"}
    h.fail_on_call = None
    assert h.run() == 1 and titles(h) == ["Paper p-2"]


def test_partial_failure_marks_only_posted_batches(conn):
    h = Harness(conn, feed_xml(["p-1"]))
    h.run()
    h.content = feed_xml([f"p-{i}" for i in range(1, 16)])      # 14 new items
    h.fail_on_call = 2                                          # 10 + 4: second message fails
    with pytest.raises(RuntimeError):
        h.run(max_items=14)
    assert len(titles(h)) == 10
    h.fail_on_call = None
    assert h.run(max_items=14) == 4                             # only the 4 that failed
    assert len(set(titles(h))) == 14                            # nothing posted twice

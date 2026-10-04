import asyncio
from types import SimpleNamespace

from bot.channels import ChannelRepo
from bot.config import FeedConfig
from bot.feeds.store import CustomFeedRepo


def make_app(conn, config_feeds):
    return SimpleNamespace(
        settings=SimpleNamespace(feeds=tuple(config_feeds), poll_interval_minutes=30, max_items_per_poll=10),
        seen=None,
        custom_feeds=CustomFeedRepo(conn),
        channels=ChannelRepo(conn),
    )


def test_all_feeds_merges_config_and_custom_with_config_winning(conn):
    from bot.cogs.feeds import FeedsCog

    app = make_app(conn, [FeedConfig("a", "A", "https://x/a", 1)])
    app.custom_feeds.add(FeedConfig("a", "Dup", "https://x/dup", 2))
    app.custom_feeds.add(FeedConfig("b", "B", "https://x/b", 3))

    async def build():
        return FeedsCog(SimpleNamespace(app=app))

    cog = asyncio.run(build())
    assert [(f.id, f.url) for f in cog.all_feeds()] == [("a", "https://x/a"), ("b", "https://x/b")]


def test_feed_id_pattern():
    from bot.cogs.feeds import FEED_ID_RE

    assert FEED_ID_RE.fullmatch("ai-ml-2")
    assert not FEED_ID_RE.fullmatch("Has Space")
    assert not FEED_ID_RE.fullmatch("x" * 41)


def test_removing_a_feed_forgets_what_was_seen_so_re_adding_does_not_burst(conn):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from bot.cogs.feeds import FeedsCog
    from bot.feeds.store import SeenStore
    from tests.fakes import fake_interaction

    app = make_app(conn, [])
    app.seen = SeenStore(conn)
    app.custom_feeds.add(FeedConfig("foo", "Foo", "https://x/foo", 3))
    app.seen.initialize("foo", ["a"], datetime(2026, 10, 4, tzinfo=timezone.utc))

    async def scenario():
        cog = FeedsCog(SimpleNamespace(app=app))
        await cog.remove_feed.callback(cog, fake_interaction(MagicMock(), MagicMock()), "foo")

    asyncio.run(scenario())
    assert app.custom_feeds.all() == []
    assert app.seen.is_initialized("foo") is False and app.seen.seen_ids("foo") == set()


def test_ready_feeds_resolve_topics_through_the_channel_bindings(conn):
    from bot.cogs.feeds import FeedsCog

    app = make_app(conn, [
        FeedConfig("tpami", "PAMI", "https://x/1", None, topic="ai-ml"),
        FeedConfig("tro", "Robotics", "https://x/2", None, topic="robotics"),
    ])
    app.custom_feeds.add(FeedConfig("custom", "Custom", "https://x/c", 99))
    app.channels.set("ai-ml", 10)

    async def build():
        return FeedsCog(SimpleNamespace(app=app))

    cog = asyncio.run(build())
    ready, skipped = cog.ready_feeds()
    assert [(f.id, f.channel_id) for f in ready] == [("tpami", 10), ("custom", 99)]
    assert [f.id for f in skipped] == ["tro"]            # topic "robotics" has no channel yet

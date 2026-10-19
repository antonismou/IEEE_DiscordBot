import asyncio
from types import SimpleNamespace

from bot.channels import ChannelRepo
from bot.config import FeedConfig
from bot.feeds.store import CustomFeedRepo, SeenStore
from bot.health import Health


def make_app(conn, config_feeds):
    return SimpleNamespace(
        settings=SimpleNamespace(
            feeds=tuple(config_feeds), poll_interval_minutes=30, max_items_per_poll=10, quiet_alert_days=7
        ),
        seen=SeenStore(conn),
        custom_feeds=CustomFeedRepo(conn),
        channels=ChannelRepo(conn),
        health=Health(),
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


def make_polling_cog(conn, poll_feed):
    from unittest.mock import AsyncMock

    from bot.channels import OFFICER_LOG
    from bot.cogs.feeds import FeedsCog

    app = make_app(conn, [FeedConfig("spectrum", "Spectrum", "https://x/s", 5)])
    app.channels.set(OFFICER_LOG, 42)
    channel = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(app=app, get_channel=lambda cid: channel if cid == 42 else None)

    async def build():
        cog = FeedsCog(bot)
        cog.poll_feed = poll_feed
        return cog

    return asyncio.run(build()), app, channel


def officer_messages(channel):
    return [call.args[0] for call in channel.send.await_args_list]


def test_a_feed_failing_three_polls_in_a_row_alerts_officers_once_then_reports_recovery(conn):
    from bot.feeds.parse import FeedError

    outcome = {"fail": True}

    async def poll_feed(feed):
        if outcome["fail"]:
            raise FeedError("HTTP 503 from https://x/s")
        return 2

    cog, app, channel = make_polling_cog(conn, poll_feed)
    for _ in range(2):
        asyncio.run(cog.poll_all())
    assert officer_messages(channel) == []                    # two failures: could be a blip, stay quiet
    asyncio.run(cog.poll_all())
    (alert,) = officer_messages(channel)
    assert "`spectrum`" in alert and "3 polls" in alert and "HTTP 503" in alert
    asyncio.run(cog.poll_all())
    assert len(officer_messages(channel)) == 1                # no repeat during the same outage

    outcome["fail"] = False
    asyncio.run(cog.poll_all())
    assert "working again" in officer_messages(channel)[-1]
    status = app.health.feeds["spectrum"]
    assert (status.failures, status.last_posted, status.last_error) == (0, 2, None)
    asyncio.run(cog.poll_all())
    assert len(officer_messages(channel)) == 2                # a normal good poll says nothing


def test_a_crash_counts_as_a_failure_without_leaking_its_message(conn):
    async def poll_feed(feed):
        raise RuntimeError("secret details")

    cog, app, channel = make_polling_cog(conn, poll_feed)
    for _ in range(3):
        asyncio.run(cog.poll_all())
    (alert,) = officer_messages(channel)
    assert "RuntimeError" in alert and "secret" not in alert


def test_feed_list_shows_each_feeds_status(conn):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from tests.fakes import fake_interaction, reply_text

    async def poll_feed(feed):
        return 0

    cog, app, _ = make_polling_cog(conn, poll_feed)
    app.custom_feeds.add(FeedConfig("new", "New", "https://x/n", 6))
    app.health.feed_ok("spectrum", 3, datetime(2026, 10, 4, tzinfo=timezone.utc))
    app.health.feed_failed("spectrum", "HTTP 404 from https://x/s")
    interaction = fake_interaction(MagicMock(), MagicMock())
    asyncio.run(cog.list_feeds.callback(cog, interaction))
    text = reply_text(interaction)
    assert "**failing** (1 poll(s) in a row: HTTP 404" in text and "last OK <t:" in text
    assert "not polled yet" in text                           # the custom feed


def test_split_message_keeps_lines_whole_and_under_the_limit():
    from bot.cogs.feeds import split_message

    chunks = split_message(["a" * 900, "b" * 900, "c" * 900], limit=2000)
    assert chunks == ["a" * 900 + "\n" + "b" * 900, "c" * 900]
    assert split_message([]) == []


def test_a_working_feed_with_nothing_new_for_7_days_alerts_once_even_across_restarts(conn, monkeypatch):
    from datetime import datetime, timedelta, timezone

    import bot.cogs.feeds as feeds_module

    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    now = {"t": start}
    monkeypatch.setattr(feeds_module, "utcnow", lambda: now["t"])

    async def poll_feed(feed):
        return 0

    cog, app, channel = make_polling_cog(conn, poll_feed)
    app.seen.initialize("spectrum", ["a"], start)
    now["t"] = start + timedelta(days=6, hours=23)
    asyncio.run(cog.poll_all())
    assert officer_messages(channel) == []                    # quiet, but not for 7 days yet

    now["t"] = start + timedelta(days=7)
    asyncio.run(cog.poll_all())
    (alert,) = officer_messages(channel)
    assert "`spectrum`" in alert and "nothing new" in alert and "7+ days" in alert

    restarted, _, channel2 = make_polling_cog(conn, poll_feed)  # same database, fresh in-memory state
    restarted.app.seen = app.seen
    asyncio.run(restarted.poll_all())
    assert officer_messages(channel2) == []                   # the alert is remembered in the database

    app.seen.mark_seen("spectrum", ["b"], now["t"])             # a new item arrived
    asyncio.run(cog.poll_all())
    assert "new posts again" in officer_messages(channel)[-1]
    assert app.seen.quiet_feeds() == []


def test_quiet_alerts_can_be_turned_off(conn, monkeypatch):
    from datetime import datetime, timedelta, timezone

    import bot.cogs.feeds as feeds_module

    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(feeds_module, "utcnow", lambda: start + timedelta(days=30))

    async def poll_feed(feed):
        return 0

    cog, app, channel = make_polling_cog(conn, poll_feed)
    app.settings.quiet_alert_days = 0
    app.seen.initialize("spectrum", ["a"], start)
    asyncio.run(cog.poll_all())
    assert officer_messages(channel) == []

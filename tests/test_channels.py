import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord

from bot.channels import OFFICER_LOG, ChannelRepo
from bot.config import FeedConfig
from tests.fakes import fake_guild, fake_interaction, fake_member, reply_text


def test_set_get_overwrite_clear_and_all(conn):
    repo = ChannelRepo(conn)
    assert repo.get("ai-ml") is None
    repo.set("ai-ml", 10)
    repo.set("robotics", 20)
    repo.set("ai-ml", 11)                       # overwrite
    assert repo.get("ai-ml") == 11 and repo.all() == {"ai-ml": 11, "robotics": 20}
    assert repo.clear("ai-ml") is True
    assert repo.clear("ai-ml") is False
    assert repo.get("ai-ml") is None


def make_cog(conn, feeds=()):
    from bot.cogs.channels import ChannelsCog

    app = SimpleNamespace(
        settings=SimpleNamespace(feeds=tuple(feeds)),
        channels=ChannelRepo(conn),
    )
    return ChannelsCog(SimpleNamespace(app=app)), app


FEEDS = [
    FeedConfig("tpami", "PAMI", "https://x/1", None, topic="ai-ml"),
    FeedConfig("tro", "Robotics", "https://x/2", None, topic="robotics"),
    FeedConfig("tnnls", "NNLS", "https://x/3", None, topic="ai-ml"),
]


def fake_channel(channel_id=123, *, can_send=True):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.mention = f"<#{channel_id}>"
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages=can_send, embed_links=True
    )
    return channel


def run_command(cog, command, *args):
    interaction = fake_interaction(fake_member(1), fake_guild())
    asyncio.run(command.callback(cog, interaction, *args))
    return interaction


def test_topics_come_from_the_feeds_plus_officer_log(conn):
    cog, _ = make_cog(conn, FEEDS)
    assert cog.topics() == ["ai-ml", "officer-log", "robotics"]


def test_set_stores_the_channel(conn):
    cog, app = make_cog(conn, FEEDS)
    interaction = run_command(cog, cog.set_channel, "ai-ml", fake_channel(123))
    assert app.channels.get("ai-ml") == 123
    assert "<#123>" in reply_text(interaction)


def test_set_accepts_the_officer_log_topic(conn):
    cog, app = make_cog(conn, FEEDS)
    run_command(cog, cog.set_channel, OFFICER_LOG, fake_channel(55))
    assert app.channels.get(OFFICER_LOG) == 55


def test_set_refuses_an_unknown_topic(conn):
    cog, app = make_cog(conn, FEEDS)
    interaction = run_command(cog, cog.set_channel, "typo-topic", fake_channel(123))
    assert app.channels.all() == {}
    text = reply_text(interaction)
    assert "Unknown topic" in text and "ai-ml" in text


def test_set_refuses_a_channel_the_bot_cannot_post_in(conn):
    cog, app = make_cog(conn, FEEDS)
    interaction = run_command(cog, cog.set_channel, "ai-ml", fake_channel(123, can_send=False))
    assert app.channels.get("ai-ml") is None
    assert "Send Messages" in reply_text(interaction)


def test_clear_unsets_a_topic(conn):
    cog, app = make_cog(conn, FEEDS)
    app.channels.set("ai-ml", 10)
    run_command(cog, cog.clear_channel, "ai-ml")
    assert app.channels.get("ai-ml") is None


def test_list_shows_each_topic_and_flags_unset_ones(conn):
    cog, app = make_cog(conn, FEEDS)
    app.channels.set("ai-ml", 10)
    text = reply_text(run_command(cog, cog.list_channels))
    assert "ai-ml" in text and "<#10>" in text
    assert "robotics" in text and "not set" in text
    assert "officer-log" in text

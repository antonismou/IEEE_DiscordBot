import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from bot.channels import OFFICER_LOG, ChannelRepo
from bot.config import FeedConfig
from bot.serversettings import ServerSettings
from tests.fakes import FakeRole, fake_guild, fake_interaction, fake_me, fake_member, reply_text

FEEDS = (
    FeedConfig("tpami", "PAMI", "https://x/1", None, topic="ai-ml"),
    FeedConfig("tro", "Robotics", "https://x/2", None, topic="robotics"),
)


def make_cog(conn):
    from bot.cogs.setup import SetupCog

    app = SimpleNamespace(
        settings=SimpleNamespace(feeds=FEEDS),
        server=ServerSettings(conn),
        channels=ChannelRepo(conn),
    )
    return SetupCog(SimpleNamespace(app=app)), app


def run_roles(cog, verified, officer, *, me=None, guild_id=111):
    guild = fake_guild()
    guild.me = me or fake_me()
    interaction = fake_interaction(fake_member(1, admin=True), guild)
    interaction.guild_id = guild_id
    asyncio.run(cog.setup_roles.callback(cog, interaction, verified, officer))
    return interaction


def test_setup_roles_saves_roles_and_locks_to_this_server(conn):
    cog, app = make_cog(conn)
    interaction = run_roles(cog, FakeRole(222), FakeRole(333))
    assert (app.server.guild_id, app.server.verified_role_id, app.server.officer_role_id) == (111, 222, 333)
    text = reply_text(interaction)
    assert "<@&222>" in text and "<@&333>" in text and "above" not in text     # no warning when all is well


def test_setup_roles_refuses_everyone_and_bot_roles(conn):
    cog, app = make_cog(conn)
    for bad in (FakeRole(1, default=True), FakeRole(2, managed=True)):
        text = reply_text(run_roles(cog, bad, FakeRole(333)))
        assert "normal role" in text
    assert app.server.configured is False


def test_setup_roles_refuses_the_same_role_for_both(conn):
    cog, app = make_cog(conn)
    text = reply_text(run_roles(cog, FakeRole(222), FakeRole(222)))
    assert "different roles" in text and app.server.configured is False


def test_setup_roles_warns_when_the_bot_role_is_too_low_but_still_saves(conn):
    cog, app = make_cog(conn)
    interaction = run_roles(cog, FakeRole(222, position=20), FakeRole(333), me=fake_me(top_position=5))
    assert app.server.verified_role_id == 222
    text = reply_text(interaction)
    assert "above" in text and "Manage Roles" in text


def test_setup_roles_warns_when_the_bot_lacks_manage_roles(conn):
    cog, _ = make_cog(conn)
    text = reply_text(run_roles(cog, FakeRole(222, position=1), FakeRole(333), me=fake_me(manage_roles=False)))
    assert "Manage Roles" in text


def test_status_lists_roles_and_flags_whatever_is_missing(conn):
    cog, app = make_cog(conn)
    app.channels.set("ai-ml", 10)
    interaction = fake_interaction(fake_member(1, admin=True), fake_guild())
    asyncio.run(cog.setup_status.callback(cog, interaction))
    text = reply_text(interaction)
    assert "not set up" in text                           # server and roles not configured yet
    assert "ai-ml" in text and "<#10>" in text
    assert "robotics" in text and "not set" in text and OFFICER_LOG in text


def test_status_after_setup_shows_the_roles(conn):
    cog, app = make_cog(conn)
    app.server.configure(guild_id=111, verified_role_id=222, officer_role_id=333)
    interaction = fake_interaction(fake_member(1, admin=True), fake_guild())
    asyncio.run(cog.setup_status.callback(cog, interaction))
    text = reply_text(interaction)
    assert "<@&222>" in text and "<@&333>" in text

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from bot.cogs.verify import CONSENT_TEXT, VerifyView, grant_role_and_reply
from bot.members import MemberRepo
from tests.fakes import fake_guild, fake_interaction, fake_member, followup_text

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
VERIFIED_ROLE = 5
OFFICER_CHANNEL = 55


def make_app(conn, *, log_channel=OFFICER_CHANNEL):
    return SimpleNamespace(
        settings=SimpleNamespace(verified_role_id=VERIFIED_ROLE, officer_log_channel_id=log_channel),
        members=MemberRepo(conn),
    )


def client_with_channel():
    channel = MagicMock()
    channel.send = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = channel
    return client, channel


def test_successful_grant_welcomes_the_member():
    async def scenario():
        user = fake_member(7)
        client, channel = client_with_channel()
        interaction = fake_interaction(user, fake_guild(), client)
        await grant_role_and_reply(SimpleNamespace(settings=SimpleNamespace(
            verified_role_id=VERIFIED_ROLE, officer_log_channel_id=OFFICER_CHANNEL)), interaction)
        return user, channel, interaction

    user, channel, interaction = asyncio.run(scenario())
    user.add_roles.assert_awaited_once()
    channel.send.assert_not_awaited()
    assert "You are verified" in followup_text(interaction)


def test_role_failure_tells_the_member_and_notifies_officers():
    async def scenario():
        user = fake_member(7)
        user.add_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
        client, channel = client_with_channel()
        interaction = fake_interaction(user, fake_guild(), client)
        app = SimpleNamespace(settings=SimpleNamespace(
            verified_role_id=VERIFIED_ROLE, officer_log_channel_id=OFFICER_CHANNEL))
        await grant_role_and_reply(app, interaction)
        return channel, interaction

    channel, interaction = asyncio.run(scenario())
    officer_text = channel.send.await_args.args[0]
    assert "<@7>" in officer_text and "role" in officer_text.lower()
    reply = followup_text(interaction)
    assert "couldn't assign the role" in reply and "press **Verify** again" in reply


def test_pressing_verify_again_restores_a_missing_role_for_a_stored_member(conn):
    async def scenario():
        app = make_app(conn)
        app.members.add_email_member(
            discord_id=7, email="real@tuc.gr", full_name="Real Student", consent_at=NOW, verified_at=NOW)
        user = fake_member(7, role_ids=())                 # stored, but never received the role
        interaction = fake_interaction(user, fake_guild(), MagicMock())
        view = VerifyView(app)
        await view.start.callback(interaction)
        return user, interaction

    user, interaction = asyncio.run(scenario())
    user.add_roles.assert_awaited_once()
    assert "restored" in followup_text(interaction)


def test_pressing_verify_when_already_holding_the_role_just_says_so(conn):
    async def scenario():
        app = make_app(conn)
        app.members.add_email_member(
            discord_id=7, email="real@tuc.gr", full_name="Real Student", consent_at=NOW, verified_at=NOW)
        user = fake_member(7, role_ids=(VERIFIED_ROLE,))
        interaction = fake_interaction(user, fake_guild(), MagicMock())
        await VerifyView(app).start.callback(interaction)
        return user, interaction

    user, interaction = asyncio.run(scenario())
    user.add_roles.assert_not_awaited()
    assert "already verified" in followup_text(interaction)


def test_consent_text_discloses_backup_retention():
    assert "backup" in CONSENT_TEXT.lower() and "7 days" in CONSENT_TEXT

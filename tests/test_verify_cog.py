import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from bot.channels import OFFICER_LOG, ChannelRepo
from bot.cogs.verify import CONSENT_TEXT, VerifyView, grant_role_and_reply
from bot.members import MemberRepo
from tests.fakes import fake_guild, fake_interaction, fake_member, followup_text

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
VERIFIED_ROLE = 5
OFFICER_CHANNEL = 55


def make_app(conn, *, log_channel=OFFICER_CHANNEL):
    channels = ChannelRepo(conn)
    if log_channel is not None:
        channels.set(OFFICER_LOG, log_channel)
    return SimpleNamespace(
        server=SimpleNamespace(verified_role_id=VERIFIED_ROLE, guild_id=111),
        members=MemberRepo(conn),
        channels=channels,
    )


def client_with_channel():
    channel = MagicMock()
    channel.send = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = channel
    return client, channel


def test_successful_grant_welcomes_the_member(conn):
    async def scenario():
        user = fake_member(7)
        client, channel = client_with_channel()
        interaction = fake_interaction(user, fake_guild(), client)
        await grant_role_and_reply(make_app(conn), interaction)
        return user, channel, interaction

    user, channel, interaction = asyncio.run(scenario())
    user.add_roles.assert_awaited_once()
    channel.send.assert_not_awaited()
    assert "You are verified" in followup_text(interaction)


def test_role_failure_tells_the_member_and_notifies_officers(conn):
    async def scenario():
        user = fake_member(7)
        user.add_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
        client, channel = client_with_channel()
        interaction = fake_interaction(user, fake_guild(), client)
        await grant_role_and_reply(make_app(conn), interaction)
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


def test_mail_failure_log_does_not_contain_the_recipient_address(conn, caplog):
    from bot.cogs.verify import start_verification
    from bot.mailer import MailError
    from bot.verification import VerificationService

    def failing_send(to, code, ttl):
        raise MailError("550 recipient victim@tuc.gr refused")

    async def scenario():
        members = MemberRepo(conn)
        app = SimpleNamespace(
            server=SimpleNamespace(verified_role_id=VERIFIED_ROLE),
            channels=ChannelRepo(conn),
            verification=VerificationService(conn, members),
            send_code=failing_send,
        )
        interaction = fake_interaction(fake_member(7), fake_guild())
        await start_verification(app, interaction, "Real Student", "victim@tuc.gr")
        app.verification.begin(7, "Real Student", "victim@tuc.gr")   # allowed at once: the send was given back
        return interaction

    interaction = asyncio.run(scenario())
    assert caplog.records and "victim@tuc.gr" not in caplog.text
    assert "couldn't send the email" in followup_text(interaction)


def test_setup_verify_explains_missing_channel_permissions():
    from bot.cogs.verify import VerifyCog

    async def scenario():
        cog = VerifyCog(SimpleNamespace(app=SimpleNamespace()))
        interaction = fake_interaction(fake_member(1), fake_guild())
        interaction.channel.send = AsyncMock(
            side_effect=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
        )
        await cog.setup_verify.callback(cog, interaction)
        return interaction

    interaction = asyncio.run(scenario())
    text = interaction.response.send_message.await_args.args[0]
    assert "Send Messages" in text and "View Channel" in text


def test_verify_buttons_say_so_when_the_server_is_not_set_up_yet(conn):
    from bot.serversettings import ServerSettings

    async def scenario():
        app = make_app(conn)
        app.server = ServerSettings(conn)                 # nothing configured
        interaction = fake_interaction(fake_member(7), fake_guild(), MagicMock())
        interaction.guild_id = 111
        view = VerifyView(app)
        await view.start.callback(interaction)
        await view.enter_code.callback(interaction)
        return interaction

    interaction = asyncio.run(scenario())
    texts = [call.args[0] for call in interaction.response.send_message.await_args_list]
    assert len(texts) == 2 and all("isn't set up yet" in t for t in texts)
    interaction.response.send_modal.assert_not_awaited()


def test_verify_buttons_refuse_other_servers(conn):
    from bot.serversettings import ServerSettings

    async def scenario():
        app = make_app(conn)
        app.server = ServerSettings(conn)
        app.server.configure(guild_id=111, verified_role_id=VERIFIED_ROLE, officer_role_id=9)
        interaction = fake_interaction(fake_member(7), fake_guild(), MagicMock())
        interaction.guild_id = 999
        await VerifyView(app).start.callback(interaction)
        return interaction

    interaction = asyncio.run(scenario())
    assert "different server" in interaction.response.send_message.await_args.args[0]

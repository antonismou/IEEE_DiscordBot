import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from bot.channels import OFFICER_LOG, ChannelRepo
from bot.health import Health
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
        server=SimpleNamespace(verified_role_id=VERIFIED_ROLE, guild_id=111, branch_role_ids={}, branch_descriptions={}, branch_labels={},
                               branch_pin_keys=lambda: set()),
        members=MemberRepo(conn),
        channels=channels,
        health=Health(),
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
            health=Health(),
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


# ---- one message per member: each step edits the previous one -------------------------------------------------

def stub_verification(app, *, begin=None, confirm_error=None, resend_=None):
    from bot.verification import PendingVerification

    def begin_(discord_id, name, email):
        if begin:
            raise begin
        return PendingVerification(email=email, code="123456", ttl_minutes=10)

    def confirm_(discord_id, code):
        if confirm_error:
            raise confirm_error

    app.verification = SimpleNamespace(
        begin=begin_, confirm=confirm_, send_failed=lambda _id: None, resend=resend_ or (lambda _id: begin_(_id, 'n', 'real@tuc.gr'))
    )
    app.send_code = lambda *args: None
    return app


def modal_with(modal, **values):
    for name, value in values.items():
        getattr(modal, name)._value = value
    return modal


def button_labels(view):
    return [child.label for child in view.children]


def test_first_step_replaces_the_consent_message_and_carries_the_enter_code_button(conn):
    from bot.cogs.verify import StartModal

    app = stub_verification(make_app(conn))
    interaction = fake_interaction(fake_member(7), fake_guild(), MagicMock())
    asyncio.run(modal_with(StartModal(app), full_name="Real Student", email="real@tuc.gr").on_submit(interaction))
    interaction.response.defer.assert_awaited_once_with()                  # "update the message", not a new one
    interaction.followup.send.assert_not_awaited()
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert "real@tuc.gr" in kwargs["content"] and button_labels(kwargs["view"]) == ["Enter code", "Resend code"]


def test_first_step_error_offers_try_again_in_the_same_message(conn):
    from bot.cogs.verify import StartModal
    from bot.verification import VerificationError

    app = stub_verification(make_app(conn), begin=VerificationError("Only @tuc.gr addresses are accepted."))
    interaction = fake_interaction(fake_member(7), fake_guild(), MagicMock())
    asyncio.run(modal_with(StartModal(app), full_name="Real Student", email="x@gmail.com").on_submit(interaction))
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert "tuc.gr" in kwargs["content"] and button_labels(kwargs["view"]) == ["Try again"]


def test_code_step_from_our_message_edits_it_into_the_branch_picker(conn):
    from bot.cogs.verify import CodeModal

    app = stub_verification(make_app(conn))
    app.server.branch_role_ids = {"main": 10, "cs": 11}
    app.members.add_email_member(
        discord_id=7, email="real@tuc.gr", full_name="Real Student", consent_at=NOW, verified_at=NOW)
    interaction = fake_interaction(fake_member(7), fake_guild(), MagicMock())
    asyncio.run(modal_with(CodeModal(app, replace=True), code="123456").on_submit(interaction))
    interaction.followup.send.assert_not_awaited()
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert "You are verified" in kwargs["content"] and kwargs["view"].children[0].placeholder


def test_wrong_code_keeps_the_enter_code_button_in_view(conn):
    from bot.cogs.verify import CodeModal
    from bot.verification import VerificationError

    app = stub_verification(make_app(conn), confirm_error=VerificationError("Wrong code. 4 attempt(s) left."))
    for replace in (True, False):
        interaction = fake_interaction(fake_member(7), fake_guild(), MagicMock())
        asyncio.run(modal_with(CodeModal(app, replace=replace), code="000000").on_submit(interaction))
        send = interaction.edit_original_response if replace else interaction.followup.send
        assert button_labels(send.await_args.kwargs["view"]) == ["Enter code", "Resend code"]


def test_code_step_from_the_public_panel_never_edits_the_panel(conn):
    from bot.cogs.verify import CodeModal

    app = stub_verification(make_app(conn))
    interaction = fake_interaction(fake_member(7), fake_guild(), MagicMock())
    asyncio.run(modal_with(CodeModal(app, replace=False), code="123456").on_submit(interaction))
    interaction.edit_original_response.assert_not_awaited()
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert followup_text(interaction)


def test_resend_button_sends_a_fresh_code_and_keeps_both_buttons(conn):
    from bot.cogs.verify import CodeView

    app = stub_verification(make_app(conn))
    sent = []
    app.send_code = lambda *args: sent.append(args)
    interaction = fake_interaction(fake_member(7), fake_guild(), MagicMock())
    asyncio.run(CodeView(app).resend.callback(interaction))
    interaction.response.defer.assert_awaited_once_with()          # edits the message the button is on
    assert len(sent) == 1
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert "real@tuc.gr" in kwargs["content"] and button_labels(kwargs["view"]) == ["Enter code", "Resend code"]


def test_resend_refusal_keeps_the_buttons(conn):
    from bot.cogs.verify import CodeView
    from bot.verification import VerificationError

    def refused(_id):
        raise VerificationError("Please wait 30 seconds before requesting another code.")

    app = stub_verification(make_app(conn), resend_=refused)
    interaction = fake_interaction(fake_member(7), fake_guild(), MagicMock())
    asyncio.run(CodeView(app).resend.callback(interaction))
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert "wait 30 seconds" in kwargs["content"] and button_labels(kwargs["view"]) == ["Enter code", "Resend code"]


def test_mail_failure_alerts_officers_at_most_once_an_hour(conn):
    from bot.cogs.verify import start_verification
    from bot.mailer import MailError

    app = stub_verification(make_app(conn))

    def failing(*args):
        raise MailError("535 bad credentials")

    app.send_code = failing
    client, channel = client_with_channel()
    for _ in range(3):
        interaction = fake_interaction(fake_member(7), fake_guild(), client)
        asyncio.run(start_verification(app, interaction, "Real Student", "real@tuc.gr"))
    assert channel.send.await_count == 1
    assert "/setup status" in channel.send.await_args.args[0] and "535" not in channel.send.await_args.args[0]


def test_successful_verification_is_written_to_the_audit_log(conn):
    from bot.channels import AUDIT_LOG
    from bot.cogs.verify import CodeModal

    app = stub_verification(make_app(conn))
    app.channels.set(AUDIT_LOG, 77)
    app.members.add_email_member(
        discord_id=7, email="real@tuc.gr", full_name="Real Student", consent_at=NOW, verified_at=NOW)
    client, channel = client_with_channel()
    interaction = fake_interaction(fake_member(7), fake_guild(), client)
    asyncio.run(modal_with(CodeModal(app, replace=True), code="123456").on_submit(interaction))
    line = channel.send.await_args_list[0].args[0]
    assert "<@7> verified by email" in line and "real@tuc.gr" not in line and "Real Student" not in line

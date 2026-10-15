from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.branches import picker_for, set_nickname
from bot.channels import OFFICER_LOG
from bot.audit import audit
from bot.checks import officer_only
from bot.gate import gate_message
from bot.db import utcnow
from bot.mailer import MailError
from bot.replies import defer, reply
from bot.roles import notify_officers, set_verified_role
from bot.verification import VerificationError

log = logging.getLogger(__name__)

PANEL_TEXT = (
    "**Verify that you are a TUC student**\n"
    "Press **Verify**, enter your name and your `@tuc.gr` email, and we will email you a 6-digit code. "
    "Then press **Enter code** to unlock the server."
)

CONSENT_TEXT = (
    "To verify you, the IEEE Student Branch of TUC will store your **full name**, your **@tuc.gr email** "
    "and your **Discord ID**. They are used only to confirm membership and are visible only to branch "
    "officers. You can delete your data at any time with `/forget-me`; copies in the nightly backups are "
    "deleted within 7 days.\n\nPress **I agree** to continue."
)


async def grant_role_and_reply(app, interaction: discord.Interaction, *, restored: bool = False, replace: bool = False) -> None:
    """Give the Verified role to a member whose verification is stored, and tell them (and officers) the result."""
    ok = await set_verified_role(
        interaction.guild, interaction.user, app.server.verified_role_id,
        add=True, reason="TUC email verified",
    )
    view = None
    if ok:
        text = "Your Verified role was restored." if restored else "You are verified. Welcome!"
        member = app.members.get(interaction.user.id)
        if member and not await set_nickname(interaction.user, member.full_name):
            await notify_officers(
                interaction.client, app.channels.get(OFFICER_LOG),
                f"I could not set the nickname of <@{interaction.user.id}> to their verified name "
                "(the server owner and members above my role can't be renamed; I also need Manage Nicknames).",
            )
        view = picker_for(app, interaction.user)
        if view:
            text += " Now choose your branch(es):"
    else:
        await notify_officers(
            interaction.client, app.channels.get(OFFICER_LOG),
            f"I could not assign the Verified role to <@{interaction.user.id}> although their verification is stored. "
            "Check that my role is above Verified and that I have Manage Roles; the member can press **Verify** "
            "again once it is fixed.",
        )
        text = (
            "Your email is verified, but I couldn't assign the role. Officers were notified; "
            "press **Verify** again later to retry."
        )
    await reply(interaction, text, replace=replace, view=view)


async def start_verification(
    app, interaction: discord.Interaction, raw_name: str, raw_email: str, *, replace: bool = False
) -> None:
    """Validate, email a code and tell the member. Never logs the address."""
    retry = RetryView(app) if replace else None
    try:
        pending = app.verification.begin(interaction.user.id, raw_name, raw_email)
    except VerificationError as exc:
        await reply(interaction, str(exc), replace=replace, view=retry)
        return
    await deliver_code(app, interaction, pending, replace=replace, failure_view=retry)


async def resend_verification(app, interaction: discord.Interaction, *, replace: bool) -> None:
    """Send a fresh code for the verification in progress. Limits and failure handling match the first send."""
    try:
        pending = app.verification.resend(interaction.user.id)
    except VerificationError as exc:
        await reply(interaction, str(exc), replace=replace, view=CodeView(app))
        return
    await deliver_code(app, interaction, pending, replace=replace, failure_view=RetryView(app) if replace else None)


async def deliver_code(app, interaction: discord.Interaction, pending, *, replace: bool, failure_view) -> None:
    try:
        await asyncio.to_thread(app.send_code, pending.email, pending.code, pending.ttl_minutes)
    except MailError as exc:
        # the exception text can contain the recipient address (e.g. "recipient refused"): log the class only
        log.error("Could not send verification email (%s)", type(exc.__cause__ or exc).__name__)
        app.verification.send_failed(interaction.user.id)
        if app.health.should_alert_mail(utcnow()):
            await notify_officers(
                interaction.client, app.channels.get(OFFICER_LOG),
                "I could not send a verification email (e.g. the Gmail app password was revoked or Gmail is "
                f"unreachable). Run `/setup status` to test the mail login. ({type(exc.__cause__ or exc).__name__})",
            )
        await reply(
            interaction, "I couldn't send the email right now. Please try again later or ask an officer.",
            replace=replace, view=failure_view,
        )
        return
    await reply(
        interaction,
        f"A code was sent to **{pending.email}** (check spam too). "
        f"Press **Enter code** within {pending.ttl_minutes} minutes. "
        "Not received? Press **Resend code**, or ask an officer to verify you manually.",
        replace=replace, view=CodeView(app),
    )


class StartModal(discord.ui.Modal, title="TUC verification"):
    full_name = discord.ui.TextInput(label="Full name", min_length=2, max_length=100)
    email = discord.ui.TextInput(label="Academic email (@tuc.gr)", placeholder="name@tuc.gr", max_length=100)

    def __init__(self, app):
        super().__init__()
        self.app = app

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await defer(interaction, replace=True)  # opened from our private message: that message becomes the next step
        await start_verification(self.app, interaction, str(self.full_name), str(self.email), replace=True)


class CodeModal(discord.ui.Modal, title="Enter your code"):
    code = discord.ui.TextInput(label="6-digit code", min_length=6, max_length=12)

    def __init__(self, app, *, replace: bool):
        super().__init__()
        self.app = app
        self.replace = replace  # False when opened from the public panel, which must never be edited

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await defer(interaction, replace=self.replace)
        try:
            self.app.verification.confirm(interaction.user.id, str(self.code))
        except VerificationError as exc:
            await reply(interaction, str(exc), replace=self.replace, view=CodeView(self.app))
            return
        await audit(self.app, interaction, "verified by email")
        await grant_role_and_reply(self.app, interaction, replace=self.replace)


class ConsentView(discord.ui.View):
    def __init__(self, app):
        super().__init__(timeout=300)
        self.app = app

    @discord.ui.button(label="I agree", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(StartModal(self.app))


class RetryView(discord.ui.View):
    """Shown under a failed first step so the member can try again without going back to the panel."""

    def __init__(self, app):
        super().__init__(timeout=300)
        self.app = app

    @discord.ui.button(label="Try again", style=discord.ButtonStyle.success)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(StartModal(self.app))


class CodeView(discord.ui.View):
    """The Enter code and Resend code buttons, kept right under the message that asks for the code."""

    def __init__(self, app):
        super().__init__(timeout=900)
        self.app = app

    @discord.ui.button(label="Enter code", style=discord.ButtonStyle.primary)
    async def enter_code(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CodeModal(self.app, replace=True))

    @discord.ui.button(label="Resend code", style=discord.ButtonStyle.secondary)
    async def resend(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await defer(interaction, replace=True)
        await resend_verification(self.app, interaction, replace=True)


class VerifyView(discord.ui.View):
    def __init__(self, app):
        super().__init__(timeout=None)
        self.app = app

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="verify:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        refusal = gate_message(self.app.server, interaction.guild_id, None)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if self.app.members.get(interaction.user.id):
            role_id = self.app.server.verified_role_id
            await interaction.response.defer(ephemeral=True, thinking=True)
            if any(role.id == role_id for role in interaction.user.roles):
                picker = picker_for(self.app, interaction.user)
                await interaction.followup.send(
                    "You are already verified." + (" You can change your branches here:" if picker else ""),
                    ephemeral=True, **({"view": picker} if picker else {}),
                )
            else:  # stored but the role never arrived (e.g. the bot lacked permissions at the time)
                await grant_role_and_reply(self.app, interaction, restored=True)
            return
        await interaction.response.send_message(CONSENT_TEXT, view=ConsentView(self.app), ephemeral=True)

    @discord.ui.button(label="Enter code", style=discord.ButtonStyle.primary, custom_id="verify:code")
    async def enter_code(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        refusal = gate_message(self.app.server, interaction.guild_id, None)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        await interaction.response.send_modal(CodeModal(self.app, replace=False))


class VerifyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(VerifyView(self.bot.app))  # keeps the buttons working after a restart

    @app_commands.command(name="setup-verify", description="Post the verification panel in this channel")
    @app_commands.guild_only()
    @officer_only()
    async def setup_verify(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.channel.send(PANEL_TEXT, view=VerifyView(self.bot.app))
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't post in this channel. Give my role **View Channel** and **Send Messages** here, "
                "then run the command again.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("Panel posted.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(VerifyCog(bot))

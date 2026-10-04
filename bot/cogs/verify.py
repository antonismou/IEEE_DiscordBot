from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.checks import officer_only
from bot.mailer import MailError
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


async def grant_role_and_reply(app, interaction: discord.Interaction, *, restored: bool = False) -> None:
    """Give the Verified role to a member whose verification is stored, and tell them (and officers) the result."""
    ok = await set_verified_role(
        interaction.guild, interaction.user, app.settings.verified_role_id,
        add=True, reason="TUC email verified",
    )
    if ok:
        text = "Your Verified role was restored." if restored else "You are verified. Welcome!"
    else:
        await notify_officers(
            interaction.client, app.settings.officer_log_channel_id,
            f"I could not assign the Verified role to <@{interaction.user.id}> although their verification is stored. "
            "Check that my role is above Verified and that I have Manage Roles; the member can press **Verify** "
            "again once it is fixed.",
        )
        text = (
            "Your email is verified, but I couldn't assign the role. Officers were notified; "
            "press **Verify** again later to retry."
        )
    await interaction.followup.send(text, ephemeral=True)


async def start_verification(app, interaction: discord.Interaction, raw_name: str, raw_email: str) -> None:
    """Validate, email a code and tell the member. Never logs the address."""
    try:
        pending = app.verification.begin(interaction.user.id, raw_name, raw_email)
    except VerificationError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    try:
        await asyncio.to_thread(app.send_code, pending.email, pending.code, pending.ttl_minutes)
    except MailError as exc:
        # the exception text can contain the recipient address (e.g. "recipient refused"): log the class only
        log.error("Could not send verification email (%s)", type(exc.__cause__ or exc).__name__)
        app.verification.send_failed(interaction.user.id)
        await interaction.followup.send(
            "I couldn't send the email right now. Please try again later or ask an officer.", ephemeral=True
        )
        return
    await interaction.followup.send(
        f"A code was sent to **{pending.email}** (check spam too). "
        f"Press **Enter code** within {pending.ttl_minutes} minutes.",
        ephemeral=True,
    )


class StartModal(discord.ui.Modal, title="TUC verification"):
    full_name = discord.ui.TextInput(label="Full name", min_length=2, max_length=100)
    email = discord.ui.TextInput(label="Academic email (@tuc.gr)", placeholder="name@tuc.gr", max_length=100)

    def __init__(self, app):
        super().__init__()
        self.app = app

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await start_verification(self.app, interaction, str(self.full_name), str(self.email))


class CodeModal(discord.ui.Modal, title="Enter your code"):
    code = discord.ui.TextInput(label="6-digit code", min_length=6, max_length=12)

    def __init__(self, app):
        super().__init__()
        self.app = app

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            self.app.verification.confirm(interaction.user.id, str(self.code))
        except VerificationError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await grant_role_and_reply(self.app, interaction)


class ConsentView(discord.ui.View):
    def __init__(self, app):
        super().__init__(timeout=300)
        self.app = app

    @discord.ui.button(label="I agree", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(StartModal(self.app))


class VerifyView(discord.ui.View):
    def __init__(self, app):
        super().__init__(timeout=None)
        self.app = app

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="verify:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.app.members.get(interaction.user.id):
            role_id = self.app.settings.verified_role_id
            await interaction.response.defer(ephemeral=True, thinking=True)
            if any(role.id == role_id for role in interaction.user.roles):
                await interaction.followup.send("You are already verified.", ephemeral=True)
            else:  # stored but the role never arrived (e.g. the bot lacked permissions at the time)
                await grant_role_and_reply(self.app, interaction, restored=True)
            return
        await interaction.response.send_message(CONSENT_TEXT, view=ConsentView(self.app), ephemeral=True)

    @discord.ui.button(label="Enter code", style=discord.ButtonStyle.primary, custom_id="verify:code")
    async def enter_code(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CodeModal(self.app))


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

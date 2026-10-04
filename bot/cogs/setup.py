from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.channels import OFFICER_LOG, topics_for
from bot.checks import admin_only

NO_MENTIONS = discord.AllowedMentions.none()


class SetupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @property
    def app(self):
        return self.bot.app

    setup_group = app_commands.Group(
        name="setup", description="First-time setup (administrators)", guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @setup_group.command(name="roles", description="Choose the Verified and Officer roles for this server")
    @admin_only()
    async def setup_roles(self, interaction: discord.Interaction, verified: discord.Role, officer: discord.Role) -> None:
        for label, role in (("Verified", verified), ("Officer", officer)):
            if role.is_default() or role.managed:
                await interaction.response.send_message(
                    f"The {label} role can't be @everyone or a bot/integration role. Pick a normal role.",
                    ephemeral=True,
                )
                return
        if verified.id == officer.id:
            await interaction.response.send_message(
                "Verified and Officer must be different roles, otherwise every verified member would be an officer.",
                ephemeral=True,
            )
            return
        self.app.server.configure(
            guild_id=interaction.guild_id, verified_role_id=verified.id, officer_role_id=officer.id
        )
        me = interaction.guild.me
        warning = ""
        if not me.guild_permissions.manage_roles or not me.top_role > verified:
            warning = (
                f"\n\n**Warning:** I can't assign {verified.mention} yet. Give my role **Manage Roles** and drag it "
                "**above** the Verified role in Server Settings -> Roles."
            )
        await interaction.response.send_message(
            f"Saved. Verified role: {verified.mention}. Officer role: {officer.mention}. "
            f"Administrators can always use officer commands too.{warning}",
            ephemeral=True, allowed_mentions=NO_MENTIONS,
        )

    @setup_group.command(name="status", description="Show what is set up and what is still missing")
    @admin_only()
    async def setup_status(self, interaction: discord.Interaction) -> None:
        server, bound = self.app.server, self.app.channels.all()

        def role(role_id):
            return f"<@&{role_id}>" if role_id else "**not set**"

        lines = [
            f"**Server:** {'set up' if server.configured else '**not set up** (run `/setup roles`)'}",
            f"**Verified role:** {role(server.verified_role_id)}",
            f"**Officer role:** {role(server.officer_role_id)}",
            "**Channels** (`/channel set`):",
        ]
        for topic in topics_for(self.app.settings.feeds):
            where = f"<#{bound[topic]}>" if topic in bound else "**not set**"
            note = " (optional)" if topic == OFFICER_LOG and topic not in bound else ""
            lines.append(f"- `{topic}` -> {where}{note}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True, allowed_mentions=NO_MENTIONS)


async def setup(bot) -> None:
    await bot.add_cog(SetupCog(bot))

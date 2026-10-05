from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.branches import picker_for
from bot.checks import officer_only
from bot.gate import gate_message

PANEL_TEXT = (
    "**Choose your branches**\n"
    "Press **Choose my roles** and pick one or more of Main Branch, CS, IAS and Quantum. "
    "You can come back here and change them whenever you like."
)


class RolesView(discord.ui.View):
    def __init__(self, app):
        super().__init__(timeout=None)
        self.app = app

    @discord.ui.button(label="Choose my roles", style=discord.ButtonStyle.primary, custom_id="roles:open")
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        refusal = gate_message(self.app.server, interaction.guild_id, None)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if not any(role.id == self.app.server.verified_role_id for role in interaction.user.roles):
            await interaction.response.send_message("Verify first in the verify channel.", ephemeral=True)
            return
        picker = picker_for(self.app, interaction.user)
        if picker is None:
            await interaction.response.send_message(
                "The branch roles aren't set up yet. Ask an administrator to run `/setup branches`.", ephemeral=True
            )
            return
        await interaction.response.send_message("Pick your branches:", view=picker, ephemeral=True)


class BranchesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(RolesView(self.bot.app))  # keeps the button working after a restart

    @app_commands.command(name="setup-roles", description="Post the branch role panel in this channel")
    @app_commands.guild_only()
    @officer_only()
    async def setup_roles_panel(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.channel.send(PANEL_TEXT, view=RolesView(self.bot.app))
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't post in this channel. Give my role **View Channel** and **Send Messages** here, "
                "then run the command again.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("Panel posted.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(BranchesCog(bot))

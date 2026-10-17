from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.branches import picker_for
from bot.checks import officer_only
from bot.gate import gate_message

log = logging.getLogger(__name__)

PANEL_COLOUR = discord.Colour(0x00629B)  # IEEE blue


def panel_embed(server) -> discord.Embed:
    """The role panel: every branch members can pick right now, with its description and whether it needs a PIN."""
    ids, labels = server.branch_role_ids, server.branch_labels
    descriptions, pins = server.branch_descriptions, server.branch_pin_keys()
    lines = []
    for key, rid in ids.items():
        line = f"**•** <@&{rid}>" + (" 🔒" if key in pins else "")
        if descriptions.get(key):
            line += f"\n> {descriptions[key]}"
        lines.append(line)
    embed = discord.Embed(
        title="Choose your branch roles",
        description=(
            "Press **Choose my roles** below and pick one or more. "
            "You can come back here and change them whenever you like.\n\n"
            + (f"**Roles you can choose ({len(ids)})**\n" + "\n".join(lines) if lines else "*No branches yet.*")
        ),
        colour=PANEL_COLOUR,
    )
    if pins:
        embed.set_footer(text="🔒 needs a PIN: ask an officer of that branch for it.")
    return embed


async def refresh_roles_panel(app, client) -> bool:
    """Show the current branches on the posted role panel. False if there is no panel or it can't be edited.
    Never raises: the branch change that triggered this is already saved."""
    where = app.server.roles_panel
    if where is None:
        return False
    channel_id, message_id = where
    try:
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        await message.edit(content=None, embed=panel_embed(app.server))
    except discord.NotFound:
        app.server.set_roles_panel(None)  # deleted by someone: stop trying
        return False
    except Exception:
        log.exception("Could not update the role panel")
        return False
    return True


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
                "The branch roles aren't set up yet. Ask an administrator to run `/setup branch-add`.", ephemeral=True
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
        app = self.bot.app
        old = app.server.roles_panel
        try:
            message = await interaction.channel.send(embed=panel_embed(app.server), view=RolesView(app))
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't post in this channel. Give my role **View Channel** and **Send Messages** here, "
                "then run the command again.",
                ephemeral=True,
            )
            return
        app.server.set_roles_panel(message.channel.id, message.id)
        await interaction.response.send_message(
            "Panel posted. It updates by itself when branches are added, removed or changed.", ephemeral=True
        )
        if old is not None:  # only one panel stays up to date, so take the previous one down
            try:
                channel = interaction.client.get_channel(old[0]) or await interaction.client.fetch_channel(old[0])
                await (await channel.fetch_message(old[1])).delete()
            except Exception:
                log.info("Could not delete the previous role panel (already gone?)")


async def setup(bot) -> None:
    await bot.add_cog(BranchesCog(bot))

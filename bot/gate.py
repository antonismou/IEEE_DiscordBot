from __future__ import annotations

import discord
from discord import app_commands

from bot.checks import Refused


def gate_message(server, guild_id: int | None, command_root: str | None) -> str | None:
    """Why this interaction must be refused, or None. `command_root` is None for button presses."""
    if server.guild_id is None:
        if command_root == "setup":
            return None
        return "This server isn't set up yet. An administrator must run `/setup roles` first."
    if guild_id != server.guild_id:
        return "This bot is set up for a different server."
    return None


class GuildLockedTree(app_commands.CommandTree):
    """Refuses commands from any server other than the one /setup roles was run in."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command = interaction.command
        root = command.qualified_name.split()[0] if command else None
        refusal = gate_message(interaction.client.app.server, interaction.guild_id, root)
        if refusal:
            raise Refused(refusal)
        return True

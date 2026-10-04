from __future__ import annotations

import logging

import discord
from discord import app_commands

log = logging.getLogger(__name__)


def is_officer(member, officer_role_id: int) -> bool:
    return any(role.id == officer_role_id for role in getattr(member, "roles", []))


def officer_only():
    def predicate(interaction: discord.Interaction) -> bool:
        return is_officer(interaction.user, interaction.client.app.settings.officer_role_id)

    return app_commands.check(predicate)


async def handle_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        message = "Only branch officers can use this command."
    else:
        log.error("Command error", exc_info=error)
        message = "Something went wrong. Please try again or tell an officer."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)

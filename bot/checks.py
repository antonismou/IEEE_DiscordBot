from __future__ import annotations

import logging

import discord
from discord import app_commands

log = logging.getLogger(__name__)


class Refused(app_commands.CheckFailure):
    """A check failure whose message is shown to the user as it is."""


def is_admin(member) -> bool:
    permissions = getattr(member, "guild_permissions", None)  # the server owner has every permission
    return bool(permissions and permissions.administrator)


def is_officer(member, officer_role_id: int | None) -> bool:
    """Server administrators always count; everyone else needs the officer role (once it is set)."""
    if is_admin(member):
        return True
    return officer_role_id is not None and any(role.id == officer_role_id for role in getattr(member, "roles", []))


def officer_only():
    def predicate(interaction: discord.Interaction) -> bool:
        return is_officer(interaction.user, interaction.client.app.server.officer_role_id)

    return app_commands.check(predicate)


def admin_only():
    def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user):
            raise Refused("Only server administrators can use this command.")
        return True

    return app_commands.check(predicate)


async def handle_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, Refused):
        message = str(error)
    elif isinstance(error, app_commands.CheckFailure):
        message = "Only branch officers can use this command."
    else:
        log.error("Command error", exc_info=error)
        message = "Something went wrong. Please try again or tell an officer."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)

from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)


async def set_verified_role(guild, member, role_id: int, *, add: bool, reason: str) -> bool:
    role = guild.get_role(role_id)
    if role is None:
        log.error("Role %s not found in guild", role_id)
        return False
    try:
        if add:
            await member.add_roles(role, reason=reason)
        else:
            await member.remove_roles(role, reason=reason)
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning(
            "Could not %s role %s for member id %s: %s",
            "add" if add else "remove", role_id, getattr(member, "id", "?"), exc,
        )
        return False
    return True


async def resolve_member(guild, user):
    """A guild Member for `user`, or None if they left.

    The bot runs without the members intent, so guild.get_member() is usually empty: fall back to the API.
    """
    if isinstance(user, discord.Member):
        return user
    member = guild.get_member(user.id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user.id)
    except discord.NotFound:
        return None


async def notify_officers(client, channel_id: int | None, text: str) -> None:
    if channel_id is None:
        return
    try:
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Could not notify officers in channel %s", channel_id)

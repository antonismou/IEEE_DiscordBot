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
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Could not %s role %s for member %s", "add" if add else "remove", role_id, member)
        return False
    return True

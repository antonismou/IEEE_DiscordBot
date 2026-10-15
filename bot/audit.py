from __future__ import annotations

import logging

import discord

from bot.channels import AUDIT_LOG
from bot.db import utcnow
from bot.roles import notify_officers

log = logging.getLogger(__name__)


async def audit(app, interaction: discord.Interaction, text: str) -> None:
    """Post "<who> <text> · <time>" to the audit-log channel, if one is set. Never raises: an audit failure must
    not undo or hide the action that was just done. Only Discord mentions and the action go in: no emails, names
    or PINs."""
    try:
        stamp = int(utcnow().timestamp())
        await notify_officers(
            interaction.client, app.channels.get(AUDIT_LOG),
            f"<@{interaction.user.id}> {text} · <t:{stamp}:f>",
        )
    except Exception:
        log.exception("Could not write the audit log")

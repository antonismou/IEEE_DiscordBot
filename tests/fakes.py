from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord


def fake_member(user_id=1, role_ids=(), bot=False):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = bot
    member.roles = [SimpleNamespace(id=r) for r in role_ids]
    member.mention = f"<@{user_id}>"
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


def fake_guild(*, role=None, cached=None, fetched=None, fetch_error=None):
    """A guild whose member cache is empty unless `cached` is given (as with Intents.default())."""
    role = role if role is not None else object()
    fetch = AsyncMock(return_value=fetched, side_effect=fetch_error)
    return SimpleNamespace(
        get_role=lambda role_id: role,
        get_member=lambda user_id: cached,
        fetch_member=fetch,
    )


def fake_interaction(user, guild, client=None):
    interaction = MagicMock()
    interaction.user = user
    interaction.guild = guild
    interaction.client = client or MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.is_done.return_value = False
    interaction.followup.send = AsyncMock()
    return interaction


def followup_text(interaction) -> str:
    args, kwargs = interaction.followup.send.await_args
    return args[0] if args else kwargs.get("content", "")

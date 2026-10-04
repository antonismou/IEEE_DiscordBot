from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord


def fake_member(user_id=1, role_ids=(), bot=False, admin=False):
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = bot
    member.roles = [SimpleNamespace(id=r) for r in role_ids]
    member.mention = f"<@{user_id}>"
    member.guild_permissions = discord.Permissions(administrator=admin)
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
        me=object(),
    )


def fake_interaction(user, guild, client=None):
    interaction = MagicMock()
    interaction.user = user
    interaction.guild = guild
    interaction.guild_id = 111
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


def reply_text(interaction) -> str:
    """Text of the interaction's reply, whether it used send_message or defer + followup."""
    if interaction.followup.send.await_args is not None:
        return followup_text(interaction)
    args, kwargs = interaction.response.send_message.await_args
    return args[0] if args else kwargs.get("content", "")


class FakeRole:
    def __init__(self, role_id, position=1, managed=False, default=False):
        self.id, self.position, self.managed, self._default = role_id, position, managed, default
        self.mention = f"<@&{role_id}>"

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __gt__(self, other):
        return self.position > other.position

    def __le__(self, other):
        return self.position <= other.position

    def __ge__(self, other):
        return self.position >= other.position


def fake_me(top_position=10, manage_roles=True):
    return SimpleNamespace(
        top_role=FakeRole(0, position=top_position),
        guild_permissions=discord.Permissions(manage_roles=manage_roles),
    )

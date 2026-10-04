import asyncio
from types import SimpleNamespace

import discord

from bot.roles import set_verified_role


class FakeMember:
    def __init__(self, error=None):
        self.added, self.removed, self.error = [], [], error

    async def add_roles(self, role, reason=None):
        if self.error:
            raise self.error
        self.added.append(role)

    async def remove_roles(self, role, reason=None):
        if self.error:
            raise self.error
        self.removed.append(role)


def guild_with(role):
    return SimpleNamespace(get_role=lambda role_id: role)


def run(coro):
    return asyncio.run(coro)


def test_adds_role():
    member, role = FakeMember(), object()
    assert run(set_verified_role(guild_with(role), member, 5, add=True, reason="r")) is True
    assert member.added == [role]


def test_removes_role():
    member, role = FakeMember(), object()
    assert run(set_verified_role(guild_with(role), member, 5, add=False, reason="r")) is True
    assert member.removed == [role]


def test_missing_role_returns_false():
    assert run(set_verified_role(guild_with(None), FakeMember(), 5, add=True, reason="r")) is False


def test_forbidden_returns_false():
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "missing permissions")
    assert run(set_verified_role(guild_with(object()), FakeMember(forbidden), 5, add=True, reason="r")) is False


# --- resolve_member / notify_officers -------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from bot.roles import notify_officers, resolve_member  # noqa: E402
from tests.fakes import fake_guild, fake_member  # noqa: E402


def test_resolve_member_returns_a_member_unchanged_without_any_lookup():
    member = fake_member(7)
    guild = fake_guild()
    assert run(resolve_member(guild, member)) is member
    guild.fetch_member.assert_not_awaited()


def test_resolve_member_uses_the_cache_before_the_api():
    cached = fake_member(7)
    guild = fake_guild(cached=cached)
    assert run(resolve_member(guild, SimpleNamespace(id=7))) is cached
    guild.fetch_member.assert_not_awaited()


def test_resolve_member_falls_back_to_the_api_when_the_cache_is_empty():
    fetched = fake_member(7)
    guild = fake_guild(fetched=fetched)
    assert run(resolve_member(guild, SimpleNamespace(id=7))) is fetched
    guild.fetch_member.assert_awaited_once_with(7)


def test_resolve_member_returns_none_for_someone_who_left():
    gone = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown member")
    guild = fake_guild(fetch_error=gone)
    assert run(resolve_member(guild, SimpleNamespace(id=7))) is None


def test_notify_officers_sends_without_pinging():
    channel = MagicMock()
    channel.send = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = channel
    run(notify_officers(client, 55, "role problem"))
    args, kwargs = channel.send.await_args
    assert args == ("role problem",) and kwargs["allowed_mentions"].everyone is False


def test_notify_officers_without_a_channel_does_nothing():
    client = MagicMock()
    run(notify_officers(client, None, "role problem"))
    client.get_channel.assert_not_called()


def test_notify_officers_swallows_discord_errors():
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no access")
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=forbidden)
    client = MagicMock()
    client.get_channel.return_value = channel
    run(notify_officers(client, 55, "role problem"))   # must not raise


def test_role_failure_log_does_not_contain_the_member_name(caplog):
    class Named(FakeMember):
        def __repr__(self):
            return "Alice Smith#1234"

        __str__ = __repr__

    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "missing permissions")
    run(set_verified_role(guild_with(object()), Named(forbidden), 5, add=True, reason="r"))
    assert caplog.records and "Alice Smith" not in caplog.text

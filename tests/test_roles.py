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

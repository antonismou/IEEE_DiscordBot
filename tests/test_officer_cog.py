import asyncio
from types import SimpleNamespace


def test_officer_cog_registers_expected_commands():
    from bot.cogs.officer import OfficerCog

    async def build():
        return OfficerCog(SimpleNamespace(app=SimpleNamespace()))

    cog = asyncio.run(build())
    names = {command.name for command in cog.get_app_commands()}
    assert {"verify-manual", "unverify", "member", "forget-me"} <= names
    member_group = next(c for c in cog.get_app_commands() if c.name == "member")
    assert {c.name for c in member_group.commands} == {"lookup", "export", "delete"}

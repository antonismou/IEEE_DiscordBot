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


# --- behaviour of the commands, with a Discord whose member cache is empty -----------------------------

from datetime import datetime, timezone  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import discord  # noqa: E402

from bot.members import MemberRepo  # noqa: E402
from bot.verification import VerificationService  # noqa: E402
from tests.fakes import fake_guild, fake_interaction, fake_member, followup_text  # noqa: E402

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def make_cog(conn):
    from bot.cogs.officer import OfficerCog

    members = MemberRepo(conn)
    app = SimpleNamespace(
        settings=SimpleNamespace(verified_role_id=5, officer_role_id=9, officer_log_channel_id=None),
        members=members,
        verification=VerificationService(conn, members),
    )
    return OfficerCog(SimpleNamespace(app=app)), app


def test_forget_me_removes_the_role_even_when_the_member_cache_is_empty(conn):
    async def scenario():
        cog, app = make_cog(conn)
        app.members.add_manual_member(discord_id=7, full_name="Zed", verified_by=1, note="n", verified_at=NOW)
        user = fake_member(7)
        interaction = fake_interaction(user, fake_guild())      # guild.get_member() returns None
        await cog.forget_me.callback(cog, interaction)
        return app, user, interaction

    app, user, interaction = asyncio.run(scenario())
    user.remove_roles.assert_awaited_once()
    assert app.members.get(7) is None
    assert followup_text(interaction) == "Stored data deleted."
    interaction.response.defer.assert_awaited_once()            # replies only after the slow role call


def test_forget_me_also_clears_a_pending_verification(conn):
    async def scenario():
        cog, app = make_cog(conn)
        app.verification.begin(7, "Zed Zed", "zed@tuc.gr")
        await cog.forget_me.callback(cog, fake_interaction(fake_member(7), fake_guild()))
        return conn.execute("SELECT COUNT(*) FROM pending_codes").fetchone()[0]

    assert asyncio.run(scenario()) == 0


def test_member_delete_fetches_a_member_who_is_not_cached(conn):
    async def scenario():
        cog, app = make_cog(conn)
        app.members.add_manual_member(discord_id=8, full_name="Imp", verified_by=1, note="n", verified_at=NOW)
        target = fake_member(8)
        guild = fake_guild(fetched=target)
        plain_user = SimpleNamespace(id=8)                       # a discord.User-like value, not a Member
        await cog.delete.callback(cog, fake_interaction(fake_member(1), guild), plain_user)
        return app, target

    app, target = asyncio.run(scenario())
    target.remove_roles.assert_awaited_once()
    assert app.members.get(8) is None


def test_member_delete_for_someone_who_left_still_deletes_the_data(conn):
    async def scenario():
        cog, app = make_cog(conn)
        app.members.add_manual_member(discord_id=8, full_name="Gone", verified_by=1, note="n", verified_at=NOW)
        gone = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown member")
        interaction = fake_interaction(fake_member(1), fake_guild(fetch_error=gone))
        await cog.delete.callback(cog, interaction, SimpleNamespace(id=8))
        return app, interaction

    app, interaction = asyncio.run(scenario())
    assert app.members.get(8) is None
    assert followup_text(interaction) == "Stored data deleted."


def test_forget_me_says_so_when_the_role_could_not_be_removed(conn):
    async def scenario():
        cog, app = make_cog(conn)
        app.members.add_manual_member(discord_id=7, full_name="Zed", verified_by=1, note="n", verified_at=NOW)
        user = fake_member(7)
        user.remove_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
        interaction = fake_interaction(user, fake_guild())
        await cog.forget_me.callback(cog, interaction)
        return interaction

    assert "couldn't remove the Verified role" in followup_text(asyncio.run(scenario()))


def test_verify_manual_refuses_to_overwrite_an_email_verified_member(conn):
    async def scenario():
        cog, app = make_cog(conn)
        app.members.add_email_member(
            discord_id=7, email="real@tuc.gr", full_name="Real Student", consent_at=NOW, verified_at=NOW
        )
        target = fake_member(7)
        interaction = fake_interaction(fake_member(1), fake_guild())
        await cog.verify_manual.callback(cog, interaction, target, "Someone Else", "officer fixing role")
        return app, target, interaction

    app, target, interaction = asyncio.run(scenario())
    stored = app.members.get(7)
    assert stored.email == "real@tuc.gr" and stored.method == "email" and stored.full_name == "Real Student"
    target.add_roles.assert_not_awaited()
    assert "already verified by email" in followup_text(interaction)


def test_verify_manual_verifies_and_grants_the_role(conn):
    async def scenario():
        cog, app = make_cog(conn)
        target = fake_member(7)
        interaction = fake_interaction(fake_member(1), fake_guild())
        await cog.verify_manual.callback(cog, interaction, target, "Prof Papadopoulos", "Visiting professor")
        return app, target, interaction

    app, target, interaction = asyncio.run(scenario())
    stored = app.members.get(7)
    assert stored.method == "manual" and stored.verified_by == 1 and stored.note == "Visiting professor"
    target.add_roles.assert_awaited_once()
    interaction.response.defer.assert_awaited_once()

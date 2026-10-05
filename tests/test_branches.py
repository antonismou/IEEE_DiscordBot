import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord

from bot.branches import NICKNAME_MAX, apply_branch_roles, set_nickname
from bot.channels import ChannelRepo
from bot.cogs.branches import RolesView
from bot.cogs.verify import grant_role_and_reply
from bot.members import MemberRepo
from tests.fakes import FakeRole, fake_guild, fake_interaction, fake_me, fake_member, followup_text, reply_text
from tests.test_setup_cog import make_cog
from tests.test_verify_cog import VERIFIED_ROLE, make_app

IDS = {"main": 10, "cs": 11, "ias": 12, "quantum": 13}
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def test_apply_adds_selected_and_removes_unselected():
    member = fake_member(7, role_ids=(11, 12))                      # has CS and IAS
    assert asyncio.run(apply_branch_roles(fake_guild(), member, IDS, {"cs", "quantum"}))
    assert len(member.add_roles.await_args.args) == 1               # quantum only; CS already held
    assert len(member.remove_roles.await_args.args) == 1            # IAS


def test_apply_reports_discord_refusal():
    member = fake_member(7)
    member.add_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
    assert asyncio.run(apply_branch_roles(fake_guild(), member, IDS, {"main"})) is False


def test_nickname_is_cut_to_discord_limit_and_failure_is_reported():
    member = fake_member(7)
    assert asyncio.run(set_nickname(member, "A" * 80))
    assert member.edit.await_args.kwargs["nick"] == "A" * NICKNAME_MAX
    member.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
    assert asyncio.run(set_nickname(member, "Real Student")) is False


def test_grant_sets_nickname_and_offers_the_picker(conn):
    app = make_app(conn)
    app.server.branch_role_ids = IDS
    app.members.add_email_member(
        discord_id=7, email="r@tuc.gr", full_name="Real Student", consent_at=NOW, verified_at=NOW)
    user = fake_member(7)
    interaction = fake_interaction(user, fake_guild(), MagicMock())
    asyncio.run(grant_role_and_reply(app, interaction))
    assert user.edit.await_args.kwargs["nick"] == "Real Student"
    assert interaction.followup.send.await_args.kwargs["view"] is not None
    assert "choose your branch" in followup_text(interaction)


def test_roles_panel_requires_verification_and_setup(conn):
    app = make_app(conn)
    app.server.branch_role_ids = IDS
    stranger = fake_interaction(fake_member(7), fake_guild(), MagicMock())
    asyncio.run(RolesView(app).open.callback(stranger))
    assert "Verify first" in reply_text(stranger)

    app.server.branch_role_ids = {}
    verified = fake_interaction(fake_member(7, role_ids=(VERIFIED_ROLE,)), fake_guild(), MagicMock())
    asyncio.run(RolesView(app).open.callback(verified))
    assert "/setup branches" in reply_text(verified)


def test_roles_panel_shows_picker_with_current_roles_preselected(conn):
    app = make_app(conn)
    app.server.branch_role_ids = IDS
    interaction = fake_interaction(fake_member(7, role_ids=(VERIFIED_ROLE, 11)), fake_guild(), MagicMock())
    asyncio.run(RolesView(app).open.callback(interaction))
    select = interaction.response.send_message.await_args.kwargs["view"].children[0]
    assert {o.value for o in select.options if o.default} == {"cs"}
    assert select.min_values == 1 and select.max_values == 4


def test_setup_branches_saves_and_rejects_duplicates(conn):
    cog, app = make_cog(conn)
    app.server.configure(guild_id=111, verified_role_id=222, officer_role_id=333)
    guild = fake_guild()
    guild.me = fake_me()

    def run(*roles):
        interaction = fake_interaction(fake_member(1, admin=True), guild)
        asyncio.run(cog.setup_branches.callback(cog, interaction, *roles))
        return reply_text(interaction)

    assert "must be different" in run(FakeRole(1), FakeRole(1), FakeRole(2), FakeRole(3))
    assert "must be different" in run(FakeRole(1), FakeRole(2), FakeRole(3), FakeRole(222))
    assert app.server.branch_role_ids == {}
    run(FakeRole(1), FakeRole(2), FakeRole(3), FakeRole(4))
    assert app.server.branch_role_ids == {"main": 1, "cs": 2, "ias": 3, "quantum": 4}


def test_branch_descriptions_show_in_the_picker_and_can_be_removed(conn):
    cog, app = make_cog(conn)
    app.server.configure_branches(IDS)
    choice = SimpleNamespace(name="CS", value="cs")

    def run(text, **kw):
        interaction = fake_interaction(fake_member(1, admin=True), fake_guild())
        asyncio.run(cog.setup_branch_description.callback(cog, interaction, choice, text))
        return reply_text(interaction)

    assert "100" in run("x" * 101) and app.server.branch_descriptions == {}
    run("Software, AI and\n  data   science")
    assert app.server.branch_descriptions == {"cs": "Software, AI and data science"}
    from bot.branches import BranchSelect
    select = asyncio.run(_build(BranchSelect, app))
    assert {o.value: o.description for o in select.options}["cs"] == "Software, AI and data science"
    run(None)
    assert app.server.branch_descriptions == {}


async def _build(cls, app):
    return cls(app, fake_member(7))

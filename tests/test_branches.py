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


# ---- PINs -----------------------------------------------------------------------------------------------------

from datetime import timedelta

from bot.branches import BranchSelect, PinModal
from bot.pins import PinLimiter, hash_pin, is_valid_pin, pin_matches
from bot.serversettings import ServerSettings


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def pin_app(conn, clock=None):
    server = ServerSettings(conn)
    server.configure(guild_id=111, verified_role_id=VERIFIED_ROLE, officer_role_id=9)
    server.configure_branches(IDS)
    server.set_branch_pin("cs", "123456")
    return SimpleNamespace(server=server, pin_limiter=PinLimiter(conn, clock=clock or Clock()))


async def pick(app, user, keys):
    interaction = fake_interaction(user, fake_guild(), MagicMock())
    select = BranchSelect(app, user)
    select._values = list(keys)
    await select.callback(interaction)
    return interaction


async def submit(app, user, selected, needs, pins):
    interaction = fake_interaction(user, fake_guild(), MagicMock())
    modal = PinModal(app, set(selected), list(needs))
    for key, pin in zip(needs, pins):
        modal.inputs[key]._value = pin
    await modal.on_submit(interaction)
    return interaction


def test_pin_format_and_hashing():
    assert is_valid_pin("012345") and not is_valid_pin("12345") and not is_valid_pin("12345a")
    assert not is_valid_pin("１２３４５６")                       # non-ASCII digits
    stored = hash_pin("123456")
    assert "123456" not in stored and pin_matches("123456", stored) and not pin_matches("123457", stored)
    assert hash_pin("123456") != stored                              # salted


def test_pin_is_stored_hashed_and_can_be_removed(conn):
    server = ServerSettings(conn)
    server.set_branch_pin("cs", "123456")
    assert server.branch_pin_keys() == {"cs"}
    assert server.check_branch_pin("cs", "123456") and not server.check_branch_pin("cs", "000000")
    assert "123456" not in str(conn.execute("SELECT value FROM server_settings").fetchall()[0][0])
    server.set_branch_pin("cs", None)
    assert server.branch_pin_keys() == set() and not server.check_branch_pin("cs", "123456")


def test_unprotected_or_already_held_branch_needs_no_pin(conn):
    app = pin_app(conn)
    user = fake_member(7, role_ids=(VERIFIED_ROLE,))
    asyncio.run(pick(app, user, ["main"]))                           # no PIN on main
    user.add_roles.assert_awaited_once()
    holder = fake_member(8, role_ids=(VERIFIED_ROLE, IDS["cs"]))
    interaction = asyncio.run(pick(app, holder, ["cs"]))             # already has CS
    interaction.response.send_modal.assert_not_awaited()


def test_choosing_a_protected_branch_opens_the_pin_form_and_changes_nothing_yet(conn):
    app = pin_app(conn)
    user = fake_member(7, role_ids=(VERIFIED_ROLE,))
    interaction = asyncio.run(pick(app, user, ["main", "cs"]))
    modal = interaction.response.send_modal.await_args.args[0]
    assert list(modal.inputs) == ["cs"]                              # only the protected, not-yet-held branch
    user.add_roles.assert_not_awaited()


def test_correct_pin_grants_every_selected_role(conn):
    app = pin_app(conn)
    user = fake_member(7, role_ids=(VERIFIED_ROLE,))
    interaction = asyncio.run(submit(app, user, ["main", "cs"], ["cs"], ["123456"]))
    assert len(user.add_roles.await_args.args) == 2
    assert "Done" in followup_text(interaction)


def test_wrong_pin_grants_nothing_and_counts_down_then_locks(conn):
    clock = Clock()
    app = pin_app(conn, clock)
    user = fake_member(7, role_ids=(VERIFIED_ROLE,))
    for left in (4, 3, 2, 1):
        interaction = asyncio.run(submit(app, user, ["main", "cs"], ["cs"], ["000000"]))
        assert f"{left} attempt(s) left" in followup_text(interaction)
    interaction = asyncio.run(submit(app, user, ["main", "cs"], ["cs"], ["000000"]))
    assert "15 minute" in followup_text(interaction)
    user.add_roles.assert_not_awaited()

    # locked: even the right PIN is refused, and the picker does not open the form
    assert "Too many" in followup_text(asyncio.run(submit(app, user, ["cs"], ["cs"], ["123456"])))
    blocked = asyncio.run(pick(app, user, ["cs"]))
    blocked.response.send_modal.assert_not_awaited()
    assert "Too many" in blocked.response.send_message.await_args.args[0]

    clock.now += timedelta(minutes=16)                               # lockout over
    asyncio.run(submit(app, user, ["cs"], ["cs"], ["123456"]))
    user.add_roles.assert_awaited_once()


def test_lockout_survives_a_restart(conn):
    clock = Clock()
    for _ in range(5):
        PinLimiter(conn, clock=clock).record_failure(7)
    assert PinLimiter(conn, clock=clock).locked_for(7) == 900          # a fresh object, same database


def test_setup_branch_pin_validates_and_never_echoes_the_pin(conn):
    cog, app = make_cog(conn)
    choice = SimpleNamespace(name="CS", value="cs")

    def run(pin):
        interaction = fake_interaction(fake_member(1, admin=True), fake_guild())
        asyncio.run(cog.setup_branch_pin.callback(cog, interaction, choice, pin))
        return reply_text(interaction)

    assert "6 digits" in run("1234") and app.server.branch_pin_keys() == set()
    text = run("123456")
    assert "123456" not in text and app.server.branch_pin_keys() == {"cs"}
    assert "removed" in run(None) and app.server.branch_pin_keys() == set()

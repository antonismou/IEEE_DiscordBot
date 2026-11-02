from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from bot.channels import TODO_REMINDERS, ChannelRepo
from bot.checks import is_officer
from bot.todo.store import TodoRepo
from tests.fakes import fake_guild, fake_interaction, fake_member, reply_text

NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
OFFICER_ROLE = 9


def make_cog(conn):
    from bot.cogs.todo import TodoCog

    audit_lines = []
    app = SimpleNamespace(
        todo=TodoRepo(conn), channels=ChannelRepo(conn), server=SimpleNamespace(officer_role_id=OFFICER_ROLE, guild_id=None),
    )
    bot = SimpleNamespace(app=app)
    cog = TodoCog(bot)
    cog._clock = lambda: NOW
    return cog, app, audit_lines


def officer(uid=1):
    return fake_member(uid, role_ids=(OFFICER_ROLE,))


def run(cog, command, interaction, *args, **kwargs):
    asyncio.run(command.callback(cog, interaction, *args, **kwargs))
    return interaction


def interaction_for(user=None, channel=None):
    interaction = fake_interaction(user or officer(), fake_guild())
    interaction.channel_posts = channel
    interaction.client.get_channel = MagicMock(return_value=channel)
    return interaction


def bound_channel(app, interaction, channel_id=77):
    channel = MagicMock()
    channel.send = AsyncMock()
    app.channels.set(TODO_REMINDERS, channel_id)
    interaction.client.get_channel = MagicMock(return_value=channel)
    return channel


def add(cog, interaction, title="Book venue", **kwargs):
    return run(cog, cog.add, interaction, title, **kwargs)


def test_group_is_hidden_and_guild_only_and_every_subcommand_is_officer_gated(conn):
    cog, app, _ = make_cog(conn)
    group = cog.todo
    assert group.guild_only
    assert group.default_permissions.administrator
    assert {c.name for c in group.commands} >= {"add", "list", "done", "edit", "assign", "delete"}
    stranger = interaction_for(fake_member(2))
    for command in group.commands:
        checks = command.checks
        assert checks, f"/todo {command.name} has no officer check"
        with pytest.raises(app_commands.CheckFailure):
            stranger.client.app = app
            assert asyncio.run(_run_checks(checks, stranger)) is False
    assert is_officer(officer(), OFFICER_ROLE)


async def _run_checks(checks, interaction):
    for check in checks:
        result = check(interaction)
        if not result:
            raise app_commands.CheckFailure()
    return True


def test_add_creates_task_reminders_and_announces(conn):
    cog, app, _ = make_cog(conn)
    interaction = interaction_for()
    channel = bound_channel(app, interaction)
    assignee = officer(5)
    add(cog, interaction, due="in 3d", assignee=assignee, priority="high")
    task = app.todo.get_task(1)
    assert (task.title, task.assignee_id, task.priority, task.created_by) == ("Book venue", 5, "high", 1)
    assert task.due_at == NOW + timedelta(days=3)
    assert len(app.todo.pending_reminders()) == 5   # 2d, 1d, 1h, due, overdue
    channel.send.assert_awaited_once()
    text = channel.send.await_args.args[0]
    assert "Book venue" in text and "<@5>" in text
    mentions = channel.send.await_args.kwargs["allowed_mentions"]
    assert [u.id for u in mentions.users] == [5]
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


def test_add_works_without_a_channel_bound(conn):
    cog, app, _ = make_cog(conn)
    interaction = interaction_for()
    add(cog, interaction)
    assert app.todo.count_open() == 1


@pytest.mark.parametrize(
    "kwargs,words",
    [
        (dict(due="banana"), "couldn't read"),
        (dict(due="yesterday"), "couldn't read"),
        (dict(repeat="weekly"), "needs a due date"),
        (dict(title="   "), "Title"),
    ],
)
def test_add_refusals(conn, kwargs, words):
    cog, app, _ = make_cog(conn)
    interaction = interaction_for()
    title = kwargs.pop("title", "x")
    add(cog, interaction, title, **kwargs)
    assert words in reply_text(interaction)
    assert app.todo.count_open() == 0


def test_add_refuses_a_non_officer_or_bot_assignee(conn):
    cog, app, _ = make_cog(conn)
    interaction = interaction_for()
    add(cog, interaction, assignee=fake_member(3))
    assert "isn't an officer" in reply_text(interaction)
    interaction = interaction_for()
    add(cog, interaction, assignee=fake_member(4, role_ids=(OFFICER_ROLE,), bot=True))
    assert "bot" in reply_text(interaction)
    assert app.todo.count_open() == 0


def test_add_refuses_past_the_open_limit(conn, monkeypatch):
    import bot.cogs.todo as module

    monkeypatch.setattr(module, "MAX_OPEN_TASKS", 1)
    cog, app, _ = make_cog(conn)
    add(cog, interaction_for(), "one")
    interaction = interaction_for()
    add(cog, interaction, "two")
    assert "already 1 open tasks" in reply_text(interaction)
    assert app.todo.count_open() == 1


def test_list_views_for_the_caller(conn):
    cog, app, _ = make_cog(conn)
    add(cog, interaction_for(), "mine", assignee=officer(1))
    add(cog, interaction_for(), "theirs", assignee=officer(2))
    interaction = interaction_for()
    run(cog, cog.list_tasks, interaction)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "mine" in embed.description and "theirs" not in embed.description
    interaction = interaction_for()
    run(cog, cog.list_tasks, interaction, "all")
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "mine" in embed.description and "theirs" in embed.description


def test_any_officer_can_finish_edit_assign_and_delete_another_officers_task(conn):
    cog, app, _ = make_cog(conn)
    add(cog, interaction_for(officer(1)), "x", due="in 5d", assignee=officer(1))
    other = interaction_for(officer(2))
    run(cog, cog.edit, other, "1", title="renamed", due="in 6d")
    assert app.todo.get_task(1).title == "renamed"
    run(cog, cog.assign, interaction_for(officer(2)), "1", officer(3))
    assert app.todo.get_task(1).assignee_id == 3
    assert {r.user_id for r in app.todo.pending_reminders()} == {3}
    run(cog, cog.done, interaction_for(officer(2)), "1")
    assert app.todo.get_task(1).status == "done" and app.todo.get_task(1).done_by == 2
    assert app.todo.pending_reminders() == []
    run(cog, cog.delete, interaction_for(officer(2)), "1")
    assert app.todo.get_task(1) is None


def test_done_twice_and_unknown_ids_are_friendly(conn):
    cog, app, _ = make_cog(conn)
    add(cog, interaction_for())
    run(cog, cog.done, interaction_for(), "1")
    again = interaction_for()
    run(cog, cog.done, again, "#1")
    assert "already done" in reply_text(again)
    for bad in ("999", "abc"):
        interaction = interaction_for()
        run(cog, cog.done, interaction, bad)
        assert "can't find" in reply_text(interaction)


def test_edit_can_clear_the_due_date_and_validates(conn):
    cog, app, _ = make_cog(conn)
    add(cog, interaction_for(), due="in 5d")
    run(cog, cog.edit, interaction_for(), "1", due="none")
    assert app.todo.get_task(1).due_at is None
    assert app.todo.pending_reminders() == []
    nothing = interaction_for()
    run(cog, cog.edit, nothing, "1")
    assert "Nothing to change" in reply_text(nothing)
    repeating = interaction_for()
    run(cog, cog.edit, repeating, "1", repeat="daily")
    assert "needs a due date" in reply_text(repeating)


def test_unassign(conn):
    cog, app, _ = make_cog(conn)
    add(cog, interaction_for(), due="in 5d", assignee=officer(5))
    run(cog, cog.assign, interaction_for(), "1", None)
    assert app.todo.get_task(1).assignee_id is None


def test_audit_lines_never_contain_titles(conn, monkeypatch):
    import bot.cogs.todo as module

    lines = []

    async def fake_audit(app, interaction, text):
        lines.append(text)

    monkeypatch.setattr(module, "audit", fake_audit)
    cog, app, _ = make_cog(conn)
    add(cog, interaction_for(), "SECRET-TITLE", assignee=officer(5))
    run(cog, cog.edit, interaction_for(), "1", title="SECRET-TWO")
    run(cog, cog.assign, interaction_for(), "1", None)
    run(cog, cog.done, interaction_for(), "1")
    run(cog, cog.delete, interaction_for(), "1")
    assert len(lines) == 5
    assert not any("SECRET" in line for line in lines)
    assert all("#1" in line for line in lines)


def test_autocomplete_is_officer_only_and_filtered(conn):
    cog, app, _ = make_cog(conn)
    add(cog, interaction_for(), "Book venue")
    add(cog, interaction_for(), "Order pizza")
    out = asyncio.run(cog._task_choices(interaction_for(), "piz"))
    assert [c.value for c in out] == ["2"]
    assert asyncio.run(cog._task_choices(interaction_for(fake_member(9)), "")) == []


def test_forget_removes_todo_data(conn):
    from bot.cogs.officer import OfficerCog
    from bot.members import MemberRepo
    from bot.quotes.store import QuoteRepo
    from bot.verification import VerificationService

    members = MemberRepo(conn)
    app = SimpleNamespace(
        server=SimpleNamespace(verified_role_id=5, officer_role_id=OFFICER_ROLE), members=members,
        verification=VerificationService(conn, members), channels=ChannelRepo(conn), quotes=QuoteRepo(conn),
        todo=TodoRepo(conn),
    )
    task = app.todo.add_task(
        title="t", description="", created_by=7, assignee_id=7, due_at=None, priority="normal", repeat="none",
        notify="both", now=NOW,
    )
    app.todo.add_reminder(task_id=None, user_id=7, kind="custom", fire_at=NOW, text="x", delivery="dm")
    cog = OfficerCog(SimpleNamespace(app=app))
    interaction = fake_interaction(fake_member(7), fake_guild())
    asyncio.run(cog.forget_me.callback(cog, interaction))
    assert app.todo.get_task(task.id).assignee_id is None
    assert app.todo.pending_reminders() == []
    assert reply_text(interaction) == "Stored data deleted."


# --- reminder delivery -------------------------------------------------------------------------------------

from bot.channels import OFFICER_LOG  # noqa: E402
from bot.todo.service import UNASSIGNED, Delivery, run_due, schedule_task  # noqa: E402
from bot.todo.store import Reminder  # noqa: E402


def delivery_for(app, *, user_id=5, mode="both", kind="1h", with_task=True):
    task = None
    if with_task:
        task = app.todo.add_task(
            title="Book venue", description="", created_by=1, assignee_id=user_id or None, due_at=NOW + timedelta(hours=1),
            priority="normal", repeat="none", notify=mode, now=NOW,
        )
    reminder = Reminder(1, task.id if task else None, user_id, kind, NOW, None if task else "call them", mode, None)
    return Delivery(reminder, task, False)


def wire(cog, app, *, dm_error=None, officer_log=True):
    dm_user = MagicMock()
    dm_user.send = AsyncMock(side_effect=dm_error)
    channel = MagicMock()
    channel.send = AsyncMock()
    log_channel = MagicMock()
    log_channel.send = AsyncMock()
    cog.bot.fetch_user = AsyncMock(return_value=dm_user)
    app.channels.set(TODO_REMINDERS, 77)
    if officer_log:
        app.channels.set(OFFICER_LOG, 88)
    cog.bot.get_channel = lambda cid: {77: channel, 88: log_channel}.get(cid)
    return dm_user, channel, log_channel


def forbidden():
    return discord.Forbidden(MagicMock(status=403), "no dms")


def test_both_goes_to_dm_and_channel_with_restricted_mentions(conn):
    cog, app, _ = make_cog(conn)
    dm, channel, _ = wire(cog, app)
    asyncio.run(cog.deliver(delivery_for(app)))
    dm.send.assert_awaited_once()
    assert dm.send.await_args.kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    text = channel.send.await_args.args[0]
    assert text.startswith("<@5> ") and "Book venue" in text
    assert [u.id for u in channel.send.await_args.kwargs["allowed_mentions"].users] == [5]


def test_dm_only_and_channel_only(conn):
    cog, app, _ = make_cog(conn)
    dm, channel, _ = wire(cog, app)
    asyncio.run(cog.deliver(delivery_for(app, mode="dm")))
    assert dm.send.await_count == 1 and channel.send.await_count == 0
    asyncio.run(cog.deliver(delivery_for(app, mode="channel")))
    assert dm.send.await_count == 1 and channel.send.await_count == 1


def test_failed_dm_falls_back_to_the_channel(conn):
    cog, app, _ = make_cog(conn)
    dm, channel, log_channel = wire(cog, app, dm_error=forbidden())
    asyncio.run(cog.deliver(delivery_for(app, mode="dm")))
    channel.send.assert_awaited_once()
    log_channel.send.assert_not_awaited()


def test_unassigned_pings_the_officer_role_only(conn):
    cog, app, _ = make_cog(conn)
    dm, channel, _ = wire(cog, app)
    asyncio.run(cog.deliver(delivery_for(app, user_id=UNASSIGNED, mode="channel")))
    dm.send.assert_not_awaited()
    assert channel.send.await_args.args[0].startswith(f"<@&{OFFICER_ROLE}> ")
    mentions = channel.send.await_args.kwargs["allowed_mentions"]
    assert [r.id for r in mentions.roles] == [OFFICER_ROLE] and mentions.users is False


def test_nothing_deliverable_tells_officer_log_without_the_task_text(conn):
    cog, app, _ = make_cog(conn)
    dm, channel, log_channel = wire(cog, app, dm_error=forbidden())
    app.channels.clear(TODO_REMINDERS)
    asyncio.run(cog.deliver(delivery_for(app, mode="dm")))
    sent = log_channel.send.await_args.args[0]
    assert "task #" in sent and "Book venue" not in sent


def test_no_channel_bound_still_sends_the_dm(conn):
    cog, app, _ = make_cog(conn)
    dm, channel, log_channel = wire(cog, app)
    app.channels.clear(TODO_REMINDERS)
    asyncio.run(cog.deliver(delivery_for(app)))
    dm.send.assert_awaited_once()
    log_channel.send.assert_not_awaited()


def test_free_standing_reminder_text(conn):
    cog, app, _ = make_cog(conn)
    dm, channel, _ = wire(cog, app)
    asyncio.run(cog.deliver(delivery_for(app, with_task=False, kind="custom")))
    assert "call them" in dm.send.await_args.args[0]


def test_loop_pass_delivers_due_reminders_and_survives_errors(conn):
    cog, app, _ = make_cog(conn)
    dm, channel, _ = wire(cog, app)
    task = app.todo.add_task(
        title="Book venue", description="", created_by=1, assignee_id=5, due_at=NOW + timedelta(minutes=5),
        priority="normal", repeat="none", notify="dm", now=NOW - timedelta(days=1),
    )
    schedule_task(app.todo, task, NOW - timedelta(days=1))
    cog._clock = lambda: NOW + timedelta(minutes=5)
    asyncio.run(cog.reminder_loop.coro(cog))
    assert dm.send.await_count == 1                      # "due"; 2d/1d/1h were superseded
    cog.deliver = AsyncMock(side_effect=RuntimeError("boom"))
    asyncio.run(cog.reminder_loop.coro(cog))             # nothing due, and must not raise


def test_loop_swallows_exceptions(conn):
    cog, app, _ = make_cog(conn)
    app.todo = None                                      # makes run_due blow up
    asyncio.run(cog.reminder_loop.coro(cog))


def test_is_active_checks_the_officer_role_and_keeps_reminders_when_unsure(conn):
    cog, app, _ = make_cog(conn)
    app.server.guild_id = 111
    member = officer(5)
    guild = fake_guild(fetched=member)
    cog.bot.get_guild = lambda gid: guild
    assert asyncio.run(cog.is_active(5)) is True
    guild.fetch_member.return_value = fake_member(5)      # lost the role
    assert asyncio.run(cog.is_active(5)) is False
    guild.fetch_member.side_effect = discord.NotFound(MagicMock(status=404), "gone")
    assert asyncio.run(cog.is_active(5)) is False         # left the server
    guild.fetch_member.side_effect = discord.HTTPException(MagicMock(status=500), "oops")
    assert asyncio.run(cog.is_active(5)) is True          # unsure: keep
    cog.bot.get_guild = lambda gid: None
    assert asyncio.run(cog.is_active(5)) is True


def test_cog_load_starts_and_unload_stops_the_loop(conn):
    async def scenario():
        cog, _, _ = make_cog(conn)
        cog.bot.wait_until_ready = AsyncMock()
        await cog.cog_load()
        running = cog.reminder_loop.is_running()
        await cog.cog_unload()
        return running

    assert asyncio.run(scenario()) is True


def test_done_on_a_repeating_task_creates_the_next_and_says_so(conn):
    cog, app, _ = make_cog(conn)
    interaction = interaction_for()
    bound_channel(app, interaction)
    add(cog, interaction, "report", due="in 1d", repeat="weekly")
    done = interaction_for()
    bound_channel(app, done)
    run(cog, cog.done, done, "1")
    assert app.todo.get_task(2).due_at == NOW + timedelta(days=8)
    assert "Next one is #2" in reply_text(done)

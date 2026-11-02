from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import commands

from bot.channels import ChannelRepo
from bot.todo.buttons import DYNAMIC_ITEMS, DoneButton, SnoozeHourButton, SnoozeTomorrowButton, build_reminder_view
from bot.todo.service import Delivery, schedule_task
from bot.todo.store import TodoRepo
from tests.fakes import fake_guild, fake_member

NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
OFFICER_ROLE = 9


def setup_world(conn, *, officer_ids=(1, 2)):
    from bot.cogs.todo import TodoCog

    app = SimpleNamespace(
        todo=TodoRepo(conn), channels=ChannelRepo(conn),
        server=SimpleNamespace(officer_role_id=OFFICER_ROLE, guild_id=111),
    )
    members = {uid: fake_member(uid, role_ids=(OFFICER_ROLE,)) for uid in officer_ids}

    async def fetch_member(uid):
        if uid in members:
            return members[uid]
        return fake_member(uid)  # present but not an officer

    guild = fake_guild()
    guild.fetch_member = AsyncMock(side_effect=fetch_member)
    bot = SimpleNamespace(app=app, get_guild=lambda gid: guild)
    cog = TodoCog(bot)
    cog._clock = lambda: NOW
    cog.announce = AsyncMock()
    bot.get_cog = lambda name: cog
    return cog, app, bot


def click(bot, user_id, message_content="⏰ Task #1"):
    interaction = MagicMock()
    interaction.client = bot
    interaction.user = SimpleNamespace(id=user_id)
    interaction.message = SimpleNamespace(content=message_content)
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


def make_task(app, **kw):
    task = app.todo.add_task(
        title="Book venue", description="", created_by=1, assignee_id=1, due_at=NOW + timedelta(days=3),
        priority="normal", repeat=kw.pop("repeat", "none"), notify="both", now=NOW,
    )
    schedule_task(app.todo, task, NOW)
    return task


def test_templates_match_the_custom_ids_and_register_with_a_real_bot():
    ids = {
        DoneButton: DoneButton(12).item.custom_id,
        SnoozeHourButton: SnoozeHourButton(7).item.custom_id,
        SnoozeTomorrowButton: SnoozeTomorrowButton(7).item.custom_id,
    }
    assert ids == {DoneButton: "todo:done:12", SnoozeHourButton: "todo:snooze1h:7", SnoozeTomorrowButton: "todo:snoozetmr:7"}
    for cls, custom_id in ids.items():
        assert re.fullmatch(cls.__discord_ui_compiled_template__, custom_id)

    async def register():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        bot.add_dynamic_items(*DYNAMIC_ITEMS)

    asyncio.run(register())


def test_views_carry_done_only_for_task_reminders():
    task = SimpleNamespace(id=3)
    reminder = SimpleNamespace(id=9)

    async def build():
        return (
            [c.item.label for c in build_reminder_view(Delivery(reminder, task, False)).children],
            [c.item.label for c in build_reminder_view(Delivery(reminder, None, False)).children],
        )

    with_task, free = asyncio.run(build())
    assert with_task == ["Done", "Snooze 1h", "Snooze until tomorrow"]
    assert free == ["Snooze 1h", "Snooze until tomorrow"]


def test_done_completes_the_task_once_and_announces(conn):
    cog, app, bot = setup_world(conn)
    task = make_task(app)
    interaction = click(bot, 2)
    asyncio.run(DoneButton(task.id).callback(interaction))
    assert app.todo.get_task(task.id).status == "done"
    assert app.todo.get_task(task.id).done_by == 2
    assert app.todo.pending_reminders() == []
    cog.announce.assert_awaited_once()
    assert "Done by <@2>" in interaction.response.edit_message.await_args.kwargs["content"]
    assert interaction.response.edit_message.await_args.kwargs["view"] is None

    second = click(bot, 1)
    asyncio.run(DoneButton(task.id).callback(second))
    assert "already done" in second.response.edit_message.await_args.kwargs["content"]
    cog.announce.assert_awaited_once()


def test_done_on_a_repeating_task_spawns_the_next_once(conn):
    cog, app, bot = setup_world(conn)
    task = make_task(app, repeat="weekly")
    asyncio.run(DoneButton(task.id).callback(click(bot, 1)))
    asyncio.run(DoneButton(task.id).callback(click(bot, 2)))
    assert app.todo.count_open() == 1


def test_done_on_a_deleted_task_is_handled(conn):
    cog, app, bot = setup_world(conn)
    interaction = click(bot, 1)
    asyncio.run(DoneButton(404).callback(interaction))
    assert "no longer exists" in interaction.response.edit_message.await_args.kwargs["content"]


def test_a_non_officer_is_refused_and_nothing_changes(conn):
    cog, app, bot = setup_world(conn)
    task = make_task(app)
    interaction = click(bot, 99)
    asyncio.run(DoneButton(task.id).callback(interaction))
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    assert app.todo.get_task(task.id).status == "open"
    reminder = app.todo.pending_reminders()[0]
    interaction = click(bot, 99)
    asyncio.run(SnoozeHourButton(reminder.id).callback(interaction))
    assert [r.kind for r in app.todo.pending_reminders()].count("snooze") == 0


def test_a_user_who_left_the_server_is_refused(conn):
    cog, app, bot = setup_world(conn)
    task = make_task(app)
    guild = bot.get_guild(111)
    guild.fetch_member.side_effect = discord.NotFound(MagicMock(status=404), "gone")
    interaction = click(bot, 1)
    asyncio.run(DoneButton(task.id).callback(interaction))
    assert app.todo.get_task(task.id).status == "open"
    interaction.response.send_message.assert_awaited_once()


def test_snooze_one_hour_adds_a_reminder_for_the_original_target(conn):
    cog, app, bot = setup_world(conn)
    task = make_task(app)
    original = app.todo.pending_reminders()[0]
    interaction = click(bot, 2)   # a different officer presses it
    asyncio.run(SnoozeHourButton(original.id).callback(interaction))
    snoozes = [r for r in app.todo.pending_reminders() if r.kind == "snooze"]
    assert len(snoozes) == 1
    assert snoozes[0].fire_at == NOW + timedelta(hours=1)
    assert (snoozes[0].user_id, snoozes[0].task_id, snoozes[0].delivery) == (original.user_id, task.id, original.delivery)
    assert "Snoozed" in interaction.response.edit_message.await_args.kwargs["content"]


def test_snooze_until_tomorrow_is_09_00_athens_next_day(conn):
    cog, app, bot = setup_world(conn)
    make_task(app)
    original = app.todo.pending_reminders()[0]
    asyncio.run(SnoozeTomorrowButton(original.id).callback(click(bot, 1)))
    snooze = [r for r in app.todo.pending_reminders() if r.kind == "snooze"][0]
    assert snooze.fire_at == datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)   # 09:00 in UTC+2


def test_snoozes_are_capped_per_task(conn):
    cog, app, bot = setup_world(conn)
    make_task(app)
    original = app.todo.pending_reminders()[0]
    for _ in range(5):
        asyncio.run(SnoozeHourButton(original.id).callback(click(bot, 1)))
    over = click(bot, 1)
    asyncio.run(SnoozeHourButton(original.id).callback(over))
    assert "too many times" in over.response.send_message.await_args.args[0]
    assert [r.kind for r in app.todo.pending_reminders()].count("snooze") == 5


def test_snooze_of_a_free_standing_reminder_keeps_its_text(conn):
    cog, app, bot = setup_world(conn)
    original = app.todo.add_reminder(task_id=None, user_id=1, kind="custom", fire_at=NOW, text="call", delivery="dm")
    asyncio.run(SnoozeHourButton(original.id).callback(click(bot, 1)))
    snooze = [r for r in app.todo.pending_reminders() if r.kind == "snooze"][0]
    assert (snooze.text, snooze.task_id, snooze.delivery) == ("call", None, "dm")


def test_snooze_on_a_finished_task_is_handled(conn):
    cog, app, bot = setup_world(conn)
    task = make_task(app)
    original = app.todo.pending_reminders()[0]
    app.todo.complete_task(task.id, 1, NOW)
    interaction = click(bot, 1)
    asyncio.run(SnoozeHourButton(original.id).callback(interaction))
    assert "already done" in interaction.response.edit_message.await_args.kwargs["content"]

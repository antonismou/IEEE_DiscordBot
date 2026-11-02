from __future__ import annotations

import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.audit import audit
from bot.channels import OFFICER_LOG, TODO_REMINDERS
from bot.checks import is_officer, officer_only
from bot.db import utcnow
from bot.roles import notify_officers, resolve_member
from bot.todo import render
from bot.todo.buttons import DYNAMIC_ITEMS, build_reminder_view
from bot.todo.dates import ATHENS, parse_when
from bot.todo.service import UNASSIGNED, Delivery, complete_and_repeat, run_due, schedule_task
from bot.validation import clean_text

log = logging.getLogger(__name__)

MAX_OPEN_TASKS = 500
Priority = Literal["low", "normal", "high"]
Repeat = Literal["none", "daily", "weekly", "monthly"]
Notify = Literal["dm", "channel", "both"]
View = Literal["mine", "all", "unassigned", "overdue", "done"]
VIEW_TITLES = {
    "mine": "My tasks", "all": "All open tasks", "unassigned": "Unassigned tasks",
    "overdue": "Overdue tasks", "done": "Recently completed",
}


class TaskListView(discord.ui.View):
    """Prev/Next buttons over a list of tasks, usable only by whoever asked for it."""

    def __init__(self, tasks, title: str, owner_id: int):
        super().__init__(timeout=120)
        self.tasks, self.title, self.owner_id, self.page = tasks, title, owner_id, 0
        self._sync()

    def _sync(self) -> None:
        pages = render.page_count(len(self.tasks))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1

    def embed(self) -> discord.Embed:
        return render.list_embed(self.tasks, self.page, self.title)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)


MAX_FREE_REMINDERS = 25
MAX_REMINDER_LINES = 25


class CancelRemindersView(discord.ui.View):
    """One Cancel button per free-standing reminder of the person who asked."""

    def __init__(self, repo, owner_id: int, own_reminders):
        super().__init__(timeout=120)
        self.repo, self.owner_id = repo, owner_id
        for reminder in own_reminders[:20]:
            button = discord.ui.Button(label=f"Cancel #{reminder.id}", style=discord.ButtonStyle.danger)
            button.callback = self._make_callback(reminder.id, button)
            self.add_item(button)

    def _make_callback(self, reminder_id: int, button: discord.ui.Button):
        async def callback(interaction: discord.Interaction) -> None:
            cancelled = self.repo.cancel_reminder(reminder_id, interaction.user.id)
            button.disabled = True
            button.label = f"#{reminder_id} cancelled" if cancelled else f"#{reminder_id} already gone"
            await interaction.response.edit_message(view=self)

        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id


class TodoCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._clock = utcnow
        self._board_dirty = False

    @property
    def app(self):
        return self.bot.app

    todo = app_commands.Group(
        name="todo", description="Officer tasks and reminders", guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(*DYNAMIC_ITEMS)  # reminder buttons keep working after a restart
        self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()
        self.bot.remove_dynamic_items(*DYNAMIC_ITEMS)

    # --- reminders -------------------------------------------------------------------------------------------
    @tasks.loop(seconds=30)
    async def reminder_loop(self) -> None:
        try:
            await run_due(self.app.todo, self._clock(), self.deliver, is_active=self.is_active)
        except Exception:
            log.exception("Reminder pass failed")

    @reminder_loop.before_loop
    async def _before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def is_active(self, user_id: int) -> bool:
        """Still an officer in the server? Reminders for someone who left or lost the role are dropped.
        When Discord can't tell us (guild not cached, API error) the reminder is kept."""
        guild = self.bot.get_guild(self.app.server.guild_id) if self.app.server.guild_id else None
        if guild is None:
            return True
        try:
            member = await resolve_member(guild, discord.Object(id=user_id))
        except discord.HTTPException:
            return True
        return member is not None and is_officer(member, self.app.server.officer_role_id)

    async def _dm(self, user_id: int, text: str, view) -> bool:
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(text, allowed_mentions=discord.AllowedMentions.none(), view=view)
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True

    async def _post(self, delivery: Delivery, text: str, view) -> bool:
        channel_id = self.app.channels.get(TODO_REMINDERS)
        if channel_id is None:
            return False
        user_id = delivery.reminder.user_id
        if user_id == UNASSIGNED:
            role_id = self.app.server.officer_role_id
            prefix = f"<@&{role_id}> " if role_id else ""
            mentions = discord.AllowedMentions(roles=[discord.Object(id=role_id)] if role_id else False, users=False)
        else:
            prefix = f"<@{user_id}> "
            mentions = discord.AllowedMentions(users=[discord.Object(id=user_id)], roles=False)
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            await channel.send(render.truncate(prefix + text), allowed_mentions=mentions, view=view)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not post a reminder to channel %s", channel_id)
            return False
        return True

    async def deliver(self, delivery: Delivery) -> None:
        """Send one reminder by DM and/or to the tasks channel, falling back to the channel if a DM fails."""
        reminder = delivery.reminder
        text = render.reminder_text(delivery)
        view = build_reminder_view(delivery)
        want_dm = reminder.delivery in ("dm", "both") and reminder.user_id != UNASSIGNED
        dm_ok = await self._dm(reminder.user_id, text, view) if want_dm else False
        want_channel = reminder.delivery in ("channel", "both") or (want_dm and not dm_ok)
        posted = await self._post(delivery, text, view) if want_channel else False
        if not dm_ok and not posted:
            label = f"task #{delivery.task.id}" if delivery.task else "a reminder"
            await notify_officers(self.bot, self.app.channels.get(OFFICER_LOG), f"Could not deliver the reminder for {label}.")

    # --- helpers ---------------------------------------------------------------------------------------------
    async def announce(self, client, text: str, *, mention_user_id: int | None = None) -> None:
        """Post a line to the tasks channel (if one is set). Never raises: it must not undo the action."""
        channel_id = self.app.channels.get(TODO_REMINDERS)
        if channel_id is None:
            return
        mentions = discord.AllowedMentions(users=[discord.Object(id=mention_user_id)]) if mention_user_id else discord.AllowedMentions.none()
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            await channel.send(text, allowed_mentions=mentions)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not post to the tasks channel %s", channel_id)

    def _find(self, task: str):
        try:
            return self.app.todo.get_task(int(task.strip().lstrip("#")))
        except ValueError:
            return None

    async def _task_or_refuse(self, interaction: discord.Interaction, task: str):
        found = self._find(task)
        if found is None:
            await interaction.response.send_message("I can't find that task. Pick one from the list.", ephemeral=True)
        return found

    async def _check_assignee(self, interaction: discord.Interaction, member) -> str | None:
        if member.bot:
            return "Tasks can't be assigned to a bot."
        if not is_officer(member, self.app.server.officer_role_id):
            return f"{member.display_name} isn't an officer, so they can't have tasks."
        return None

    async def _task_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        # Autocomplete does not go through the command's checks, so repeat the officer check here.
        if not is_officer(interaction.user, self.app.server.officer_role_id):
            return []
        needle = current.lower().lstrip("#")
        choices = []
        for task in self.app.todo.list_tasks(interaction.user.id, "all", self._clock()):
            label = f"#{task.id} {task.title}"
            if task.due_at:
                label += f" (due {task.due_at.astimezone(ATHENS):%a %d/%m %H:%M})"
            if needle in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=str(task.id)))
            if len(choices) == 25:
                break
        return choices

    # --- commands --------------------------------------------------------------------------------------------
    @todo.command(name="add", description="Create a task for the team")
    @app_commands.describe(
        title="What needs doing", due="e.g. in 3h, tomorrow 18:00, fri, 2026-11-20 14:00, 20/11",
        assignee="Who is doing it", priority="How urgent", repeat="Create the next one when this is done",
        notify="Where reminders go (default: DM and the channel)", description="More detail",
    )
    @officer_only()
    async def add(
        self, interaction: discord.Interaction, title: str, due: str | None = None,
        assignee: discord.Member | None = None, priority: Priority = "normal", repeat: Repeat = "none",
        notify: Notify = "both", description: str | None = None,
    ) -> None:
        now = self._clock()
        try:
            clean_title = clean_text(title, min_len=1, max_len=100, label="Title")
            clean_description = clean_text(description or "", min_len=0, max_len=500, label="Description")
            due_at = parse_when(due, now) if due else None
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if repeat != "none" and due_at is None:
            await interaction.response.send_message("A repeating task needs a due date.", ephemeral=True)
            return
        if self.app.todo.count_open() >= MAX_OPEN_TASKS:
            await interaction.response.send_message(
                f"There are already {MAX_OPEN_TASKS} open tasks. Complete or delete some first.", ephemeral=True
            )
            return
        if assignee is not None:
            problem = await self._check_assignee(interaction, assignee)
            if problem:
                await interaction.response.send_message(problem, ephemeral=True)
                return
        task = self.app.todo.add_task(
            title=clean_title, description=clean_description, created_by=interaction.user.id,
            assignee_id=assignee.id if assignee else None, due_at=due_at, priority=priority, repeat=repeat,
            notify=notify, now=now,
        )
        schedule_task(self.app.todo, task, now)
        self._board_dirty = True
        await audit(self.app, interaction, f"created task #{task.id}")
        await self.announce(
            interaction.client, render.announcement_text("created", task, interaction.user.id),
            mention_user_id=task.assignee_id,
        )
        await interaction.response.send_message(f"Task #{task.id} created.", embed=render.task_embed(task), ephemeral=True)

    @todo.command(name="list", description="Show tasks")
    @app_commands.describe(view="Which tasks (default: assigned to me)")
    @officer_only()
    async def list_tasks(self, interaction: discord.Interaction, view: View = "mine") -> None:
        tasks = self.app.todo.list_tasks(interaction.user.id, view, self._clock())
        paged = TaskListView(tasks, VIEW_TITLES[view], interaction.user.id)
        await interaction.response.send_message(embed=paged.embed(), view=paged, ephemeral=True)

    @todo.command(name="done", description="Mark a task as done")
    @officer_only()
    async def done(self, interaction: discord.Interaction, task: str) -> None:
        found = await self._task_or_refuse(interaction, task)
        if found is None:
            return
        finished, following = complete_and_repeat(self.app.todo, found, interaction.user.id, self._clock())
        if finished is None:
            await interaction.response.send_message(f"Task #{found.id} is already done.", ephemeral=True)
            return
        self._board_dirty = True
        await audit(self.app, interaction, f"completed task #{found.id}")
        await self.announce(interaction.client, render.announcement_text("done", finished, interaction.user.id, following))
        extra = f" Next one is #{following.id}, due {render.stamp(following.due_at)}." if following else ""
        await interaction.response.send_message(f"Task #{found.id} marked done.{extra}", ephemeral=True)

    @todo.command(name="edit", description="Change a task")
    @app_commands.describe(due="New due date, or `none` to clear it")
    @officer_only()
    async def edit(
        self, interaction: discord.Interaction, task: str, title: str | None = None, due: str | None = None,
        priority: Priority | None = None, repeat: Repeat | None = None, notify: Notify | None = None,
        description: str | None = None,
    ) -> None:
        found = await self._task_or_refuse(interaction, task)
        if found is None:
            return
        if found.status != "open":
            await interaction.response.send_message(f"Task #{found.id} is already done.", ephemeral=True)
            return
        now = self._clock()
        fields: dict = {}
        try:
            if title is not None:
                fields["title"] = clean_text(title, min_len=1, max_len=100, label="Title")
            if description is not None:
                fields["description"] = clean_text(description, min_len=0, max_len=500, label="Description")
            if due is not None:
                fields["due_at"] = None if due.strip().lower() == "none" else parse_when(due, now)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        for name, value in (("priority", priority), ("repeat", repeat), ("notify", notify)):
            if value is not None:
                fields[name] = value
        if not fields:
            await interaction.response.send_message("Nothing to change: give at least one field.", ephemeral=True)
            return
        new_due = fields.get("due_at", found.due_at)
        if fields.get("repeat", found.repeat) != "none" and new_due is None:
            await interaction.response.send_message("A repeating task needs a due date.", ephemeral=True)
            return
        updated = self.app.todo.update_task(found.id, **fields)
        schedule_task(self.app.todo, updated, now)
        self._board_dirty = True
        await audit(self.app, interaction, f"edited task #{found.id}")
        await interaction.response.send_message(f"Task #{found.id} updated.", embed=render.task_embed(updated), ephemeral=True)

    @todo.command(name="assign", description="Give a task to an officer (leave the officer out to unassign)")
    @officer_only()
    async def assign(self, interaction: discord.Interaction, task: str, user: discord.Member | None = None) -> None:
        found = await self._task_or_refuse(interaction, task)
        if found is None:
            return
        if found.status != "open":
            await interaction.response.send_message(f"Task #{found.id} is already done.", ephemeral=True)
            return
        if user is not None:
            problem = await self._check_assignee(interaction, user)
            if problem:
                await interaction.response.send_message(problem, ephemeral=True)
                return
        updated = self.app.todo.update_task(found.id, assignee_id=user.id if user else None)
        schedule_task(self.app.todo, updated, self._clock())
        self._board_dirty = True
        await audit(self.app, interaction, f"assigned task #{found.id} to " + (f"<@{user.id}>" if user else "nobody"))
        await self.announce(
            interaction.client, render.announcement_text("assigned", updated, interaction.user.id),
            mention_user_id=updated.assignee_id,
        )
        await interaction.response.send_message(
            f"Task #{found.id} is now " + (f"assigned to {user.mention}." if user else "unassigned."),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @todo.command(name="delete", description="Delete a task")
    @officer_only()
    async def delete(self, interaction: discord.Interaction, task: str) -> None:
        found = await self._task_or_refuse(interaction, task)
        if found is None:
            return
        self.app.todo.delete_task(found.id)
        self._board_dirty = True
        await audit(self.app, interaction, f"deleted task #{found.id}")
        await self.announce(interaction.client, render.announcement_text("deleted", found, interaction.user.id))
        await interaction.response.send_message(f"Task #{found.id} deleted.", ephemeral=True)

    @todo.command(name="remind", description="Remind yourself (and the team) about something")
    @app_commands.describe(when="e.g. in 2h, tomorrow 09:00, fri 18:00", text="What to remind you of", delivery="Where")
    @officer_only()
    async def remind(self, interaction: discord.Interaction, when: str, text: str, delivery: Notify = "both") -> None:
        now = self._clock()
        try:
            fire_at = parse_when(when, now)
            clean_reminder = clean_text(text, min_len=1, max_len=200, label="Text")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if self.app.todo.count_pending_free(interaction.user.id) >= MAX_FREE_REMINDERS:
            await interaction.response.send_message(
                f"You already have {MAX_FREE_REMINDERS} pending reminders. Cancel some with `/todo reminders`.",
                ephemeral=True,
            )
            return
        reminder = self.app.todo.add_reminder(
            task_id=None, user_id=interaction.user.id, kind="custom", fire_at=fire_at, text=clean_reminder,
            delivery=delivery,
        )
        where = {"dm": "by DM", "channel": "in the tasks channel", "both": "by DM and in the tasks channel"}[delivery]
        await interaction.response.send_message(
            f"Reminder #{reminder.id} set for {render.stamp(fire_at)} {where}.", ephemeral=True
        )

    @todo.command(name="reminders", description="Show pending reminders")
    @officer_only()
    async def reminders(self, interaction: discord.Interaction) -> None:
        pending = self.app.todo.pending_reminders()
        lines = []
        for reminder in pending[:MAX_REMINDER_LINES]:
            if reminder.task_id is None:
                who = f"<@{reminder.user_id}>"
                lines.append(f"#{reminder.id} {render.stamp(reminder.fire_at)} {who}: {render.clean(reminder.text or '')}")
            else:
                lines.append(f"Task #{reminder.task_id} ({reminder.kind}) {render.stamp(reminder.fire_at, 'R')}")
        if len(pending) > MAX_REMINDER_LINES:
            lines.append(f"+{len(pending) - MAX_REMINDER_LINES} more")
        mine = [r for r in pending if r.task_id is None and r.user_id == interaction.user.id]
        body = render.truncate("\n".join(lines) or "No pending reminders.", 4000)
        embed = discord.Embed(title="Pending reminders", description=body)
        await interaction.response.send_message(
            embed=embed, view=CancelRemindersView(self.app.todo, interaction.user.id, mine) if mine else discord.utils.MISSING,
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    done.autocomplete("task")(_task_choices)
    edit.autocomplete("task")(_task_choices)
    assign.autocomplete("task")(_task_choices)
    delete.autocomplete("task")(_task_choices)


async def setup(bot) -> None:
    await bot.add_cog(TodoCog(bot))

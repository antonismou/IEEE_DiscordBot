from __future__ import annotations

import re
from datetime import timedelta

import discord

from bot.checks import is_officer
from bot.roles import resolve_member
from bot.todo import render
from bot.todo.dates import tomorrow_morning
from bot.todo.service import complete_and_repeat

MAX_SNOOZES = 5
NONE_MENTIONS = discord.AllowedMentions.none()


async def _authorize(interaction: discord.Interaction):
    """The TodoCog, if the person who pressed the button is (still) an officer; otherwise refuse privately."""
    client = interaction.client
    cog = client.get_cog("TodoCog")
    guild_id = client.app.server.guild_id
    guild = client.get_guild(guild_id) if guild_id else None
    member = None
    if guild is not None:
        try:
            member = await resolve_member(guild, interaction.user)
        except discord.HTTPException:
            member = None
    if cog is None or member is None or not is_officer(member, client.app.server.officer_role_id):
        await interaction.response.send_message("Only branch officers can use this button.", ephemeral=True)
        return None
    return cog


async def _finish(interaction: discord.Interaction, note: str) -> None:
    """Replace the reminder's buttons with a one-line outcome."""
    content = (interaction.message.content if interaction.message else "") or ""
    await interaction.response.edit_message(
        content=render.truncate(f"{content}\n{note}"), view=None, allowed_mentions=NONE_MENTIONS
    )


class DoneButton(discord.ui.DynamicItem[discord.ui.Button], template=r"todo:done:(?P<id>\d+)"):
    def __init__(self, task_id: int):
        super().__init__(discord.ui.Button(label="Done", style=discord.ButtonStyle.success, custom_id=f"todo:done:{task_id}"))
        self.task_id = task_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _authorize(interaction)
        if cog is None:
            return
        repo = cog.app.todo
        task = repo.get_task(self.task_id)
        if task is None:
            await _finish(interaction, "This task no longer exists.")
            return
        finished, following = complete_and_repeat(repo, task, interaction.user.id, cog._clock())
        if finished is None:
            await _finish(interaction, "This task was already done.")
            return
        cog._board_dirty = True
        await cog.announce(interaction.client, render.announcement_text("done", finished, interaction.user.id, following))
        note = f"✅ Done by <@{interaction.user.id}>"
        if following:
            note += f". Next one is due {render.stamp(following.due_at)}"
        await _finish(interaction, note)


class _SnoozeMixin:
    """Shared behaviour of the two snooze buttons; each concrete class supplies its template, label and time."""

    prefix = ""
    label = ""

    def __init__(self, reminder_id: int):
        super().__init__(
            discord.ui.Button(label=self.label, style=discord.ButtonStyle.secondary, custom_id=f"{self.prefix}{reminder_id}")
        )  # DynamicItem.__init__ via the MRO
        self.reminder_id = reminder_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    def snooze_until(self, now):
        raise NotImplementedError

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = await _authorize(interaction)
        if cog is None:
            return
        repo = cog.app.todo
        original = repo.get_reminder(self.reminder_id)
        if original is None:
            await _finish(interaction, "This reminder no longer exists.")
            return
        if original.task_id is not None:
            task = repo.get_task(original.task_id)
            if task is None or task.status != "open":
                await _finish(interaction, "This task is already done or gone.")
                return
            if repo.count_pending_snoozes(original.task_id, original.user_id) >= MAX_SNOOZES:
                await interaction.response.send_message("This has been snoozed too many times already.", ephemeral=True)
                return
        until = self.snooze_until(cog._clock())
        repo.add_reminder(
            task_id=original.task_id, user_id=original.user_id, kind="snooze", fire_at=until, text=original.text,
            delivery=original.delivery,
        )
        await _finish(interaction, f"😴 Snoozed by <@{interaction.user.id}> until {render.stamp(until)}")


class SnoozeHourButton(_SnoozeMixin, discord.ui.DynamicItem[discord.ui.Button], template=r"todo:snooze1h:(?P<id>\d+)"):
    prefix = "todo:snooze1h:"
    label = "Snooze 1h"

    def snooze_until(self, now):
        return now + timedelta(hours=1)


class SnoozeTomorrowButton(_SnoozeMixin, discord.ui.DynamicItem[discord.ui.Button], template=r"todo:snoozetmr:(?P<id>\d+)"):
    prefix = "todo:snoozetmr:"
    label = "Snooze until tomorrow"

    def snooze_until(self, now):
        return tomorrow_morning(now)


DYNAMIC_ITEMS = (DoneButton, SnoozeHourButton, SnoozeTomorrowButton)


def build_reminder_view(delivery) -> discord.ui.View:
    """The buttons under a reminder: Done (for task reminders) and the two snoozes."""
    view = discord.ui.View(timeout=None)
    if delivery.task is not None:
        view.add_item(DoneButton(delivery.task.id))
    view.add_item(SnoozeHourButton(delivery.reminder.id))
    view.add_item(SnoozeTomorrowButton(delivery.reminder.id))
    return view

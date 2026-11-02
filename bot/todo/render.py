from __future__ import annotations

import math
from datetime import timedelta

import discord

PER_PAGE = 10
LIMIT = 2000
PRIORITY_MARK = {"low": "🔽", "normal": "", "high": "🔺"}
KIND_TEXT = {
    "2d": "is due in 2 days",
    "1d": "is due tomorrow",
    "1h": "is due in 1 hour",
    "due": "is due now",
    "overdue": "is overdue",
    "snooze": "reminder",
}


def clean(text: str) -> str:
    """Text safe to show: no markdown, no live mentions."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def truncate(text: str, limit: int = LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def stamp(dt, style: str = "f") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def task_line(task, *, show_assignee: bool = True) -> str:
    parts = [f"#{task.id} **{clean(task.title)}**"]
    mark = PRIORITY_MARK.get(task.priority, "")
    if mark:
        parts[0] = f"{mark} {parts[0]}"
    if task.due_at:
        parts.append(stamp(task.due_at, "R"))
    if show_assignee:
        parts.append(f"<@{task.assignee_id}>" if task.assignee_id else "unassigned")
    if task.repeat != "none":
        parts.append(f"repeats {task.repeat}")
    return " · ".join(parts)


def task_embed(task) -> discord.Embed:
    embed = discord.Embed(title=truncate(f"#{task.id} {clean(task.title)}", 256), description=truncate(clean(task.description), 4000) or None)
    embed.add_field(name="Status", value=task.status, inline=True)
    embed.add_field(name="Priority", value=task.priority, inline=True)
    embed.add_field(name="Assignee", value=f"<@{task.assignee_id}>" if task.assignee_id else "nobody yet", inline=True)
    embed.add_field(name="Due", value=stamp(task.due_at) if task.due_at else "no date", inline=True)
    embed.add_field(name="Repeats", value=task.repeat, inline=True)
    embed.add_field(name="Reminders", value=task.notify, inline=True)
    return embed


def page_count(total: int, per_page: int = PER_PAGE) -> int:
    return max(1, math.ceil(total / per_page))


def list_embed(tasks, page: int, title: str, per_page: int = PER_PAGE) -> discord.Embed:
    pages = page_count(len(tasks), per_page)
    page = min(max(page, 0), pages - 1)
    chunk = tasks[page * per_page:(page + 1) * per_page]
    body = "\n".join(task_line(t) for t in chunk) or "Nothing here."
    embed = discord.Embed(title=title, description=truncate(body, 4000))
    embed.set_footer(text=f"Page {page + 1}/{pages} · {len(tasks)} task(s)")
    return embed


def reminder_text(delivery) -> str:
    """What a reminder says (the caller adds who to mention)."""
    reminder, task = delivery.reminder, delivery.task
    if task is None:
        text = f"⏰ Reminder: {clean(reminder.text or '')}"
    else:
        what = KIND_TEXT.get(reminder.kind, "reminder")
        when = f" ({stamp(task.due_at)})" if task.due_at else ""
        text = f"⏰ Task #{task.id} **{clean(task.title)}** {what}{when}"
    if delivery.late:
        text += " (late)"
    return truncate(text)


def announcement_text(event: str, task, actor_id: int, next_task=None) -> str:
    who = f"<@{actor_id}>" if actor_id else "someone"
    title = clean(task.title)
    if event == "created":
        extra = f", assigned to <@{task.assignee_id}>" if task.assignee_id else ", unassigned"
        due = f", due {stamp(task.due_at)}" if task.due_at else ""
        text = f"📝 Task #{task.id} **{title}** created by {who}{extra}{due}"
    elif event == "assigned":
        target = f"<@{task.assignee_id}>" if task.assignee_id else "nobody"
        text = f"👤 Task #{task.id} **{title}** assigned to {target} by {who}"
    elif event == "done":
        text = f"✅ Task #{task.id} **{title}** done by {who}"
        if next_task is not None and next_task.due_at:
            text += f". Next one (#{next_task.id}) is due {stamp(next_task.due_at)}"
    elif event == "deleted":
        text = f"🗑️ Task #{task.id} **{title}** deleted by {who}"
    else:
        raise ValueError(f"Unknown event: {event}")
    return truncate(text)


BOARD_LINES = 25


def board_embed(tasks, now) -> discord.Embed:
    """The live board: every open task grouped by how soon it is due, at most BOARD_LINES lines."""
    week = now + timedelta(days=7)
    groups = (
        ("🔴 Overdue", [t for t in tasks if t.due_at and t.due_at < now]),
        ("🟠 Next 7 days", [t for t in tasks if t.due_at and now <= t.due_at <= week]),
        ("🟢 Later", [t for t in tasks if t.due_at and t.due_at > week]),
        ("⚪ No date", [t for t in tasks if not t.due_at]),
    )
    lines: list[str] = []
    shown = 0
    for heading, members in groups:
        if not members or shown >= BOARD_LINES:
            continue
        lines.append(f"**{heading}**")
        for task in members:
            if shown >= BOARD_LINES:
                break
            lines.append(task_line(task))
            shown += 1
    if len(tasks) > shown:
        lines.append(f"+{len(tasks) - shown} more")
    embed = discord.Embed(title="Open tasks", description=truncate("\n".join(lines) or "No open tasks.", 4000))
    embed.set_footer(text=f"{len(tasks)} open")
    embed.timestamp = now
    return embed

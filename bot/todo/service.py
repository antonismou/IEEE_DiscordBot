from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.todo.store import Reminder, Task, TodoRepo

log = logging.getLogger(__name__)

# (kind, how long before the due time it fires). "overdue" fires a day after.
OFFSETS = (
    ("2d", timedelta(days=2)),
    ("1d", timedelta(days=1)),
    ("1h", timedelta(hours=1)),
    ("due", timedelta(0)),
    ("overdue", -timedelta(days=1)),
)
BEFORE_DUE = ("2d", "1d", "1h")  # superseded once the due time has passed
LATE_AFTER = timedelta(minutes=10)
UNASSIGNED = 0  # reminder user id meaning "ping the Officer role"


@dataclass(frozen=True)
class Delivery:
    reminder: Reminder
    task: Task | None
    late: bool


def schedule_task(repo: TodoRepo, task: Task, now: datetime) -> None:
    """(Re)create the unsent deadline reminders of a task. Rows already in the past are not created."""
    repo.delete_unsent_for_task(task.id, tuple(kind for kind, _ in OFFSETS))
    if task.due_at is None or task.status != "open":
        return
    for kind, before in OFFSETS:
        fire_at = task.due_at - before
        if fire_at <= now:
            continue
        if task.assignee_id is None:
            user_id, delivery = UNASSIGNED, "channel"
        else:
            user_id, delivery = task.assignee_id, task.notify
        repo.add_reminder(task_id=task.id, user_id=user_id, kind=kind, fire_at=fire_at, text=None, delivery=delivery)


async def run_due(
    repo: TodoRepo,
    now: datetime,
    send: Callable[[Delivery], Awaitable[None]],
    *,
    is_active: Callable[[int], Awaitable[bool]],
) -> int:
    """Send every reminder that is due, once. Returns how many were handed to `send`."""
    sent = 0
    for reminder in repo.due_reminders(now):
        if not repo.mark_sent(reminder.id, now):  # claim first: a crash may lose a reminder, never repeat one
            continue
        task = repo.get_task(reminder.task_id) if reminder.task_id is not None else None
        if reminder.task_id is not None:
            if task is None or task.status != "open":
                continue
            if reminder.kind in BEFORE_DUE and task.due_at is not None and task.due_at <= now:
                continue
        try:
            if reminder.user_id != UNASSIGNED and not await is_active(reminder.user_id):
                continue
            await send(Delivery(reminder, task, now - reminder.fire_at > LATE_AFTER))
            sent += 1
        except Exception:
            log.exception("Could not deliver reminder %s", reminder.id)
    return sent

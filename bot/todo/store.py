from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from bot.db import from_iso, to_iso

UPDATABLE = {"title", "description", "assignee_id", "due_at", "priority", "repeat", "notify"}


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    description: str
    created_by: int
    assignee_id: int | None
    due_at: datetime | None
    priority: str
    status: str
    repeat: str
    notify: str
    created_at: datetime
    done_at: datetime | None
    done_by: int | None


@dataclass(frozen=True)
class Reminder:
    id: int
    task_id: int | None
    user_id: int
    kind: str
    fire_at: datetime
    text: str | None
    delivery: str
    sent_at: datetime | None


def _reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=row["id"], task_id=row["task_id"], user_id=row["user_id"], kind=row["kind"],
        fire_at=from_iso(row["fire_at"]), text=row["text"], delivery=row["delivery"], sent_at=_opt(row["sent_at"]),
    )


def _opt(value: str | None) -> datetime | None:
    return from_iso(value) if value else None


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"], title=row["title"], description=row["description"], created_by=row["created_by"],
        assignee_id=row["assignee_id"], due_at=_opt(row["due_at"]), priority=row["priority"], status=row["status"],
        repeat=row["repeat"], notify=row["notify"], created_at=from_iso(row["created_at"]),
        done_at=_opt(row["done_at"]), done_by=row["done_by"],
    )


class TodoRepo:
    """The officers' shared task list and its reminders."""

    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    # --- tasks -----------------------------------------------------------------------------------------------
    def add_task(
        self, *, title: str, description: str, created_by: int, assignee_id: int | None, due_at: datetime | None,
        priority: str, repeat: str, notify: str, now: datetime,
    ) -> Task:
        with self._c:
            cur = self._c.execute(
                "INSERT INTO tasks (title, description, created_by, assignee_id, due_at, priority, repeat, notify, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, description, created_by, assignee_id, to_iso(due_at) if due_at else None, priority, repeat,
                 notify, to_iso(now)),
            )
        return self.get_task(cur.lastrowid)

    def get_task(self, task_id: int) -> Task | None:
        row = self._c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task(row) if row else None

    def update_task(self, task_id: int, **fields) -> Task:
        unknown = set(fields) - UPDATABLE
        if unknown:
            raise ValueError(f"Cannot update: {', '.join(sorted(unknown))}")
        if "due_at" in fields and fields["due_at"] is not None:
            fields["due_at"] = to_iso(fields["due_at"])
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            with self._c:
                self._c.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*fields.values(), task_id))
        return self.get_task(task_id)

    def complete_task(self, task_id: int, by: int, now: datetime) -> Task | None:
        """Mark an open task done. None when it doesn't exist or was already done (so a double click is harmless)."""
        with self._c:
            cur = self._c.execute(
                "UPDATE tasks SET status = 'done', done_at = ?, done_by = ? WHERE id = ? AND status = 'open'",
                (to_iso(now), by, task_id),
            )
        return self.get_task(task_id) if cur.rowcount else None

    def delete_task(self, task_id: int) -> bool:
        with self._c:
            self._c.execute("DELETE FROM reminders WHERE task_id = ?", (task_id,))
            cur = self._c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0

    def count_open(self) -> int:
        return self._c.execute("SELECT COUNT(*) FROM tasks WHERE status = 'open'").fetchone()[0]

    def list_tasks(self, user_id: int, view: str, now: datetime) -> list[Task]:
        """Tasks for a view: mine | all | unassigned | overdue | done. Open ones come soonest-due first."""
        if view == "done":
            rows = self._c.execute("SELECT * FROM tasks WHERE status = 'done' ORDER BY done_at DESC, id DESC LIMIT 20")
            return [_task(r) for r in rows]
        where = {
            "mine": "assignee_id = :user",
            "all": "1",
            "unassigned": "assignee_id IS NULL",
            "overdue": "due_at IS NOT NULL AND due_at < :now",
        }.get(view)
        if where is None:
            raise ValueError(f"Unknown view: {view}")
        rows = self._c.execute(
            f"SELECT * FROM tasks WHERE status = 'open' AND {where} "
            "ORDER BY due_at IS NULL, due_at, id",
            {"user": user_id, "now": to_iso(now)},
        )
        return [_task(r) for r in rows]

    def delete_by_user(self, user_id: int) -> int:
        """Forget a member: drop their reminders, unassign them, and remove them as creator/completer."""
        with self._c:
            changed = self._c.execute("DELETE FROM reminders WHERE user_id = ?", (user_id,)).rowcount
            changed += self._c.execute("UPDATE tasks SET assignee_id = NULL WHERE assignee_id = ?", (user_id,)).rowcount
            changed += self._c.execute("UPDATE tasks SET created_by = 0 WHERE created_by = ?", (user_id,)).rowcount
            changed += self._c.execute("UPDATE tasks SET done_by = 0 WHERE done_by = ?", (user_id,)).rowcount
        return changed

    # --- reminders -------------------------------------------------------------------------------------------
    def add_reminder(
        self, *, task_id: int | None, user_id: int, kind: str, fire_at: datetime, text: str | None, delivery: str
    ) -> Reminder:
        with self._c:
            cur = self._c.execute(
                "INSERT INTO reminders (task_id, user_id, kind, fire_at, text, delivery) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, user_id, kind, to_iso(fire_at), text, delivery),
            )
        return self.get_reminder(cur.lastrowid)

    def get_reminder(self, reminder_id: int) -> Reminder | None:
        row = self._c.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return _reminder(row) if row else None

    def due_reminders(self, now: datetime) -> list[Reminder]:
        rows = self._c.execute(
            "SELECT * FROM reminders WHERE sent_at IS NULL AND fire_at <= ? ORDER BY fire_at, id", (to_iso(now),)
        )
        return [_reminder(r) for r in rows]

    def mark_sent(self, reminder_id: int, now: datetime) -> bool:
        """Claim a reminder. True for exactly one caller, so it can never be sent twice."""
        with self._c:
            cur = self._c.execute(
                "UPDATE reminders SET sent_at = ? WHERE id = ? AND sent_at IS NULL", (to_iso(now), reminder_id)
            )
        return cur.rowcount > 0

    def delete_unsent_for_task(self, task_id: int, kinds: tuple[str, ...] | None = None) -> None:
        sql, args = "DELETE FROM reminders WHERE task_id = ? AND sent_at IS NULL", [task_id]
        if kinds:
            sql += f" AND kind IN ({', '.join('?' * len(kinds))})"
            args += kinds
        with self._c:
            self._c.execute(sql, args)

    def pending_reminders(self) -> list[Reminder]:
        rows = self._c.execute("SELECT * FROM reminders WHERE sent_at IS NULL ORDER BY fire_at, id")
        return [_reminder(r) for r in rows]

    def cancel_reminder(self, reminder_id: int, user_id: int) -> bool:
        """Cancel one of the caller's own free-standing reminders."""
        with self._c:
            cur = self._c.execute(
                "DELETE FROM reminders WHERE id = ? AND user_id = ? AND task_id IS NULL AND sent_at IS NULL",
                (reminder_id, user_id),
            )
        return cur.rowcount > 0

    def count_pending_free(self, user_id: int) -> int:
        return self._c.execute(
            "SELECT COUNT(*) FROM reminders WHERE user_id = ? AND task_id IS NULL AND sent_at IS NULL", (user_id,)
        ).fetchone()[0]

    def count_pending_snoozes(self, task_id: int, user_id: int) -> int:
        return self._c.execute(
            "SELECT COUNT(*) FROM reminders WHERE task_id = ? AND user_id = ? AND kind = 'snooze' AND sent_at IS NULL",
            (task_id, user_id),
        ).fetchone()[0]

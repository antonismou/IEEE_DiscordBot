from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.todo.render import announcement_text, list_embed, reminder_text, task_line
from bot.todo.service import Delivery
from bot.todo.store import Reminder, Task

NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)


def task(**kw):
    base = dict(
        id=12, title="Book venue", description="", created_by=1, assignee_id=5, due_at=NOW + timedelta(days=2),
        priority="normal", status="open", repeat="none", notify="both", created_at=NOW, done_at=None, done_by=None,
    )
    base.update(kw)
    return Task(**base)


def delivery(kind="2d", t=None, late=False, text=None):
    r = Reminder(1, None if t is None else t.id, 5, kind, NOW, text, "both", None)
    return Delivery(r, t, late)


def test_titles_cannot_ping_or_format():
    line = task_line(task(title="@everyone **hi** <@1>"))
    assert "@everyone" not in line.replace("@​everyone", "")
    assert "**hi**" not in line.replace("**" + "@", "")  # the title's own asterisks are escaped
    assert "\\*\\*hi\\*\\*" in line


def test_line_shows_assignee_or_unassigned_and_repeat():
    assert "<@5>" in task_line(task())
    assert "unassigned" in task_line(task(assignee_id=None))
    assert "repeats weekly" in task_line(task(repeat="weekly"))


def test_empty_list_has_a_friendly_message():
    assert "Nothing here" in list_embed([], 0, "Mine").description


def test_paging_boundaries():
    tasks = [task(id=i, title=f"t{i}") for i in range(25)]
    assert "Page 1/3" in list_embed(tasks, 0, "x").footer.text
    assert "Page 3/3" in list_embed(tasks, 2, "x").footer.text
    assert "Page 3/3" in list_embed(tasks, 99, "x").footer.text  # clamped
    assert "Page 1/3" in list_embed(tasks, -4, "x").footer.text
    assert list_embed(tasks, 2, "x").description.count("\n") == 4


@pytest.mark.parametrize(
    "kind,words",
    [("2d", "in 2 days"), ("1d", "tomorrow"), ("1h", "in 1 hour"), ("due", "due now"), ("overdue", "overdue")],
)
def test_reminder_wording(kind, words):
    assert words in reminder_text(delivery(kind, task()))


def test_free_standing_reminder_and_late_suffix():
    text = reminder_text(delivery("custom", None, late=True, text="call @everyone"))
    assert text.startswith("⏰ Reminder: call")
    assert text.endswith("(late)")
    assert "@everyone" not in text.replace("@​everyone", "")


def test_long_text_is_truncated_to_discord_limit():
    assert len(reminder_text(delivery("custom", None, text="x" * 5000))) <= 2000
    assert len(announcement_text("created", task(title="y" * 5000), 1)) <= 2000


def test_announcements():
    assert "created by <@1>" in announcement_text("created", task(), 1)
    assert "assigned to <@5>" in announcement_text("assigned", task(), 1)
    assert "done by <@1>" in announcement_text("done", task(), 1)
    nxt = task(id=13)
    assert "#13" in announcement_text("done", task(), 1, nxt)
    assert "deleted" in announcement_text("deleted", task(), 1)
    with pytest.raises(ValueError):
        announcement_text("exploded", task(), 1)

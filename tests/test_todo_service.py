from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone


import pytest

from bot.todo.service import UNASSIGNED, Delivery, run_due, schedule_task
from bot.todo.store import TodoRepo

NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)


def make(repo, *, due=None, assignee=5, notify="both"):
    return repo.add_task(
        title="t", description="", created_by=1, assignee_id=assignee, due_at=due, priority="normal",
        repeat="none", notify=notify, now=NOW,
    )


def kinds(repo, task):
    return sorted(
        ((r.kind, r.fire_at - task.due_at) for r in repo.pending_reminders() if r.task_id == task.id),
        key=lambda x: x[1],
    )


def test_five_reminders_for_a_task_due_in_five_days(conn):
    repo = TodoRepo(conn)
    task = make(repo, due=NOW + timedelta(days=5))
    schedule_task(repo, task, NOW)
    assert [k for k, _ in kinds(repo, task)] == ["2d", "1d", "1h", "due", "overdue"]


def test_only_future_rows_are_created(conn):
    repo = TodoRepo(conn)
    t3h = make(repo, due=NOW + timedelta(hours=3))
    t30m = make(repo, due=NOW + timedelta(minutes=30))
    schedule_task(repo, t3h, NOW)
    schedule_task(repo, t30m, NOW)
    assert [k for k, _ in kinds(repo, t3h)] == ["1h", "due", "overdue"]
    assert [k for k, _ in kinds(repo, t30m)] == ["due", "overdue"]


def test_no_due_date_means_no_reminders(conn):
    repo = TodoRepo(conn)
    task = make(repo)
    schedule_task(repo, task, NOW)
    assert repo.pending_reminders() == []


def test_rescheduling_replaces_unsent_rows_but_keeps_sent_ones_and_free_reminders(conn):
    repo = TodoRepo(conn)
    task = make(repo, due=NOW + timedelta(days=5))
    schedule_task(repo, task, NOW)
    first = repo.pending_reminders()[0]
    repo.mark_sent(first.id, NOW)
    free = repo.add_reminder(task_id=None, user_id=5, kind="custom", fire_at=NOW + timedelta(hours=1), text="x", delivery="dm")
    schedule_task(repo, repo.update_task(task.id, due_at=NOW + timedelta(days=9)), NOW)
    pending = repo.pending_reminders()
    assert len([r for r in pending if r.task_id == task.id]) == 5
    assert free.id in {r.id for r in pending}
    assert repo.get_reminder(first.id).sent_at is not None


def test_notify_and_unassigned_routing(conn):
    repo = TodoRepo(conn)
    dm = make(repo, due=NOW + timedelta(days=5), notify="dm")
    nobody = make(repo, due=NOW + timedelta(days=5), assignee=None, notify="dm")
    schedule_task(repo, dm, NOW)
    schedule_task(repo, nobody, NOW)
    by_task = {}
    for r in repo.pending_reminders():
        by_task.setdefault(r.task_id, set()).add((r.user_id, r.delivery))
    assert by_task[dm.id] == {(5, "dm")}
    assert by_task[nobody.id] == {(UNASSIGNED, "channel")}


def run(coro):
    return asyncio.run(coro)


class Sink:
    def __init__(self):
        self.items: list[Delivery] = []

    async def __call__(self, delivery):
        self.items.append(delivery)


async def active(_user_id):
    return True


def test_run_due_sends_due_rows_in_order_once(conn):
    repo = TodoRepo(conn)
    task = make(repo, due=NOW + timedelta(days=5))
    schedule_task(repo, task, NOW)
    sink = Sink()
    later = NOW + timedelta(days=3, hours=1)  # the 2d reminder (due-2d = now+3d) is due; 1d is not
    assert run(run_due(repo, later, sink, is_active=active)) == 1
    assert [d.reminder.kind for d in sink.items] == ["2d"]
    assert run(run_due(repo, later, sink, is_active=active)) == 0


def test_superseded_before_due_reminders_are_dropped_after_the_due_time(conn):
    repo = TodoRepo(conn)
    task = make(repo, due=NOW + timedelta(days=5))
    schedule_task(repo, task, NOW)
    sink = Sink()
    after = NOW + timedelta(days=5, minutes=30)  # bot was off for the whole run-up
    run(run_due(repo, after, sink, is_active=active))
    assert [d.reminder.kind for d in sink.items] == ["due"]
    assert all(d.late for d in sink.items)


def test_done_or_deleted_tasks_send_nothing(conn):
    repo = TodoRepo(conn)
    done = make(repo, due=NOW + timedelta(days=5))
    gone = make(repo, due=NOW + timedelta(days=5))
    schedule_task(repo, done, NOW)
    schedule_task(repo, gone, NOW)
    repo.complete_task(done.id, 1, NOW)
    conn.execute("DELETE FROM tasks WHERE id = ?", (gone.id,))
    sink = Sink()
    run(run_due(repo, NOW + timedelta(days=30), sink, is_active=active))
    assert sink.items == []


def test_inactive_user_is_dropped_but_unassigned_is_not_checked(conn):
    repo = TodoRepo(conn)
    mine = make(repo, due=NOW + timedelta(hours=2))
    nobody = make(repo, due=NOW + timedelta(hours=2), assignee=None)
    schedule_task(repo, mine, NOW)
    schedule_task(repo, nobody, NOW)

    async def nobody_is_active(_):
        return False

    sink = Sink()
    run(run_due(repo, NOW + timedelta(hours=2), sink, is_active=nobody_is_active))
    assert {d.task.id for d in sink.items} == {nobody.id}


def test_a_failing_send_does_not_stop_the_rest(conn):
    repo = TodoRepo(conn)
    for _ in range(2):
        schedule_task(repo, make(repo, due=NOW + timedelta(minutes=5)), NOW)
    seen = []

    async def flaky(delivery):
        seen.append(delivery.reminder.id)
        if len(seen) == 1:
            raise RuntimeError("boom")

    assert run(run_due(repo, NOW + timedelta(minutes=5), flaky, is_active=active)) == 1
    assert len(seen) == 2


def test_late_flag_only_past_ten_minutes(conn):
    repo = TodoRepo(conn)
    task = make(repo, due=NOW + timedelta(minutes=5))
    schedule_task(repo, task, NOW)
    sink = Sink()
    run(run_due(repo, NOW + timedelta(minutes=5, seconds=30), sink, is_active=active))
    assert [d.late for d in sink.items] == [False]


def test_mark_sent_is_claimed_once(conn):
    repo = TodoRepo(conn)
    r = repo.add_reminder(task_id=None, user_id=1, kind="custom", fire_at=NOW, text="x", delivery="dm")
    assert repo.mark_sent(r.id, NOW) is True
    assert repo.mark_sent(r.id, NOW) is False


def test_cancel_reminder_only_own_free_standing(conn):
    repo = TodoRepo(conn)
    free = repo.add_reminder(task_id=None, user_id=1, kind="custom", fire_at=NOW, text="x", delivery="dm")
    task = make(repo, due=NOW + timedelta(days=1))
    tied = repo.add_reminder(task_id=task.id, user_id=1, kind="1h", fire_at=NOW, text=None, delivery="dm")
    assert repo.cancel_reminder(free.id, 2) is False
    assert repo.cancel_reminder(tied.id, 1) is False
    assert repo.cancel_reminder(free.id, 1) is True
    assert repo.count_pending_free(1) == 0


# --- repeating tasks ---------------------------------------------------------------------------------------

from bot.todo.service import complete_and_repeat  # noqa: E402


def repeating(repo, repeat, due):
    return repo.add_task(
        title="weekly report", description="d", created_by=1, assignee_id=5, due_at=due, priority="high",
        repeat=repeat, notify="dm", now=NOW,
    )


@pytest.mark.parametrize(
    "repeat,days", [("daily", 1), ("weekly", 7)]
)
def test_repeat_spawns_the_next_with_fresh_reminders(conn, repeat, days):
    repo = TodoRepo(conn)
    due = NOW + timedelta(days=1)
    task = repeating(repo, repeat, due)
    schedule_task(repo, task, NOW)
    finished, nxt = complete_and_repeat(repo, task, 5, NOW)
    assert finished.status == "done" and nxt.status == "open"
    assert nxt.due_at == due + timedelta(days=days)
    assert (nxt.title, nxt.assignee_id, nxt.priority, nxt.repeat, nxt.notify) == ("weekly report", 5, "high", repeat, "dm")
    assert {r.task_id for r in repo.pending_reminders()} == {nxt.id}


def test_month_end_clamps(conn):
    repo = TodoRepo(conn)
    jan31 = datetime(2027, 1, 31, 16, 0, tzinfo=timezone.utc)
    task = repeating(repo, "monthly", jan31)
    _, nxt = complete_and_repeat(repo, task, 5, datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert nxt.due_at.astimezone().date().month in (2, 3)  # 28 Feb 18:00 Athens
    assert nxt.due_at == datetime(2027, 2, 28, 16, 0, tzinfo=timezone.utc)


def test_overdue_by_weeks_still_lands_in_the_future(conn):
    repo = TodoRepo(conn)
    task = repeating(repo, "weekly", NOW - timedelta(days=30))
    _, nxt = complete_and_repeat(repo, task, 5, NOW)
    assert NOW < nxt.due_at <= NOW + timedelta(days=7)


def test_non_repeating_and_already_done_spawn_nothing(conn):
    repo = TodoRepo(conn)
    plain = repeating(repo, "none", NOW + timedelta(days=1))
    finished, nxt = complete_and_repeat(repo, plain, 5, NOW)
    assert finished is not None and nxt is None
    again = complete_and_repeat(repo, plain, 5, NOW)
    assert again == (None, None)
    weekly = repeating(repo, "weekly", NOW + timedelta(days=1))
    complete_and_repeat(repo, weekly, 5, NOW)
    assert complete_and_repeat(repo, weekly, 5, NOW) == (None, None)
    assert repo.count_open() == 1   # exactly one successor despite two attempts

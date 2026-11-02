from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from bot.todo.store import Task, TodoRepo

NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)


def add(repo, title="Book venue", *, created_by=1, assignee_id=None, due_at=None, **kw):
    return repo.add_task(
        title=title, description=kw.pop("description", ""), created_by=created_by, assignee_id=assignee_id,
        due_at=due_at, priority=kw.pop("priority", "normal"), repeat=kw.pop("repeat", "none"),
        notify=kw.pop("notify", "both"), now=NOW,
    )


def test_add_and_get_round_trip(conn):
    repo = TodoRepo(conn)
    due = NOW + timedelta(days=3)
    task = add(repo, assignee_id=5, due_at=due, priority="high", description="d")
    assert task == Task(
        id=task.id, title="Book venue", description="d", created_by=1, assignee_id=5, due_at=due,
        priority="high", status="open", repeat="none", notify="both", created_at=NOW, done_at=None, done_by=None,
    )
    assert repo.get_task(task.id) == task
    assert repo.get_task(999) is None


def test_list_views(conn):
    repo = TodoRepo(conn)
    mine = add(repo, "mine", assignee_id=7, due_at=NOW + timedelta(days=2))
    nodate = add(repo, "nodate", assignee_id=7)
    soon = add(repo, "soon", assignee_id=8, due_at=NOW + timedelta(hours=1))
    unassigned = add(repo, "unassigned")
    overdue = add(repo, "overdue", assignee_id=8, due_at=NOW - timedelta(days=1))
    finished = add(repo, "finished", assignee_id=7)
    repo.complete_task(finished.id, 7, NOW)

    ids = lambda view: [t.id for t in repo.list_tasks(7, view, NOW)]  # noqa: E731
    assert ids("mine") == [mine.id, nodate.id]
    assert ids("all") == [overdue.id, soon.id, mine.id, nodate.id, unassigned.id]
    assert ids("unassigned") == [unassigned.id]
    assert ids("overdue") == [overdue.id]
    assert ids("done") == [finished.id]


def test_done_view_is_the_latest_twenty(conn):
    repo = TodoRepo(conn)
    for i in range(25):
        task = add(repo, f"t{i}")
        repo.complete_task(task.id, 1, NOW + timedelta(minutes=i))
    done = repo.list_tasks(1, "done", NOW)
    assert len(done) == 20
    assert done[0].title == "t24"


def test_complete_task_records_who_and_when(conn):
    repo = TodoRepo(conn)
    task = add(repo)
    done = repo.complete_task(task.id, 9, NOW)
    assert (done.status, done.done_by, done.done_at) == ("done", 9, NOW)


def test_complete_task_twice_returns_none_the_second_time(conn):
    repo = TodoRepo(conn)
    task = add(repo)
    assert repo.complete_task(task.id, 9, NOW) is not None
    assert repo.complete_task(task.id, 9, NOW) is None
    assert repo.complete_task(999, 9, NOW) is None


def test_update_task_changes_only_the_given_fields(conn):
    repo = TodoRepo(conn)
    task = add(repo, assignee_id=5)
    updated = repo.update_task(task.id, title="New", due_at=NOW + timedelta(days=1), assignee_id=None)
    assert updated.title == "New"
    assert updated.assignee_id is None
    assert updated.due_at == NOW + timedelta(days=1)
    assert updated.priority == "normal"
    with pytest.raises(ValueError):
        repo.update_task(task.id, id=5)


def test_delete_task_removes_its_reminders(conn):
    repo = TodoRepo(conn)
    task = add(repo)
    keep = add(repo, "keep")
    for tid in (task.id, keep.id):
        conn.execute(
            "INSERT INTO reminders (task_id, user_id, kind, fire_at, delivery) VALUES (?, 1, '1h', 'x', 'dm')", (tid,)
        )
    assert repo.delete_task(task.id) is True
    assert repo.delete_task(task.id) is False
    assert [r["task_id"] for r in conn.execute("SELECT task_id FROM reminders")] == [keep.id]


def test_count_open(conn):
    repo = TodoRepo(conn)
    a = add(repo)
    add(repo)
    repo.complete_task(a.id, 1, NOW)
    assert repo.count_open() == 1


def test_delete_by_user_unassigns_anonymises_and_drops_reminders(conn):
    repo = TodoRepo(conn)
    theirs = add(repo, "theirs", created_by=7, assignee_id=7)
    other = add(repo, "other", created_by=8, assignee_id=8)
    done_by_them = add(repo, "done", created_by=8)
    repo.complete_task(done_by_them.id, 7, NOW)
    for uid in (7, 8):
        conn.execute("INSERT INTO reminders (task_id, user_id, kind, fire_at, delivery) VALUES (NULL, ?, 'custom', 'x', 'dm')", (uid,))

    assert repo.delete_by_user(7) > 0
    t = repo.get_task(theirs.id)
    assert (t.created_by, t.assignee_id) == (0, None)
    assert repo.get_task(other.id).assignee_id == 8
    assert repo.get_task(done_by_them.id).done_by == 0
    assert [r["user_id"] for r in conn.execute("SELECT user_id FROM reminders")] == [8]
    assert repo.delete_by_user(7) == 0


def test_check_constraints_reject_bad_priority(conn):
    repo = TodoRepo(conn)
    task = add(repo)
    with pytest.raises(sqlite3.IntegrityError):
        repo.update_task(task.id, priority="urgent")

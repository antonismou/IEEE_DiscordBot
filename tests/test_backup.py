import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone

from bot.backup import backup_database

NOW = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)


def test_backup_is_a_private_readable_copy(conn, members, tmp_path):
    members.add_manual_member(discord_id=1, full_name="Prof X", verified_by=2, note="Professor", verified_at=NOW)
    dest = backup_database(conn, tmp_path / "backups", now=NOW)
    assert dest.name == "bot-20261004.sqlite3"
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(dest.parent).st_mode) == 0o700
    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT full_name FROM members").fetchone()[0] == "Prof X"
    copy.close()


def test_only_the_last_seven_backups_are_kept(conn, tmp_path):
    folder = tmp_path / "backups"
    for day in range(9):
        backup_database(conn, folder, now=NOW + timedelta(days=day))
    names = sorted(p.name for p in folder.glob("bot-*.sqlite3"))
    assert len(names) == 7
    assert names[0] == "bot-20261006.sqlite3" and names[-1] == "bot-20261012.sqlite3"


def test_backing_up_twice_in_a_day_overwrites(conn, tmp_path):
    folder = tmp_path / "backups"
    backup_database(conn, folder, now=NOW)
    backup_database(conn, folder, now=NOW)
    assert len(list(folder.glob("bot-*.sqlite3"))) == 1

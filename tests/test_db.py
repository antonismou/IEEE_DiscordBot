import os
import stat
from datetime import datetime, timezone

from bot.db import connect, from_iso, to_iso, utcnow


def test_database_and_wal_files_are_private(tmp_path):
    path = tmp_path / "data" / "bot.sqlite3"
    conn = connect(path)
    conn.execute("INSERT INTO send_log (sent_at) VALUES ('x')")
    conn.commit()
    for suffix in ("", "-wal"):
        file = path.parent / (path.name + suffix)
        if file.exists():
            assert stat.S_IMODE(os.stat(file).st_mode) == 0o600, file
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    conn.close()


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "bot.sqlite3"
    connect(path).close()
    connect(path).close()


def test_time_helpers_round_trip():
    now = utcnow()
    assert now.tzinfo is not None
    assert from_iso(to_iso(now)) == now.replace(microsecond=0)
    assert to_iso(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2026-01-01T00:00:00+00:00"

import os
import stat
from datetime import datetime, timezone

from bot.db import connect, from_iso, to_iso, utcnow


def test_database_and_wal_files_are_private(tmp_path):
    path = tmp_path / "data" / "bot.sqlite3"
    conn = connect(path)
    conn.execute("INSERT INTO send_log (discord_id, email_hash, sent_at) VALUES (1, 'h', 'x')")
    conn.commit()
    for suffix in ("", "-wal"):
        file = path.parent / (path.name + suffix)
        if file.exists():
            assert stat.S_IMODE(os.stat(file).st_mode) == 0o600, file
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    conn.close()


def test_deleted_personal_data_does_not_linger_in_the_database_file(tmp_path):
    path = tmp_path / "bot.sqlite3"
    conn = connect(path)
    for i in range(20):  # a realistic table: deleting one row must not leave its bytes on a shared page
        marker = i == 10
        conn.execute(
            "INSERT INTO members (discord_id, email, full_name, method, verified_at) VALUES (?, ?, ?, 'email', 'x')",
            (i, "zzuniquemarker@tuc.gr" if marker else f"s{i}@tuc.gr", "ZZUNIQUEMARKERNAME" if marker else f"Student {i}"),
        )
    conn.commit()
    conn.execute("DELETE FROM members WHERE discord_id = 10")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    raw = path.read_bytes()
    assert b"ZZUNIQUEMARKERNAME" not in raw and b"zzuniquemarker@tuc.gr" not in raw


def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "bot.sqlite3"
    connect(path).close()
    connect(path).close()


def test_time_helpers_round_trip():
    now = utcnow()
    assert now.tzinfo is not None
    assert from_iso(to_iso(now)) == now.replace(microsecond=0)
    assert to_iso(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "2026-01-01T00:00:00+00:00"


def test_existing_database_file_is_tightened_to_0600(tmp_path):
    path = tmp_path / "bot.sqlite3"
    connect(path).close()
    os.chmod(path, 0o644)
    connect(path).close()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_unwritable_data_directory_gives_a_clear_error(tmp_path):
    import pytest

    from bot.db import DataDirError

    folder = tmp_path / "ro"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        with pytest.raises(DataDirError, match="must be writable by the user the bot runs as"):
            connect(folder / "bot.sqlite3")
    finally:
        folder.chmod(0o700)


def test_pre_existing_open_directory_is_warned_about_not_chmodded(tmp_path, caplog):
    folder = tmp_path / "open"
    folder.mkdir()
    folder.chmod(0o755)
    connect(folder / "bot.sqlite3").close()
    assert "accessible by other users" in caplog.text
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o755   # a possibly shared directory is left alone

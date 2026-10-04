from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from bot.db import utcnow


def backup_database(conn: sqlite3.Connection, backup_dir: Path, *, keep: int = 7, now: datetime | None = None) -> Path:
    now = now or utcnow()
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = backup_dir / f"bot-{now:%Y%m%d}.sqlite3"
    os.close(os.open(dest, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    os.chmod(dest, 0o600)
    for old in sorted(backup_dir.glob("bot-*.sqlite3"))[:-keep]:
        old.unlink()
    return dest

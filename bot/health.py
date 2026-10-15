from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAIL_ALERT_EVERY = timedelta(hours=1)


@dataclass
class Health:
    """What the bot remembers about itself while it runs (nothing here needs to survive a restart)."""

    last_poll: datetime | None = None
    last_mail_alert: datetime | None = None

    def should_alert_mail(self, now: datetime) -> bool:
        """True at most once per hour, and records that an alert is being sent."""
        if self.last_mail_alert is not None and now - self.last_mail_alert < MAIL_ALERT_EVERY:
            return False
        self.last_mail_alert = now
        return True


def last_backup_time(backup_dir: Path) -> datetime | None:
    """When the newest backup file was written (read from disk, so it survives restarts)."""
    files = list(Path(backup_dir).glob("bot-*.sqlite3"))
    if not files:
        return None
    return datetime.fromtimestamp(max(f.stat().st_mtime for f in files), tz=timezone.utc)

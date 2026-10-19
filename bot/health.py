from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAIL_ALERT_EVERY = timedelta(hours=1)
FEED_ALERT_AFTER = 3  # failed polls in a row before officers are told about a feed


@dataclass
class FeedStatus:
    last_ok: datetime | None = None
    last_posted: int = 0          # items posted by the last successful poll
    last_error: str | None = None
    failures: int = 0             # failed polls in a row
    alerted: bool = False         # officers were told about the current outage


@dataclass
class Health:
    """What the bot remembers about itself while it runs (nothing here needs to survive a restart)."""

    last_poll: datetime | None = None
    last_mail_alert: datetime | None = None
    feeds: dict[str, FeedStatus] = field(default_factory=dict)

    def should_alert_mail(self, now: datetime) -> bool:
        """True at most once per hour, and records that an alert is being sent."""
        if self.last_mail_alert is not None and now - self.last_mail_alert < MAIL_ALERT_EVERY:
            return False
        self.last_mail_alert = now
        return True

    def feed_ok(self, feed_id: str, posted: int, now: datetime) -> bool:
        """Record a successful poll. True if officers were told the feed was failing, so they should hear it is back."""
        status = self.feeds.setdefault(feed_id, FeedStatus())
        recovered = status.alerted
        status.last_ok, status.last_posted = now, posted
        status.last_error, status.failures, status.alerted = None, 0, False
        return recovered

    def feed_failed(self, feed_id: str, error: str) -> bool:
        """Record a failed poll. True once per outage, when the feed has failed FEED_ALERT_AFTER polls in a row."""
        status = self.feeds.setdefault(feed_id, FeedStatus())
        status.last_error, status.failures = error[:200], status.failures + 1
        if status.failures >= FEED_ALERT_AFTER and not status.alerted:
            status.alerted = True
            return True
        return False


def last_backup_time(backup_dir: Path) -> datetime | None:
    """When the newest backup file was written (read from disk, so it survives restarts)."""
    files = list(Path(backup_dir).glob("bot-*.sqlite3"))
    if not files:
        return None
    return datetime.fromtimestamp(max(f.stat().st_mtime for f in files), tz=timezone.utc)

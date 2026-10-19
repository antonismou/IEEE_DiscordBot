import os
from datetime import datetime, timedelta, timezone

from bot.health import Health, last_backup_time
from bot.mailer import MailError, make_gmail_checker

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def test_mail_alert_fires_once_per_hour():
    health = Health()
    assert health.should_alert_mail(T0)
    assert not health.should_alert_mail(T0 + timedelta(minutes=59))
    assert health.should_alert_mail(T0 + timedelta(minutes=61))


def test_last_backup_comes_from_the_newest_file(tmp_path):
    assert last_backup_time(tmp_path) is None
    old, new = tmp_path / "bot-20261001.sqlite3", tmp_path / "bot-20261002.sqlite3"
    old.write_text("x")
    new.write_text("x")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    assert last_backup_time(tmp_path).timestamp() == 2_000_000


def test_mail_checker_logs_in_without_sending_and_reports_failure(monkeypatch):
    import smtplib

    calls = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            calls.append(("login", user))
            if password == "bad":
                raise smtplib.SMTPAuthenticationError(535, b"nope")

        def send_message(self, *a):
            calls.append(("send",))

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    make_gmail_checker("bot@gmail.com", "good")()
    assert calls == [("login", "bot@gmail.com")]
    try:
        make_gmail_checker("bot@gmail.com", "bad")()
    except MailError as exc:
        assert isinstance(exc.__cause__, smtplib.SMTPAuthenticationError)
    else:
        raise AssertionError("expected MailError")


def test_feed_alert_fires_once_per_outage_and_recovery_only_after_an_alert():
    health = Health()
    assert not health.feed_ok("s", 1, T0)                     # healthy feed: nothing to report
    assert [health.feed_failed("s", "boom") for _ in range(4)] == [False, False, True, False]
    assert health.feeds["s"].failures == 4 and health.feeds["s"].last_error == "boom"
    assert health.feed_ok("s", 0, T0)                         # it was alerted, so report the recovery
    assert not health.feed_failed("s", "x" * 500)             # a new outage counts from zero
    assert len(health.feeds["s"].last_error) == 200

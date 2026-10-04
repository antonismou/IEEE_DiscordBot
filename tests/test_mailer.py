import smtplib

import pytest

from bot import mailer
from bot.mailer import MailError, build_message, make_gmail_sender


class FakeSMTP:
    instances = []
    fail_with = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.logged_in = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)


def test_build_message_contains_code_and_headers():
    msg = build_message("branch@gmail.com", "a@tuc.gr", "123456", 10)
    assert msg["To"] == "a@tuc.gr" and "branch@gmail.com" in msg["From"]
    body = msg.get_content()
    assert "123456" in body and "10 minutes" in body


def test_send_logs_in_and_sends():
    send = make_gmail_sender("branch@gmail.com", "app-pass")
    send("a@tuc.gr", "123456", 10)
    (smtp,) = FakeSMTP.instances
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.gmail.com", 465, 20)
    assert smtp.logged_in == ("branch@gmail.com", "app-pass")
    assert smtp.sent[0]["To"] == "a@tuc.gr"


def test_smtp_errors_become_mail_error():
    FakeSMTP.fail_with = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    with pytest.raises(MailError):
        make_gmail_sender("a@gmail.com", "x")("a@tuc.gr", "123456", 10)


def test_network_errors_become_mail_error():
    FakeSMTP.fail_with = OSError("network down")
    with pytest.raises(MailError):
        make_gmail_sender("a@gmail.com", "x")("a@tuc.gr", "123456", 10)


def test_header_injection_never_reaches_smtp():
    send = make_gmail_sender("a@gmail.com", "x")
    with pytest.raises(MailError):
        send("a@tuc.gr\nBcc: victim@example.com", "123456", 10)
    assert all(not smtp.sent for smtp in FakeSMTP.instances)

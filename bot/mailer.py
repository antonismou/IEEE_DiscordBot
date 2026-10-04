from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable


class MailError(Exception):
    pass


def build_message(sender: str, to: str, code: str, ttl_minutes: int) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"IEEE Student Branch TUC <{sender}>"
    msg["To"] = to  # raises ValueError on a newline, which make_gmail_sender turns into MailError
    msg["Subject"] = "Your IEEE TUC Discord verification code"
    msg.set_content(
        f"Your verification code is: {code}\n\n"
        f"It expires in {ttl_minutes} minutes. If you did not request this, you can ignore this email.\n"
    )
    return msg


def make_gmail_sender(
    address: str, app_password: str, *, host: str = "smtp.gmail.com", port: int = 465
) -> Callable[[str, str, int], None]:
    def send(to: str, code: str, ttl_minutes: int) -> None:
        try:
            message = build_message(address, to, code, ttl_minutes)
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as smtp:
                smtp.login(address, app_password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            raise MailError(str(exc)) from exc

    return send

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from bot.config import Settings
from bot.db import connect
from bot.feeds.store import CustomFeedRepo, SeenStore
from bot.mailer import make_gmail_sender
from bot.members import MemberRepo
from bot.verification import VerificationService


@dataclass
class AppContext:
    settings: Settings
    conn: sqlite3.Connection
    members: MemberRepo
    verification: VerificationService
    send_code: Callable[[str, str, int], None]
    seen: SeenStore
    custom_feeds: CustomFeedRepo


def build_app(settings: Settings) -> AppContext:
    conn = connect(settings.db_path)
    members = MemberRepo(conn)
    return AppContext(
        settings=settings,
        conn=conn,
        members=members,
        verification=VerificationService(conn, members),
        send_code=make_gmail_sender(settings.gmail_address, settings.gmail_app_password),
        seen=SeenStore(conn),
        custom_feeds=CustomFeedRepo(conn),
    )

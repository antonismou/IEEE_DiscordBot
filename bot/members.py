from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from bot.db import to_iso


class EmailAlreadyUsed(Exception):
    pass


@dataclass(frozen=True)
class Member:
    discord_id: int
    email: str | None
    full_name: str
    method: str
    verified_by: int | None
    note: str | None
    consent_at: str | None
    verified_at: str


_COLUMNS = "discord_id, email, full_name, method, verified_by, note, consent_at, verified_at"


def _member(row: sqlite3.Row) -> Member:
    return Member(**{key: row[key] for key in row.keys()})


class MemberRepo:
    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    def get(self, discord_id: int) -> Member | None:
        row = self._c.execute(f"SELECT {_COLUMNS} FROM members WHERE discord_id = ?", (discord_id,)).fetchone()
        return _member(row) if row else None

    def get_by_email(self, email: str) -> Member | None:
        row = self._c.execute(f"SELECT {_COLUMNS} FROM members WHERE email = ?", (email,)).fetchone()
        return _member(row) if row else None

    def all(self) -> list[Member]:
        rows = self._c.execute(f"SELECT {_COLUMNS} FROM members ORDER BY verified_at, discord_id").fetchall()
        return [_member(row) for row in rows]

    def add_email_member(
        self, *, discord_id: int, email: str, full_name: str, consent_at: datetime, verified_at: datetime
    ) -> Member:
        try:
            with self._c:
                self._c.execute(
                    """
                    INSERT INTO members (discord_id, email, full_name, method, verified_by, note, consent_at, verified_at)
                    VALUES (?, ?, ?, 'email', NULL, NULL, ?, ?)
                    ON CONFLICT(discord_id) DO UPDATE SET
                        email = excluded.email, full_name = excluded.full_name, method = 'email',
                        verified_by = NULL, note = NULL,
                        consent_at = excluded.consent_at, verified_at = excluded.verified_at
                    """,
                    (discord_id, email, full_name, to_iso(consent_at), to_iso(verified_at)),
                )
        except sqlite3.IntegrityError:
            raise EmailAlreadyUsed(email) from None
        return self.get(discord_id)

    def add_manual_member(
        self, *, discord_id: int, full_name: str, verified_by: int, note: str, verified_at: datetime
    ) -> Member:
        with self._c:
            self._c.execute(
                """
                INSERT INTO members (discord_id, email, full_name, method, verified_by, note, consent_at, verified_at)
                VALUES (?, NULL, ?, 'manual', ?, ?, NULL, ?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    email = NULL, full_name = excluded.full_name, method = 'manual',
                    verified_by = excluded.verified_by, note = excluded.note,
                    consent_at = NULL, verified_at = excluded.verified_at
                """,
                (discord_id, full_name, verified_by, note, to_iso(verified_at)),
            )
        return self.get(discord_id)

    def delete(self, discord_id: int) -> bool:
        with self._c:
            cursor = self._c.execute("DELETE FROM members WHERE discord_id = ?", (discord_id,))
        return cursor.rowcount > 0


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def members_to_csv(members: list[Member]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["discord_id", "email", "full_name", "method", "verified_by", "note", "consent_at", "verified_at"])
    for m in members:
        writer.writerow([_csv_safe(v) for v in (
            m.discord_id, m.email, m.full_name, m.method, m.verified_by, m.note, m.consent_at, m.verified_at
        )])
    return out.getvalue()

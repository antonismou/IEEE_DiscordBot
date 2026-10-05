from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Callable

from bot.db import from_iso, to_iso, utcnow

PIN_LENGTH = 6
_ITERATIONS = 100_000


def is_valid_pin(pin: str) -> bool:
    return len(pin) == PIN_LENGTH and pin.isascii() and pin.isdigit()


def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), _ITERATIONS).hex()
    return f"{salt}${digest}"


def pin_matches(pin: str, stored: str) -> bool:
    salt, _, digest = stored.partition("$")
    candidate = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), _ITERATIONS).hex()
    return hmac.compare_digest(candidate, digest)


class PinLimiter:
    """Locks a member out of all PIN checks after too many wrong submissions. Kept in the database so a restart
    does not reset it."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Callable[[], datetime] = utcnow,
        max_failures: int = 5,
        lockout_seconds: int = 900,
    ):
        self._c = conn
        self._clock = clock
        self._max = max_failures
        self._lockout = lockout_seconds

    def locked_for(self, discord_id: int) -> int:
        """Seconds the member must still wait, or 0."""
        row = self._c.execute(
            "SELECT locked_until FROM pin_attempts WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        if row is None or row["locked_until"] is None:
            return 0
        remaining = (from_iso(row["locked_until"]) - self._clock()).total_seconds()
        return max(0, int(remaining) + (1 if remaining % 1 else 0))

    def record_failure(self, discord_id: int) -> int:
        """Count a wrong submission. Returns the attempts left, 0 meaning the member is now locked out."""
        row = self._c.execute("SELECT failures FROM pin_attempts WHERE discord_id = ?", (discord_id,)).fetchone()
        failures = (row["failures"] if row else 0) + 1
        locked_until = None
        if failures >= self._max:
            failures, locked_until = 0, to_iso(self._clock() + timedelta(seconds=self._lockout))
        with self._c:
            self._c.execute(
                "INSERT INTO pin_attempts (discord_id, failures, locked_until) VALUES (?, ?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET failures = excluded.failures, "
                "locked_until = excluded.locked_until",
                (discord_id, failures, locked_until),
            )
        return 0 if locked_until else self._max - failures

    def record_success(self, discord_id: int) -> None:
        with self._c:
            self._c.execute("DELETE FROM pin_attempts WHERE discord_id = ?", (discord_id,))

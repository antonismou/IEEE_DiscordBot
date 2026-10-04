from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from bot.db import from_iso, to_iso, utcnow
from bot.members import EmailAlreadyUsed, Member, MemberRepo
from bot.validation import clean_text, is_tuc_email, normalize_email


class VerificationError(Exception):
    """The message is safe to show to the member."""


@dataclass(frozen=True)
class PendingVerification:
    email: str
    code: str
    ttl_minutes: int


def _default_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def _email_key(email: str) -> str:
    """Rate-limit key for a recipient; stored instead of the address itself."""
    return hashlib.sha256(email.encode()).hexdigest()


def _hash(discord_id: int, code: str) -> str:
    return hashlib.sha256(f"{discord_id}:{code}".encode()).hexdigest()


class VerificationService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        members: MemberRepo,
        *,
        clock: Callable[[], datetime] = utcnow,
        code_factory: Callable[[], str] | None = None,
        code_ttl_seconds: int = 600,
        max_attempts: int = 5,
        resend_cooldown_seconds: int = 60,
        hourly_send_cap: int = 40,
        per_user_hourly_cap: int = 5,
        per_email_hourly_cap: int = 3,
    ):
        self._c = conn
        self._members = members
        self._clock = clock
        self._code_factory = code_factory or _default_code
        self._ttl = code_ttl_seconds
        self._max_attempts = max_attempts
        self._cooldown = resend_cooldown_seconds
        self._hourly_cap = hourly_send_cap
        self._per_user_cap = per_user_hourly_cap
        self._per_email_cap = per_email_hourly_cap

    def _count_sends(self, since: datetime, *, discord_id: int | None = None, email_hash: str | None = None) -> int:
        query, args = "SELECT COUNT(*) FROM send_log WHERE sent_at >= ?", [to_iso(since)]
        if discord_id is not None:
            query += " AND discord_id = ?"
            args.append(discord_id)
        if email_hash is not None:
            query += " AND email_hash = ?"
            args.append(email_hash)
        return self._c.execute(query, args).fetchone()[0]

    def begin(self, discord_id: int, raw_name: str, raw_email: str) -> PendingVerification:
        try:
            name = clean_text(raw_name, min_len=2, max_len=100, label="Name")
        except ValueError as exc:
            raise VerificationError(str(exc)) from None
        email = normalize_email(raw_email)
        if not is_tuc_email(email):
            raise VerificationError(
                "Only @tuc.gr addresses (or a tuc.gr subdomain such as @isc.tuc.gr) are accepted."
            )
        if self._members.get(discord_id):
            raise VerificationError("You are already verified.")
        if self._members.get_by_email(email):
            raise VerificationError("That email is already linked to another Discord account. Ask an officer for help.")

        # All limits read send_log, which abort(), expiry and wrong codes never touch: deleting the pending
        # row (e.g. via /forget-me) must not reset them.
        now = self._clock()
        hour_ago = now - timedelta(hours=1)
        email_hash = _email_key(email)
        last = self._c.execute("SELECT MAX(sent_at) FROM send_log WHERE discord_id = ?", (discord_id,)).fetchone()[0]
        if last:
            elapsed = (now - from_iso(last)).total_seconds()
            if elapsed < self._cooldown:
                wait = math.ceil(self._cooldown - elapsed)
                raise VerificationError(f"Please wait {wait} seconds before requesting another code.")
        if self._count_sends(hour_ago, discord_id=discord_id) >= self._per_user_cap:
            raise VerificationError("You have requested too many codes. Please try again in an hour.")
        if self._count_sends(hour_ago, email_hash=email_hash) >= self._per_email_cap:
            raise VerificationError("That address has received too many codes recently. Please try again later.")
        if self._count_sends(hour_ago) >= self._hourly_cap:
            raise VerificationError("Too many verification emails were sent recently. Please try again later.")

        code = self._code_factory()
        with self._c:
            self._c.execute(
                "INSERT OR REPLACE INTO pending_codes "
                "(discord_id, email, full_name, code_hash, expires_at, attempts, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (discord_id, email, name, _hash(discord_id, code),
                 to_iso(now + timedelta(seconds=self._ttl)), to_iso(now)),
            )
            self._c.execute(
                "INSERT INTO send_log (discord_id, email_hash, sent_at) VALUES (?, ?, ?)",
                (discord_id, email_hash, to_iso(now)),
            )
            self._c.execute("DELETE FROM send_log WHERE sent_at < ?", (to_iso(now - timedelta(hours=24)),))
        return PendingVerification(email=email, code=code, ttl_minutes=self._ttl // 60)

    def abort(self, discord_id: int) -> None:
        """Drop the pending code. The send still counts towards the rate limits."""
        self._delete_pending(discord_id)

    def send_failed(self, discord_id: int) -> None:
        """The email never left: drop the pending code and give the send back so the user can retry now."""
        with self._c:
            self._c.execute("DELETE FROM pending_codes WHERE discord_id = ?", (discord_id,))
            self._c.execute(
                "DELETE FROM send_log WHERE rowid = (SELECT rowid FROM send_log WHERE discord_id = ? "
                "ORDER BY sent_at DESC, rowid DESC LIMIT 1)",
                (discord_id,),
            )

    def confirm(self, discord_id: int, raw_code: str) -> Member:
        row = self._c.execute("SELECT * FROM pending_codes WHERE discord_id = ?", (discord_id,)).fetchone()
        if row is None:
            raise VerificationError("No verification in progress. Press **Verify** first.")
        now = self._clock()
        if now >= from_iso(row["expires_at"]):
            self._delete_pending(discord_id)
            raise VerificationError("That code has expired. Press **Verify** to get a new one.")

        code = "".join(raw_code.split())
        if not hmac.compare_digest(_hash(discord_id, code), row["code_hash"]):
            attempts = row["attempts"] + 1
            if attempts >= self._max_attempts:
                self._delete_pending(discord_id)
                raise VerificationError("Too many wrong attempts. Press **Verify** to start again.")
            with self._c:
                self._c.execute("UPDATE pending_codes SET attempts = ? WHERE discord_id = ?", (attempts, discord_id))
            left = self._max_attempts - attempts
            raise VerificationError(f"Wrong code. {left} attempt(s) left.")

        try:
            member = self._members.add_email_member(
                discord_id=discord_id,
                email=row["email"],
                full_name=row["full_name"],
                consent_at=from_iso(row["created_at"]),
                verified_at=now,
            )
        except EmailAlreadyUsed:
            self._delete_pending(discord_id)
            raise VerificationError(
                "That email is already linked to another Discord account. Ask an officer for help."
            ) from None
        self._delete_pending(discord_id)
        return member

    def _delete_pending(self, discord_id: int) -> None:
        with self._c:
            self._c.execute("DELETE FROM pending_codes WHERE discord_id = ?", (discord_id,))

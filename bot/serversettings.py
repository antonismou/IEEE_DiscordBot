from __future__ import annotations

import sqlite3

from bot.pins import hash_pin, pin_matches


class ServerSettings:
    """The one Discord server the bot serves, and its two roles. Set with /setup roles, kept in the database."""

    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    def _get(self, key: str) -> int | None:
        row = self._c.execute("SELECT value FROM server_settings WHERE key = ?", (key,)).fetchone()
        return int(row["value"]) if row else None

    @property
    def guild_id(self) -> int | None:
        return self._get("guild_id")

    @property
    def verified_role_id(self) -> int | None:
        return self._get("verified_role_id")

    @property
    def officer_role_id(self) -> int | None:
        return self._get("officer_role_id")

    @property
    def configured(self) -> bool:
        return self.guild_id is not None

    @property
    def branch_role_ids(self) -> dict[str, int]:
        """Branch key -> role id, for the branches that are set (see bot.branches.BRANCHES)."""
        ids = {}
        for key in ("main", "cs", "ias", "quantum"):
            value = self._get(f"branch_{key}")
            if value is not None:
                ids[key] = value
        return ids

    @property
    def branch_descriptions(self) -> dict[str, str]:
        rows = self._c.execute("SELECT key, value FROM server_settings WHERE key LIKE 'branch_desc_%'").fetchall()
        return {row["key"].removeprefix("branch_desc_"): row["value"] for row in rows}

    def set_branch_description(self, key: str, text: str | None) -> None:
        with self._c:
            if text:
                self._c.execute(
                    "INSERT INTO server_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (f"branch_desc_{key}", text),
                )
            else:
                self._c.execute("DELETE FROM server_settings WHERE key = ?", (f"branch_desc_{key}",))

    def branch_pin_keys(self) -> set[str]:
        """The branches that need a PIN."""
        rows = self._c.execute("SELECT key FROM server_settings WHERE key LIKE 'branch_pin_%'").fetchall()
        return {row["key"].removeprefix("branch_pin_") for row in rows}

    def set_branch_pin(self, key: str, pin: str | None) -> None:
        """Store a salted hash of the PIN (never the PIN itself); None removes it."""
        with self._c:
            if pin:
                self._c.execute(
                    "INSERT INTO server_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (f"branch_pin_{key}", hash_pin(pin)),
                )
            else:
                self._c.execute("DELETE FROM server_settings WHERE key = ?", (f"branch_pin_{key}",))

    def check_branch_pin(self, key: str, pin: str) -> bool:
        row = self._c.execute("SELECT value FROM server_settings WHERE key = ?", (f"branch_pin_{key}",)).fetchone()
        return row is not None and pin_matches(pin, row["value"])

    def configure_branches(self, role_ids: dict[str, int]) -> None:
        with self._c:
            self._c.executemany(
                "INSERT INTO server_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(f"branch_{key}", str(rid)) for key, rid in role_ids.items()],
            )

    def configure(self, *, guild_id: int, verified_role_id: int, officer_role_id: int) -> None:
        with self._c:
            self._c.executemany(
                "INSERT INTO server_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [
                    ("guild_id", str(guild_id)),
                    ("verified_role_id", str(verified_role_id)),
                    ("officer_role_id", str(officer_role_id)),
                ],
            )

from __future__ import annotations

import sqlite3


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

from __future__ import annotations

import re
import sqlite3

from bot.pins import hash_pin, pin_matches

MAX_BRANCHES = 25  # Discord's limit for the options of one dropdown
LABEL_MAX = 80
LEGACY_LABELS = {"main": "Main Branch", "cs": "CS", "ias": "IAS", "quantum": "Quantum"}  # set before branches were dynamic


def branch_key(name: str) -> str:
    """Stable identifier for a branch name: lowercase letters and digits joined by hyphens (no underscores, so
    it can never be confused with the branch_pin_/branch_desc_/branch_label_ setting names)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-")


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
        """Branch key -> role id, in the order the branches were added."""
        rows = self._c.execute(
            "SELECT key, value FROM server_settings WHERE key LIKE 'branch\\_%' ESCAPE '\\' ORDER BY rowid"
        ).fetchall()
        # pin/description/label settings are branch_<kind>_<key>; role rows have no second underscore
        return {
            row["key"].removeprefix("branch_"): int(row["value"])
            for row in rows if "_" not in row["key"].removeprefix("branch_")
        }

    @property
    def branch_labels(self) -> dict[str, str]:
        """Branch key -> display name (branches set before names were stored fall back to their original name)."""
        rows = self._c.execute("SELECT key, value FROM server_settings WHERE key LIKE 'branch\\_label\\_%' ESCAPE '\\'").fetchall()
        labels = {row["key"].removeprefix("branch_label_"): row["value"] for row in rows}
        return {key: labels.get(key) or LEGACY_LABELS.get(key, key) for key in self.branch_role_ids}

    def add_branch(self, key: str, label: str, role_id: int) -> None:
        """Add a branch, or change the name and role of an existing one."""
        with self._c:
            self._c.executemany(
                "INSERT INTO server_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(f"branch_{key}", str(role_id)), (f"branch_label_{key}", label)],
            )

    def remove_branch(self, key: str) -> None:
        """Forget a branch and its PIN and description. The Discord role and who holds it are left alone."""
        with self._c:
            self._c.execute(
                "DELETE FROM server_settings WHERE key IN (?, ?, ?, ?)",
                (f"branch_{key}", f"branch_label_{key}", f"branch_pin_{key}", f"branch_desc_{key}"),
            )

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

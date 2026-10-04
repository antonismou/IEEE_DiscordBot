from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

REQUIRED_ENV = ("DISCORD_TOKEN", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class FeedConfig:
    id: str
    title: str
    url: str
    channel_id: int


@dataclass(frozen=True)
class Settings:
    discord_token: str
    gmail_address: str
    gmail_app_password: str
    guild_id: int
    verified_role_id: int
    officer_role_id: int
    db_path: Path
    backup_dir: Path
    poll_interval_minutes: int
    max_items_per_poll: int
    feeds: tuple[FeedConfig, ...]
    officer_log_channel_id: int | None = None


def load_settings(config_path: Path, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        raise ConfigError(f"Missing environment variables: {', '.join(missing)}")
    try:
        raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {config_path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid config file: {exc}") from None

    channels = raw.get("channels", {})
    feeds: list[FeedConfig] = []
    seen_ids: set[str] = set()
    for entry in raw.get("feeds", []):
        try:
            feed_id, title, url, channel = entry["id"], entry["title"], entry["url"], entry["channel"]
        except KeyError as exc:
            raise ConfigError(f"Feed entry is missing key {exc}: {entry}") from None
        if channel not in channels:
            raise ConfigError(f"Feed '{feed_id}' uses unknown channel '{channel}'")
        if feed_id in seen_ids:
            raise ConfigError(f"Duplicate feed id '{feed_id}'")
        seen_ids.add(feed_id)
        feeds.append(FeedConfig(feed_id, title, url, int(channels[channel])))

    try:
        return Settings(
            discord_token=env["DISCORD_TOKEN"],
            gmail_address=env["GMAIL_ADDRESS"],
            gmail_app_password=env["GMAIL_APP_PASSWORD"],
            guild_id=int(raw["guild_id"]),
            verified_role_id=int(raw["verified_role_id"]),
            officer_role_id=int(raw["officer_role_id"]),
            db_path=Path(raw.get("db_path", "data/bot.sqlite3")),
            backup_dir=Path(raw.get("backup_dir", "data/backups")),
            poll_interval_minutes=int(raw.get("poll_interval_minutes", 30)),
            max_items_per_poll=int(raw.get("max_items_per_poll", 10)),
            feeds=tuple(feeds),
            officer_log_channel_id=int(raw.get("officer_log_channel_id", 0)) or None,
        )
    except KeyError as exc:
        raise ConfigError(f"Missing config key: {exc}") from None

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from bot.channels import OFFICER_LOG

REQUIRED_ENV = ("DISCORD_TOKEN", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class FeedConfig:
    id: str
    title: str
    url: str
    channel_id: int | None  # set for feeds added with /feed add
    topic: str | None = None  # set for feeds in config.toml; resolved through /channel set


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


def load_settings(config_path: Path, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        raise ConfigError(f"Missing environment variables: {', '.join(missing)}")
    try:
        raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {config_path}") from None
    except IsADirectoryError:
        raise ConfigError(
            f"{config_path} is a directory, not a file. With Docker this happens when config.toml did not exist "
            "before `docker compose up`: remove the empty folder, run `cp config.example.toml config.toml`, "
            "fill it in, and start again."
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid config file: {exc}") from None

    feeds: list[FeedConfig] = []
    seen_ids: set[str] = set()
    for entry in raw.get("feeds", []):
        try:
            feed_id, title, url, topic = entry["id"], entry["title"], entry["url"], entry["channel"]
        except KeyError as exc:
            raise ConfigError(f"Feed entry is missing key {exc}: {entry}") from None
        if topic == OFFICER_LOG:
            raise ConfigError(f"Feed '{feed_id}' cannot use '{OFFICER_LOG}': that topic is reserved for the officers' log")
        if feed_id in seen_ids:
            raise ConfigError(f"Duplicate feed id '{feed_id}'")
        seen_ids.add(feed_id)
        feeds.append(FeedConfig(feed_id, title, url, None, topic=topic))

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
        )
    except KeyError as exc:
        raise ConfigError(f"Missing config key: {exc}") from None

"""Run on the server: python -m scripts.check_feeds [config.toml]

Fetches every configured feed and prints the HTTP result, so a block by IEEE shows up before the bot runs.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from bot.config import load_settings
from bot.feeds.parse import FeedError, fetch_feed, parse_feed

DUMMY_ENV = {"DISCORD_TOKEN": "x", "GMAIL_ADDRESS": "x", "GMAIL_APP_PASSWORD": "x"}


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
    settings = load_settings(path, DUMMY_ENV)
    failures = 0
    for feed in settings.feeds:
        try:
            items = parse_feed(fetch_feed(feed.url))
            print(f"OK    {feed.id:<28} {len(items):>3} items  {feed.title}")
        except FeedError as exc:
            failures += 1
            print(f"FAIL  {feed.id:<28} {exc}")
        time.sleep(1)  # be polite to the server
    print(f"\n{len(settings.feeds) - failures}/{len(settings.feeds)} feeds reachable")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

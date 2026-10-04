from pathlib import Path

import pytest

from bot.config import ConfigError, load_settings

ENV = {"DISCORD_TOKEN": "t", "GMAIL_ADDRESS": "a@gmail.com", "GMAIL_APP_PASSWORD": "p"}

GOOD = """
[[feeds]]
id = "tpami"
title = "Pattern Analysis and Machine Intelligence"
url = "https://ieeexplore.ieee.org/rss/TOC34.XML"
channel = "ai-ml"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_values_and_defaults(tmp_path):
    s = load_settings(write(tmp_path, GOOD), ENV)
    assert s.discord_token == "t" and s.gmail_address == "a@gmail.com"
    assert s.poll_interval_minutes == 30 and s.max_items_per_poll == 10
    assert s.db_path == Path("data/bot.sqlite3")
    assert s.backup_dir == Path("data/backups")
    (feed,) = s.feeds
    assert (feed.id, feed.topic, feed.channel_id) == ("tpami", "ai-ml", None)


def test_missing_env_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="GMAIL_APP_PASSWORD"):
        load_settings(write(tmp_path, GOOD), {"DISCORD_TOKEN": "t", "GMAIL_ADDRESS": "a"})


def test_duplicate_feed_id(tmp_path):
    text = GOOD + '\n[[feeds]]\nid = "tpami"\ntitle = "x"\nurl = "https://x"\nchannel = "ai-ml"\n'
    with pytest.raises(ConfigError, match="Duplicate feed id"):
        load_settings(write(tmp_path, text), ENV)


def test_missing_file_and_missing_key(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.toml", ENV)
    with pytest.raises(ConfigError, match="Invalid config"):
        load_settings(write(tmp_path, "this is = not [valid toml"), ENV)


def test_config_path_that_is_a_directory_explains_the_docker_pitfall(tmp_path):
    (tmp_path / "config.toml").mkdir()
    with pytest.raises(ConfigError, match="directory"):
        load_settings(tmp_path / "config.toml", ENV)


def test_feed_must_name_a_topic(tmp_path):
    with pytest.raises(ConfigError, match="channel"):
        load_settings(write(tmp_path, GOOD.replace('channel = "ai-ml"\n', "")), ENV)


def test_officer_log_is_a_reserved_topic(tmp_path):
    with pytest.raises(ConfigError, match="reserved"):
        load_settings(write(tmp_path, GOOD.replace('channel = "ai-ml"', 'channel = "officer-log"')), ENV)


def test_old_id_keys_are_ignored_not_errors(tmp_path):
    old = "guild_id = 1\nverified_role_id = 2\nofficer_role_id = 3\n" + GOOD
    assert load_settings(write(tmp_path, old), ENV).feeds[0].id == "tpami"

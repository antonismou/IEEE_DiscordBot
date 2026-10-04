from pathlib import Path

import pytest

from bot.config import ConfigError, load_settings

ENV = {"DISCORD_TOKEN": "t", "GMAIL_ADDRESS": "a@gmail.com", "GMAIL_APP_PASSWORD": "p"}

GOOD = """
guild_id = 1
verified_role_id = 2
officer_role_id = 3

[channels]
ai-ml = 10

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
    assert (s.guild_id, s.verified_role_id, s.officer_role_id) == (1, 2, 3)
    assert s.discord_token == "t" and s.gmail_address == "a@gmail.com"
    assert s.poll_interval_minutes == 30 and s.max_items_per_poll == 10
    assert s.db_path == Path("data/bot.sqlite3")
    assert s.backup_dir == Path("data/backups")
    (feed,) = s.feeds
    assert (feed.id, feed.channel_id) == ("tpami", 10)


def test_officer_log_channel_is_optional(tmp_path):
    assert load_settings(write(tmp_path, GOOD), ENV).officer_log_channel_id is None
    with_channel = GOOD.replace("officer_role_id = 3", "officer_role_id = 3\nofficer_log_channel_id = 55")
    assert load_settings(write(tmp_path, with_channel), ENV).officer_log_channel_id == 55
    zero = GOOD.replace("officer_role_id = 3", "officer_role_id = 3\nofficer_log_channel_id = 0")
    assert load_settings(write(tmp_path, zero), ENV).officer_log_channel_id is None


def test_missing_env_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="GMAIL_APP_PASSWORD"):
        load_settings(write(tmp_path, GOOD), {"DISCORD_TOKEN": "t", "GMAIL_ADDRESS": "a"})


def test_unknown_channel_name(tmp_path):
    with pytest.raises(ConfigError, match="nope"):
        load_settings(write(tmp_path, GOOD.replace('channel = "ai-ml"', 'channel = "nope"')), ENV)


def test_duplicate_feed_id(tmp_path):
    text = GOOD + '\n[[feeds]]\nid = "tpami"\ntitle = "x"\nurl = "https://x"\nchannel = "ai-ml"\n'
    with pytest.raises(ConfigError, match="Duplicate feed id"):
        load_settings(write(tmp_path, text), ENV)


def test_missing_file_and_missing_key(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.toml", ENV)
    with pytest.raises(ConfigError, match="guild_id"):
        load_settings(write(tmp_path, GOOD.replace("guild_id = 1", "")), ENV)


def test_config_path_that_is_a_directory_explains_the_docker_pitfall(tmp_path):
    (tmp_path / "config.toml").mkdir()
    with pytest.raises(ConfigError, match="directory"):
        load_settings(tmp_path / "config.toml", ENV)

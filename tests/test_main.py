import pytest

from bot.main import main


def set_env(monkeypatch, config_path):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("GMAIL_ADDRESS", "a@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "p")
    monkeypatch.setenv("BOT_CONFIG", str(config_path))


def test_bad_config_exits_with_a_readable_message_not_a_traceback(tmp_path, monkeypatch):
    (tmp_path / "config.toml").mkdir()          # what Docker creates when the file did not exist
    set_env(monkeypatch, tmp_path / "config.toml")
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert "directory" in str(exit_info.value) and "Traceback" not in str(exit_info.value)


def test_unwritable_data_directory_exits_with_a_readable_message(tmp_path, monkeypatch):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    config = tmp_path / "config.toml"
    config.write_text(
        f'guild_id = 1\nverified_role_id = 2\nofficer_role_id = 3\ndb_path = "{locked}/bot.sqlite3"\n'
    )
    set_env(monkeypatch, config)
    try:
        with pytest.raises(SystemExit) as exit_info:
            main()
    finally:
        locked.chmod(0o700)
    assert "chown" in str(exit_info.value)

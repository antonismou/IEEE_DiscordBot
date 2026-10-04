from pathlib import Path

from bot.app import build_app
from bot.config import Settings


def test_build_app_wires_services(tmp_path):
    settings = Settings(
        discord_token="t", gmail_address="a@gmail.com", gmail_app_password="p",
        db_path=tmp_path / "bot.sqlite3", backup_dir=tmp_path / "backups",
        poll_interval_minutes=30, max_items_per_poll=10, feeds=(),
    )
    app = build_app(settings)
    assert app.settings is settings
    assert callable(app.send_code)
    assert app.members.all() == []
    assert app.custom_feeds.all() == []
    app.conn.close()

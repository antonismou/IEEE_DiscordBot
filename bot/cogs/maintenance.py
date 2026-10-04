from __future__ import annotations

import logging
from datetime import time, timezone

from discord.ext import commands, tasks

from bot.backup import backup_database

log = logging.getLogger(__name__)


class MaintenanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.nightly_backup.start()

    async def cog_unload(self) -> None:
        self.nightly_backup.cancel()

    @tasks.loop(time=time(hour=3, minute=0, tzinfo=timezone.utc))
    async def nightly_backup(self) -> None:
        app = self.bot.app
        try:
            path = backup_database(app.conn, app.settings.backup_dir)
            log.info("Database backed up to %s", path)
        except Exception:
            log.exception("Database backup failed")


async def setup(bot) -> None:
    await bot.add_cog(MaintenanceCog(bot))

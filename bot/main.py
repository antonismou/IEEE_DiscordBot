from __future__ import annotations

import logging
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from bot.app import AppContext, build_app
from bot.checks import handle_app_command_error
from bot.config import ConfigError, load_settings
from bot.db import DataDirError
from bot.gate import GuildLockedTree

log = logging.getLogger(__name__)

EXTENSIONS = (
    "bot.cogs.setup", "bot.cogs.verify", "bot.cogs.officer", "bot.cogs.feeds", "bot.cogs.channels",
    "bot.cogs.maintenance", "bot.cogs.branches",
)


async def sync_commands_to(tree, guild) -> None:
    """Guild-scoped commands appear at once (global ones can take up to an hour)."""
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)


class IEEEBot(commands.Bot):
    def __init__(self, app: AppContext):
        super().__init__(
            command_prefix=commands.when_mentioned, intents=discord.Intents.default(), tree_cls=GuildLockedTree
        )
        self.app = app
        self.tree.on_error = handle_app_command_error
        self._synced_on_ready = False

    async def setup_hook(self) -> None:
        for extension in EXTENSIONS:
            await self.load_extension(extension)

    async def on_ready(self) -> None:
        if self._synced_on_ready:
            return
        self._synced_on_ready = True
        for guild in self.guilds:
            await sync_commands_to(self.tree, guild)
        log.info("Commands synced to %d server(s)", len(self.guilds))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await sync_commands_to(self.tree, guild)
        log.info("Joined a server; commands synced")


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings(Path(os.environ.get("BOT_CONFIG", "config.toml")))
        app = build_app(settings)
    except (ConfigError, DataDirError) as exc:
        raise SystemExit(f"Startup error: {exc}") from None  # a readable message, not a traceback
    IEEEBot(app).run(settings.discord_token)


if __name__ == "__main__":
    main()

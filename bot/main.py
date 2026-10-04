from __future__ import annotations

import logging
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from bot.app import AppContext, build_app
from bot.checks import handle_app_command_error
from bot.config import load_settings

EXTENSIONS = ("bot.cogs.verify", "bot.cogs.officer", "bot.cogs.feeds", "bot.cogs.maintenance")


class IEEEBot(commands.Bot):
    def __init__(self, app: AppContext):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.app = app
        self.tree.on_error = handle_app_command_error

    async def setup_hook(self) -> None:
        for extension in EXTENSIONS:
            await self.load_extension(extension)
        guild = discord.Object(id=self.app.settings.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(Path(os.environ.get("BOT_CONFIG", "config.toml")))
    IEEEBot(build_app(settings)).run(settings.discord_token)


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.checks import officer_only
from bot.config import FeedConfig
from bot.feeds.parse import FeedError, fetch_feed, parse_feed
from bot.feeds.service import process_feed
from bot.feeds.store import FeedExists

log = logging.getLogger(__name__)

FEED_ID_RE = re.compile(r"[a-z0-9][a-z0-9\-]{0,39}")


class FeedsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.poll.change_interval(minutes=self.app.settings.poll_interval_minutes)

    @property
    def app(self):
        return self.bot.app

    feed_group = app_commands.Group(name="feed", description="Manage RSS feeds", guild_only=True)

    async def cog_load(self) -> None:
        self.poll.start()

    async def cog_unload(self) -> None:
        self.poll.cancel()

    def all_feeds(self) -> list[FeedConfig]:
        feeds = list(self.app.settings.feeds)
        used = {feed.id for feed in feeds}
        feeds.extend(feed for feed in self.app.custom_feeds.all() if feed.id not in used)
        return feeds

    async def send(self, channel_id: int, embeds: list[discord.Embed], content: str | None) -> None:
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        await channel.send(content=content, embeds=embeds, allowed_mentions=discord.AllowedMentions.none())

    async def poll_feed(self, feed: FeedConfig) -> int:
        return await process_feed(
            feed, fetch=fetch_feed, store=self.app.seen, send=self.send,
            max_items=self.app.settings.max_items_per_poll,
        )

    @tasks.loop(minutes=30)
    async def poll(self) -> None:
        for feed in self.all_feeds():
            try:
                posted = await self.poll_feed(feed)
                log.info("Feed %s: %d new item(s) posted", feed.id, posted)
            except FeedError as exc:
                log.warning("Feed %s failed: %s", feed.id, exc)
            except Exception:
                log.exception("Feed %s crashed", feed.id)

    @poll.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    @feed_group.command(name="list", description="List the configured feeds")
    @officer_only()
    async def list_feeds(self, interaction: discord.Interaction) -> None:
        custom_ids = {feed.id for feed in self.app.custom_feeds.all()}
        lines = [
            f"`{feed.id}` {feed.title} -> <#{feed.channel_id}>" + (" (added by command)" if feed.id in custom_ids else "")
            for feed in self.all_feeds()
        ]
        await interaction.response.send_message("\n".join(lines) or "No feeds.", ephemeral=True)

    @feed_group.command(name="add", description="Add an RSS feed")
    @officer_only()
    async def add_feed(
        self, interaction: discord.Interaction, id: str, title: str, url: str, channel: discord.TextChannel
    ) -> None:
        if not FEED_ID_RE.fullmatch(id):
            await interaction.response.send_message(
                "The id must be 1-40 characters: lowercase letters, digits and `-`.", ephemeral=True
            )
            return
        if not url.startswith("https://"):
            await interaction.response.send_message("The URL must start with https://", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            content = await asyncio.to_thread(fetch_feed, url)
            parse_feed(content)
        except FeedError as exc:
            await interaction.followup.send(f"I couldn't read that feed: {exc}", ephemeral=True)
            return
        feed = FeedConfig(id, title[:100], url, channel.id)
        if id in {f.id for f in self.app.settings.feeds}:
            await interaction.followup.send("That id is used by a feed in the config file.", ephemeral=True)
            return
        try:
            self.app.custom_feeds.add(feed)
        except FeedExists:
            await interaction.followup.send("A feed with that id already exists.", ephemeral=True)
            return
        try:
            await self.poll_feed(feed)  # first run: marks current items as seen, posts nothing
        except FeedError as exc:
            log.warning("Initial poll of new feed %s failed: %s", id, exc)
        await interaction.followup.send(
            f"Added `{id}`. Existing items were skipped; new ones will appear in {channel.mention}.", ephemeral=True
        )

    @feed_group.command(name="remove", description="Remove a feed that was added with /feed add")
    @officer_only()
    async def remove_feed(self, interaction: discord.Interaction, id: str) -> None:
        if id in {f.id for f in self.app.settings.feeds}:
            await interaction.response.send_message("That feed is in config.toml; remove it there.", ephemeral=True)
            return
        removed = self.app.custom_feeds.remove(id)
        if removed:
            self.app.seen.forget(id)  # re-adding the same id later must start fresh, not burst
        await interaction.response.send_message("Removed." if removed else "No such feed.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(FeedsCog(bot))

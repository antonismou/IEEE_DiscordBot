from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.channels import OFFICER_LOG
from bot.checks import officer_only
from bot.config import FeedConfig
from bot.db import utcnow
from bot.feeds.parse import FeedError, fetch_feed, parse_feed
from bot.feeds.service import process_feed
from bot.feeds.store import FeedExists
from bot.health import FEED_ALERT_AFTER
from bot.roles import notify_officers

log = logging.getLogger(__name__)

FEED_ID_RE = re.compile(r"[a-z0-9][a-z0-9\-]{0,39}")


def split_message(lines: list[str], limit: int = 2000) -> list[str]:
    """Join lines into as few messages as fit Discord's length limit, never splitting a line."""
    chunks: list[str] = []
    for line in lines:
        line = line[:limit]
        if chunks and len(chunks[-1]) + 1 + len(line) <= limit:
            chunks[-1] += "\n" + line
        else:
            chunks.append(line)
    return chunks


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

    def _channel_for(self, feed: FeedConfig) -> int | None:
        if feed.channel_id:
            return feed.channel_id
        return self.app.channels.get(feed.topic) if feed.topic else None

    def ready_feeds(self) -> tuple[list[FeedConfig], list[FeedConfig]]:
        """(feeds with a channel resolved, feeds still waiting for /channel set)."""
        ready: list[FeedConfig] = []
        skipped: list[FeedConfig] = []
        for feed in self.all_feeds():
            channel_id = self._channel_for(feed)
            if channel_id:
                ready.append(dataclasses.replace(feed, channel_id=channel_id))
            else:
                skipped.append(feed)
        return ready, skipped

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
        await self.poll_all()

    async def poll_all(self) -> None:
        ready, skipped = self.ready_feeds()
        if skipped:
            topics = sorted({feed.topic for feed in skipped if feed.topic})
            log.warning("Feeds paused, no channel set for topic(s) %s: run /channel set", ", ".join(topics))
        for feed in ready:
            try:
                posted = await self.poll_feed(feed)
                log.info("Feed %s: %d new item(s) posted", feed.id, posted)
            except FeedError as exc:
                log.warning("Feed %s failed: %s", feed.id, exc)
                await self._feed_failed(feed, str(exc))
                continue
            except Exception as exc:
                log.exception("Feed %s crashed", feed.id)
                await self._feed_failed(feed, f"internal error ({type(exc).__name__})")
                continue
            if self.app.health.feed_ok(feed.id, posted, utcnow()):
                await self._tell_officers(f"Feed `{feed.id}` ({feed.title}) is working again.")
            await self._check_quiet(feed)
        self.app.health.last_poll = utcnow()

    async def _check_quiet(self, feed: FeedConfig) -> None:
        """Tell officers once when a feed that works has had nothing new for quiet_alert_days, and once when it has."""
        days = self.app.settings.quiet_alert_days
        last_new = self.app.seen.last_new(feed.id)
        if not days or last_new is None:
            return
        now, alerted = utcnow(), self.app.seen.quiet_alerted(feed.id)
        quiet = now - last_new >= timedelta(days=days)
        if quiet and not alerted:
            await self._tell_officers(
                f"Feed `{feed.id}` ({feed.title}) has had nothing new since <t:{int(last_new.timestamp())}:f> "
                f"({days}+ days), although it loads fine. Check that the URL still points to the live feed."
            )
        elif alerted and not quiet:
            await self._tell_officers(f"Feed `{feed.id}` ({feed.title}) has new posts again.")
        if quiet != alerted:
            self.app.seen.set_quiet_alerted(feed.id, quiet, now)

    async def _feed_failed(self, feed: FeedConfig, error: str) -> None:
        if self.app.health.feed_failed(feed.id, error):
            await self._tell_officers(
                f"Feed `{feed.id}` ({feed.title}) has failed {FEED_ALERT_AFTER} polls in a row: {error}. "
                "I will keep trying and tell you when it works again. `/feed list` shows the status of every feed."
            )

    async def _tell_officers(self, text: str) -> None:
        await notify_officers(self.bot, self.app.channels.get(OFFICER_LOG), text)

    def _status(self, feed_id: str) -> str:
        status = self.app.health.feeds.get(feed_id)
        if status is None:
            text = "not polled yet"
        elif status.failures:
            since = f", last OK <t:{int(status.last_ok.timestamp())}:R>" if status.last_ok else ""
            text = f"**failing** ({status.failures} poll(s) in a row: {status.last_error}{since})"
        else:
            text = f"OK <t:{int(status.last_ok.timestamp())}:R>, {status.last_posted} new posted"
        last_new = self.app.seen.last_new(feed_id)
        if last_new is not None:
            quiet = " **(quiet)**" if self.app.seen.quiet_alerted(feed_id) else ""
            text += f" · last new item <t:{int(last_new.timestamp())}:R>{quiet}"
        return text

    @poll.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    @feed_group.command(name="list", description="List the configured feeds")
    @officer_only()
    async def list_feeds(self, interaction: discord.Interaction) -> None:
        custom_ids = {feed.id for feed in self.app.custom_feeds.all()}
        lines = [
            f"`{feed.id}` {feed.title} -> "
            + (f"<#{self._channel_for(feed)}>" if self._channel_for(feed) else f"**no channel** (topic `{feed.topic}`: /channel set)")
            + (" (added by command)" if feed.id in custom_ids else "")
            + f"\n  {self._status(feed.id)}"
            for feed in self.all_feeds()
        ]
        chunks = split_message(lines) or ["No feeds."]
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

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
            self.app.health.feeds.pop(id, None)
        await interaction.response.send_message("Removed." if removed else "No such feed.", ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(FeedsCog(bot))

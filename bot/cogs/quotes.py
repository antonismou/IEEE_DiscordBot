from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from bot.db import utcnow
from bot.quotes.backgrounds import get_background
from bot.quotes.render import MAX_GIF_BYTES, clip_text, render_quote_gif

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
MAX_TERM_LENGTH = 50
MAX_PARALLEL_RENDERS = 2  # rendering is CPU-heavy; more at once would only slow the bot down
# spoilers (hidden, never shown in the meme) and custom emoji (shown by name: the font cannot draw them)
SPOILER_OR_EMOJI_RE = re.compile(r"\|\|.+?\|\||<a?:(\w+):\d+>", re.DOTALL)
MESSAGE_LINK_RE = re.compile(
    r"<?https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)/?>?"
)
MESSAGE_ID_RE = re.compile(r"\d{1,20}")

BAD_INPUT = "Give me a message link (right-click a message, Copy Message Link) or a message ID from this channel."
NOT_FOUND = "I couldn't find that message, or I can't read it."
OTHER_SERVER = "That message is not in this server."
FROM_A_BOT = "I don't quote bots, only people."
NO_TEXT = "That message has no text to quote."
NO_QUOTES = "There are no quotes yet. Right-click a message, Apps, **Quote it** to save the first one."
RENDER_FAILED = "Sorry, I couldn't make that meme this time. Please try again."


async def _refuse(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


def _quote_text(message: discord.Message) -> str:
    """The message as people read it: mentions as names, no markdown, spoilers hidden, custom emoji by name."""
    # clean_content shows mentions as names; drop the zero-width spaces it puts in @everyone/@here
    text = (message.clean_content or "").replace("\u200b", "")
    parts, pos = [], 0
    for match in SPOILER_OR_EMOJI_RE.finditer(text):  # markdown removal would mangle emoji names like pepe_laugh
        parts.append(discord.utils.remove_markdown(text[pos:match.start()]))
        parts.append(f":{match.group(1)}:" if match.group(1) else "[spoiler]")
        pos = match.end()
    parts.append(discord.utils.remove_markdown(text[pos:]))
    return "".join(parts).strip()


def _channel_id(message_url: str) -> tuple[int, int] | None:
    match = MESSAGE_LINK_RE.fullmatch(message_url or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _seen_by_everyone(guild, quote) -> bool:
    """Whether the quote's source channel is visible to @everyone, so replaying it leaks nothing."""
    ids = _channel_id(quote.message_url)
    if guild is None or ids is None or ids[0] != guild.id:
        return False
    channel = guild.get_channel_or_thread(ids[1])
    if channel is None or getattr(channel, "type", None) is discord.ChannelType.private_thread:
        return False
    try:
        return bool(channel.permissions_for(guild.default_role).view_channel)
    except discord.ClientException:  # a thread whose parent channel is not cached
        return False


def _replayable(guild, quote, here: int | None) -> bool:
    """Whether /randomquote may post `quote` in channel `here`: its own channel (same audience) or any public one."""
    ids = _channel_id(quote.message_url)
    if guild is not None and ids is not None and ids[0] == guild.id and ids[1] == here:
        return True
    return _seen_by_everyone(guild, quote)


def _message_year(quote) -> int:
    """The year the quoted message was sent: its ID (the last part of the link) encodes the time."""
    try:
        return discord.utils.snowflake_time(int(quote.message_url.rstrip("/").rsplit("/", 1)[-1])).year
    except (ValueError, OverflowError, OSError):
        return quote.quoted_at.year


class QuotesCog(commands.Cog):
    COOLDOWN_SECONDS = 15

    def __init__(self, bot):
        self.bot = bot
        self._clock = time.monotonic
        self._last_use: dict[int, float] = {}
        self._render_slots: asyncio.Semaphore | None = None
        self.ctx_menu = app_commands.guild_only()(
            app_commands.ContextMenu(name="Quote it", callback=self.quote_it_menu)
        )

    @property
    def app(self):
        return self.bot.app

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    # --- cooldown ------------------------------------------------------------------------------------

    def _wait_left(self, user_id: int) -> float:
        last = self._last_use.get(user_id)
        if last is None:
            return 0.0
        return max(0.0, self.COOLDOWN_SECONDS - (self._clock() - last))

    async def _with_cooldown(self, interaction: discord.Interaction, job: Callable[[], Awaitable[bool]]) -> None:
        """Run `job` if the user is not cooling down. The slot is taken at once, so a second command sent while
        the first is still fetching is refused, and given back unless `job` returns True (a meme was posted)."""
        user_id = interaction.user.id
        left = self._wait_left(user_id)
        if left > 0:
            await _refuse(interaction, f"Slow down! Try again in {max(1, round(left))} s.")
            return
        self._last_use[user_id] = self._clock()
        posted = False
        try:
            posted = await job()
        finally:
            if not posted:
                self._last_use.pop(user_id, None)

    # --- commands --------------------------------------------------------------------------------------

    @app_commands.command(name="quoteit", description="Turn a message into a chaotic meme GIF")
    @app_commands.guild_only()
    @app_commands.describe(
        message="A message link (right-click a message, Copy Message Link) or a message ID from this channel",
        gif="Optional Giphy search term for the background",
    )
    async def quoteit(self, interaction: discord.Interaction, message: str, gif: str | None = None) -> None:
        async def job() -> bool:
            target = await self._resolve(interaction, message)
            return target is not None and await self.make_card(interaction, target, gif)

        await self._with_cooldown(interaction, job)

    async def quote_it_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await self._with_cooldown(interaction, lambda: self.make_card(interaction, message, None))

    @app_commands.command(name="randomquote", description="Post a saved quote again with a new random look")
    @app_commands.guild_only()
    async def randomquote(self, interaction: discord.Interaction) -> None:
        async def job() -> bool:
            rng = random.Random()
            # replay a quote only in its own channel or from a channel everyone can see
            quote = self.app.quotes.random(
                rng, accept=lambda q: _replayable(interaction.guild, q, interaction.channel_id)
            )
            if quote is None:
                await _refuse(interaction, NO_QUOTES)
                return False
            return await self._render_and_post(
                interaction, text=quote.text, author=quote.author_name, year=_message_year(quote), term=None,
                rng=rng,
            )

        await self._with_cooldown(interaction, job)

    # --- shared flow -----------------------------------------------------------------------------------

    async def _resolve(self, interaction: discord.Interaction, raw: str) -> discord.Message | None:
        """The message a link or ID points to, or None after telling the user why not."""
        raw = (raw or "").strip()
        link = MESSAGE_LINK_RE.fullmatch(raw)
        if link:
            guild_id, channel_id, message_id = (int(part) for part in link.groups())
            if interaction.guild is None or guild_id != interaction.guild.id:
                await _refuse(interaction, OTHER_SERVER)
                return None
            channel = interaction.guild.get_channel_or_thread(channel_id)
        elif MESSAGE_ID_RE.fullmatch(raw):
            channel, message_id = interaction.channel, int(raw)
        else:
            await _refuse(interaction, BAD_INPUT)
            return None
        if channel is None or not hasattr(channel, "fetch_message"):
            await _refuse(interaction, NOT_FOUND)
            return None
        if not await self._member_can_read(channel, interaction.user):
            await _refuse(interaction, NOT_FOUND)
            return None
        try:
            return await channel.fetch_message(message_id)
        except discord.HTTPException:  # NotFound and Forbidden included
            await _refuse(interaction, NOT_FOUND)
            return None

    @staticmethod
    async def _member_can_read(channel, member) -> bool:
        """The bot may see channels the member cannot; never let a link leak those."""
        try:
            permissions = channel.permissions_for(member)
        except discord.ClientException:  # a thread whose parent channel is not cached
            return False
        if not (permissions.view_channel and permissions.read_message_history):
            return False
        # a thread's permissions are its parent's, so a private thread also needs membership
        if getattr(channel, "type", None) is discord.ChannelType.private_thread and not permissions.manage_threads:
            try:
                await channel.fetch_member(member.id)
            except discord.HTTPException:  # NotFound: not in the thread
                return False
        return True

    async def make_card(self, interaction: discord.Interaction, message: discord.Message, term: str | None) -> bool:
        """Validate, render and post the meme for `message`, then save it as a quote. True if posted."""
        if message.author.bot:
            await _refuse(interaction, FROM_A_BOT)
            return False
        text = _quote_text(message)
        if not text:
            await _refuse(interaction, NO_TEXT)
            return False
        term = (term or "").strip()[:MAX_TERM_LENGTH] or None
        posted = await self._render_and_post(
            interaction, text=text, author=message.author.display_name,
            year=message.created_at.year, term=term, rng=random.Random(),
        )
        if posted:
            self.app.quotes.add(
                text=text, author_id=message.author.id, author_name=message.author.display_name,
                message_url=message.jump_url, quoted_by=interaction.user.id, quoted_at=utcnow(),
            )
        return posted

    async def _render_and_post(
        self, interaction: discord.Interaction, *, text: str, author: str, year: int, term: str | None,
        rng: random.Random,
    ) -> bool:
        await interaction.response.defer()  # public: rendering takes longer than Discord's 3 s limit
        settings = self.app.settings

        def work() -> bytes:
            background = get_background(
                term, api_key=settings.giphy_api_key, memes_dir=settings.memes_dir, rng=rng,
            )
            gif = render_quote_gif(background, text=clip_text(text), author=author, year=year, rng=rng)
            if len(gif) > MAX_GIF_BYTES:
                raise ValueError(f"rendered GIF is {len(gif)} bytes, over Discord's limit")
            return gif

        if self._render_slots is None:  # created here so it belongs to the running event loop
            self._render_slots = asyncio.Semaphore(MAX_PARALLEL_RENDERS)
        try:
            async with self._render_slots:
                gif = await asyncio.to_thread(work)
        except Exception:
            log.exception("quote meme rendering failed")
            try:
                await interaction.delete_original_response()  # so the apology below can be private
            except discord.HTTPException:
                pass
            await interaction.followup.send(RENDER_FAILED, ephemeral=True)
            return False
        await interaction.followup.send(
            file=discord.File(io.BytesIO(gif), filename="quote.gif"), allowed_mentions=NO_MENTIONS,
        )
        return True


async def setup(bot) -> None:
    await bot.add_cog(QuotesCog(bot))

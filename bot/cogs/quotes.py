from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from bot.db import utcnow
from bot.quotes.backgrounds import get_background
from bot.quotes.render import clip_text, render_quote_gif

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
MAX_TERM_LENGTH = 50
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
    # clean_content shows mentions as names; drop the zero-width spaces it puts in @everyone/@here
    return (message.clean_content or "").replace("​", "").strip()


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

    async def _cooling_down(self, interaction: discord.Interaction) -> bool:
        left = self._wait_left(interaction.user.id)
        if left > 0:
            await _refuse(interaction, f"Slow down! Try again in {max(1, round(left))} s.")
            return True
        return False

    # --- commands --------------------------------------------------------------------------------------

    @app_commands.command(name="quoteit", description="Turn a message into a chaotic meme GIF")
    @app_commands.guild_only()
    @app_commands.describe(
        message="A message link (right-click a message, Copy Message Link) or a message ID from this channel",
        gif="Optional Giphy search term for the background",
    )
    async def quoteit(self, interaction: discord.Interaction, message: str, gif: str | None = None) -> None:
        if await self._cooling_down(interaction):
            return
        target = await self._resolve(interaction, message)
        if target is not None:
            await self.make_card(interaction, target, gif)

    async def quote_it_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if await self._cooling_down(interaction):
            return
        await self.make_card(interaction, message, None)

    @app_commands.command(name="randomquote", description="Post a saved quote again with a new random look")
    @app_commands.guild_only()
    async def randomquote(self, interaction: discord.Interaction) -> None:
        if await self._cooling_down(interaction):
            return
        rng = random.Random()
        quote = self.app.quotes.random(rng)
        if quote is None:
            await _refuse(interaction, NO_QUOTES)
            return
        await self._render_and_post(
            interaction, text=quote.text, author=quote.author_name, year=_message_year(quote), term=None, rng=rng,
        )

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

    async def make_card(self, interaction: discord.Interaction, message: discord.Message, term: str | None) -> None:
        """Validate, render and post the meme for `message`, then save it as a quote."""
        if message.author.bot:
            await _refuse(interaction, FROM_A_BOT)
            return
        text = _quote_text(message)
        if not text:
            await _refuse(interaction, NO_TEXT)
            return
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

    async def _render_and_post(
        self, interaction: discord.Interaction, *, text: str, author: str, year: int, term: str | None,
        rng: random.Random,
    ) -> bool:
        user_id = interaction.user.id
        self._last_use[user_id] = self._clock()  # started only once the request is valid
        await interaction.response.defer()  # public: rendering takes longer than Discord's 3 s limit
        settings = self.app.settings

        def work() -> bytes:
            background = get_background(
                term, api_key=settings.giphy_api_key, memes_dir=settings.memes_dir, rng=rng,
            )
            return render_quote_gif(background, text=clip_text(text), author=author, year=year, rng=rng)

        try:
            gif = await asyncio.to_thread(work)
        except Exception:
            log.exception("quote meme rendering failed")
            self._last_use.pop(user_id, None)  # our failure, so let them retry at once
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

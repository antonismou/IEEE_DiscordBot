from __future__ import annotations

import discord

from bot.feeds.parse import FeedItem, truncate

MAX_EMBEDS_PER_MESSAGE = 10
MAX_CHARS_PER_MESSAGE = 5500  # Discord's limit is 6000; keep a safety margin
IEEE_BLUE = 0x00629B


def item_to_embed(item: FeedItem, feed_title: str) -> discord.Embed:
    embed = discord.Embed(
        title=truncate(item.title, 256),
        url=item.link,
        description=item.summary or None,
        timestamp=item.published,
        colour=discord.Colour(IEEE_BLUE),
    )
    embed.set_footer(text=truncate(feed_title, 100))
    if item.image_url:
        embed.set_image(url=item.image_url)
    return embed


def batch_embeds(embeds: list[discord.Embed]) -> list[list[discord.Embed]]:
    batches: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    size = 0
    for embed in embeds:
        length = len(embed)
        if current and (len(current) >= MAX_EMBEDS_PER_MESSAGE or size + length > MAX_CHARS_PER_MESSAGE):
            batches.append(current)
            current, size = [], 0
        current.append(embed)
        size += length
    if current:
        batches.append(current)
    return batches

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

import discord

from bot.config import FeedConfig
from bot.db import utcnow
from bot.feeds.batch import batch_embeds, item_to_embed
from bot.feeds.parse import FeedItem, parse_feed
from bot.feeds.store import SeenStore

_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Selection:
    to_post: list[FeedItem]      # chronological order, oldest first
    overflow_ids: list[str]      # new items beyond the cap: never posted, marked seen
    first_run: bool


def select_new(store: SeenStore, feed_id: str, items: list[FeedItem], max_items: int, now: datetime) -> Selection:
    unique: dict[str, FeedItem] = {}
    for item in items:
        unique.setdefault(item.item_id, item)
    if not store.is_initialized(feed_id):
        store.initialize(feed_id, list(unique), now)
        return Selection([], [], first_run=True)
    seen = store.seen_ids(feed_id)
    new = [item for item in unique.values() if item.item_id not in seen]
    new.sort(key=lambda item: item.published or _OLDEST, reverse=True)  # stable: ties keep feed order
    kept, overflow = new[:max_items], new[max_items:]
    return Selection(list(reversed(kept)), [item.item_id for item in overflow], first_run=False)


async def process_feed(
    feed: FeedConfig,
    *,
    fetch: Callable[[str], bytes],
    store: SeenStore,
    send: Callable[[int, list[discord.Embed], str | None], Awaitable[None]],
    max_items: int,
    clock: Callable[[], datetime] = utcnow,
) -> int:
    content = await asyncio.to_thread(fetch, feed.url)
    items = parse_feed(content)
    selection = select_new(store, feed.id, items, max_items, clock())
    if selection.first_run or not (selection.to_post or selection.overflow_ids):
        return 0

    note = None
    if selection.overflow_ids:
        note = f"+{len(selection.overflow_ids)} more new items from **{feed.title}** were not shown."
    embeds = [item_to_embed(item, feed.title) for item in selection.to_post]
    batches = batch_embeds(embeds)
    start = 0
    for index, batch in enumerate(batches):
        last = index == len(batches) - 1
        await send(feed.channel_id, batch, note if last else None)
        posted = selection.to_post[start:start + len(batch)]
        store.mark_seen(feed.id, [item.item_id for item in posted], clock())  # only after a successful send
        start += len(batch)
    store.mark_seen(feed.id, selection.overflow_ids, clock())
    return len(selection.to_post)

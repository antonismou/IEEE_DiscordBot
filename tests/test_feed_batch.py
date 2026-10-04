from datetime import datetime, timezone

import discord

from bot.feeds.batch import MAX_CHARS_PER_MESSAGE, MAX_EMBEDS_PER_MESSAGE, batch_embeds, item_to_embed
from bot.feeds.parse import FeedItem


def item(**kw):
    base = dict(item_id="1", title="A paper", link="https://x/1", summary="", image_url=None, published=None)
    base.update(kw)
    return FeedItem(**base)


def test_embed_fields():
    stamp = datetime(2026, 10, 4, tzinfo=timezone.utc)
    embed = item_to_embed(
        item(summary="Short summary", image_url="https://x/i.png", published=stamp), "IEEE Transactions on Robotics"
    )
    assert embed.title == "A paper" and embed.url == "https://x/1"
    assert embed.description == "Short summary"
    assert embed.image.url == "https://x/i.png"
    assert embed.footer.text == "IEEE Transactions on Robotics"
    assert embed.timestamp == stamp


def test_empty_summary_and_missing_link_are_allowed():
    embed = item_to_embed(item(summary="", link=None), "J")
    assert embed.description is None and embed.url is None


def test_title_is_truncated_to_256():
    assert len(item_to_embed(item(title="t" * 400), "J").title) <= 256


def test_batches_split_at_ten_embeds():
    embeds = [discord.Embed(title=f"t{i}") for i in range(25)]
    sizes = [len(b) for b in batch_embeds(embeds)]
    assert sizes == [MAX_EMBEDS_PER_MESSAGE, MAX_EMBEDS_PER_MESSAGE, 5]


def test_batches_split_by_text_size_and_keep_order():
    big = [discord.Embed(title=f"t{i}", description="x" * 3000) for i in range(3)]
    batches = batch_embeds(big)
    assert [len(b) for b in batches] == [1, 1, 1]
    assert [e.title for b in batches for e in b] == ["t0", "t1", "t2"]
    assert all(sum(len(e) for e in b) <= MAX_CHARS_PER_MESSAGE for b in batches)


def test_no_embeds_no_batches():
    assert batch_embeds([]) == []

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.quotes.store import QuoteRepo
from tests.fakes import fake_interaction, fake_member, reply_text

WHEN = datetime(2024, 5, 6, 7, 8, tzinfo=timezone.utc)
URL = "https://discord.com/channels/111/222/333"


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def fake_message(text="hello there", *, bot=False, author_id=42, name="Kenobi", message_id=333):
    author = SimpleNamespace(id=author_id, bot=bot, display_name=name)
    return SimpleNamespace(
        id=message_id, content=text, clean_content=text, author=author,
        created_at=WHEN, jump_url=URL, attachments=[],
    )


def make_cog(conn, monkeypatch, *, render=None):
    import bot.cogs.quotes as quotes_module

    monkeypatch.setattr(quotes_module, "get_background", MagicMock(return_value=b"BG"))
    monkeypatch.setattr(quotes_module, "render_quote_gif", render or MagicMock(return_value=b"GIF89a-fake"))
    app = SimpleNamespace(
        quotes=QuoteRepo(conn),
        settings=SimpleNamespace(giphy_api_key="", memes_dir=Path("nowhere")),
    )
    cog = quotes_module.QuotesCog(SimpleNamespace(app=app, tree=MagicMock()))
    cog._clock = Clock()
    return cog, app, quotes_module


def interaction_with(message=None, fetch_error=None, user_id=1):
    interaction = fake_interaction(fake_member(user_id), MagicMock())
    interaction.guild.id = 111
    interaction.channel.id = 222
    interaction.channel.fetch_message = AsyncMock(return_value=message, side_effect=fetch_error)
    interaction.guild.get_channel_or_thread = MagicMock(return_value=interaction.channel)
    interaction.delete_original_response = AsyncMock()
    return interaction


def sent_kwargs(interaction):
    return interaction.followup.send.await_args.kwargs


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]


def refused_privately(interaction) -> str:
    """The text of an ephemeral refusal, sent either as the first response or as a followup."""
    if interaction.response.send_message.await_args is not None:
        call = interaction.response.send_message.await_args
    else:
        call = interaction.followup.send.await_args
    assert call.kwargs.get("ephemeral") is True
    assert "file" not in call.kwargs
    return reply_text(interaction)


def test_registers_commands(conn, monkeypatch):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        await cog.cog_load()
        added = cog.bot.tree.add_command.call_args.args[0]
        await cog.cog_unload()
        return cog, added

    cog, added = asyncio.run(scenario())
    assert {"quoteit", "randomquote"} <= {c.name for c in cog.get_app_commands()}
    assert isinstance(added, discord.app_commands.ContextMenu)
    assert added.name == "Quote it" and added.type == discord.AppCommandType.message
    assert added.guild_only
    cog.bot.tree.remove_command.assert_called_once_with("Quote it", type=discord.AppCommandType.message)


def test_quoting_saves_and_posts_a_gif_file_reply_with_no_mentions(conn, monkeypatch):
    async def scenario():
        cog, app, module = make_cog(conn, monkeypatch)
        interaction = interaction_with(fake_message())
        await cog.quoteit.callback(cog, interaction, URL, "facepalm")
        return interaction, app, module

    interaction, app, module = asyncio.run(scenario())
    kwargs = sent_kwargs(interaction)
    assert isinstance(kwargs["file"], discord.File) and kwargs["file"].filename == "quote.gif"
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert not kwargs.get("ephemeral")
    interaction.response.defer.assert_awaited_once()
    assert interaction.response.defer.await_args.kwargs.get("ephemeral", False) is False
    interaction.channel.fetch_message.assert_awaited_once_with(333)
    assert count(conn) == 1
    quote = app.quotes.random()
    assert (quote.text, quote.author_id, quote.author_name, quote.message_url, quote.quoted_by) == (
        "hello there", 42, "Kenobi", URL, 1,
    )
    assert module.get_background.call_args.args[0] == "facepalm"
    render_kwargs = module.render_quote_gif.call_args.kwargs
    assert (render_kwargs["text"], render_kwargs["author"], render_kwargs["year"]) == ("hello there", "Kenobi", 2024)


def test_context_menu_quotes_the_clicked_message(conn, monkeypatch):
    async def scenario():
        cog, app, _ = make_cog(conn, monkeypatch)
        interaction = interaction_with()
        await cog.ctx_menu.callback(interaction, fake_message())
        return interaction

    interaction = asyncio.run(scenario())
    assert isinstance(sent_kwargs(interaction)["file"], discord.File)
    interaction.channel.fetch_message.assert_not_awaited()
    assert count(conn) == 1


def test_a_bare_message_id_is_looked_up_in_the_current_channel(conn, monkeypatch):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        interaction = interaction_with(fake_message())
        await cog.quoteit.callback(cog, interaction, " 333 ", None)
        return interaction

    interaction = asyncio.run(scenario())
    interaction.channel.fetch_message.assert_awaited_once_with(333)
    assert isinstance(sent_kwargs(interaction)["file"], discord.File)


def test_mentions_in_quoted_text_are_not_in_message_content(conn, monkeypatch):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        interaction = interaction_with(fake_message("@everyone <@123> look"))
        await cog.quoteit.callback(cog, interaction, URL, None)
        return interaction

    interaction = asyncio.run(scenario())
    args, kwargs = interaction.followup.send.await_args
    content = (args[0] if args else kwargs.get("content")) or ""
    assert "@everyone" not in content and "<@123>" not in content
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


def test_bot_author_is_refused(conn, monkeypatch):
    async def scenario():
        cog, _, module = make_cog(conn, monkeypatch)
        interaction = interaction_with(fake_message(bot=True))
        await cog.quoteit.callback(cog, interaction, URL, None)
        return interaction, module

    interaction, module = asyncio.run(scenario())
    assert "bot" in refused_privately(interaction).lower()
    module.render_quote_gif.assert_not_called()
    assert count(conn) == 0


def test_empty_text_is_refused(conn, monkeypatch):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        message = fake_message("   ")
        message.attachments = [object()]                    # an attachment-only message
        interaction = interaction_with(message)
        await cog.quoteit.callback(cog, interaction, URL, None)
        return interaction

    interaction = asyncio.run(scenario())
    assert "text" in refused_privately(interaction).lower()
    assert count(conn) == 0


@pytest.mark.parametrize("error", [
    discord.NotFound(MagicMock(status=404), "Unknown Message"),
    discord.Forbidden(MagicMock(status=403), "Missing Access"),
    discord.HTTPException(MagicMock(status=500), "boom"),
])
def test_unknown_or_unreadable_message_is_a_friendly_refusal(conn, monkeypatch, error):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        interaction = interaction_with(fetch_error=error)
        await cog.quoteit.callback(cog, interaction, URL, None)
        return interaction

    interaction = asyncio.run(scenario())
    assert "couldn't find" in refused_privately(interaction).lower()
    assert count(conn) == 0


@pytest.mark.parametrize("garbage", [
    "not a link", "https://example.com/channels/111/222/333", "", "12ab", "https://discord.com/channels/111/222",
])
def test_garbage_message_input_is_a_friendly_refusal(conn, monkeypatch, garbage):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        interaction = interaction_with(fake_message())
        await cog.quoteit.callback(cog, interaction, garbage, None)
        return interaction

    interaction = asyncio.run(scenario())
    assert "link" in refused_privately(interaction).lower()
    interaction.channel.fetch_message.assert_not_awaited()
    assert count(conn) == 0


@pytest.mark.parametrize("link", [
    "https://ptb.discord.com/channels/111/222/333",
    "https://canary.discord.com/channels/111/222/333",
    "https://discordapp.com/channels/111/222/333",
    "<https://discord.com/channels/111/222/333>",
])
def test_link_variants_are_accepted(conn, monkeypatch, link):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        interaction = interaction_with(fake_message())
        await cog.quoteit.callback(cog, interaction, link, None)
        return interaction

    interaction = asyncio.run(scenario())
    assert isinstance(sent_kwargs(interaction)["file"], discord.File)


def test_a_link_to_another_server_is_refused(conn, monkeypatch):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        interaction = interaction_with(fake_message())
        await cog.quoteit.callback(cog, interaction, "https://discord.com/channels/999/222/333", None)
        return interaction

    interaction = asyncio.run(scenario())
    assert "this server" in refused_privately(interaction).lower()
    interaction.channel.fetch_message.assert_not_awaited()


def test_a_link_to_a_channel_the_user_cannot_read_is_refused(conn, monkeypatch):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        interaction = interaction_with(fake_message())
        secret = MagicMock()
        secret.id = 555
        secret.fetch_message = AsyncMock(return_value=fake_message())
        secret.permissions_for.return_value = discord.Permissions.none()
        interaction.guild.get_channel_or_thread = MagicMock(return_value=secret)
        await cog.quoteit.callback(cog, interaction, "https://discord.com/channels/111/555/333", None)
        return interaction, secret

    interaction, secret = asyncio.run(scenario())
    assert "couldn't find" in refused_privately(interaction).lower()
    secret.fetch_message.assert_not_awaited()
    assert count(conn) == 0


def test_a_long_gif_term_is_cut(conn, monkeypatch):
    async def scenario():
        cog, _, module = make_cog(conn, monkeypatch)
        await cog.quoteit.callback(cog, interaction_with(fake_message()), URL, "x" * 500)
        return module

    module = asyncio.run(scenario())
    assert len(module.get_background.call_args.args[0]) <= 50


def test_long_text_is_clipped_before_rendering(conn, monkeypatch):
    async def scenario():
        cog, _, module = make_cog(conn, monkeypatch)
        await cog.quoteit.callback(cog, interaction_with(fake_message("word " * 200)), URL, None)
        return module

    module = asyncio.run(scenario())
    assert len(module.render_quote_gif.call_args.kwargs["text"]) <= 280


def test_second_use_inside_cooldown_is_refused_then_allowed_after(conn, monkeypatch):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        first = interaction_with(fake_message())
        await cog.quoteit.callback(cog, first, URL, None)
        cog._clock.now += 5
        second = interaction_with(fake_message())
        await cog.quoteit.callback(cog, second, URL, None)
        other_user = interaction_with(fake_message(), user_id=2)
        await cog.quoteit.callback(cog, other_user, URL, None)
        cog._clock.now += cog.COOLDOWN_SECONDS
        third = interaction_with(fake_message())
        await cog.quoteit.callback(cog, third, URL, None)
        return first, second, other_user, third

    first, second, other_user, third = asyncio.run(scenario())
    assert isinstance(sent_kwargs(first)["file"], discord.File)
    assert "slow down" in refused_privately(second).lower()
    second.channel.fetch_message.assert_not_awaited()
    assert isinstance(sent_kwargs(other_user)["file"], discord.File)
    assert isinstance(sent_kwargs(third)["file"], discord.File)
    assert count(conn) == 3


def test_a_refused_attempt_does_not_start_the_cooldown(conn, monkeypatch):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        await cog.quoteit.callback(cog, interaction_with(fake_message(bot=True)), URL, None)
        retry = interaction_with(fake_message())
        await cog.quoteit.callback(cog, retry, URL, None)
        return retry

    assert isinstance(sent_kwargs(asyncio.run(scenario()))["file"], discord.File)


def test_randomquote_with_no_quotes_says_so(conn, monkeypatch):
    async def scenario():
        cog, _, module = make_cog(conn, monkeypatch)
        interaction = interaction_with()
        await cog.randomquote.callback(cog, interaction)
        return interaction, module

    interaction, module = asyncio.run(scenario())
    assert "no quotes" in refused_privately(interaction).lower()
    module.render_quote_gif.assert_not_called()


def test_randomquote_renders_a_saved_quote(conn, monkeypatch):
    async def scenario():
        cog, app, module = make_cog(conn, monkeypatch)
        app.quotes.add(text="saved words", author_id=9, author_name="Ana", message_url=URL,
                       quoted_by=1, quoted_at=WHEN)
        interaction = interaction_with()
        await cog.randomquote.callback(cog, interaction)
        return interaction, module

    interaction, module = asyncio.run(scenario())
    kwargs = sent_kwargs(interaction)
    assert isinstance(kwargs["file"], discord.File)
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    render_kwargs = module.render_quote_gif.call_args.kwargs
    assert (render_kwargs["text"], render_kwargs["author"]) == ("saved words", "Ana")
    assert render_kwargs["year"] == discord.utils.snowflake_time(333).year     # when the message was sent
    assert count(conn) == 1                                 # nothing new saved


def test_render_failure_gives_a_friendly_message_and_no_quote_saved(conn, monkeypatch):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch, render=MagicMock(side_effect=OSError("bad image")))
        interaction = interaction_with(fake_message())
        await cog.quoteit.callback(cog, interaction, URL, None)
        retry = interaction_with(fake_message())
        await cog.quoteit.callback(cog, retry, URL, None)   # a failure does not lock the user out
        return interaction, retry

    interaction, retry = asyncio.run(scenario())
    kwargs = sent_kwargs(interaction)
    assert kwargs.get("ephemeral") is True and "file" not in kwargs
    assert "sorry" in reply_text(interaction).lower()
    interaction.delete_original_response.assert_awaited_once()
    retry.response.defer.assert_awaited_once()
    assert count(conn) == 0


def private_thread(*, member=False, manage_threads=False):
    thread = MagicMock()
    thread.id = 777
    thread.type = discord.ChannelType.private_thread
    thread.fetch_message = AsyncMock(return_value=fake_message())
    thread.permissions_for.return_value = discord.Permissions(
        view_channel=True, read_message_history=True, manage_threads=manage_threads,
    )
    error = None if member else discord.NotFound(MagicMock(status=404), "Unknown Member")
    thread.fetch_member = AsyncMock(return_value=SimpleNamespace(id=1), side_effect=error)
    return thread


def quote_from(conn, monkeypatch, channel):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        interaction = interaction_with(fake_message())
        interaction.guild.get_channel_or_thread = MagicMock(return_value=channel)
        await cog.quoteit.callback(cog, interaction, "https://discord.com/channels/111/777/333", None)
        return interaction

    return asyncio.run(scenario())


def test_a_private_thread_the_member_is_not_in_is_refused(conn, monkeypatch):
    thread = private_thread()
    interaction = quote_from(conn, monkeypatch, thread)
    assert "couldn't find" in refused_privately(interaction).lower()
    thread.fetch_member.assert_awaited_once_with(1)
    thread.fetch_message.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
    assert count(conn) == 0


def test_a_private_thread_the_member_is_in_can_be_quoted(conn, monkeypatch):
    thread = private_thread(member=True)
    interaction = quote_from(conn, monkeypatch, thread)
    thread.fetch_message.assert_awaited_once_with(333)
    assert isinstance(sent_kwargs(interaction)["file"], discord.File)
    assert count(conn) == 1


def test_a_private_thread_can_be_quoted_by_someone_who_manages_threads(conn, monkeypatch):
    thread = private_thread(manage_threads=True)
    interaction = quote_from(conn, monkeypatch, thread)
    thread.fetch_message.assert_awaited_once_with(333)
    assert isinstance(sent_kwargs(interaction)["file"], discord.File)
    assert count(conn) == 1


def test_a_thread_whose_parent_is_not_cached_is_a_friendly_refusal(conn, monkeypatch):
    thread = private_thread(member=True)
    thread.permissions_for.side_effect = discord.ClientException("Parent channel not found")
    interaction = quote_from(conn, monkeypatch, thread)
    assert "couldn't find" in refused_privately(interaction).lower()
    thread.fetch_message.assert_not_awaited()
    assert count(conn) == 0


# --- /randomquote only serves quotes from channels everyone can see ----------------------------------------

def channel_seen_by_everyone(visible: bool, *, kind=discord.ChannelType.text):
    channel = MagicMock()
    channel.type = kind
    channel.permissions_for.return_value = discord.Permissions(view_channel=visible)
    return channel


def random_from(conn, monkeypatch, channels: dict, *, here=None):
    async def scenario():
        cog, app, module = make_cog(conn, monkeypatch)
        for channel_id in channels:
            app.quotes.add(text=f"from {channel_id}", author_id=9, author_name="Ana",
                           message_url=f"https://discord.com/channels/111/{channel_id}/333",
                           quoted_by=1, quoted_at=WHEN)
        interaction = interaction_with()
        interaction.channel_id = here
        interaction.guild.get_channel_or_thread = MagicMock(side_effect=lambda cid: channels.get(cid))
        await cog.randomquote.callback(cog, interaction)
        return interaction, module

    return asyncio.run(scenario())


def test_randomquote_never_serves_a_quote_from_a_private_channel(conn, monkeypatch):
    interaction, module = random_from(conn, monkeypatch, {501: channel_seen_by_everyone(False)})
    assert "no quotes" in refused_privately(interaction).lower()
    module.render_quote_gif.assert_not_called()


def test_randomquote_serves_a_private_channels_quote_back_into_that_same_channel(conn, monkeypatch):
    interaction, module = random_from(conn, monkeypatch, {501: channel_seen_by_everyone(False)}, here=501)
    assert module.render_quote_gif.call_args.kwargs["text"] == "from 501"
    assert isinstance(sent_kwargs(interaction)["file"], discord.File)


def test_randomquote_still_refuses_a_private_channels_quote_in_another_channel(conn, monkeypatch):
    interaction, module = random_from(conn, monkeypatch, {501: channel_seen_by_everyone(False)}, here=777)
    assert "no quotes" in refused_privately(interaction).lower()
    module.render_quote_gif.assert_not_called()


def test_randomquote_serves_a_quote_from_a_public_channel(conn, monkeypatch):
    for _ in range(5):
        conn.execute("DELETE FROM quotes")
        conn.commit()
        interaction, module = random_from(conn, monkeypatch, {
            501: channel_seen_by_everyone(False),
            502: channel_seen_by_everyone(True),
            503: channel_seen_by_everyone(True, kind=discord.ChannelType.private_thread),
        })
        assert module.render_quote_gif.call_args.kwargs["text"] == "from 502"
        assert isinstance(sent_kwargs(interaction)["file"], discord.File)


def test_randomquote_skips_quotes_whose_channel_is_gone(conn, monkeypatch):
    uncached_parent = channel_seen_by_everyone(True)
    uncached_parent.permissions_for.side_effect = discord.ClientException("Parent channel not found")
    interaction, module = random_from(conn, monkeypatch, {501: None, 502: uncached_parent})
    assert "no quotes" in refused_privately(interaction).lower()
    module.render_quote_gif.assert_not_called()


# --- text cleanup ----------------------------------------------------------------------------------------

def rendered_text(conn, monkeypatch, text):
    async def scenario():
        cog, _, module = make_cog(conn, monkeypatch)
        interaction = interaction_with(fake_message(text))
        await cog.quoteit.callback(cog, interaction, URL, None)
        return interaction, module

    return asyncio.run(scenario())


def test_custom_emoji_markdown_and_spoilers_are_cleaned_up(conn, monkeypatch):
    interaction, module = rendered_text(
        conn, monkeypatch, "**so** <:pepe_laugh:123> _true_ <a:party_time:456> ||the butler did it||"
    )
    text = module.render_quote_gif.call_args.kwargs["text"]
    assert text == "so :pepe_laugh: true :party_time: [spoiler]"
    assert "butler" not in QuoteRepo(conn).random().text


def test_the_zero_width_space_clean_content_adds_to_everyone_is_dropped(conn, monkeypatch):
    _, module = rendered_text(conn, monkeypatch, "@\u200beveryone look")
    assert module.render_quote_gif.call_args.kwargs["text"] == "@everyone look"


def test_text_that_is_empty_after_cleanup_is_refused(conn, monkeypatch):
    interaction, module = rendered_text(conn, monkeypatch, "** **")
    assert "text" in refused_privately(interaction).lower()
    module.render_quote_gif.assert_not_called()


# --- concurrency and size --------------------------------------------------------------------------------

def test_a_second_parallel_use_is_refused_while_the_first_is_still_fetching(conn, monkeypatch):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        gate = asyncio.Event()

        async def slow_fetch(message_id):
            await gate.wait()
            return fake_message()

        first = interaction_with()
        first.channel.fetch_message = AsyncMock(side_effect=slow_fetch)
        task = asyncio.create_task(cog.quoteit.callback(cog, first, URL, None))
        await asyncio.sleep(0)                               # the first is now waiting on fetch_message
        second = interaction_with(fake_message())
        await cog.quoteit.callback(cog, second, URL, None)
        gate.set()
        await task
        return first, second

    first, second = asyncio.run(scenario())
    assert "slow down" in refused_privately(second).lower()
    second.channel.fetch_message.assert_not_awaited()
    assert isinstance(sent_kwargs(first)["file"], discord.File)
    assert count(conn) == 1


def test_a_refusal_after_fetching_releases_the_cooldown(conn, monkeypatch):
    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch)
        missing = discord.NotFound(MagicMock(status=404), "Unknown Message")
        await cog.quoteit.callback(cog, interaction_with(fetch_error=missing), URL, None)
        retry = interaction_with(fake_message())
        await cog.quoteit.callback(cog, retry, URL, None)
        return retry

    assert isinstance(sent_kwargs(asyncio.run(scenario()))["file"], discord.File)


def test_at_most_two_renders_run_at_once(conn, monkeypatch):
    import threading

    lock, release = threading.Lock(), threading.Event()
    state = {"active": 0, "peak": 0}

    def blocking_render(background, **kwargs):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        release.wait(5)
        with lock:
            state["active"] -= 1
        return b"GIF89a-fake"

    async def scenario():
        cog, _, _ = make_cog(conn, monkeypatch, render=MagicMock(side_effect=blocking_render))
        tasks = [
            asyncio.create_task(cog.quoteit.callback(cog, interaction_with(fake_message(), user_id=uid), URL, None))
            for uid in (1, 2, 3, 4)
        ]
        for _ in range(200):
            if state["active"] == 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)                             # give a third render the chance to (wrongly) start
        peak_while_blocked = state["peak"]
        release.set()
        await asyncio.gather(*tasks)
        return peak_while_blocked

    assert asyncio.run(scenario()) == 2
    assert count(conn) == 4


def test_a_gif_over_the_size_limit_is_a_render_failure(conn, monkeypatch):
    import bot.quotes.render as render_module

    async def scenario():
        too_big = MagicMock(return_value=b"x" * (render_module.MAX_GIF_BYTES + 1))
        cog, _, _ = make_cog(conn, monkeypatch, render=too_big)
        interaction = interaction_with(fake_message())
        await cog.quoteit.callback(cog, interaction, URL, None)
        retry = interaction_with(fake_message())
        await cog.quoteit.callback(cog, retry, URL, None)
        return interaction, retry

    interaction, retry = asyncio.run(scenario())
    kwargs = sent_kwargs(interaction)
    assert kwargs.get("ephemeral") is True and "file" not in kwargs
    assert "sorry" in reply_text(interaction).lower()
    retry.response.defer.assert_awaited_once()               # the cooldown was released
    assert count(conn) == 0

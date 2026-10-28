import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import voice_recv

from bot.channels import MEETING_SUMMARIES, ChannelRepo
from bot.recording.meeting import Meeting
from tests.fakes import fake_guild, fake_interaction, fake_member, reply_text
from tests.test_recording_posting import FakeChannel, meeting_with

FRAME = b"\x01\x00" * 320
BOT_ID = 999


class FakeVoiceClient:
    def __init__(self, events):
        self.events = events
        self.sink = None
        self.disconnect = AsyncMock(side_effect=lambda **kw: events.append("disconnect"))
        self._connection = SimpleNamespace(dave_protocol_version=0, dave_session=None)

    def listen(self, sink):
        self.events.append("listen")
        self.sink = sink

    def stop_listening(self):
        self.events.append("stop_listening")

    def _get_id_from_ssrc(self, ssrc):
        return None


def make_channel(events, send_error=None):
    vc = FakeVoiceClient(events)
    channel = MagicMock()
    channel.id, channel.mention = 55, "<#55>"
    channel.guild = fake_guild()
    channel.voice_states = {3: object(), BOT_ID: object()}         # the officer and the bot are in the call
    channel.connect = AsyncMock(side_effect=lambda **kw: events.append("connect") or vc)
    channel.send = AsyncMock(side_effect=lambda *a, **kw: events.append("announce"))
    if send_error:
        channel.send.side_effect = send_error
    return channel, vc


def make_cog(conn, tmp_path, summaries=None):
    from bot.cogs.recorder import RecorderCog

    app = SimpleNamespace(
        settings=SimpleNamespace(meetings_dir=tmp_path / "meetings"),
        channels=ChannelRepo(conn),
        server=SimpleNamespace(officer_role_id=9),
    )
    bot = SimpleNamespace(
        app=app, user=SimpleNamespace(id=BOT_ID), wait_until_ready=AsyncMock(),
        get_channel=lambda channel_id: summaries if channel_id == 77 else None,
        fetch_channel=AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")),
    )
    return RecorderCog(bot), app


def officer_in(channel, user_id=3):
    user = fake_member(user_id, role_ids=(9,))
    user.voice = SimpleNamespace(channel=channel) if channel else None
    return user


def meeting_dirs(app):
    root = app.settings.meetings_dir
    return sorted(root.iterdir()) if root.exists() else []


def test_record_commands_exist_and_are_officer_only():
    from bot.cogs.recorder import RecorderCog

    async def build():
        return RecorderCog(SimpleNamespace(app=SimpleNamespace()))

    cog = asyncio.run(build())
    group = next(c for c in cog.get_app_commands() if c.name == "record")
    assert {c.name for c in group.commands} == {"start", "stop", "status"}
    assert all(c.checks for c in group.commands)


def test_start_needs_the_officer_to_be_in_a_voice_channel(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        interaction = fake_interaction(officer_in(None), fake_guild())
        await cog.start.callback(cog, interaction)
        return app, interaction

    app, interaction = asyncio.run(scenario())
    assert "voice channel" in reply_text(interaction) and meeting_dirs(app) == []


def test_start_is_refused_while_another_meeting_is_recording_or_being_processed(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel, _ = make_channel(events)
        Meeting.create(app.settings.meetings_dir, 1, 1, datetime(2026, 10, 1, 10, 0, 0)).set_status("transcribing")
        interaction = fake_interaction(officer_in(channel), fake_guild())
        await cog.start.callback(cog, interaction)
        return events, interaction

    events, interaction = asyncio.run(scenario())
    assert "connect" not in events and "already" in reply_text(interaction).lower()


def test_start_announces_in_the_channel_before_any_audio_is_captured(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel, vc = make_channel(events)
        interaction = fake_interaction(officer_in(channel), fake_guild())
        await cog.start.callback(cog, interaction)
        await cog.cog_unload()
        return app, events, channel, interaction

    app, events, channel, interaction = asyncio.run(scenario())
    assert events == ["connect", "announce", "listen"]
    channel.connect.assert_awaited_once_with(cls=voice_recv.VoiceRecvClient)
    assert "recorded" in channel.send.await_args.args[0]
    [folder] = meeting_dirs(app)
    assert json.loads((folder / "manifest.json").read_text())["status"] == "recording"
    assert "<#55>" in reply_text(interaction)


def test_if_the_announcement_cannot_be_posted_nothing_is_recorded(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        response = SimpleNamespace(status=403, reason="Forbidden")
        channel, vc = make_channel(events, send_error=discord.Forbidden(response, "no access"))
        interaction = fake_interaction(officer_in(channel), fake_guild())
        await cog.start.callback(cog, interaction)
        return app, events, interaction

    app, events, interaction = asyncio.run(scenario())
    assert events == ["connect", "disconnect"] and meeting_dirs(app) == []
    assert "announce" in reply_text(interaction).lower()


def test_stop_with_no_recording_says_so(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        interaction = fake_interaction(officer_in(None), fake_guild())
        await cog.stop.callback(cog, interaction)
        return interaction

    assert "no recording" in reply_text(asyncio.run(scenario())).lower()


def test_stop_saves_the_speakers_names_closes_the_recording_and_reports_what_was_captured(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel, vc = make_channel(events)
        guild = fake_guild(fetched=None)
        speaker = fake_member(7)
        speaker.display_name = "Μαρία"
        guild.fetch_member = AsyncMock(return_value=speaker)
        await cog.start.callback(cog, fake_interaction(officer_in(channel), guild))
        cog.active.writer.add(7, FRAME, 1.0)           # someone spoke
        stop = fake_interaction(officer_in(channel), guild)
        await cog.stop.callback(cog, stop)
        return app, events, stop, cog

    app, events, stop, cog = asyncio.run(scenario())
    assert events[-2:] == ["stop_listening", "disconnect"] and cog.active is None
    manifest = json.loads((meeting_dirs(app)[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "queued" and manifest["speakers"] == {"7": "Μαρία"}   # ready for the job
    assert [s["speaker"] for s in manifest["segments"]] == [7]
    text = reply_text(stop)
    assert "1 speaker" in text and "1 segment" in text


def test_stop_warns_when_no_audio_could_be_decoded(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel, vc = make_channel([])
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        stop = fake_interaction(officer_in(channel), fake_guild())
        await cog.stop.callback(cog, stop)
        return stop

    text = reply_text(asyncio.run(scenario()))
    assert "no audio" in text.lower()


def test_after_a_restart_a_leftover_recording_is_marked_interrupted(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        left = Meeting.create(app.settings.meetings_dir, 1, 1, datetime(2026, 10, 1, 10, 0, 0))
        await cog.cog_load()
        await cog.cog_unload()
        return Meeting.load(left.directory).status

    assert asyncio.run(scenario()) == "interrupted"


def test_the_recorder_extension_loads_into_a_bot_and_is_part_of_startup(tmp_path):
    from discord.ext import commands

    from bot.main import EXTENSIONS

    async def scenario():
        bot = commands.Bot(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        bot.app = SimpleNamespace(settings=SimpleNamespace(meetings_dir=tmp_path / "meetings"))
        await bot.load_extension("bot.cogs.recorder")
        return {c.name for c in bot.tree.get_commands()}

    assert "bot.cogs.recorder" in EXTENSIONS
    assert "record" in asyncio.run(scenario())


def voice_state(channel):
    return SimpleNamespace(channel=channel)


async def recording(cog, channel):
    await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
    cog.active.writer.add(7, FRAME, 1.0)
    cog.active.writer.flush_idle(100.0)                           # they went quiet: the segment is finished
    channel.send.reset_mock()


def test_the_poster_waits_for_the_summaries_channel_to_be_set_and_then_posts(conn, tmp_path):
    summaries = FakeChannel()

    async def scenario():
        cog, app = make_cog(conn, tmp_path, summaries)
        meeting = meeting_with(app.settings.meetings_dir, "done")
        await cog.post_updates()
        before = list(summaries.sent)
        app.channels.set(MEETING_SUMMARIES, 77)
        await cog.post_updates()
        await cog.post_updates()
        return before, Meeting.load(meeting.directory)

    before, meeting = asyncio.run(scenario())
    assert before == [] and len(summaries.sent) == 2 and meeting.posted


def test_the_poster_keeps_the_result_when_the_channel_cannot_be_reached(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        app.channels.set(MEETING_SUMMARIES, 78)                     # deleted channel: fetch fails
        meeting = meeting_with(app.settings.meetings_dir, "done")
        await cog.post_updates()
        return Meeting.load(meeting.directory)

    assert not asyncio.run(scenario()).posted


def test_a_post_that_fails_is_kept_and_retried_on_the_next_turn(conn, tmp_path):
    summaries = FakeChannel()

    async def scenario():
        cog, app = make_cog(conn, tmp_path, summaries)
        app.channels.set(MEETING_SUMMARIES, 77)
        meeting = meeting_with(app.settings.meetings_dir, "done", transcript=None)   # not written yet
        await cog.post_updates()
        await cog.post_updates()
        unposted = not Meeting.load(meeting.directory).posted
        meeting.transcript_path.write_text("[00:00:01] Μαρία: Γεια\n", encoding="utf-8")
        await cog.post_updates()
        return unposted, Meeting.load(meeting.directory)

    unposted, meeting = asyncio.run(scenario())
    assert unposted and meeting.posted and len(summaries.sent) == 2


def test_the_poster_survives_a_broken_manifest(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path, FakeChannel())
        app.channels.set(MEETING_SUMMARIES, 77)
        broken = app.settings.meetings_dir / "20261001-100000"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text("{not json", encoding="utf-8")
        await cog.poster.coro(cog)                                   # one turn of the loop, as it runs

    asyncio.run(scenario())


def test_status_with_no_meetings_says_so(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        interaction = fake_interaction(officer_in(None), fake_guild())
        await cog.status.callback(cog, interaction)
        return interaction

    interaction = asyncio.run(scenario())
    assert "no meeting" in reply_text(interaction).lower()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


def test_status_shows_the_stage_of_the_latest_meeting(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        meeting_with(app.settings.meetings_dir, "done")
        Meeting.create(app.settings.meetings_dir, 1, 1, datetime(2026, 10, 27, 9, 0, 0)).set_status("summarizing")
        interaction = fake_interaction(officer_in(None), fake_guild())
        await cog.status.callback(cog, interaction)
        return reply_text(interaction)

    text = asyncio.run(scenario())
    assert "20261027-090000" in text and "summar" in text.lower()


def test_status_of_a_finished_meeting_says_whether_it_was_posted(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        meeting_with(app.settings.meetings_dir, "done")
        interaction = fake_interaction(officer_in(None), fake_guild())
        await cog.status.callback(cog, interaction)
        return reply_text(interaction)

    text = asyncio.run(scenario())
    assert "done" in text.lower() and "/channel set" in text


def test_status_during_a_recording_shows_what_was_captured(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel, _ = make_channel([])
        await recording(cog, channel)
        interaction = fake_interaction(officer_in(None), fake_guild())
        await cog.status.callback(cog, interaction)
        await cog.cog_unload()
        return reply_text(interaction)

    text = asyncio.run(scenario())
    assert "recording" in text.lower() and "<#55>" in text and "1 segment" in text


def test_if_the_bot_is_kicked_from_the_call_the_recording_stops_and_is_queued(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel, _ = make_channel(events)
        await recording(cog, channel)
        await cog.on_voice_state_update(SimpleNamespace(id=BOT_ID), voice_state(channel), voice_state(None))
        return app, events, channel, cog

    app, events, channel, cog = asyncio.run(scenario())
    assert cog.active is None and events[-3:] == ["stop_listening", "disconnect", "announce"]
    manifest = json.loads((meeting_dirs(app)[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "queued" and [s["speaker"] for s in manifest["segments"]] == [7]
    assert "σταμάτησε" in channel.send.await_args.args[0]


def test_when_everyone_leaves_the_call_the_recording_stops(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel, _ = make_channel([])
        await recording(cog, channel)
        channel.voice_states = {4: object(), BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=3), voice_state(channel), voice_state(None))
        still_recording = cog.active is not None
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=4), voice_state(channel), voice_state(None))
        return app, channel, cog, still_recording

    app, channel, cog, still_recording = asyncio.run(scenario())
    assert still_recording and cog.active is None
    assert Meeting.load(meeting_dirs(app)[0]).status == "queued"
    assert channel.send.await_count == 1


def test_an_automatic_stop_still_finishes_if_the_chat_cannot_be_posted_in(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel, _ = make_channel([])
        await recording(cog, channel)
        channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
        await cog.on_voice_state_update(SimpleNamespace(id=BOT_ID), voice_state(channel), voice_state(None))
        return app, cog

    app, cog = asyncio.run(scenario())
    assert cog.active is None and Meeting.load(meeting_dirs(app)[0]).status == "queued"


def test_voice_changes_elsewhere_do_not_stop_the_recording(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel, _ = make_channel([])
        other = SimpleNamespace(id=66, voice_states={})
        await recording(cog, channel)
        await cog.on_voice_state_update(SimpleNamespace(id=5), voice_state(other), voice_state(None))
        await cog.on_voice_state_update(SimpleNamespace(id=5), voice_state(None), voice_state(channel))
        await cog.on_voice_state_update(SimpleNamespace(id=BOT_ID), voice_state(channel), voice_state(channel))
        active = cog.active
        await cog.cog_unload()
        return active

    assert asyncio.run(scenario()) is not None

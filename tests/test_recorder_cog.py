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


def test_any_error_posting_the_announcement_still_makes_the_bot_leave_and_answer(conn, tmp_path):
    """Not only an HTTPException: the bot must never stay in the call, even if leaving fails as well."""
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel, vc = make_channel(events, send_error=RuntimeError("session closed"))
        vc.disconnect.side_effect = RuntimeError("not connected")
        interaction = fake_interaction(officer_in(channel), fake_guild())
        await cog.start.callback(cog, interaction)
        return app, cog, vc, interaction

    app, cog, vc, interaction = asyncio.run(scenario())
    vc.disconnect.assert_awaited_once_with(force=True)
    assert cog.active is None and not cog._starting and meeting_dirs(app) == []
    assert "announce" in reply_text(interaction).lower()


def test_if_the_meeting_cannot_be_saved_the_bot_leaves_says_why_and_can_start_again_later(conn, tmp_path):
    """A meetings folder the bot cannot write (a root-owned bind mount, a full disk): it must not stay in the call
    claiming to record, the command must get an answer, and a later start must work once it is fixed."""
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        meetings = app.settings.meetings_dir
        blocker = tmp_path / "not-a-folder"
        blocker.write_text("x")
        app.settings.meetings_dir = blocker / "meetings"         # mkdir fails like a root-owned folder would
        events = []
        channel, vc = make_channel(events)
        interaction = fake_interaction(officer_in(channel), fake_guild())
        await cog.start.callback(cog, interaction)
        failed = cog.active
        app.settings.meetings_dir = meetings                     # fixed
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        again = cog.active is not None
        await cog.cog_unload()
        return events, interaction, channel, failed, again

    events, interaction, channel, failed, again = asyncio.run(scenario())
    assert events[:4] == ["connect", "announce", "disconnect", "announce"] and failed is None and again
    assert "cancelled" in channel.send.await_args_list[1].args[0].lower()   # the call is told nothing is recorded
    assert interaction.followup.send.await_count == 1 and "could not start" in reply_text(interaction)


def test_a_failure_after_the_meeting_was_created_leaves_no_folder_behind(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel, vc = make_channel(events)
        vc.listen = MagicMock(side_effect=discord.ClientException("Already listening."))
        interaction = fake_interaction(officer_in(channel), fake_guild())
        await cog.start.callback(cog, interaction)
        return app, events, cog, interaction

    app, events, cog, interaction = asyncio.run(scenario())
    assert cog.active is None and meeting_dirs(app) == [] and "disconnect" in events
    assert "Already listening" in reply_text(interaction)


def test_an_error_finishing_the_last_audio_files_still_ends_the_recording(conn, tmp_path):
    """Closing a WAV can fail (disk full: wave patches the header on close). The bot must still leave, and the
    meeting must leave `recording`, or it would block every later /record start. The file that was open is
    recovered from disk."""
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel, vc = make_channel(events)
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        cog.active.writer.add(7, FRAME * 10, 1.0)

        def disk_full():
            raise OSError(28, "No space left on device")
        cog.active.writer.close = disk_full
        stop = fake_interaction(officer_in(channel), fake_guild())
        await cog.stop.callback(cog, stop)
        return app, events, cog, stop

    app, events, cog, stop = asyncio.run(scenario())
    assert "disconnect" in events and cog.active is None and "stopped" in reply_text(stop)
    meeting = Meeting.load(meeting_dirs(app)[0])
    assert meeting.status == "queued" and [s["file"] for s in meeting.segments] == ["7/000001.wav"]


def test_an_error_saving_the_stop_still_leaves_the_call_and_replies(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel, vc = make_channel(events)
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        cog.active.meeting.set_status = MagicMock(side_effect=OSError(28, "No space left on device"))
        stop = fake_interaction(officer_in(channel), fake_guild())
        await cog.stop.callback(cog, stop)
        return events, cog, stop

    events, cog, stop = asyncio.run(scenario())
    assert "disconnect" in events and cog.active is None and stop.followup.send.await_count == 1


def test_a_recording_stopped_with_no_audio_fails_and_posts_one_notice(conn, tmp_path):
    summaries = FakeChannel()

    async def scenario():
        cog, app = make_cog(conn, tmp_path, summaries)
        app.channels.set(MEETING_SUMMARIES, 77)
        channel, vc = make_channel([])
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        await cog.stop.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        await cog.post_updates()
        await cog.post_updates()
        return Meeting.load(meeting_dirs(app)[0])

    meeting = asyncio.run(scenario())
    assert meeting.status == "failed" and meeting.error == "no audio was captured" and meeting.posted
    [notice] = summaries.sent
    assert "απέτυχε" in notice.content and "no audio was captured" in notice.content


class Passthrough:
    stats = {"decrypted": 0, "passthrough": 0, "dropped": 0, "errors": 0}

    def decrypt(self, user_id, payload):
        return payload


class SilentDecoder:
    def decode(self, payload, fec=False):
        return b"\x01\x00" * 1920                                  # 20 ms of 48 kHz stereo


def test_audio_that_cannot_be_saved_is_counted_reported_and_announced_once_in_the_call(conn, tmp_path):
    from bot.recording.pipeline import FAILING_AFTER

    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel, vc = make_channel([])
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        channel.send.reset_mock()
        pipeline = cog.active.pipeline
        pipeline._decryptor, pipeline._decoder_factory = Passthrough(), SilentDecoder
        cog.active.writer.add = MagicMock(side_effect=OSError(28, "No space left on device"))
        for _ in range(FAILING_AFTER + 10):
            await asyncio.to_thread(vc.sink.write, SimpleNamespace(id=7), opus_frame())   # never raises
        await settle()
        status = fake_interaction(officer_in(None), fake_guild())
        await cog.status.callback(cog, status)
        stop = fake_interaction(officer_in(channel), fake_guild())
        await cog.stop.callback(cog, stop)
        return channel, reply_text(status), reply_text(stop)

    channel, status, stop = asyncio.run(scenario())
    [warning] = channel.send.await_args_list
    assert "not being saved" in warning.args[0] and "No space left" in warning.args[0]
    assert f"{FAILING_AFTER + 10} audio frames could not be saved" in status
    assert f"{FAILING_AFTER + 10} not saved" in stop


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


def without_decoding(vc):
    vc.sink._pipeline = SimpleNamespace(handle=lambda user_id, opus: None)  # libopus is not needed to test names


def opus_frame(ssrc=99):
    return SimpleNamespace(packet=SimpleNamespace(decrypted_data=b"\xf8\xff\xfe", ssrc=ssrc))


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def speakers_saved(app):
    return json.loads((meeting_dirs(app)[0] / "manifest.json").read_text(encoding="utf-8"))["speakers"]


def test_a_speakers_name_is_saved_as_soon_as_they_first_speak(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel, vc = make_channel([])
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        without_decoding(vc)
        speaker = fake_member(7)
        speaker.display_name = "Μαρία"
        vc.sink.write(speaker, opus_frame())                       # what the voice thread hands the sink
        await settle()
        saved = speakers_saved(app)
        await cog.cog_unload()
        return saved, cog.active.meeting.status

    saved, status = asyncio.run(scenario())
    assert status == "recording" and saved == {"7": "Μαρία"}       # already safe if the bot dies now


def test_a_speaker_known_only_by_id_is_named_from_the_guild_on_the_event_loop(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel, vc = make_channel([])
        speaker = fake_member(7)
        speaker.display_name = "Νίκος"
        channel.guild = fake_guild(cached=speaker)
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        without_decoding(vc)
        vc.sink._id_from_ssrc = {99: 7}.get
        await asyncio.to_thread(vc.sink.write, None, opus_frame(99))   # from another thread, like the voice thread
        await settle()
        saved = speakers_saved(app)
        await cog.cog_unload()
        return saved

    assert asyncio.run(scenario()) == {"7": "Νίκος"}


def test_stop_refreshes_names_but_never_replaces_a_known_name_with_nothing(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel, vc = make_channel([])
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        without_decoding(vc)
        maria, nikos = fake_member(7), fake_member(8)
        maria.display_name, nikos.display_name = "Μαρία", "Νίκος"
        vc.sink.write(maria, opus_frame())
        vc.sink.write(nikos, opus_frame())
        await settle()
        cog.active.writer.add(7, FRAME, 1.0)
        cog.active.writer.add(8, FRAME, 1.0)
        cog.active.writer.add(9, FRAME, 1.0)                        # never identified by name
        renamed = fake_member(8)
        renamed.display_name = "Νίκος Π."
        guild = fake_guild()
        guild.fetch_member = AsyncMock(side_effect=lambda user_id: renamed if user_id == 8 else None)
        await cog.stop.callback(cog, fake_interaction(officer_in(channel), guild))  # Μαρία left before the stop
        return speakers_saved(app)

    assert asyncio.run(scenario()) == {"7": "Μαρία", "8": "Νίκος Π."}


def test_a_name_found_after_the_stop_is_not_written_once_the_job_may_own_the_manifest(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel, vc = make_channel([])
        release = asyncio.Event()
        late = fake_member(7)
        late.display_name = "Μαρία"
        calls = []

        async def fetch(user_id):
            calls.append(user_id)
            if len(calls) == 1:                                     # the lookup started when they spoke is slow
                await release.wait()
                return late
            return None

        channel.guild = fake_guild()
        channel.guild.fetch_member = fetch
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        without_decoding(vc)
        vc.sink.write(SimpleNamespace(id=7), opus_frame())
        await settle()
        cog.active.writer.add(7, FRAME, 1.0)
        await cog.stop.callback(cog, fake_interaction(officer_in(channel), channel.guild))
        release.set()
        await settle()
        return speakers_saved(app)

    assert asyncio.run(scenario()) == {}


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


def test_after_a_restart_a_leftover_recording_is_queued_so_its_audio_is_still_processed(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        left = Meeting.create(app.settings.meetings_dir, 1, 1, datetime(2026, 10, 1, 10, 0, 0))
        left.add_segment({"speaker": 7, "file": "7/000001.wav", "start": 0.0, "duration": 2.0})
        left.set_speaker(7, "Μαρία")                               # saved when they first spoke
        await cog.cog_load()
        await cog.cog_unload()
        interaction = fake_interaction(officer_in(None), fake_guild())
        await cog.status.callback(cog, interaction)
        return Meeting.load(left.directory), reply_text(interaction)

    meeting, status = asyncio.run(scenario())
    assert meeting.status == "queued" and meeting.interrupted
    assert meeting.speakers == {7: "Μαρία"}                        # recovery keeps the names
    assert "restart" in status.lower()


def test_a_recovered_meeting_without_names_gets_them_once_the_bot_is_ready_and_only_then_is_queued(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        ready = asyncio.Event()
        cog.bot.wait_until_ready = ready.wait
        speaker = fake_member(7)
        speaker.display_name = "Μαρία"
        voice_channel = SimpleNamespace(guild=fake_guild(cached=speaker))
        cog.bot.get_channel = lambda channel_id: voice_channel if channel_id == 55 else None
        left = Meeting.create(app.settings.meetings_dir, 55, 1, datetime(2026, 10, 1, 10, 0, 0))
        left.add_segment({"speaker": 7, "file": "7/000001.wav", "start": 0.0, "duration": 2.0})  # by an older version
        await cog.cog_load()
        before = Meeting.load(left.directory)
        ready.set()
        await cog._recovery
        await cog.cog_unload()
        return before, Meeting.load(left.directory)

    before, after = asyncio.run(scenario())
    assert before.status == "recording" and before.interrupted     # the job cannot pick it up yet
    assert after.status == "queued" and after.speakers == {7: "Μαρία"}


def test_a_recovered_meeting_is_queued_even_if_its_names_cannot_be_found(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)                          # the meeting's channel is gone
        left = Meeting.create(app.settings.meetings_dir, 55, 1, datetime(2026, 10, 1, 10, 0, 0))
        left.add_segment({"speaker": 7, "file": "7/000001.wav", "start": 0.0, "duration": 2.0})
        await cog.cog_load()
        await cog._recovery
        await cog.cog_unload()
        return Meeting.load(left.directory)

    meeting = asyncio.run(scenario())
    assert meeting.status == "queued" and meeting.speakers == {}     # the transcript falls back to the id


def test_a_new_recording_waits_for_a_recovered_meeting_but_not_for_an_empty_one(conn, tmp_path):
    async def scenario(with_audio):
        cog, app = make_cog(conn, tmp_path / str(with_audio))
        left = Meeting.create(app.settings.meetings_dir, 1, 1, datetime(2026, 10, 1, 10, 0, 0))
        if with_audio:
            left.add_segment({"speaker": 7, "file": "7/000001.wav", "start": 0.0, "duration": 2.0})
        await cog.cog_load()
        channel, _ = make_channel([])
        interaction = fake_interaction(officer_in(channel), fake_guild())
        await cog.start.callback(cog, interaction)
        started = cog.active is not None
        await cog.cog_unload()
        return started

    assert asyncio.run(scenario(True)) is False
    assert asyncio.run(scenario(False)) is True


def test_an_empty_interrupted_recording_posts_one_failure_notice(conn, tmp_path):
    summaries = FakeChannel()

    async def scenario():
        cog, app = make_cog(conn, tmp_path, summaries)
        app.channels.set(MEETING_SUMMARIES, 77)
        left = Meeting.create(app.settings.meetings_dir, 1, 1, datetime(2026, 10, 1, 10, 0, 0))
        await cog.cog_load()
        await cog.cog_unload()
        await cog.post_updates()
        await cog.post_updates()
        return Meeting.load(left.directory)

    meeting = asyncio.run(scenario())
    [notice] = summaries.sent
    assert meeting.posted and "απέτυχε" in notice.content and "no audio was captured" in notice.content
    assert "επανεκκίνηση" in notice.content


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


def test_a_broken_manifest_does_not_stop_the_other_meetings(conn, tmp_path):
    summaries = FakeChannel()

    async def scenario():
        cog, app = make_cog(conn, tmp_path, summaries)
        app.channels.set(MEETING_SUMMARIES, 77)
        broken = app.settings.meetings_dir / "20261001-100000"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text("{not json", encoding="utf-8")
        meeting = meeting_with(app.settings.meetings_dir, "done")
        await cog.cog_load()                                         # startup recovery reads every manifest
        await cog.cog_unload()
        await cog.poster.coro(cog)                                   # one turn of the loop, as it runs
        channel, _ = make_channel([])
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        started = cog.active is not None
        await cog.cog_unload()
        return Meeting.load(meeting.directory), started

    meeting, started = asyncio.run(scenario())
    assert meeting.posted and len(summaries.sent) == 2 and started


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


class Gate:
    """Stands in for asyncio.sleep: the wait before an empty call stops ends only when the test opens it."""

    def __init__(self):
        self.waits = []
        self._open = asyncio.Event()

    async def sleep(self, seconds):
        self.waits.append(seconds)
        await self._open.wait()

    async def open(self):
        self._open.set()
        await settle()


def test_when_everyone_leaves_the_call_the_recording_stops_after_a_grace_period(conn, tmp_path):
    from bot.cogs.recorder import EMPTY_GRACE

    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate.sleep
        channel, _ = make_channel([])
        await recording(cog, channel)
        channel.voice_states = {4: object(), BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=3), voice_state(channel), voice_state(None))
        await settle()
        someone_left = cog.active is not None and gate.waits == []      # someone is still there: no wait at all
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=4), voice_state(channel), voice_state(None))
        await settle()
        waiting = cog.active is not None                                # not instantly
        await gate.open()
        return app, channel, cog, someone_left, waiting, gate.waits

    app, channel, cog, someone_left, waiting, waits = asyncio.run(scenario())
    assert someone_left and waiting and waits == [EMPTY_GRACE] and cog.active is None
    assert Meeting.load(meeting_dirs(app)[0]).status == "queued"
    assert channel.send.await_count == 1 and "σταμάτησε" in channel.send.await_args.args[0]


def test_someone_whose_connection_drops_and_who_rejoins_in_time_keeps_the_recording_going(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate.sleep
        channel, _ = make_channel([])
        await recording(cog, channel)
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=3), voice_state(channel), voice_state(None))
        await cog.on_voice_state_update(SimpleNamespace(id=3), voice_state(channel), voice_state(None))  # repeated
        await settle()
        channel.voice_states = {3: object(), BOT_ID: object()}         # back before the wait is over
        await gate.open()
        active = cog.active
        await cog.cog_unload()
        return active, gate.waits, channel

    active, waits, channel = asyncio.run(scenario())
    assert active is not None and len(waits) == 1 and channel.send.await_count == 0


def test_other_bots_left_in_the_call_do_not_keep_it_recording(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        cog._sleep = AsyncMock()
        channel, _ = make_channel([])
        music_bot = fake_member(5, bot=True)                             # a music bot stays in the call
        channel.guild.get_member = lambda user_id: music_bot if user_id == 5 else None  # only 5 is a bot
        channel.voice_states = {3: object(), 5: object(), BOT_ID: object()}
        await recording(cog, channel)
        await settle()
        before = cog.active is not None                                  # the officer still counts as a person
        channel.voice_states = {5: object(), BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=3), voice_state(channel), voice_state(None))
        await settle()
        return before, cog

    before, cog = asyncio.run(scenario())
    assert before and cog.active is None


def test_another_bot_joining_an_empty_call_does_not_cancel_the_grace_period(conn, tmp_path):
    """Other bots do not count as people, so one joining must not reset the wait (spec: Automatic stop)."""
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate.sleep
        channel, _ = make_channel([])
        music_bot = fake_member(5, bot=True)
        channel.guild.get_member = lambda user_id: music_bot if user_id == 5 else None  # only 5 is a bot
        await recording(cog, channel)
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=3), voice_state(channel), voice_state(None))
        await settle()
        channel.voice_states = {5: object(), BOT_ID: object()}                 # a music bot joins meanwhile
        await cog.on_voice_state_update(SimpleNamespace(id=5), voice_state(None), voice_state(channel))
        await settle()
        await gate.open()
        return app, cog

    app, cog = asyncio.run(scenario())
    assert cog.active is None and Meeting.load(meeting_dirs(app)[0]).status == "queued"


def test_a_stop_during_the_grace_period_is_not_repeated_when_it_ends(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate.sleep
        events = []
        channel, vc = make_channel(events)
        await recording(cog, channel)
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=3), voice_state(channel), voice_state(None))
        await settle()
        await cog.stop.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        await gate.open()
        return events, channel

    events, channel = asyncio.run(scenario())
    assert events.count("disconnect") == 1 and channel.send.await_count == 0


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


class Gates:
    """Like Gate, but each wait has its own gate, so one grace period can end while a later one goes on."""

    def __init__(self):
        self.waits = []
        self._gates = []

    async def sleep(self, seconds):
        self.waits.append(seconds)
        gate = asyncio.Event()
        self._gates.append(gate)
        await gate.wait()

    async def open(self, number):
        self._gates[number].set()
        await settle()


def test_someone_who_rejoins_and_leaves_again_gets_a_whole_new_grace_period(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gates = Gates()
        cog._sleep = gates.sleep
        channel, _ = make_channel([])
        await recording(cog, channel)
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=3), voice_state(channel), voice_state(None))
        await settle()
        channel.voice_states = {3: object(), BOT_ID: object()}                 # back in time
        await cog.on_voice_state_update(SimpleNamespace(id=3), voice_state(None), voice_state(channel))
        await settle()
        channel.voice_states = {BOT_ID: object()}                              # and gone again, a while later
        await cog.on_voice_state_update(SimpleNamespace(id=3), voice_state(channel), voice_state(None))
        await settle()
        await gates.open(0)                                                    # when the first wait would end
        kept = cog.active is not None
        await gates.open(1)
        return kept, cog, gates.waits

    kept, cog, waits = asyncio.run(scenario())
    assert kept and cog.active is None and len(waits) == 2


def test_two_starts_at_once_record_only_once(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel, vc = make_channel(events)
        joined = asyncio.Event()

        async def slow_connect(**kw):
            events.append("connect")
            await joined.wait()
            return vc
        channel.connect.side_effect = slow_connect
        first = fake_interaction(officer_in(channel), fake_guild())
        second = fake_interaction(officer_in(channel), fake_guild())
        task = asyncio.create_task(cog.start.callback(cog, first))
        await settle()
        again = asyncio.create_task(cog.start.callback(cog, second))           # while the first is still joining
        await settle()
        joined.set()
        await asyncio.gather(task, again)
        await cog.cog_unload()
        return app, events, second

    app, events, second = asyncio.run(scenario())
    assert events.count("connect") == 1 and len(meeting_dirs(app)) == 1
    assert "already" in reply_text(second).lower()


def test_a_join_that_fails_unexpectedly_leaves_no_voice_client_behind(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel, vc = make_channel(events)
        leftover = FakeVoiceClient(events)                                     # registered before the handshake
        channel.guild.voice_client = leftover
        channel.connect.side_effect = RuntimeError("voice handshake failed")
        interaction = fake_interaction(officer_in(channel), fake_guild())
        await cog.start.callback(cog, interaction)
        channel.guild.voice_client = None
        channel.connect.side_effect = lambda **kw: events.append("connect") or vc
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        again = cog.active is not None
        await cog.cog_unload()
        return app, leftover, interaction, again

    app, leftover, interaction, again = asyncio.run(scenario())
    leftover.disconnect.assert_awaited_once_with(force=True)
    assert "couldn't join" in reply_text(interaction) and again and len(meeting_dirs(app)) == 1


def test_an_officer_who_leaves_while_the_bot_joins_still_gets_the_empty_call_stop(conn, tmp_path):
    from bot.cogs.recorder import EMPTY_GRACE

    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate.sleep
        events = []
        channel, vc = make_channel(events)

        def connect(**kw):
            channel.voice_states = {BOT_ID: object()}                          # left before the recording began
            return vc
        channel.connect.side_effect = connect
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        await settle()
        waits = list(gate.waits)
        await gate.open()
        return app, cog, waits

    app, cog, waits = asyncio.run(scenario())
    assert waits == [EMPTY_GRACE] and cog.active is None
    assert Meeting.load(meeting_dirs(app)[0]).status == "failed"               # stopped with no audio


def test_a_malformed_manifest_does_not_stop_the_later_meetings_from_being_posted(conn, tmp_path):
    summaries = FakeChannel()

    async def scenario():
        cog, app = make_cog(conn, tmp_path, summaries)
        app.channels.set(MEETING_SUMMARIES, 77)
        broken = meeting_with(app.settings.meetings_dir, "done")
        manifest = json.loads((broken.directory / "manifest.json").read_text(encoding="utf-8"))
        del manifest["started_at"]                                             # edited by hand
        (broken.directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        meeting = meeting_with(app.settings.meetings_dir, "done")
        await cog.post_updates()
        return Meeting.load(meeting.directory)

    meeting = asyncio.run(scenario())
    assert meeting.posted and len(summaries.sent) == 2

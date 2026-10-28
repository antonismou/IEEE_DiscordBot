"""REVIEW reproductions for the bot side of the meeting recorder (adversarial review, 2026-10-28).

Every test here FAILS on the current code, for the reason in its docstring.
"""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

from bot.recording.meeting import Meeting, find_busy
from bot.recording.pipeline import SpeakerPipeline
from bot.recording.posting import post_update
from bot.recording.segments import SegmentWriter
from bot.recording.sink import MeetingSink
from tests.fakes import fake_guild, fake_interaction
from tests.test_recorder_cog import make_channel, make_cog, officer_in
from tests.test_recording_posting import FakeChannel, meeting_with

FRAME = b"\x01\x00" * 320


def test_review_an_unwritable_meetings_folder_leaves_the_bot_in_the_call_with_no_reply(conn, tmp_path):
    """HIGH. deploy/README.md says a missing bind-mount folder is created root-owned and "/record start fails with a
    permission error". What actually happens: the bot joins, posts "Recording started" in the channel, then
    Meeting.create raises inside _begin. The voice client is never disconnected (the bot sits in the call
    claiming to record), the deferred interaction never gets a reply, and every later /record start fails with
    "couldn't join" (discord.py: already connected) until the bot restarts."""
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        blocker = tmp_path / "not-a-folder"
        blocker.write_text("x")
        app.settings.meetings_dir = blocker / "meetings"      # mkdir fails like a root-owned folder would
        events = []
        channel, vc = make_channel(events)
        interaction = fake_interaction(officer_in(channel), fake_guild())
        try:
            await cog.start.callback(cog, interaction)
        except Exception:
            pass
        return events, interaction

    events, interaction = asyncio.run(scenario())
    assert "disconnect" in events
    assert interaction.followup.send.await_count == 1


def test_review_an_error_closing_the_last_segments_keeps_the_meeting_busy_forever(conn, tmp_path):
    """MEDIUM. RecorderCog._end runs writer.close() outside its try/finally. If closing a WAV fails (disk full:
    wave patches the header on close; or add_segment's manifest write fails), the voice client is never
    disconnected and the status is never changed from `recording`: find_busy() then refuses every /record start
    until the bot is restarted, and the job keeps doing live passes on it."""
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel, vc = make_channel(events)
        await cog.start.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        cog.active.writer.add(7, FRAME, 1.0)

        def disk_full():
            raise OSError(28, "No space left on device")
        cog.active.writer.close = disk_full
        try:
            await cog.stop.callback(cog, fake_interaction(officer_in(channel), fake_guild()))
        except OSError:
            pass
        return app, events

    app, events = asyncio.run(scenario())
    assert "disconnect" in events
    assert find_busy(app.settings.meetings_dir) is None or \
        find_busy(app.settings.meetings_dir).status != "recording"


class _Passthrough:
    def decrypt(self, user_id, payload):
        return payload


class _Decoder:
    def decode(self, payload, fec=False):
        return b"\x01\x00" * 1920


class _FullDisk:
    segments = []

    def add(self, *args):
        raise OSError(28, "No space left on device")


def test_review_a_write_error_escapes_the_sink_and_stops_receiving_for_everyone():
    """MEDIUM. voice_recv's PacketRouter calls sink.write() with no try/except; an exception ends the router
    thread and calls stop_listening(). SpeakerPipeline catches opus decode errors but not the writer's: one
    OSError (disk full, EIO on the bind mount) silently ends the recording for the rest of the meeting while the
    bot stays in the call and /record status still says "Recording". The sink must never raise."""
    pipeline = SpeakerPipeline(_Passthrough(), _Decoder, _FullDisk(), lambda: 1.0)
    sink = MeetingSink(pipeline, lambda ssrc: None)
    data = SimpleNamespace(packet=SimpleNamespace(decrypted_data=b"\xfc\x00", ssrc=1))
    sink.write(SimpleNamespace(id=7), data)               # must not raise


def test_review_audio_arriving_after_close_creates_an_untracked_wav(tmp_path):
    """LOW. voice_recv's stop_listening() only signals the router thread (the real stop runs on another thread), so
    a frame already popped from the jitter buffer can reach writer.add() after _end called writer.close(). The
    writer then opens a new WAV that is never closed, never in the manifest, never transcribed or deleted."""
    writer = SegmentWriter(tmp_path)
    writer.add(7, FRAME, 0.0)
    writer.close()
    writer.add(7, FRAME, 0.02)                            # the late frame
    assert sorted(p.name for p in tmp_path.rglob("*.wav")) == ["000001.wav"]


def test_review_a_reprocessed_meeting_is_never_posted(tmp_path):
    """MEDIUM. deploy/README.md: "To reprocess a meeting, set its status in manifest.json back to queued". But
    posting.json still says posted=True (the failure notice, or the first summary), so post_update returns at
    once and the new summary is never posted. The typical case is a meeting that failed (Ollama model not
    pulled, the PyAV bug) and is reprocessed after the fix."""
    meeting = meeting_with(tmp_path, "failed")
    channel = FakeChannel()
    asyncio.run(post_update(channel, meeting))            # the failure notice
    assert Meeting.load(meeting.directory).posted

    Meeting.load(meeting.directory).set_status("queued")  # README's reprocess step; the job then finishes it
    Meeting.load(meeting.directory).set_status("done")
    before = len(channel.sent)
    asyncio.run(post_update(channel, Meeting.load(meeting.directory)))
    assert len(channel.sent) > before

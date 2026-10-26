import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.ext import voice_recv

from bot.channels import ChannelRepo
from bot.recording.meeting import Meeting
from tests.fakes import fake_guild, fake_interaction, fake_member, reply_text

FRAME = b"\x01\x00" * 320


class FakeVoiceClient:
    def __init__(self, events):
        self.events = events
        self.sink = None
        self.disconnect = AsyncMock(side_effect=lambda: events.append("disconnect"))
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
    channel.connect = AsyncMock(side_effect=lambda **kw: events.append("connect") or vc)
    channel.send = AsyncMock(side_effect=lambda *a, **kw: events.append("announce"))
    if send_error:
        channel.send.side_effect = send_error
    return channel, vc


def make_cog(conn, tmp_path):
    from bot.cogs.recorder import RecorderCog

    app = SimpleNamespace(
        settings=SimpleNamespace(meetings_dir=tmp_path / "meetings"),
        channels=ChannelRepo(conn),
        server=SimpleNamespace(officer_role_id=9),
    )
    return RecorderCog(SimpleNamespace(app=app)), app


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
    assert {c.name for c in group.commands} == {"start", "stop"}
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
    assert manifest["status"] == "done" and manifest["speakers"] == {"7": "Μαρία"}
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

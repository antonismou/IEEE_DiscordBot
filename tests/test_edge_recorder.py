"""Edge cases for the recorder cog (bot/cogs/recorder.py), written from the design spec, treating the cog as a
black box driven through its commands and voice state events."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import bot.cogs.recorder as recorder
from bot.channels import ChannelRepo
from bot.recording.meeting import Meeting
from tests.fakes import fake_interaction, fake_member, reply_text

BOT_ID = 999
OFFICER = 3
FRAME = b"\x01\x00" * 320


class VoiceClient:
    def __init__(self, guild, events):
        self.guild, self.events = guild, events
        self.sink = None
        self._connection = SimpleNamespace(dave_protocol_version=0, dave_session=None)
        self.disconnects = 0

    def listen(self, sink):
        self.events.append("listen")
        self.sink = sink

    def stop_listening(self):
        self.events.append("stop_listening")

    def _get_id_from_ssrc(self, ssrc):
        return None

    async def disconnect(self, force=False):
        self.disconnects += 1
        self.events.append("disconnect")
        if self.guild.voice_client is self:
            self.guild.voice_client = None


class Guild:
    """Members the cache knows are in `members`; everyone else is unknown (no members intent)."""

    def __init__(self, members=None):
        self.members = members or {}
        self.voice_client = None
        self.fetch_member = AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone"))

    def get_member(self, user_id):
        return self.members.get(user_id)


def make_channel(guild, events, people=(OFFICER,)):
    channel = SimpleNamespace(id=55, mention="<#55>", guild=guild, sent=[])
    channel.voice_states = {user_id: object() for user_id in (*people, BOT_ID)}

    async def connect(cls=None, **kwargs):
        events.append("connect")
        client = VoiceClient(guild, events)
        guild.voice_client = client
        channel.client = client
        return client

    async def send(content=None, **kwargs):
        channel.sent.append(content)

    channel.connect = connect
    channel.send = send
    return channel


def make_cog(conn, tmp_path):
    app = SimpleNamespace(
        settings=SimpleNamespace(meetings_dir=tmp_path / "meetings"),
        channels=ChannelRepo(conn),
        server=SimpleNamespace(officer_role_id=9),
    )
    bot = SimpleNamespace(app=app, user=SimpleNamespace(id=BOT_ID), wait_until_ready=AsyncMock(),
                          get_channel=lambda channel_id: None, fetch_channel=AsyncMock())
    cog = recorder.RecorderCog(bot)
    return cog, app


def officer_in(channel):
    user = fake_member(OFFICER, role_ids=(9,))
    user.voice = SimpleNamespace(channel=channel) if channel else None
    return user


def interaction_for(channel):
    return fake_interaction(officer_in(channel), channel.guild if channel else Guild())


def folders(app):
    root = app.settings.meetings_dir
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


def status_in(folder):
    return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["status"]


def state(channel):
    return SimpleNamespace(channel=channel)


class Gate:
    """Replaces the cog's sleep: each wait blocks until released, and records how long it was asked to wait."""

    def __init__(self):
        self.waits = []
        self.events = []

    async def __call__(self, seconds):
        event = asyncio.Event()
        self.waits.append(seconds)
        self.events.append(event)
        await event.wait()

    async def release(self, index=-1):
        self.events[index].set()
        await settle()


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def start_recording(cog, channel, with_audio=True):
    interaction = interaction_for(channel)
    await cog.start.callback(cog, interaction)
    assert cog.active is not None, reply_text(interaction)
    if with_audio:
        cog.active.writer.add(7, FRAME, 1.0)
        cog.active.writer.flush_idle(100.0)
    return interaction


# --- start --------------------------------------------------------------------------------------------------------

def test_start_while_recording_is_refused_and_does_not_join_again(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel = make_channel(Guild(), events)
        await start_recording(cog, channel)
        second = interaction_for(channel)
        await cog.start.callback(cog, second)
        count = events.count("connect")
        await cog.cog_unload()
        return app, second, count

    app, second, count = asyncio.run(scenario())
    assert count == 1 and "already" in reply_text(second).lower() and len(folders(app)) == 1


def test_start_while_another_start_is_still_joining_is_refused(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        guild = Guild()
        channel = make_channel(guild, events)
        joined = asyncio.Event()
        real_connect = channel.connect

        async def slow_connect(**kwargs):
            await joined.wait()
            return await real_connect(**kwargs)

        channel.connect = slow_connect
        first = asyncio.create_task(cog.start.callback(cog, interaction_for(channel)))
        await settle()
        second = interaction_for(channel)
        await cog.start.callback(cog, second)
        joined.set()
        await first
        result = (events.count("connect"), reply_text(second).lower(), len(folders(app)), cog.active is not None)
        await cog.cog_unload()
        return result

    connects, text, count, active = asyncio.run(scenario())
    assert connects == 1 and "already" in text and count == 1 and active


@pytest.mark.parametrize("status,refused", [
    ("recording", True), ("queued", True), ("transcribing", True), ("summarizing", True),
    ("done", False), ("failed", False),
])
def test_start_with_a_meeting_on_disk_is_refused_only_while_it_is_busy(conn, tmp_path, status, refused):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        Meeting.create(app.settings.meetings_dir, 1, 1, datetime(2026, 10, 1, 10, 0, 0)).set_status(status)
        events = []
        channel = make_channel(Guild(), events)
        interaction = interaction_for(channel)
        await cog.start.callback(cog, interaction)
        result = ("connect" in events, reply_text(interaction).lower(), cog.active is not None)
        await cog.cog_unload()
        return result

    connected, text, active = asyncio.run(scenario())
    if refused:
        assert not connected and "already" in text and not active
    else:
        assert connected and active


@pytest.mark.parametrize("error", [
    asyncio.TimeoutError(), discord.ClientException("Already connected to a voice channel."),
    OSError("network down"), RuntimeError("opus missing"),
    discord.errors.ConnectionClosed(SimpleNamespace(close_code=4006), shard_id=None),
])
def test_a_failed_join_of_any_kind_leaves_no_voice_client_and_a_later_start_works(conn, tmp_path, error):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        guild = Guild()
        channel = make_channel(guild, events)
        real_connect = channel.connect

        async def failing(**kwargs):
            # discord.py registers the client before connecting and, except on a timeout, leaves it there
            if not isinstance(error, asyncio.TimeoutError):
                guild.voice_client = VoiceClient(guild, events)
            raise error

        channel.connect = failing
        interaction = interaction_for(channel)
        await cog.start.callback(cog, interaction)
        after_failure = (guild.voice_client, cog.active, cog._starting, folders(app))
        channel.connect = real_connect
        await cog.start.callback(cog, interaction_for(channel))
        again = cog.active is not None
        await cog.cog_unload()
        return after_failure, interaction, again

    (client, active, starting, made), interaction, again = asyncio.run(scenario())
    assert client is None and active is None and starting is False and made == []
    assert "couldn't join" in reply_text(interaction).lower()
    assert again


def test_a_starter_who_leaves_while_the_bot_joins_ends_the_recording_after_the_grace_period(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate
        events = []
        guild = Guild()
        channel = make_channel(guild, events)
        real_connect = channel.connect

        async def connect_while_they_leave(**kwargs):
            client = await real_connect(**kwargs)
            channel.voice_states = {BOT_ID: object()}           # the officer left; the event came before we listened
            return client

        channel.connect = connect_while_they_leave
        await cog.start.callback(cog, interaction_for(channel))
        await settle()
        waiting = cog.active is not None and gate.waits == [recorder.EMPTY_GRACE]
        await gate.release()
        return app, channel, waiting, cog.active

    app, channel, waiting, active = asyncio.run(scenario())
    assert waiting and active is None
    [folder] = folders(app)
    assert status_in(folder) == "failed"                                 # nothing was said
    assert channel.sent[-1] == recorder.STOPPED["empty"]


# --- stop ---------------------------------------------------------------------------------------------------------

def test_stop_with_nothing_active_says_so_and_touches_nothing(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        Meeting.create(app.settings.meetings_dir, 1, 1, datetime(2026, 10, 1, 10, 0, 0)).set_status("done")
        interaction = interaction_for(make_channel(Guild(), []))
        await cog.stop.callback(cog, interaction)
        return app, interaction

    app, interaction = asyncio.run(scenario())
    assert "no recording" in reply_text(interaction).lower()
    assert [status_in(f) for f in folders(app)] == ["done"]


def test_stop_twice_ends_once_and_the_second_says_nothing_is_recording(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel = make_channel(Guild(), events)
        await start_recording(cog, channel)
        first, second = interaction_for(channel), interaction_for(channel)
        await asyncio.gather(cog.stop.callback(cog, first), cog.stop.callback(cog, second))
        return app, events, first, second

    app, events, first, second = asyncio.run(scenario())
    texts = sorted([reply_text(first), reply_text(second)])
    assert sum("no recording" in t.lower() for t in texts) == 1
    assert events.count("disconnect") == 1 and events.count("stop_listening") == 1
    [folder] = folders(app)
    assert status_in(folder) == "queued"


def test_stop_then_start_again_makes_a_new_meeting_once_the_last_is_finished(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel = make_channel(Guild(), [])
        await start_recording(cog, channel, with_audio=False)
        await cog.stop.callback(cog, interaction_for(channel))          # no audio: failed, blocks nothing
        await start_recording(cog, channel)
        result = [status_in(f) for f in folders(app)]
        await cog.cog_unload()
        return result

    assert asyncio.run(scenario()) == ["failed", "recording"]


# --- automatic stop -----------------------------------------------------------------------------------------------

def test_the_call_emptying_stops_after_the_grace_period_and_says_so(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate
        events = []
        channel = make_channel(Guild(), events)
        await start_recording(cog, channel)
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(channel), state(None))
        await settle()
        still = cog.active is not None
        await gate.release()
        return app, channel, events, gate, still, cog.active

    app, channel, events, gate, still, active = asyncio.run(scenario())
    assert still and active is None and gate.waits == [recorder.EMPTY_GRACE]
    assert channel.sent[-1] == recorder.STOPPED["empty"] and events.count("disconnect") == 1
    [folder] = folders(app)
    assert status_in(folder) == "queued"


def test_a_rejoin_cancels_the_wait_and_the_next_empty_call_gets_the_whole_grace_period_again(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate
        channel = make_channel(Guild(), [])
        await start_recording(cog, channel)
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(channel), state(None))
        await settle()
        channel.voice_states = {OFFICER: object(), BOT_ID: object()}  # their connection came back
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(None), state(channel))
        await settle()
        await gate.release(0)                                           # the first wait is over: must not stop
        survived = cog.active is not None
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(channel), state(None))
        await settle()
        second_wait = list(gate.waits)
        await gate.release(-1)
        return survived, second_wait, cog.active

    survived, waits, active = asyncio.run(scenario())
    assert survived
    assert waits == [recorder.EMPTY_GRACE, recorder.EMPTY_GRACE]        # a fresh, full wait
    assert active is None


def test_a_rejoin_from_another_channel_also_cancels_the_wait(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate
        channel = make_channel(Guild(), [])
        other = SimpleNamespace(id=66)
        await start_recording(cog, channel)
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(channel), state(other))
        await settle()
        channel.voice_states = {OFFICER: object(), BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(other), state(channel))
        await settle()
        await gate.release(0)
        active = cog.active is not None
        await cog.cog_unload()
        return active

    assert asyncio.run(scenario())


def test_other_bots_in_the_call_do_not_count_as_people(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate
        music = fake_member(500, bot=True)
        channel = make_channel(Guild({500: music}), [], people=(OFFICER, 500))
        await start_recording(cog, channel)
        channel.voice_states = {500: object(), BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(channel), state(None))
        await settle()
        waited = list(gate.waits)
        await gate.release()
        return waited, cog.active

    waited, active = asyncio.run(scenario())
    assert waited == [recorder.EMPTY_GRACE] and active is None


def test_a_bot_joining_during_the_wait_does_not_cancel_it(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate
        music = fake_member(500, bot=True)
        channel = make_channel(Guild({500: music}), [])
        await start_recording(cog, channel)
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(channel), state(None))
        await settle()
        channel.voice_states = {500: object(), BOT_ID: object()}
        await cog.on_voice_state_update(music, state(None), state(channel))
        await settle()
        await gate.release(0)
        return cog.active

    assert asyncio.run(scenario()) is None


def test_someone_the_member_cache_does_not_know_counts_as_a_person(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate
        channel = make_channel(Guild(), [], people=(OFFICER, 4242))
        await start_recording(cog, channel)
        channel.voice_states = {4242: object(), BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(channel), state(None))
        await settle()
        result = (gate.waits, cog.active is not None)
        await cog.cog_unload()
        return result

    waits, active = asyncio.run(scenario())
    assert waits == [] and active


def test_the_bot_kicked_from_the_call_stops_at_once(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate
        events = []
        channel = make_channel(Guild(), events)
        await start_recording(cog, channel)
        channel.voice_states = {OFFICER: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=BOT_ID), state(channel), state(None))
        await settle()
        return app, channel, events, gate, cog.active

    app, channel, events, gate, active = asyncio.run(scenario())
    assert active is None and gate.waits == [] and channel.sent[-1] == recorder.STOPPED["kicked"]
    [folder] = folders(app)
    assert status_in(folder) == "queued" and events.count("disconnect") == 1


def test_the_bot_moved_to_another_channel_stops_too(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel = make_channel(Guild(), [])
        await start_recording(cog, channel)
        await cog.on_voice_state_update(SimpleNamespace(id=BOT_ID), state(channel), state(SimpleNamespace(id=66)))
        await settle()
        return cog.active

    assert asyncio.run(scenario()) is None


def test_the_bots_own_mute_in_the_same_call_does_not_stop_it(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        channel = make_channel(Guild(), [])
        await start_recording(cog, channel)
        await cog.on_voice_state_update(SimpleNamespace(id=BOT_ID), state(channel), state(channel))
        await settle()
        active = cog.active is not None
        await cog.cog_unload()
        return active

    assert asyncio.run(scenario())


def test_stop_during_the_grace_period_ends_once_and_the_wait_does_nothing_later(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate
        events = []
        channel = make_channel(Guild(), events)
        await start_recording(cog, channel)
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(channel), state(None))
        await settle()
        await cog.stop.callback(cog, interaction_for(channel))
        gate.events[0].set()
        await settle()
        return channel, events

    channel, events = asyncio.run(scenario())
    assert events.count("disconnect") == 1 and recorder.STOPPED["empty"] not in channel.sent


def test_kicked_during_the_grace_period_ends_once(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        gate = Gate()
        cog._sleep = gate
        events = []
        channel = make_channel(Guild(), events)
        await start_recording(cog, channel)
        channel.voice_states = {BOT_ID: object()}
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(channel), state(None))
        await settle()
        await cog.on_voice_state_update(SimpleNamespace(id=BOT_ID), state(channel), state(None))
        await settle()
        gate.events[0].set()
        await settle()
        return channel, events

    channel, events = asyncio.run(scenario())
    assert events.count("disconnect") == 1
    assert channel.sent.count(recorder.STOPPED["kicked"]) == 1 and recorder.STOPPED["empty"] not in channel.sent


def test_events_after_the_recording_ended_are_ignored(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        channel = make_channel(Guild(), events)
        await start_recording(cog, channel)
        await cog.stop.callback(cog, interaction_for(channel))
        channel.voice_states = {}
        await cog.on_voice_state_update(SimpleNamespace(id=BOT_ID), state(channel), state(None))
        await cog.on_voice_state_update(SimpleNamespace(id=OFFICER), state(channel), state(None))
        await settle()
        return channel, events

    channel, events = asyncio.run(scenario())
    assert events.count("disconnect") == 1 and not any(text in channel.sent for text in recorder.STOPPED.values())


def test_a_failed_join_whose_cleanup_also_fails_still_answers_and_allows_a_new_start(conn, tmp_path):
    async def scenario():
        cog, app = make_cog(conn, tmp_path)
        events = []
        guild = Guild()
        channel = make_channel(guild, events)
        real_connect = channel.connect

        class Stuck(VoiceClient):
            async def disconnect(self, force=False):
                raise RuntimeError("already gone")

        async def failing(**kwargs):
            guild.voice_client = Stuck(guild, events)
            raise discord.ClientException("handshake failed")

        channel.connect = failing
        interaction = interaction_for(channel)
        await cog.start.callback(cog, interaction)
        starting = cog._starting
        guild.voice_client = None                                         # the library dropped it eventually
        channel.connect = real_connect
        await cog.start.callback(cog, interaction_for(channel))
        result = (interaction, starting, cog.active is not None)
        await cog.cog_unload()
        return result

    interaction, starting, again = asyncio.run(scenario())
    assert "couldn't join" in reply_text(interaction).lower() and starting is False and again

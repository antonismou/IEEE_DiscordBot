from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands, tasks, voice_recv

from bot.audit import audit
from bot.channels import MEETING_SUMMARIES
from bot.checks import officer_only
from bot.recording.dave import DaveDecryptor
from bot.recording.meeting import (
    NO_AUDIO_STOPPED, STALE, Meeting, adopt_orphans, all_meetings, end_interrupted, find_busy, latest,
    recover_interrupted,
)
from bot.recording.pipeline import SpeakerPipeline
from bot.recording.posting import FINAL, STATUS_TEXT, needs_update, post_update
from bot.recording.segments import SegmentWriter
from bot.recording.sink import MeetingSink
from bot.roles import resolve_member

log = logging.getLogger(__name__)

# The recording is announced in the voice channel's own chat before any audio is captured.
ANNOUNCEMENT = (
    "🔴 **Recording started.** This call is being recorded to produce a transcript and a summary. "
    "Leave the channel if you do not agree.\n"
    "🔴 **Ξεκίνησε η ηχογράφηση.** Η συζήτηση ηχογραφείται για απομαγνητοφώνηση και σύνοψη. "
    "Αποχωρήστε από το κανάλι αν διαφωνείτε."
)
STOPPED = {  # posted in the same chat when the recording ends without /record stop
    "kicked": (
        "⏹️ **Recording stopped:** I was disconnected from the call. What was recorded so far will be summarized.\n"
        "⏹️ **Η ηχογράφηση σταμάτησε:** αποσυνδέθηκα από την κλήση. Όσα ηχογραφήθηκαν θα συνοψιστούν."
    ),
    "empty": (
        "⏹️ **Recording stopped:** everyone left the call. The meeting will be summarized.\n"
        "⏹️ **Η ηχογράφηση σταμάτησε:** όλοι αποχώρησαν από την κλήση. Η συνάντηση θα συνοψιστεί."
    ),
}
CANCELLED = (  # posted in the same chat when the recording was announced but could not start
    "⏹️ **Recording cancelled:** it could not start, so nothing was recorded.\n"
    "⏹️ **Η ηχογράφηση ακυρώθηκε:** δεν μπόρεσε να ξεκινήσει, οπότε δεν ηχογραφήθηκε τίποτα."
)
START_FAILED = (
    "The recording could not start ({error}), so I left the call and nothing was recorded. Check that the bot can "
    "write to the meetings folder (`meetings_dir`); the bot's log has the details.\n"
    "Η ηχογράφηση δεν ξεκίνησε ({error}): αποχώρησα από την κλήση και δεν ηχογραφήθηκε τίποτα."
)
WRITE_FAILING = (  # posted once in the call's chat when received audio keeps failing to be saved
    "⚠️ **Audio is not being saved** ({error}). The recording may be incomplete: tell an officer to check the "
    "server's disk.\n"
    "⚠️ **Ο ήχος δεν αποθηκεύεται** ({error}). Η ηχογράφηση μπορεί να είναι ελλιπής."
)
STATUS_AFTER_STOP = "queued"  # the host's path unit starts the processing job when it sees this
EMPTY_GRACE = 45  # seconds a call may look empty (someone's connection dropped) before the recording stops
STAGES = {  # how /record status names each status
    "recording": "recording",
    "queued": "waiting for the processing job to start",
    "transcribing": "transcribing",
    "summarizing": "summarizing",
    "done": "done",
    "failed": "**failed**",
}


@dataclass
class ActiveRecording:
    meeting: Meeting
    channel: discord.VoiceChannel
    voice_client: voice_recv.VoiceRecvClient
    writer: SegmentWriter
    decryptor: DaveDecryptor
    pipeline: SpeakerPipeline
    clock: Callable[[], float]
    flusher: asyncio.Task
    sink: MeetingSink | None = None
    empty_check: asyncio.Task | None = None  # the wait before stopping a call that looks empty

    @property
    def write_errors(self) -> int:
        """Received frames that could not be saved (and, as a backstop, frames whose handling failed at all)."""
        return self.pipeline.write_error_count + (self.sink.errors if self.sink else 0)


def plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def short(exc: BaseException) -> str:
    return (str(exc) or type(exc).__name__)[:200]


class RecorderCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active: ActiveRecording | None = None
        self._starting = False  # a /record start is joining the call: a second one must not start another recording
        self._warned_no_channel = False
        self._post_problems: set[tuple[str, str]] = set()  # (meeting id, error) already logged
        self._naming: set[asyncio.Task] = set()  # name lookups and chat warnings in flight (kept from the GC)
        self._recovery: asyncio.Task | None = None
        self._sleep = asyncio.sleep  # replaced in tests, so the wait before an empty call stops takes no time

    @property
    def app(self):
        return self.bot.app

    async def cog_load(self) -> None:
        # A recording cannot survive a restart: the voice session is gone, so what was captured is processed as is.
        # Names are saved as people first speak, so a meeting usually has them all and is queued right away. One
        # missing some (recorded by an older version) waits, still `recording`, until the guild is known after login.
        held = []
        for meeting in recover_interrupted(self.app.settings.meetings_dir, hold_unnamed=True):
            if meeting.status in STALE:
                held.append(meeting)
                log.warning("Meeting %s was recording when the bot stopped; queued once its speakers are named",
                            meeting.id)
            else:
                log.warning("Meeting %s was recording when the bot stopped; now %s", meeting.id, meeting.status)
        if held:
            self._recovery = asyncio.create_task(self._finish_recovery(held))
        self.poster.start()

    async def cog_unload(self) -> None:
        self.poster.cancel()
        if self._recovery:
            self._recovery.cancel()  # a meeting not finished yet is held again at the next start
        if self.active:
            self.active.flusher.cancel()
            if self.active.empty_check:
                self.active.empty_check.cancel()

    async def _finish_recovery(self, held: list[Meeting]) -> None:
        """Name the speakers of meetings recovered without names, then queue them. Only the bot writes a `recording`
        manifest, so nothing races with this; whatever happens to the lookup, the meeting is queued."""
        await self.bot.wait_until_ready()
        for meeting in held:
            try:
                channel = self.bot.get_channel(meeting.channel_id)
                guild = getattr(channel, "guild", None)
                for user_id in meeting.unnamed_speakers() if guild else []:
                    name = await self._display_name(guild, user_id)
                    if name:
                        meeting.set_speaker(user_id, name)
            except Exception:
                log.exception("Could not name the speakers of recovered meeting %s", meeting.id)
            end_interrupted(meeting)
            log.info("Recovered meeting %s is now %s", meeting.id, meeting.status)

    record_group = app_commands.Group(name="record", description="Record a voice meeting", guild_only=True)

    @record_group.command(name="start", description="Record the voice channel you are in")
    @officer_only()
    async def start(self, interaction: discord.Interaction) -> None:
        voice = getattr(interaction.user, "voice", None)
        channel = voice.channel if voice else None
        if channel is None:
            await interaction.response.send_message("Join the voice channel you want to record first.", ephemeral=True)
            return
        if self.active or self._starting or find_busy(self.app.settings.meetings_dir):
            await interaction.response.send_message(
                "A recording is already in progress, or the last one is still being processed.", ephemeral=True
            )
            return
        self._starting = True  # before the first await, so a start meanwhile is refused
        try:
            await self._start(interaction, channel)
        finally:
            self._starting = False

    async def _start(self, interaction: discord.Interaction, channel) -> None:
        await interaction.response.defer(ephemeral=True)  # joining a call can outlast Discord's 3 s limit
        try:
            voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
        except Exception as exc:
            log.warning("Could not join the voice channel: %s", exc)
            await self._leave_half_joined(channel)
            await interaction.followup.send("I couldn't join that voice channel. Check my permissions.", ephemeral=True)
            return
        try:
            await channel.send(ANNOUNCEMENT)
        except Exception as exc:  # usually a missing permission; whatever it is, the bot must not stay in the call
            log.warning("Could not post the recording announcement in channel %s: %s", channel.id, exc)
            await self._leave_after_failed_start(voice_client)
            await interaction.followup.send(
                "I couldn't post the recording announcement in the channel's chat, so I did not record. "
                "Give me permission to send messages there.", ephemeral=True,
            )
            return
        try:
            self._begin(voice_client, channel, interaction.user.id)
        except Exception as exc:  # the meetings folder is not writable, the disk is full...
            log.exception("Could not start recording in channel %s", channel.id)
            await self._abort_start(voice_client, channel)
            await interaction.followup.send(START_FAILED.format(error=short(exc)), ephemeral=True)
            return
        if not self._has_people(channel):  # everyone left while the bot was joining: no leave event will come now
            self._check_empty_later(self.active)
        await audit(self.app, interaction, f"started a recording in <#{channel.id}>")
        await interaction.followup.send(
            f"Recording {channel.mention}. Use `/record stop` when the meeting is over.", ephemeral=True
        )

    def _begin(self, voice_client, channel, started_by: int) -> None:
        """Create the meeting and start receiving. On any error nothing is left behind (no active recording, no
        meeting folder), so the caller only has to leave the call and say why."""
        meeting = Meeting.create(self.app.settings.meetings_dir, channel.id, started_by, datetime.now())
        flusher = None
        try:
            began = time.monotonic()
            clock = lambda: time.monotonic() - began  # noqa: E731
            loop = asyncio.get_running_loop()

            def later(callback, *args) -> None:  # on the voice thread: hand over to the loop and return at once
                try:
                    loop.call_soon_threadsafe(callback, *args)
                except RuntimeError:  # the loop is shutting down
                    pass

            writer = SegmentWriter(meeting.directory, on_close=meeting.add_segment)
            decryptor = DaveDecryptor(lambda: voice_client._connection)
            pipeline = SpeakerPipeline(decryptor, discord.opus.Decoder, writer, clock,
                                       on_failing=lambda exc: later(self._warn_write_failing, meeting, exc))
            flusher = asyncio.create_task(self._flush_idle_segments(writer, clock))
            active = ActiveRecording(meeting, channel, voice_client, writer, decryptor, pipeline, clock, flusher)
            active.sink = MeetingSink(
                pipeline, voice_client._get_id_from_ssrc,
                on_new_speaker=lambda user_id, user: later(self._learn_name, active, user_id, user),
            )
            self.active = active
            voice_client.listen(active.sink)
        except BaseException:
            self.active = None
            if flusher:
                flusher.cancel()
            self._discard(meeting)
            raise

    @staticmethod
    def _discard(meeting: Meeting) -> None:
        """Remove the folder of a meeting that never started; if that fails, fail it so it blocks nothing."""
        try:
            shutil.rmtree(meeting.directory)
        except OSError:
            log.exception("Could not remove the folder of meeting %s, which did not start", meeting.id)
            try:
                meeting.set_error("the recording could not start")
                meeting.set_status("failed")
            except OSError:
                log.exception("Could not mark meeting %s failed", meeting.id)

    @staticmethod
    async def _leave_half_joined(channel) -> None:
        """discord.py leaves its voice client registered when a join fails with anything but a timeout, and every
        later join would then fail with "Already connected"."""
        voice_client = getattr(channel.guild, "voice_client", None)
        if voice_client is None:
            return
        try:
            await voice_client.disconnect(force=True)
        except Exception:
            log.exception("Could not clean up after a failed join")

    async def _abort_start(self, voice_client, channel) -> None:
        await self._leave_after_failed_start(voice_client)
        try:
            await channel.send(CANCELLED)  # the announcement already said it is recording
        except discord.HTTPException:
            log.warning("Could not tell channel %s that the recording was cancelled", channel.id)

    @staticmethod
    async def _leave_after_failed_start(voice_client) -> None:
        try:
            await voice_client.disconnect(force=True)
        except Exception:
            log.exception("Could not leave the voice channel after a failed start")

    def _warn_write_failing(self, meeting: Meeting, exc: BaseException) -> None:
        """Received audio keeps failing to be saved: say so once in the call's chat, as people are relying on it."""
        active = self.active
        if active is None or active.meeting is not meeting:
            return
        log.error("Saving the audio of meeting %s keeps failing: %s", meeting.id, exc)

        async def warn() -> None:
            try:
                await active.channel.send(WRITE_FAILING.format(error=short(exc)))
            except discord.HTTPException:
                log.warning("Could not warn channel %s that audio is not being saved", active.channel.id)

        task = asyncio.create_task(warn())
        self._naming.add(task)
        task.add_done_callback(self._naming.discard)

    def _learn_name(self, active: ActiveRecording, user_id: int, user) -> None:
        """Save a speaker's name as soon as they first speak, so a crash or restart does not lose it."""
        task = asyncio.create_task(self._save_name(active, user_id, user))
        self._naming.add(task)
        task.add_done_callback(self._naming.discard)

    async def _save_name(self, active: ActiveRecording, user_id: int, user) -> None:
        name = await self._display_name(active.channel.guild, user_id, user)
        # Once the recording has ended the job may own the manifest; the stop saved the names itself.
        if name and self.active is active:
            active.meeting.set_speaker(user_id, name)

    @staticmethod
    async def _flush_idle_segments(writer: SegmentWriter, clock: Callable[[], float]) -> None:
        """Finish a speaker's file soon after they go quiet, so the processing job can pick it up. A file that cannot
        be finished is left to the stop, which adds or deletes it (adopt_orphans); the loop keeps going."""
        failed = False
        while True:
            await asyncio.sleep(1)
            try:
                writer.flush_idle(clock())
            except Exception:
                if not failed:
                    log.exception("Could not finish an audio file")
                failed = True

    @record_group.command(name="stop", description="Stop the recording")
    @officer_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        active, self.active = self.active, None
        if active is None:
            await interaction.response.send_message("No recording is in progress.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._end(active, interaction.guild)
        await audit(self.app, interaction, f"stopped the recording {active.meeting.id}")
        await interaction.followup.send(self._report(active), ephemeral=True)

    async def _end(self, active: ActiveRecording, guild) -> None:
        """Leave the call and hand the meeting to the job. Never raises, so the caller always replies: every step
        runs even if one before it failed, and the meeting always leaves `recording` (queued with the audio it has,
        or failed with none), unless the manifest itself cannot be written, in which case the next bot start
        recovers it."""
        active.flusher.cancel()
        if active.empty_check and active.empty_check is not asyncio.current_task():
            active.empty_check.cancel()
        meeting = active.meeting
        try:
            active.voice_client.stop_listening()
        except Exception:
            log.exception("Could not stop receiving audio for meeting %s", meeting.id)
        problem = None
        try:
            active.writer.close()
        except Exception as exc:  # disk full: wave patches the header on close, then the manifest is rewritten
            problem = exc
            log.exception("Could not finish the last audio files of meeting %s", meeting.id)
        try:
            adopt_orphans(meeting)  # files whose close or manifest entry failed: kept if usable, else deleted
        except Exception:
            log.exception("Could not check meeting %s for unfinished audio files", meeting.id)
        try:
            await self._save_speaker_names(guild, active)
        except Exception:
            log.exception("Could not save the speakers' names of meeting %s", meeting.id)
        try:
            await active.voice_client.disconnect(force=True)  # force: after a kick the connection is already gone
        except Exception:
            log.exception("Could not leave the voice channel of meeting %s", meeting.id)
        try:
            if meeting.has_audio:
                meeting.set_status(STATUS_AFTER_STOP)
            else:  # as after a restart with no audio: failed, so one notice is posted and nothing waits for it
                reason = f"{NO_AUDIO_STOPPED} (saving it failed: {short(problem)})" if problem else NO_AUDIO_STOPPED
                meeting.set_error(reason)
                meeting.set_status("failed")
        except Exception:
            log.exception("Could not save the end of meeting %s; the next bot start finishes it", meeting.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        """Stop like /record stop would when the bot is kicked (or moved) out of the call, at once, or when the call
        has had nobody in it (other bots do not count) for EMPTY_GRACE seconds: a dropped connection rejoins. Someone
        joining ends the wait, so the next time the call empties it gets the whole grace period again."""
        active = self.active
        if active is None:
            return
        recorded = active.channel.id
        if member.id == self.bot.user.id:
            if after.channel is not None and after.channel.id == recorded:
                return
            await self._stop_automatically(active, "kicked")
        elif before.channel is not None and before.channel.id == recorded:
            if not self._has_people(active.channel):
                self._check_empty_later(active)
        elif after.channel is not None and after.channel.id == recorded:
            if active.empty_check and self._has_people(active.channel):
                active.empty_check.cancel()  # not running _stop_automatically: that sets self.active to None first
                active.empty_check = None

    def _check_empty_later(self, active: ActiveRecording) -> None:
        if active.empty_check is None or active.empty_check.done():
            active.empty_check = asyncio.create_task(self._stop_if_still_empty(active))

    def _has_people(self, channel) -> bool:
        """Anyone in the call but bots. voice_states, not members: without the members intent the member cache is
        mostly empty, so someone the cache does not know counts as a person."""
        guild = getattr(channel, "guild", None)
        for user_id in channel.voice_states:
            if user_id == self.bot.user.id:
                continue
            member = guild.get_member(user_id) if guild is not None else None
            if member is not None and getattr(member, "bot", False) is True:
                continue
            return True
        return False

    async def _stop_if_still_empty(self, active: ActiveRecording) -> None:
        await self._sleep(EMPTY_GRACE)
        if self.active is active and not self._has_people(active.channel):
            await self._stop_automatically(active, "empty")

    async def _stop_automatically(self, active: ActiveRecording, reason: str) -> None:
        if self.active is not active:  # stopped meanwhile, by /record stop or the other reason
            return
        self.active = None
        log.info("Recording %s stopped automatically (%s)", active.meeting.id, reason)
        await self._end(active, active.channel.guild)
        try:
            await active.channel.send(STOPPED[reason])
        except discord.HTTPException:
            log.warning("Could not announce the end of recording %s", active.meeting.id)

    @record_group.command(name="status", description="Show how far the latest meeting has got")
    @officer_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(self._status_text(), ephemeral=True)

    def _status_text(self) -> str:
        active = self.active
        if active:
            text = (
                f"Recording `{active.meeting.id}` in {active.channel.mention} for {active.clock() / 60:.0f} min, "
                f"{plural(len(active.writer.segments), 'segment')} finished so far. Use `/record stop` to end it."
            )
            if active.write_errors:
                text += f"\n**{active.write_errors} audio frames could not be saved** (see the bot's log)."
            return text
        meeting = latest(self.app.settings.meetings_dir)
        if meeting is None:
            return "No meetings have been recorded yet."
        started = f"<t:{int(meeting.started_at.timestamp())}:f>"
        text = f"Meeting `{meeting.id}` (started {started}): {STAGES.get(meeting.status, meeting.status)}."
        if meeting.status == "failed" and meeting.error:
            text += f" Reason: {meeting.error[:300]}"
        if meeting.interrupted:
            text += "\nThe bot restarted during this recording, so only what was captured before it is processed."
        channel_id = self.app.channels.get(MEETING_SUMMARIES)
        if meeting.posted:
            text += f"\nPosted in <#{channel_id}>." if channel_id else ""
        elif meeting.status in STATUS_TEXT and channel_id is None:
            text += f"\nNo summaries channel is set, so nothing is posted: `/channel set` it for `{MEETING_SUMMARIES}`."
        elif meeting.status in FINAL:
            text += f"\nThe result is waiting to be posted in <#{channel_id}>: I keep retrying, check my permissions."
        return text

    @tasks.loop(seconds=30)
    async def poster(self) -> None:
        try:
            await self.post_updates()
        except Exception:
            log.exception("Posting meeting updates failed")  # keep the loop alive; it tries again next time

    async def post_updates(self) -> None:
        """Post status changes and finished meetings to the summaries channel. Anything not posted stays pending."""
        root = self.app.settings.meetings_dir
        pending = [m for m in all_meetings(root) if needs_update(m)]
        if not pending:
            return
        channel_id = self.app.channels.get(MEETING_SUMMARIES)
        if channel_id is None:
            if not self._warned_no_channel:
                log.warning("Meeting results are waiting: set the %s channel with /channel set", MEETING_SUMMARIES)
                self._warned_no_channel = True
            return
        self._warned_no_channel = False
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except discord.HTTPException as exc:
            log.warning("Cannot reach the %s channel %s: %s", MEETING_SUMMARIES, channel_id, exc)
            return
        for meeting in pending:
            try:
                await post_update(channel, meeting)
            except (discord.HTTPException, OSError) as exc:
                problem = (meeting.id, str(exc))
                level = logging.DEBUG if problem in self._post_problems else logging.WARNING  # once, not every turn
                self._post_problems.add(problem)
                log.log(level, "Could not post meeting %s yet, will retry: %s", meeting.id, exc)
            except Exception as exc:  # a manifest edited by hand (no started_at, a bad date): the others still post
                problem = (meeting.id, repr(exc))
                if problem not in self._post_problems:
                    self._post_problems.add(problem)
                    log.exception("Could not post meeting %s; check its manifest.json", meeting.id)

    @poster.before_loop
    async def before_poster(self) -> None:
        await self.bot.wait_until_ready()

    @classmethod
    async def _save_speaker_names(cls, guild, active: ActiveRecording) -> None:
        """Refresh every speaker's name (it may have changed during the call). One that cannot be looked up now keeps
        the name saved when they first spoke, or stays unnamed for the job to show by id."""
        for user_id in sorted({segment["speaker"] for segment in active.meeting.segments}):
            name = await cls._display_name(guild, user_id)
            if name:
                active.meeting.set_speaker(user_id, name)

    @staticmethod
    async def _display_name(guild, user_id: int, user=None) -> str | None:
        """The speaker's name in the guild, from the Member the library gave, the member cache, or the API."""
        try:
            member = await resolve_member(guild, user or SimpleNamespace(id=user_id))
        except discord.HTTPException:
            member = None
        if member is not None:
            return member.display_name
        return getattr(user, "display_name", None)  # a User the library found outside the guild's cache

    @staticmethod
    def _report(active: ActiveRecording) -> str:
        segments = active.meeting.segments  # with the files recovered at the stop
        speakers = {segment["speaker"] for segment in segments}
        stats = active.decryptor.stats
        audio = (
            f"Audio frames: {stats['decrypted']} decrypted, {stats['passthrough']} plain, "
            f"{stats['dropped']} dropped, {stats['errors']} decrypt errors, "
            f"{active.pipeline.decode_errors} decode errors, {active.write_errors} not saved."
        )
        if active.write_errors:
            audio += "\n**Some audio could not be saved** (disk full or not writable?): see the bot's log."
        if not segments:
            return (
                f"Recording `{active.meeting.id}` stopped. **No audio was captured.** {audio}\n"
                "If frames were dropped or failed to decrypt, voice receiving is not working with this call."
            )
        minutes = sum(segment["duration"] for segment in segments) / 60
        return (
            f"Recording `{active.meeting.id}` stopped. Captured {plural(len(segments), 'segment')} from "
            f"{plural(len(speakers), 'speaker')} ({minutes:.1f} min of speech). {audio}"
        )


async def setup(bot) -> None:
    await bot.add_cog(RecorderCog(bot))

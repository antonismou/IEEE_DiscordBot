from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
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
    STALE, Meeting, all_meetings, end_interrupted, find_busy, latest, recover_interrupted,
)
from bot.recording.pipeline import SpeakerPipeline
from bot.recording.posting import FINAL, STATUS_TEXT, post_update
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
STATUS_AFTER_STOP = "queued"  # the host's path unit starts the processing job when it sees this
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


def plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


class RecorderCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active: ActiveRecording | None = None
        self._warned_no_channel = False
        self._post_problems: set[tuple[str, str]] = set()  # (meeting id, error) already logged
        self._naming: set[asyncio.Task] = set()  # name lookups in flight (kept so they are not garbage collected)
        self._recovery: asyncio.Task | None = None

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
        if self.active or find_busy(self.app.settings.meetings_dir):
            await interaction.response.send_message(
                "A recording is already in progress, or the last one is still being processed.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)  # joining a call can outlast Discord's 3 s limit
        try:
            voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
        except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError) as exc:
            log.warning("Could not join the voice channel: %s", exc)
            await interaction.followup.send("I couldn't join that voice channel. Check my permissions.", ephemeral=True)
            return
        try:
            await channel.send(ANNOUNCEMENT)
        except discord.HTTPException:
            await voice_client.disconnect()
            await interaction.followup.send(
                "I couldn't post the recording announcement in the channel's chat, so I did not record. "
                "Give me permission to send messages there.", ephemeral=True,
            )
            return
        self._begin(voice_client, channel, interaction.user.id)
        await audit(self.app, interaction, f"started a recording in <#{channel.id}>")
        await interaction.followup.send(
            f"Recording {channel.mention}. Use `/record stop` when the meeting is over.", ephemeral=True
        )

    def _begin(self, voice_client, channel, started_by: int) -> None:
        meeting = Meeting.create(self.app.settings.meetings_dir, channel.id, started_by, datetime.now())
        began = time.monotonic()
        clock = lambda: time.monotonic() - began  # noqa: E731
        writer = SegmentWriter(meeting.directory, on_close=meeting.add_segment)
        decryptor = DaveDecryptor(lambda: voice_client._connection)
        pipeline = SpeakerPipeline(decryptor, discord.opus.Decoder, writer, clock)
        flusher = asyncio.create_task(self._flush_idle_segments(writer, clock))
        self.active = ActiveRecording(meeting, channel, voice_client, writer, decryptor, pipeline, clock, flusher)
        active = self.active
        loop = asyncio.get_running_loop()

        def heard(user_id, user) -> None:  # on the voice thread: hand over to the loop and return at once
            try:
                loop.call_soon_threadsafe(self._learn_name, active, user_id, user)
            except RuntimeError:  # the loop is shutting down
                pass

        voice_client.listen(MeetingSink(pipeline, voice_client._get_id_from_ssrc, on_new_speaker=heard))

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
        """Finish a speaker's file soon after they go quiet, so the processing job can pick it up."""
        while True:
            await asyncio.sleep(1)
            writer.flush_idle(clock())

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
        active.flusher.cancel()
        active.voice_client.stop_listening()
        active.writer.close()
        try:
            await self._save_speaker_names(guild, active)
        finally:
            await active.voice_client.disconnect(force=True)  # force: after a kick the connection is already gone
            active.meeting.set_status(STATUS_AFTER_STOP)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after) -> None:
        """Stop like /record stop would when the bot is kicked (or moved) out of the call, or the call empties."""
        active = self.active
        if active is None:
            return
        recorded = active.channel.id
        if member.id == self.bot.user.id:
            if after.channel is not None and after.channel.id == recorded:
                return
            reason = "kicked"
        elif before.channel is not None and before.channel.id == recorded:
            # voice_states, not members: without the members intent the member cache is mostly empty
            if any(user_id != self.bot.user.id for user_id in active.channel.voice_states):
                return
            reason = "empty"
        else:
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
            return (
                f"Recording `{active.meeting.id}` in {active.channel.mention} for {active.clock() / 60:.0f} min, "
                f"{plural(len(active.writer.segments), 'segment')} finished so far. Use `/record stop` to end it."
            )
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
        pending = [m for m in all_meetings(root) if m.status in STATUS_TEXT and not m.posted]
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

    @poster.before_loop
    async def before_poster(self) -> None:
        await self.bot.wait_until_ready()

    @classmethod
    async def _save_speaker_names(cls, guild, active: ActiveRecording) -> None:
        """Refresh every speaker's name (it may have changed during the call). One that cannot be looked up now keeps
        the name saved when they first spoke, or stays unnamed for the job to show by id."""
        for user_id in sorted({segment["speaker"] for segment in active.writer.segments}):
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
        segments = active.writer.segments
        speakers = {segment["speaker"] for segment in segments}
        stats = active.decryptor.stats
        audio = (
            f"Audio frames: {stats['decrypted']} decrypted, {stats['passthrough']} plain, "
            f"{stats['dropped']} dropped, {stats['errors']} decrypt errors, {active.pipeline.decode_errors} decode errors."
        )
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

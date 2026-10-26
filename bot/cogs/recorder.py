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
from discord.ext import commands, voice_recv

from bot.audit import audit
from bot.checks import officer_only
from bot.recording.dave import DaveDecryptor
from bot.recording.meeting import Meeting, find_busy, interrupt_stale
from bot.recording.pipeline import SpeakerPipeline
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
STATUS_AFTER_STOP = "done"  # becomes "queued" once the processing job exists


@dataclass
class ActiveRecording:
    meeting: Meeting
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

    @property
    def app(self):
        return self.bot.app

    async def cog_load(self) -> None:
        # A recording cannot survive a restart: the voice session is gone.
        for meeting in interrupt_stale(self.app.settings.meetings_dir):
            log.warning("Meeting %s was recording when the bot stopped; marked interrupted", meeting.id)

    async def cog_unload(self) -> None:
        if self.active:
            self.active.flusher.cancel()

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
        self.active = ActiveRecording(meeting, voice_client, writer, decryptor, pipeline, clock, flusher)
        voice_client.listen(MeetingSink(pipeline, voice_client._get_id_from_ssrc))

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
        active.flusher.cancel()
        active.voice_client.stop_listening()
        active.writer.close()
        try:
            await self._save_speaker_names(interaction.guild, active)
        finally:
            await active.voice_client.disconnect()
            active.meeting.set_status(STATUS_AFTER_STOP)
        await audit(self.app, interaction, f"stopped the recording {active.meeting.id}")
        await interaction.followup.send(self._report(active), ephemeral=True)

    @staticmethod
    async def _save_speaker_names(guild, active: ActiveRecording) -> None:
        for user_id in sorted({segment["speaker"] for segment in active.writer.segments}):
            try:
                member = await resolve_member(guild, SimpleNamespace(id=user_id))
            except discord.HTTPException:
                member = None
            active.meeting.set_speaker(user_id, member.display_name if member else str(user_id))

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

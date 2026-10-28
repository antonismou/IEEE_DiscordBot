"""Which summary windows of a meeting are complete, so their notes can be made while the call is still going.

The summary stage reads the transcript in fixed windows (window k covers [k*W, (k+1)*W) by segment start offset, see
worker/summarize.py). A window's notes may be made once no more speech can arrive for it:

- after /record stop (any status but `recording`) every window is complete, the last partial one included;
- during the call, window k is complete only when all of these hold:
  1. the meeting has run for at least (k+1)*W + MAX_SEGMENT + MARGIN seconds. The bot adds a segment to the
     manifest only when its file is closed (SegmentWriter._close -> on_close -> Meeting.add_segment), with `start`
     the offset of its first audio from the meeting start; a file is closed after a 2 s pause or at 5 minutes of
     audio, so a segment that started before the window's end is normally in the manifest 5 minutes later.
  2. every manifest segment that starts before the window's end has its text (.txt), so the notes see all of it.
  3. no WAV still being written can belong to it. Five minutes of audio can span longer than five minutes on the
     clock (pauses under 2 s do not close a file), so condition 1 alone is not a guarantee. A WAV on disk that is not
     in the manifest is still open; it started after the same speaker's previous segment ended (files are numbered
     per speaker), which bounds the windows it can belong to.

The manifest is read before the folder is listed, so a segment the bot closes in between is seen as open (safe).
Nothing here writes the manifest. Elapsed time is wall time since `started_at`, which the bot writes as naive local
time (the bot and worker images both run with TZ=Europe/Athens and tzdata); both sides are converted to UTC, so a
daylight-saving change during the call does not matter.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from bot.recording.meeting import MANIFEST
from worker import summarize as summary_stage
from worker import transcribe as transcribe_stage

LIVE = "recording"
MAX_SEGMENT = 300  # seconds of audio after which SegmentWriter closes a file (its max_seconds)
MARGIN = 60  # the 2 s pause, the bot's 1 s flush loop, started_at cut to whole seconds, clock drift


def complete_windows(meeting_dir: Path | str, chunk_minutes: int, now: datetime | None = None) -> list[int]:
    """The non-empty windows (by segment start) whose speech is all in and transcribed, in order."""
    directory = Path(meeting_dir)
    manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))  # before listing the folder
    segments = manifest["segments"]
    width = chunk_minutes * 60
    present = sorted({int(s["start"]) // width for s in segments})
    if manifest["status"] != LIVE:
        return present
    elapsed = (_utc(now or datetime.now(timezone.utc)) - _utc(datetime.fromisoformat(manifest["started_at"]))) \
        .total_seconds()
    open_from = _earliest_open_start(directory, segments)
    complete = []
    for k in present:  # each condition only gets harder for later windows, so stop at the first that fails
        end = (k + 1) * width
        if elapsed < end + MAX_SEGMENT + MARGIN or open_from < end:
            break
        if any(s["start"] < end and not (directory / s["file"]).with_suffix(".txt").exists() for s in segments):
            break
        complete.append(k)
    return complete


def pending_windows(meeting_dir: Path | str, chunk_minutes: int, now: datetime | None = None) -> list[int]:
    """Complete windows with speech and no notes yet. Writes nothing unless some complete window lacks notes, so
    the check every idle tick makes is a manifest read and a folder listing."""
    directory = Path(meeting_dir)
    candidates = [k for k in complete_windows(directory, chunk_minutes, now)
                  if not summary_stage.notes_path(directory, k, chunk_minutes).exists()]
    if not candidates:
        return []
    with_speech = summary_stage.windows(_lines(directory), chunk_minutes)
    return [k for k in candidates if k in with_speech]


def extract(meeting_dir: Path | str, k: int, chunk_minutes: int, generate: summary_stage.Generate) -> str:
    """Makes and saves the notes of window `k` from the segment texts on disk."""
    directory = Path(meeting_dir)
    lines = summary_stage.windows(_lines(directory), chunk_minutes).get(k, [])
    return summary_stage.extract_window(directory, k, lines, generate=generate, chunk_minutes=chunk_minutes)


def _lines(directory: Path) -> list[summary_stage.Line]:
    # Rebuilt from the segment texts rather than trusting transcript.txt, which a killed run may have left behind
    # its .txt files. write_transcript only writes transcript.txt, never the manifest.
    path = transcribe_stage.write_transcript(directory)
    return summary_stage.parse_transcript(path.read_text(encoding="utf-8"))


def _earliest_open_start(directory: Path, segments: list[dict]) -> float:
    """A lower bound on the start of any segment the bot still has open (infinity if none)."""
    by_file = {s["file"]: s for s in segments}
    earliest = math.inf
    for wav in directory.glob("*/*.wav"):
        relative = f"{wav.parent.name}/{wav.name}"
        if relative in by_file:
            continue
        try:
            previous = by_file.get(f"{wav.parent.name}/{int(wav.stem) - 1:06d}.wav")
        except ValueError:
            previous = None
        bound = previous["start"] + previous["duration"] if previous else 0.0
        earliest = min(earliest, bound)
    return earliest


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc)  # a naive time is taken as the local time zone

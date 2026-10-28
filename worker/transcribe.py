"""Transcribes a meeting's segment WAVs with faster-whisper and merges them into one transcript.

Each segment's text is saved next to its audio (`7/000001.wav` -> `7/000001.txt`), so a crash resumes where it
stopped, and the audio is deleted once its text is safe on disk. It can run repeatedly while the call is still going.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

from bot.recording.meeting import MANIFEST, Meeting

log = logging.getLogger(__name__)

TRANSCRIPT = "transcript.txt"
# Words Whisper otherwise gets wrong in Greek speech.
VOCABULARY = (
    "IEEE Student Branch, workshop, event, email, "
    "Δευτέρα, Τρίτη, Τετάρτη, Πέμπτη, Παρασκευή, Σάββατο, Κυριακή"
)


def whisper_model(name: str, threads: int):
    from faster_whisper import WhisperModel  # imported here so the module loads without it

    return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)


def transcribe_meeting(
    meeting_dir: Path,
    model_factory: Callable[[str, int], object] = whisper_model,
    model_name: str = "large-v3-turbo",
    threads: int = 8,
    beam_size: int = 1,
    prompt: str = VOCABULARY,
) -> int:
    """Transcribe every segment that has no transcript yet, then rewrite transcript.txt. Returns how many were done.

    A segment that fails keeps its audio and is retried on the next run.
    """
    meeting = Meeting.load(meeting_dir)
    if meeting.status != "recording":  # during the call the bot owns the manifest, so leave it alone
        meeting.set_status("transcribing")
    model = None
    done = 0
    for segment in _segments(meeting):
        audio = meeting.directory / segment["file"]
        text_path = audio.with_suffix(".txt")
        if text_path.exists():
            audio.unlink(missing_ok=True)  # a crash between saving the text and deleting the audio
            continue
        if not audio.exists():
            log.warning("segment %s has no audio and no transcript", segment["file"])
            continue
        if model is None:
            model = model_factory(model_name, threads)
        try:
            pieces, _ = model.transcribe(str(audio), language="el", beam_size=beam_size, vad_filter=True,
                                         initial_prompt=prompt)
            text = " ".join(piece.text.strip() for piece in pieces).strip()  # decoding happens while iterating
        except Exception:
            log.exception("could not transcribe %s", segment["file"])
            continue
        _write_atomic(text_path, text)
        audio.unlink()
        done += 1
    write_transcript(meeting.directory)
    return done


def write_transcript(meeting_dir: Path) -> Path:
    """Merge every finished segment, all speakers together by start offset, as `[hh:mm:ss] Name: text` lines."""
    meeting = Meeting.load(meeting_dir)
    speakers = meeting.speakers
    lines = []
    for segment in sorted(_segments(meeting), key=lambda s: s["start"]):
        text_path = (meeting.directory / segment["file"]).with_suffix(".txt")
        if not text_path.exists():
            continue
        text = text_path.read_text(encoding="utf-8").strip()
        if text:
            name = speakers.get(segment["speaker"]) or f"Ομιλητής {segment['speaker']}"
            lines.append(f"[{format_offset(segment['start'])}] {name}: {text}\n")
    path = meeting.directory / TRANSCRIPT
    _write_atomic(path, "".join(lines))
    return path


def format_offset(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def _segments(meeting: Meeting) -> list[dict]:
    """Meeting has no accessor for the list (the bot only appends to it), so read it from the manifest."""
    return json.loads((meeting.directory / MANIFEST).read_text(encoding="utf-8"))["segments"]


def _write_atomic(path: Path, text: str) -> None:
    """A half-written file must never look finished, or the resume would skip its segment."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)

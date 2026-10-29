"""Transcribes a meeting's segment WAVs with faster-whisper and merges them into one transcript.

Each segment's text is saved next to its audio (`7/000001.wav` -> `7/000001.txt`), so a crash resumes where it
stopped, and the audio is deleted once its text is safe on disk. It can run repeatedly while the call is still going.
The text is one line per Whisper piece (about a sentence), `seconds-into-the-segment<TAB>text`, so the transcript
gets a timestamp per sentence instead of one per segment; a text saved by an older worker (plain text) is one piece.

The WAVs are read here with the standard `wave` module and handed to Whisper as samples, so faster-whisper's own
decoder (PyAV, whose API changes between major versions) is never used.
"""
from __future__ import annotations

import json
import logging
import os
import re
import wave
from pathlib import Path
from typing import Callable

from bot.recording.meeting import MANIFEST, Meeting

log = logging.getLogger(__name__)

TRANSCRIPT = "transcript.txt"
# Words Whisper otherwise gets wrong in Greek speech. Passed as hotwords, which go into every 30-second window
# (initial_prompt only reaches the first one once condition_on_previous_text is off). Whisper keeps at most about
# 220 tokens of it and Greek costs several tokens a word, so keep the list short.
VOCABULARY = (
    "IEEE Student Branch, workshop, event, email, Discord, bot, server, Claude, logs, record, segment, "
    "ηχογράφηση, απομαγνητοφώνηση, πρακτικά, συνάντηση, "
    "Δευτέρα, Τρίτη, Τετάρτη, Πέμπτη, Παρασκευή, Σάββατο, Κυριακή"
)
# Text Whisper invents on silence or noise, learned from subtitled videos ("Υπότιτλοι AUTHORWAVE"). A piece is
# dropped only when it is short and all of it matches, so someone saying one of these words mid-sentence is kept.
INVENTED = [re.compile(pattern) for pattern in (
    r"υπότιτλοι\b.*",
    r"(subtitles|captions) by\b.*",
    r".*amara\.org.*",
    r"(ευχαριστώ|ευχαριστούμε)( πολύ)? (που|για την) παρακολ\w*.*",
    r".*(εγγραφείτε|subscribe)\b.*",
)]
INVENTED_MAX_WORDS = 8


SAMPLE_RATE = 16000  # what the bot writes: 16 kHz, mono, 16-bit
MIN_SAMPLES = SAMPLE_RATE // 10  # under 0.1 s there is nothing to hear, and Whisper is not loaded for it


class AudioError(Exception):
    """A segment's WAV cannot be read."""


def read_wav(path: Path):
    """The samples of a segment as float32 in [-1, 1], or None when there is nothing to hear (an empty file, or
    under MIN_SAMPLES). A file that is not a 16 kHz mono 16-bit WAV raises AudioError."""
    import numpy as np  # comes with faster-whisper; imported here like it

    path = Path(path)
    if path.stat().st_size == 0:  # created, but the bot stopped before writing its header
        return None
    try:
        with wave.open(str(path), "rb") as wav:
            layout = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AudioError(f"{path.name} is not a readable WAV file: {exc or 'it ends too early'}") from exc
    if layout != (1, 2, SAMPLE_RATE):
        raise AudioError(f"{path.name} has {layout[0]} channel(s), {8 * layout[1]}-bit samples at {layout[2]} Hz; "
                         f"expected mono 16-bit at {SAMPLE_RATE} Hz")
    samples = np.frombuffer(frames[:len(frames) // 2 * 2], dtype="<i2")
    if len(samples) < MIN_SAMPLES:
        return None
    return samples.astype(np.float32) / 32768.0


def whisper_model(name: str, threads: int):
    from faster_whisper import WhisperModel  # imported here so the module loads without it

    return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)


def transcribe_meeting(
    meeting_dir: Path,
    model_factory: Callable[[str, int], object] = whisper_model,
    load_audio: Callable[[Path], object] = read_wav,
    model_name: str = "large-v3-turbo",
    threads: int = 8,
    beam_size: int = 3,  # fewer misheard words than 1; turbo's decoder is small, so it costs about 10-25% more time
    prompt: str = VOCABULARY,
) -> int:
    """Transcribe every segment that has no transcript yet, then rewrite transcript.txt. Returns how many were done.

    A segment that fails keeps its audio and is retried on the next run. One with nothing to hear gets an empty text.
    So does a lost one, with a warning: its WAV cannot be read (AudioError, which no retry fixes), or it has neither
    audio nor text (deleted by an older worker when the meeting failed). The rest of the meeting can then finish.
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
            log.warning("segment %s has no audio and no transcript, so it is lost", segment["file"])
            _write_atomic(text_path, "")
            done += 1
            continue
        try:
            samples = load_audio(audio)
        except AudioError as exc:
            log.warning("segment %s cannot be read, so it is lost: %s", segment["file"], exc)
            samples = None
        except Exception as exc:  # not opened at all (permissions, I/O): kept for the next run
            log.error("could not read %s: %s", segment["file"], exc)
            continue
        if samples is None:
            text = ""
        else:
            if model is None:
                model = model_factory(model_name, threads)
            try:
                # Without the previous window's text as context, one invented or misheard phrase cannot be
                # repeated through the rest of a long segment.
                pieces, _ = model.transcribe(samples, language="el", beam_size=beam_size, vad_filter=True,
                                             condition_on_previous_text=False, hotwords=prompt)
                text = "".join(f"{piece.start:.2f}\t{piece.text.strip()}\n"  # decoding happens while iterating
                               for piece in pieces if piece.text.strip() and not is_invented(piece.text))
            except Exception:
                log.exception("could not transcribe %s", segment["file"])
                continue
        _write_atomic(text_path, text)
        audio.unlink()
        done += 1
    write_transcript(meeting.directory)
    return done


def is_invented(text: str) -> bool:
    """A piece that is only text Whisper makes up on silence (see INVENTED)."""
    words = re.sub(r"[^\w\s.]", " ", text.lower()).split()
    return 0 < len(words) <= INVENTED_MAX_WORDS and any(p.fullmatch(" ".join(words)) for p in INVENTED)


def write_transcript(meeting_dir: Path) -> Path:
    """Merge every finished segment's pieces, all speakers together by time, as `[hh:mm:ss] Name: text` lines."""
    meeting = Meeting.load(meeting_dir)
    speakers = meeting.speakers
    pieces = []
    for segment in _segments(meeting):
        text_path = (meeting.directory / segment["file"]).with_suffix(".txt")
        if not text_path.exists():
            continue
        name = speakers.get(segment["speaker"]) or f"Ομιλητής {segment['speaker']}"
        for offset, text in read_pieces(text_path.read_text(encoding="utf-8")):
            pieces.append((segment["start"] + offset, name, text))
    pieces.sort(key=lambda piece: piece[0])  # stable: a tie keeps the manifest's order
    path = meeting.directory / TRANSCRIPT
    _write_atomic(path, "".join(f"[{format_offset(start)}] {name}: {text}\n" for start, name, text in pieces))
    return path


_PIECE = re.compile(r"(\d+(?:\.\d+)?)\t(.*)")


def read_pieces(saved: str) -> list[tuple[float, str]]:
    """A segment's saved text as (seconds into the segment, text) pairs. Plain text from an older worker is one
    piece at the segment's start."""
    lines = [line for line in saved.splitlines() if line.strip()]
    matches = [_PIECE.fullmatch(line) for line in lines]
    if all(matches):
        return [(float(m[1]), m[2].strip()) for m in matches if m[2].strip()]
    return [(0.0, " ".join(line.strip() for line in lines))]


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

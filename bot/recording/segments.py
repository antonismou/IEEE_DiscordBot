"""Writes each speaker's continuous speech to its own WAV file, split at pauses.

Discord stops sending packets while someone is silent, so a gap between frames is a natural place to cut.
Each finished file is a ready-made unit of work for the transcription job.
"""
from __future__ import annotations

import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from bot.recording.pcm import SAMPLE_RATE

_BYTES_PER_SECOND = SAMPLE_RATE * 2  # 16-bit mono
_EPSILON = 1e-9


@dataclass
class _Open:
    wav: wave.Wave_write
    file: str
    start: float
    written: int = 0
    last_end: float = 0.0

    @property
    def duration(self) -> float:
        return self.written / _BYTES_PER_SECOND


class SegmentWriter:
    """Feed it 16 kHz mono PCM per speaker with the time (seconds since the meeting started) it arrived.

    `segments` lists the finished files: {"speaker", "file" (relative to `directory`), "start", "duration"}.
    """

    def __init__(
        self,
        directory: Path,
        gap_seconds: float = 2.0,
        max_seconds: float = 300.0,
        on_close: Callable[[dict], None] | None = None,
    ):
        self._dir = Path(directory)
        self._gap = gap_seconds
        self._max = max_seconds
        self._on_close = on_close
        self._lock = threading.Lock()  # audio arrives on the voice thread; idle flushes run on the event loop
        self._open: dict[int, _Open] = {}
        self._counts: dict[int, int] = {}
        self.segments: list[dict] = []

    def add(self, speaker: int, pcm: bytes, at: float) -> None:
        with self._lock:
            self._add(speaker, pcm, at)

    def _add(self, speaker: int, pcm: bytes, at: float) -> None:
        current = self._open.get(speaker)
        if current and (at - current.last_end > self._gap or current.duration >= self._max - _EPSILON):
            self._close(speaker)
            current = None
        if current is None:
            current = self._open[speaker] = self._start(speaker, at)
        current.wav.writeframes(pcm)
        current.written += len(pcm)
        current.last_end = at + len(pcm) / _BYTES_PER_SECOND

    def flush_idle(self, now: float) -> None:
        """Close the segments of speakers who have been quiet for longer than the gap."""
        with self._lock:
            for speaker in [s for s, o in self._open.items() if now - o.last_end > self._gap]:
                self._close(speaker)

    def close(self) -> None:
        with self._lock:
            for speaker in list(self._open):
                self._close(speaker)

    def _start(self, speaker: int, at: float) -> _Open:
        self._counts[speaker] = self._counts.get(speaker, 0) + 1
        relative = f"{speaker}/{self._counts[speaker]:06d}.wav"
        path = self._dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        wav = wave.open(str(path), "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        return _Open(wav=wav, file=relative, start=at)

    def _close(self, speaker: int) -> None:
        current = self._open.pop(speaker)
        current.wav.close()
        segment = {"speaker": speaker, "file": current.file, "start": current.start, "duration": current.duration}
        self.segments.append(segment)
        if self._on_close:
            self._on_close(segment)

"""Frame in, file out: DAVE-decrypt, opus-decode, convert and hand one speaker's audio to the segment writer."""
from __future__ import annotations

import logging
from typing import Any, Callable

from bot.recording.pcm import to_16k_mono
from bot.recording.segments import SegmentWriter

log = logging.getLogger(__name__)

FAILING_AFTER = 250  # failed writes (about 5 s of one person's speech) before `on_failing` is called


def error_kind(exc: BaseException) -> str:
    """How an error is told apart for logging: its type, plus the errno for an OSError (disk full vs. I/O error)."""
    errno = getattr(exc, "errno", None)
    return f"{type(exc).__name__}[{errno}]" if errno is not None else type(exc).__name__


class SpeakerPipeline:
    """`decoder_factory` makes an opus decoder; each speaker needs their own because decoding keeps per-stream
    state. `now` returns seconds since the meeting started.

    Runs on the voice thread, where an exception would end receiving for everyone, so it never raises: errors are
    counted (`decode_errors`, `write_errors` per speaker) and each kind is logged once. `on_failing` is called once,
    on the voice thread, when writing keeps failing (FAILING_AFTER errors), so the caller can warn the call."""

    def __init__(self, decryptor: Any, decoder_factory: Callable[[], Any], writer: SegmentWriter,
                 now: Callable[[], float], on_failing: Callable[[BaseException], None] | None = None):
        self._decryptor = decryptor
        self._decoder_factory = decoder_factory
        self._writer = writer
        self._now = now
        self._on_failing = on_failing
        self._decoders: dict[int, Any] = {}
        self._logged: set[str] = set()
        self.decode_errors = 0
        self.write_errors: dict[int, int] = {}

    @property
    def write_error_count(self) -> int:
        return sum(self.write_errors.values())

    def handle(self, user_id: int, opus: bytes) -> None:
        try:
            payload = self._decryptor.decrypt(user_id, opus)
            if payload is None:
                return
            decoder = self._decoders.get(user_id)
            if decoder is None:
                decoder = self._decoders[user_id] = self._decoder_factory()
            pcm = decoder.decode(payload, fec=False)
        except Exception as exc:  # a corrupt frame must never end the recording
            self.decode_errors += 1
            self._log_once("decode", exc, "Opus decode failed: %s")
            return
        try:
            self._writer.add(user_id, to_16k_mono(pcm), self._now())
        except Exception as exc:  # disk full, an I/O error on the mount: keep receiving, the others may still work
            self.write_errors[user_id] = self.write_errors.get(user_id, 0) + 1
            self._log_once("write", exc, "Could not save received audio: %s")
            if self.write_error_count == FAILING_AFTER and self._on_failing:
                try:
                    self._on_failing(exc)
                except Exception:
                    log.exception("Could not report that saving audio keeps failing")

    def _log_once(self, stage: str, exc: Exception, message: str) -> None:
        kind = f"{stage}:{error_kind(exc)}"
        if kind not in self._logged:
            self._logged.add(kind)
            log.warning(message, exc)

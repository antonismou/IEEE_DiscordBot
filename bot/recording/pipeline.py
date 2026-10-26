"""Frame in, file out: DAVE-decrypt, opus-decode, convert and hand one speaker's audio to the segment writer."""
from __future__ import annotations

import logging
from typing import Any, Callable

from bot.recording.pcm import to_16k_mono
from bot.recording.segments import SegmentWriter

log = logging.getLogger(__name__)


class SpeakerPipeline:
    """`decoder_factory` makes an opus decoder; each speaker needs their own because decoding keeps per-stream
    state. `now` returns seconds since the meeting started."""

    def __init__(self, decryptor: Any, decoder_factory: Callable[[], Any], writer: SegmentWriter,
                 now: Callable[[], float]):
        self._decryptor = decryptor
        self._decoder_factory = decoder_factory
        self._writer = writer
        self._now = now
        self._decoders: dict[int, Any] = {}
        self.decode_errors = 0

    def handle(self, user_id: int, opus: bytes) -> None:
        payload = self._decryptor.decrypt(user_id, opus)
        if payload is None:
            return
        decoder = self._decoders.get(user_id)
        if decoder is None:
            decoder = self._decoders[user_id] = self._decoder_factory()
        try:
            pcm = decoder.decode(payload, fec=False)
        except Exception as exc:  # a corrupt frame must never end the recording
            self.decode_errors += 1
            if self.decode_errors == 1:
                log.warning("Opus decode failed: %s", exc)
            return
        self._writer.add(user_id, to_16k_mono(pcm), self._now())

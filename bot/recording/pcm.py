"""Turns Discord voice audio into what Whisper wants: 16 kHz, mono, 16-bit."""
from __future__ import annotations

from array import array

SAMPLE_RATE = 16_000
FRAME_BYTES_16K_MONO = SAMPLE_RATE // 50 * 2  # one 20 ms Discord frame, as 16-bit mono samples

_GROUP_BYTES = 12  # 3 stereo frames of 16-bit samples: 48 kHz -> 16 kHz is every third frame


def to_16k_mono(pcm: bytes) -> bytes:
    """Convert 48 kHz 16-bit interleaved stereo to 16 kHz mono by averaging each group of 3 stereo frames.

    Averaging is a crude low-pass filter, which is fine for speech recognition. Bytes that do not fill a whole
    group are dropped, so the output is always aligned.
    """
    usable = len(pcm) // _GROUP_BYTES * _GROUP_BYTES
    samples = array("h")
    samples.frombytes(pcm[:usable])
    return array("h", (sum(samples[i:i + 6]) // 6 for i in range(0, len(samples), 6))).tobytes()

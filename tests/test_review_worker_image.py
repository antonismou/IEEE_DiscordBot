"""REVIEW reproduction (CRITICAL): the worker image cannot decode a single WAV.

worker/requirements.txt asks for faster-whisper>=1.1,<2 and leaves PyAV unpinned. A fresh build (2026-10-28) gets
faster-whisper 1.2.1 + av 19.0.2, and faster_whisper.audio.decode_audio calls
av.open(..., metadata_errors="ignore"), a keyword PyAV 19 no longer accepts:

    TypeError: open() got an unexpected keyword argument 'metadata_errors'

WhisperModel.transcribe(str(path)) goes through decode_audio, so every segment fails, every meeting ends
`failed` ("no transcript for N segment(s)"), and during a call every 2-minute live tick loads Whisper (3-4 GB)
only to fail again. Verified by building Dockerfile.worker; with av<16 the same code decodes fine.

The first test needs faster-whisper, so it is skipped in the dev suite; run it inside the worker image:
    docker compose --profile job build worker
    docker compose --profile job run --rm --no-deps -v "$PWD/tests:/app/tests:ro" worker \
        python -m pytest -q -p no:cacheprovider tests/test_review_worker_image.py   (needs pytest in the image)
or simply: docker compose --profile job run --rm --no-deps worker python -c \
    "import wave;w=wave.open('/tmp/a.wav','wb');w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);\
w.writeframes(bytes(640));w.close();from faster_whisper.audio import decode_audio;print(decode_audio('/tmp/a.wav'))"
"""
import re
import wave
from pathlib import Path

import pytest

from bot.recording.segments import SegmentWriter

ROOT = Path(__file__).resolve().parent.parent


def test_review_faster_whisper_decodes_a_segment_the_bot_writes(tmp_path):
    audio = pytest.importorskip("faster_whisper.audio")
    writer = SegmentWriter(tmp_path)
    writer.add(7, b"\x01\x00" * 16000, 0.0)
    writer.close()
    samples = audio.decode_audio(str(tmp_path / "7" / "000001.wav"))
    assert len(samples) == 16000


def test_review_worker_requirements_constrain_pyav():
    """The guard that runs everywhere: PyAV must be pinned (or the WAVs read without PyAV, e.g. with `wave` and
    passed to transcribe() as a float32 array, which also removes the dependency on faster-whisper's decoder)."""
    requirements = (ROOT / "worker" / "requirements.txt").read_text(encoding="utf-8")
    transcribe = (ROOT / "worker" / "transcribe.py").read_text(encoding="utf-8")
    pinned = re.search(r"^av\s*[<=~]", requirements, re.MULTILINE)
    decodes_itself = "wave.open" in transcribe or "readframes" in transcribe
    assert pinned or decodes_itself

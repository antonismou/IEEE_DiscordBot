"""The worker image must decode the WAVs the bot writes.

faster-whisper 1.2.1 with PyAV 19 could not decode a single file (`av.open() got an unexpected keyword argument
'metadata_errors'`), so every segment failed. worker/transcribe.py now reads the WAVs itself and hands Whisper the
samples, and worker/requirements.txt pins faster-whisper and keeps PyAV below 16 as well.

The tests that need faster-whisper are skipped in the dev suite; run them inside the worker image:
    docker build -f Dockerfile.worker -t ieee-worker:test .
    docker run --rm -u 1000 -e HOME=/tmp -v "$PWD":/src:ro --entrypoint bash ieee-worker:test -c \
        'cp -r /src /tmp/work && cd /tmp/work && pip install -q --user pytest && \
         python -m pytest -q -p no:cacheprovider tests/test_worker_image.py tests/test_worker_transcribe.py'
"""
import re
from pathlib import Path

import pytest

from bot.recording.segments import SegmentWriter

ROOT = Path(__file__).resolve().parent.parent


def test_faster_whisper_decodes_a_segment_the_bot_writes(tmp_path):
    audio = pytest.importorskip("faster_whisper.audio")
    writer = SegmentWriter(tmp_path)
    writer.add(7, b"\x01\x00" * 16000, 0.0)
    writer.close()
    samples = audio.decode_audio(str(tmp_path / "7" / "000001.wav"))
    assert len(samples) == 16000


def test_the_worker_reads_a_segment_like_faster_whisper_would(tmp_path):
    audio = pytest.importorskip("faster_whisper.audio")
    from worker.transcribe import read_wav

    writer = SegmentWriter(tmp_path)
    writer.add(7, bytes(range(256)) * 250, 0.0)
    writer.close()
    path = tmp_path / "7" / "000001.wav"
    ours, theirs = read_wav(path), audio.decode_audio(str(path))
    assert ours.dtype == theirs.dtype and ours.shape == theirs.shape
    assert abs(ours - theirs).max() < 1e-6


def test_worker_requirements_pin_faster_whisper_and_constrain_pyav():
    requirements = (ROOT / "worker" / "requirements.txt").read_text(encoding="utf-8")
    transcribe = (ROOT / "worker" / "transcribe.py").read_text(encoding="utf-8")
    assert re.search(r"^faster-whisper==", requirements, re.MULTILINE)
    assert re.search(r"^av\s*[<=~>].*<\s*16", requirements, re.MULTILINE)
    assert "wave.open" in transcribe  # the WAVs are read without PyAV
    dockerfile = (ROOT / "Dockerfile.worker").read_text(encoding="utf-8")
    assert re.search(r'"requests>=[\d.]+,<\d+"', dockerfile)

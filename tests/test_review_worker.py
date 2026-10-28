"""REVIEW reproductions (adversarial review of the meeting recorder, 2026-10-28).

Every test here FAILS on the current code, for the reason in its docstring. They are bug reports, not specs of the
fix: a fix may change the assertions as long as the failure scenario is handled.
"""
from datetime import datetime
from pathlib import Path

from bot.recording.meeting import Meeting, find_busy, recover_interrupted
from bot.recording.segments import SegmentWriter
from worker.run import Config, run
from worker.transcribe import transcribe_meeting

NOW = datetime(2026, 10, 25, 18, 30, 5)


def test_review_audio_of_a_segment_open_when_the_bot_died_is_never_deleted(tmp_path):
    """MEDIUM (privacy + lost speech). A segment is added to the manifest only when it is closed. If the bot is
    killed mid-call, every speaker's open WAV stays on disk, is not in the manifest, so the worker never
    transcribes or deletes it: raw audio is kept forever, against "audio is deleted after transcription"."""
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    writer = SegmentWriter(meeting.directory, on_close=meeting.add_segment)
    writer.add(7, b"\x01\x00" * 16000, 0.0)
    writer.add(7, b"\x01\x00" * 16000, 5.0)          # first segment closed (gap), second one open...
    # ... and the bot is killed here: no writer.close()
    recover_interrupted(tmp_path)

    class Model:
        def transcribe(self, path, **options):
            return iter([type("P", (), {"text": "λόγια"})()]), None

    transcribe_meeting(meeting.directory, model_factory=lambda name, threads: Model())
    left = sorted(p.relative_to(meeting.directory).as_posix() for p in meeting.directory.rglob("*.wav"))
    assert left == []


def test_review_one_unreadable_manifest_stops_every_other_meeting(tmp_path):
    """MEDIUM. all_meetings() does json.loads on every manifest with no error handling. One bad manifest (a hand
    edit per deploy/README.md's "set its status back to queued", a disk-full truncation) makes find_busy (so
    /record start), the bot's startup recovery in cog_load (so the whole recorder cog fails to load), the poster
    and every worker run raise. Expected: skip/log the bad folder and keep serving the others."""
    good = Meeting.create(tmp_path, 11, 22, NOW)
    good.set_status("queued")
    bad = tmp_path / "20261026-100000"
    bad.mkdir()
    (bad / "manifest.json").write_text('{"id": "20261026-100000", "status": "queued",', encoding="utf-8")

    assert find_busy(tmp_path) is not None                # must not raise
    calls = []
    run(Config(meetings_dir=str(tmp_path)), transcribe=lambda d: calls.append(d),
        summarize=lambda d: (Path(d) / "summary.md").write_text("x"), wait=lambda: None)
    assert Path(calls[0]).name == good.id

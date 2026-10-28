"""REVIEW reproductions (adversarial review of the meeting recorder, 2026-10-28).

Every test here FAILS on the current code, for the reason in its docstring. They are bug reports, not specs of the
fix: a fix may change the assertions as long as the failure scenario is handled.
"""
import json
import wave
from datetime import datetime
from pathlib import Path

import pytest

from bot.recording.meeting import Meeting, find_busy, recover_interrupted
from bot.recording.segments import SegmentWriter
from worker.run import Config, run
from worker.summarize import ensure_headings, make_ollama_generate, summarize_meeting
from worker.transcribe import transcribe_meeting

NOW = datetime(2026, 10, 25, 18, 30, 5)
GOOD = "## Θέματα\n- α\n\n## Αποφάσεις\n- β\n\n## Εργασίες\n- γ\n\n## Ανοιχτά θέματα\n- δ"


def _meeting_with_transcript(root, lines, speakers):
    meeting = Meeting.create(root, 11, 22, NOW)
    for user_id, name in speakers.items():
        meeting.set_speaker(user_id, name)
    (meeting.directory / "transcript.txt").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return meeting


def test_review_backslash_in_a_display_name_crashes_the_summary(tmp_path):
    """HIGH. worker/summarize.py with_names passes the display name to re.sub as the *replacement template*, so a
    name containing a backslash (Discord allows it, e.g. "Νίκος\\d" or a kaomoji like "¯\\_(ツ)_/¯") raises
    re.error even when nothing matches. Any meeting longer than one window then fails as a whole."""
    meeting = _meeting_with_transcript(
        tmp_path, ["[00:00:05] Νίκος: Καλησπέρα", "[00:30:00] Νίκος: Τέλος"], {7: "Νίκος \\o/"})
    summary = summarize_meeting(meeting.directory, generate=lambda prompt: GOOD)
    assert "Αποφάσεις" in summary


def test_review_the_alternative_spelling_of_open_questions_is_misfiled():
    """MEDIUM. "Ανοικτά θέματα" is an equally correct Greek spelling of "Ανοιχτά θέματα". ensure_headings does not
    recognise it, so the open questions end up under Εργασίες and the real section says «Δεν υπάρχουν.»."""
    fixed = ensure_headings("## Θέματα\n- α\n## Αποφάσεις\n- β\n## Εργασίες\n- γ\n## Ανοικτά θέματα\n- δ")
    open_questions = fixed.split("## Ανοιχτά θέματα\n")[1]
    assert "- δ" in open_questions


def test_review_inline_bold_headings_put_everything_under_the_first_section():
    """MEDIUM. A model answering "**Αποφάσεις:** ..." (heading and content on one line, a common Markdown style)
    is not recognised as a heading: every section lands under Θέματα and the summary then states that there were
    no decisions and no tasks («Δεν υπάρχουν.»), which is false."""
    fixed = ensure_headings("**Θέματα:** Προϋπολογισμός\n**Αποφάσεις:** Workshop στις 12/11\n**Εργασίες:** Μαρία")
    decisions = fixed.split("## Αποφάσεις\n")[1].split("\n\n")[0]
    assert "Workshop" in decisions


def test_review_an_empty_model_answer_is_accepted_and_cached_as_window_notes(tmp_path):
    """MEDIUM. If Ollama answers 200 with an empty "response" (it does this e.g. for a model still loading, or a
    truncated/aborted generation), the empty notes are saved and trusted on every later run, and the meeting is
    marked done with a summary claiming «Δεν υπάρχουν.» everywhere. Expected: treat an empty answer as an error."""
    meeting = _meeting_with_transcript(
        tmp_path, ["[00:00:05] Μαρία: Αποφασίσαμε workshop", "[00:30:00] Μαρία: Τέλος"], {7: "Μαρία"})
    with pytest.raises(Exception):
        summarize_meeting(meeting.directory, generate=lambda prompt: "")


class _Response:
    ok = True
    status_code = 200
    text = "<html>proxy error</html>"

    def json(self):
        raise json.JSONDecodeError("Expecting value", self.text, 0)


class _Session:
    def __init__(self):
        self.calls = 0

    def post(self, url, json, timeout):
        self.calls += 1
        if self.calls == 1:
            return _Response()
        good = _Response()
        good.json = lambda: {"response": GOOD}
        return good


def test_review_a_non_json_ollama_reply_is_not_retried():
    """LOW. make_ollama_generate only wraps HTTP/connection errors in OllamaError; a 200 with a body that is not
    JSON (or JSON that is not an object) raises ValueError/AttributeError, which _call does not retry and which
    fails the meeting on the first hiccup."""
    session = _Session()
    from worker.summarize import _call

    assert _call(make_ollama_generate(session=session), "x", "test") == GOOD


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

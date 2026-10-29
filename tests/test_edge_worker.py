"""Edge cases for the processing job (worker/run.py, worker/transcribe.py), written from the design spec and
deploy/README.md, treating the implementation as a black box."""
import fcntl
import json
import os
import signal
import wave
from datetime import datetime
from functools import partial

import pytest

import worker.run as worker_run
from bot.recording.meeting import Meeting
from worker.run import LOCK_BUSY, LOCK_FILE, STOPPED, Config, run
from worker.summarize import notes_path, summarize_meeting
from worker.transcribe import transcribe_meeting

NOW = datetime(2026, 10, 25, 18, 0, 0)


# --- helpers ------------------------------------------------------------------------------------------------------

def write_wav(path, seconds=1.0, sample=b"\x10\x00"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(sample * int(16000 * seconds))


def make_meeting(root, segments=(), status="queued", minute=0, names=None):
    """segments: (speaker, number, start[, kind]) with kind 'wav' (default), 'txt:<text>', 'empty', 'short',
    'garbage' or 'none' (listed in the manifest but no file at all)."""
    meeting = Meeting.create(root, 11, 22, NOW.replace(minute=minute))
    for user_id, name in (names or {7: "Μαρία", 8: "Νίκος"}).items():
        meeting.set_speaker(user_id, name)
    for speaker, number, start, *rest in segments:
        kind = rest[0] if rest else "wav"
        relative = f"{speaker}/{number:06d}.wav"
        audio = meeting.directory / relative
        audio.parent.mkdir(parents=True, exist_ok=True)
        if kind == "wav":
            write_wav(audio)
        elif kind.startswith("txt:"):
            audio.with_suffix(".txt").write_text(kind[4:], encoding="utf-8")
        elif kind == "empty":
            audio.write_bytes(b"")
        elif kind == "short":
            write_wav(audio, seconds=0.05)
        elif kind == "garbage":
            audio.write_bytes(b"this is not a wav file at all" * 10)
        meeting.add_segment({"speaker": speaker, "file": relative, "start": start, "duration": 1.0})
    meeting.set_status(status)
    return meeting


def manifest(meeting):
    return json.loads((meeting.directory / "manifest.json").read_text(encoding="utf-8"))


def status_of(meeting):
    return manifest(meeting)["status"]


def wavs(meeting):
    return sorted(p.relative_to(meeting.directory).as_posix() for p in meeting.directory.rglob("*.wav"))


def attempts(meeting):
    path = meeting.directory / "attempts.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class Piece:
    def __init__(self, text):
        self.text = text


class FakeWhisper:
    """A Whisper model that answers with the file's number, so the transcript shows which segment is which."""

    def __init__(self, log, factory):
        self.log = log
        self.factory = factory

    def transcribe(self, samples, **kwargs):
        if self.factory.fail_times:
            self.factory.fail_times -= 1
            raise RuntimeError("whisper hiccup")
        self.log.append(len(samples))
        return iter([Piece(f" λόγια{len(self.log)} ")]), None


class Factory:
    def __init__(self, fail_load=False, fail_times=0):
        self.loads = 0
        self.calls = []
        self.fail_load = fail_load
        self.fail_times = fail_times

    def __call__(self, name, threads):
        self.loads += 1
        if self.fail_load:
            raise RuntimeError("cannot load whisper")
        return FakeWhisper(self.calls, self)


def real_transcribe(factory):
    return partial(transcribe_meeting, model_factory=factory)


class Summary:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def __call__(self, directory):
        self.calls += 1
        if self.error:
            raise self.error
        (directory / "summary.md").write_text("## Θέματα\n- κάτι\n", encoding="utf-8")


def go(root, transcribe, summarize=None, wait=None, **config):
    return run(Config(meetings_dir=str(root), **config), transcribe=transcribe, summarize=summarize or Summary(),
               wait=wait or (lambda: None))


class Killed(BaseException):
    """Stands in for the worker being killed outright (SIGKILL, the memory limit): nothing after it runs."""


def killing_transcribe(directory):
    raise Killed()


def run_killed(root):
    with pytest.raises(Killed):
        go(root, killing_transcribe)


# --- odd meetings -------------------------------------------------------------------------------------------------

def test_a_meeting_with_zero_segments_is_done_without_loading_whisper_or_waiting_for_ollama(tmp_path):
    meeting = make_meeting(tmp_path, [])
    factory, summary, waits = Factory(), Summary(), []

    assert go(tmp_path, real_transcribe(factory), summary, wait=lambda: waits.append(1)) == 0

    assert status_of(meeting) == "done" and factory.loads == 0 and waits == [] and summary.calls == 1
    assert (meeting.directory / "transcript.txt").read_text(encoding="utf-8") == ""
    assert not (meeting.directory / "attempts.json").exists()


def test_a_meeting_where_every_segment_is_silent_or_empty_is_done_without_loading_whisper(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0, "empty"), (7, 2, 5.0, "short"), (8, 1, 3.0, "short")])
    factory = Factory()

    assert go(tmp_path, real_transcribe(factory)) == 0

    assert status_of(meeting) == "done" and factory.loads == 0
    assert (meeting.directory / "transcript.txt").read_text(encoding="utf-8") == ""
    assert wavs(meeting) == []
    for number, speaker in ((1, 7), (2, 7), (1, 8)):
        assert (meeting.directory / f"{speaker}/{number:06d}.txt").read_text(encoding="utf-8") == ""


def test_one_unreadable_segment_among_many_is_lost_and_the_rest_finish_in_order(tmp_path):
    segments = [(7, n, float(10 * n)) for n in range(1, 6)] + [(8, 1, 25.0, "garbage"), (8, 2, 35.0)]
    meeting = make_meeting(tmp_path, segments)
    factory = Factory()

    assert go(tmp_path, real_transcribe(factory)) == 0

    assert status_of(meeting) == "done" and factory.loads == 1
    lines = (meeting.directory / "transcript.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6                                   # 7 segments, one lost
    stamps = [line.split("]")[0] for line in lines]
    assert stamps == sorted(stamps)
    assert [line[:10] for line in lines] == ["[00:00:10]", "[00:00:20]", "[00:00:30]", "[00:00:35]",
                                             "[00:00:40]", "[00:00:50]"]
    assert (meeting.directory / "8/000001.txt").read_text(encoding="utf-8") == ""
    assert wavs(meeting) == []


def test_a_segment_listed_without_audio_or_text_is_lost_and_does_not_block_the_meeting(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0), (7, 2, 4.0, "none"), (8, 1, 2.0)])

    assert go(tmp_path, real_transcribe(Factory())) == 0

    assert status_of(meeting) == "done"
    assert len((meeting.directory / "transcript.txt").read_text(encoding="utf-8").splitlines()) == 2


def test_a_segment_whisper_fails_on_once_is_retried_in_the_same_run(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    factory = Factory(fail_times=1)

    assert go(tmp_path, real_transcribe(factory)) == 0
    assert status_of(meeting) == "done"


# --- requeue (reprocessing) ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["queued", "transcribing", "summarizing"])
def test_a_requeued_meeting_with_some_texts_finishes_with_every_line_in_start_order(tmp_path, status):
    """Texts that exist are reused, the missing ones transcribed, and speakers interleave by start offset."""
    meeting = make_meeting(tmp_path, [
        (7, 1, 0.0, "txt:πρώτο"), (8, 1, 10.0), (7, 2, 20.0, "txt:τρίτο"), (8, 2, 30.0), (7, 3, 3725.0),
    ], status=status)
    factory = Factory()

    assert go(tmp_path, real_transcribe(factory)) == 0

    assert status_of(meeting) == "done"
    lines = (meeting.directory / "transcript.txt").read_text(encoding="utf-8").splitlines()
    assert [line.split(" ", 2)[:2] for line in lines] == [
        ["[00:00:00]", "Μαρία:"], ["[00:00:10]", "Νίκος:"], ["[00:00:20]", "Μαρία:"], ["[00:00:30]", "Νίκος:"],
        ["[01:02:05]", "Μαρία:"],
    ]
    assert lines[0].endswith("πρώτο") and lines[2].endswith("τρίτο")
    assert len(factory.calls) == 3                           # only the three without a text


def test_a_failed_meeting_requeued_while_summarizing_reuses_its_window_notes(tmp_path):
    """Notes for window 0 survive from the failed run, so only window 1 and the merge are asked of the model."""
    meeting = make_meeting(tmp_path, [(7, 1, 5.0, "txt:αρχή"), (8, 1, 800.0, "txt:μετά")], status="failed")
    path = notes_path(meeting.directory, 0, 12)
    path.parent.mkdir(parents=True)
    path.write_text("## Θέματα\n- σημειώσεις παραθύρου 0\n", encoding="utf-8")
    Meeting.load(meeting.directory).set_status("summarizing")             # set back by hand
    prompts = []

    def generate(prompt):
        prompts.append(prompt)
        return "## Θέματα\n- απάντηση\n## Αποφάσεις\n## Εργασίες\n## Ανοιχτά θέματα\n"

    summarize = partial(summarize_meeting, generate=generate, chunk_minutes=12)
    factory = Factory()
    assert go(tmp_path, real_transcribe(factory), summarize) == 0

    assert status_of(meeting) == "done" and factory.loads == 0
    assert len(prompts) == 2                                              # window 1, then the merge
    assert "μετά" in prompts[0] and "αρχή" not in prompts[0]
    assert "σημειώσεις παραθύρου 0" in prompts[1]
    assert notes_path(meeting.directory, 1, 12).is_file()
    lines = (meeting.directory / "transcript.txt").read_text(encoding="utf-8").splitlines()
    assert lines == ["[00:00:05] Μαρία: αρχή", "[00:13:20] Νίκος: μετά"]


def test_a_meeting_given_up_and_requeued_finishes_with_the_lost_segments_left_out(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0, "txt:κρατήθηκε"), (8, 1, 5.0)])
    for _ in range(3):
        run_killed(tmp_path)
    assert go(tmp_path, killing_transcribe) == 1                         # the 4th run gives up
    assert status_of(meeting) == "failed" and wavs(meeting) == []

    Meeting.load(meeting.directory).set_status("queued")
    assert go(tmp_path, real_transcribe(Factory())) == 0
    assert status_of(meeting) == "done"
    assert (meeting.directory / "transcript.txt").read_text(encoding="utf-8") == "[00:00:00] Μαρία: κρατήθηκε\n"


# --- attempts -----------------------------------------------------------------------------------------------------

def test_three_runs_that_never_return_are_allowed_and_the_fourth_gives_up(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    for number in range(1, 4):
        run_killed(tmp_path)
        assert status_of(meeting) == "transcribing" and wavs(meeting) == ["7/000001.wav"]
        assert attempts(meeting)["full"] == number
    transcribed = []

    assert go(tmp_path, lambda d: transcribed.append(d)) == 1

    assert transcribed == []                                              # not even tried again
    assert status_of(meeting) == "failed" and wavs(meeting) == []
    assert "3" in Meeting.load(meeting.directory).error
    assert not (meeting.directory / "attempts.json").exists()


def test_with_max_attempts_one_the_second_run_gives_up(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    run_killed(tmp_path)
    assert go(tmp_path, killing_transcribe, max_attempts=1) == 1
    assert status_of(meeting) == "failed"


def test_runs_that_return_with_a_failed_transcription_count_and_the_last_error_is_given(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    factory = Factory(fail_load=True)
    for _ in range(3):
        assert go(tmp_path, real_transcribe(factory)) == 1
        assert status_of(meeting) == "transcribing" and wavs(meeting) == ["7/000001.wav"]
    assert go(tmp_path, real_transcribe(factory)) == 1
    assert factory.loads == 3
    error = Meeting.load(meeting.directory).error
    assert status_of(meeting) == "failed" and "cannot load whisper" in error
    assert wavs(meeting) == []


def test_runs_that_keep_saving_new_texts_never_use_up_the_attempts(tmp_path):
    """Each run is killed after transcribing one more segment: it got somewhere, so the count starts again."""
    meeting = make_meeting(tmp_path, [(7, n, float(n)) for n in range(1, 7)])

    def one_then_die(directory):
        for segment in manifest(meeting)["segments"]:
            audio = directory / segment["file"]
            if audio.exists():
                audio.with_suffix(".txt").write_text("x", encoding="utf-8")
                audio.unlink()
                break
        raise Killed()

    for _ in range(6):
        with pytest.raises(Killed):
            go(tmp_path, one_then_die)
        assert status_of(meeting) == "transcribing"
    assert go(tmp_path, real_transcribe(Factory())) == 0
    assert status_of(meeting) == "done"


def test_saved_window_notes_count_as_progress_too(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0, "txt:α"), (8, 1, 800.0, "txt:β"), (8, 2, 1600.0, "txt:γ")],
                           status="summarizing")
    windows = iter([0, 1, 2, 3, 4])

    def note_then_die(directory):
        path = notes_path(directory, next(windows), 12)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("σημειώσεις", encoding="utf-8")
        raise Killed()

    for _ in range(5):
        with pytest.raises(Killed):
            go(tmp_path, killing_transcribe, note_then_die)
    assert status_of(meeting) == "summarizing"                            # five runs, none counted twice in a row


def test_a_run_stopped_by_sigterm_is_not_counted(tmp_path):
    """Two killed runs, one SIGTERM, one killed run: three counted, so the next run gives up, not the one before."""
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    run_killed(tmp_path)
    run_killed(tmp_path)

    def sigterm(directory):
        os.kill(os.getpid(), signal.SIGTERM)

    assert go(tmp_path, sigterm) == STOPPED
    assert attempts(meeting).get("full") == 2 and status_of(meeting) == "transcribing"
    run_killed(tmp_path)
    assert attempts(meeting).get("full") == 3 and status_of(meeting) == "transcribing"
    assert go(tmp_path, killing_transcribe) == 1
    assert status_of(meeting) == "failed"


def test_a_real_sigterm_mid_stage_ends_the_run_leaves_the_meeting_and_restores_the_handler(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    other = make_meeting(tmp_path, [(7, 1, 0.0)], minute=30)
    seen = []

    def previous(signum, frame):
        seen.append(signum)

    old = signal.signal(signal.SIGTERM, previous)
    after_stage = []
    try:
        def sigterm(directory):
            os.kill(os.getpid(), signal.SIGTERM)
            after_stage.append("not reached")  # the handler raises at the next bytecode, before this

        assert go(tmp_path, sigterm) == STOPPED
        handler_after = signal.getsignal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, old)

    assert handler_after is previous and seen == []                       # restored, and the worker's ran instead
    assert after_stage == []
    assert status_of(meeting) == "transcribing" and wavs(meeting) == ["7/000001.wav"]
    assert attempts(meeting).get("full", 0) == 0
    assert status_of(other) == "queued" and attempts(other) == {}        # the run ended; not started on it


def test_sigterm_while_no_meeting_is_being_processed(tmp_path, monkeypatch):
    """SIGTERM arrives between two meetings: the finished one keeps its result, the next is untouched."""
    first = make_meeting(tmp_path, [(7, 1, 0.0, "txt:α")])
    second = make_meeting(tmp_path, [(7, 1, 0.0)], minute=30)
    real = worker_run.all_meetings
    calls = []

    def all_meetings(root):
        calls.append(1)
        if len(calls) == 2:                                               # looking for the next meeting
            os.kill(os.getpid(), signal.SIGTERM)
        return real(root)

    monkeypatch.setattr(worker_run, "all_meetings", all_meetings)
    old = signal.getsignal(signal.SIGTERM)

    assert go(tmp_path, real_transcribe(Factory())) == STOPPED

    assert signal.getsignal(signal.SIGTERM) is old
    assert status_of(first) == "done" and attempts(first) == {}
    assert status_of(second) == "queued" and attempts(second) == {} and wavs(second) == ["7/000001.wav"]


def test_sigterm_during_a_live_pass_is_not_counted(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)], status="recording")
    before = (meeting.directory / "manifest.json").read_bytes()

    def sigterm(directory):
        os.kill(os.getpid(), signal.SIGTERM)

    assert go(tmp_path, sigterm, live=True) == STOPPED
    assert attempts(meeting).get("live", 0) == 0
    assert (meeting.directory / "manifest.json").read_bytes() == before


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe\x00garbage", b"[1, 2, 3]", b'"text"', b"",
                                     b'{"full": "many", "error": 5}', b"null"])
def test_a_corrupt_attempts_file_is_treated_as_no_attempts(tmp_path, content):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    (meeting.directory / "attempts.json").write_bytes(content)

    assert go(tmp_path, real_transcribe(Factory())) == 0

    assert status_of(meeting) == "done" and not (meeting.directory / "attempts.json").exists()


def test_a_corrupt_attempts_file_still_counts_from_then_on(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    (meeting.directory / "attempts.json").write_bytes(b"\x00\x01garbage")
    run_killed(tmp_path)
    assert attempts(meeting)["full"] == 1


# --- several meetings ---------------------------------------------------------------------------------------------

def test_a_meeting_that_keeps_failing_does_not_hold_up_the_next_one(tmp_path):
    bad = make_meeting(tmp_path, [(7, 1, 0.0)], minute=0)
    good = make_meeting(tmp_path, [(7, 1, 0.0)], minute=30)
    order = []
    factory = Factory()

    def transcribe(directory):
        order.append(directory.name)
        if directory == bad.directory:
            raise RuntimeError("cannot load whisper")
        return transcribe_meeting(directory, model_factory=factory)

    assert go(tmp_path, transcribe) == 1
    assert order[0] == bad.id and good.id in order                       # oldest first, then the next one
    assert status_of(good) == "done" and status_of(bad) == "transcribing"
    for _ in range(3):
        go(tmp_path, transcribe)
    assert status_of(bad) == "failed" and status_of(good) == "done"


def test_a_run_that_was_killed_on_the_oldest_meeting_still_reaches_the_newer_one_eventually(tmp_path):
    bad = make_meeting(tmp_path, [(7, 1, 0.0)], minute=0)
    good = make_meeting(tmp_path, [(7, 1, 0.0)], minute=30)
    factory = Factory()

    def transcribe(directory):
        if directory == bad.directory:
            raise Killed()
        return transcribe_meeting(directory, model_factory=factory)

    for _ in range(3):
        with pytest.raises(Killed):
            go(tmp_path, transcribe)
    assert status_of(good) == "queued"
    assert go(tmp_path, transcribe) == 1                                 # gives up on the bad one, does the good
    assert status_of(bad) == "failed" and status_of(good) == "done"


# --- audio deletion -----------------------------------------------------------------------------------------------

def stray(meeting):
    """A WAV in a speaker folder the manifest does not list (a segment open when the bot died)."""
    path = meeting.directory / "9" / "000004.wav"
    write_wav(path)
    return path


def test_a_retryable_transcription_failure_keeps_all_audio_including_unlisted_files(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0), (8, 1, 1.0)])
    extra = stray(meeting)

    assert go(tmp_path, real_transcribe(Factory(fail_load=True))) == 1

    assert extra.exists() and wavs(meeting) == ["7/000001.wav", "8/000001.wav", "9/000004.wav"]


def test_a_segment_whisper_keeps_failing_on_keeps_its_audio_for_the_next_run(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0), (8, 1, 1.0)])
    factory = Factory(fail_times=10 ** 6)

    assert go(tmp_path, real_transcribe(factory)) == 1

    assert status_of(meeting) == "transcribing" and wavs(meeting) == ["7/000001.wav", "8/000001.wav"]


def test_a_done_meeting_has_no_audio_left_even_files_the_manifest_does_not_list(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    stray(meeting)

    assert go(tmp_path, real_transcribe(Factory())) == 0
    assert status_of(meeting) == "done" and wavs(meeting) == []


def test_a_meeting_given_up_has_no_audio_left_but_keeps_texts_and_notes(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0, "txt:κείμενο"), (8, 1, 1.0)])
    stray(meeting)
    path = notes_path(meeting.directory, 0, 12)
    path.parent.mkdir(parents=True)
    path.write_text("σημειώσεις", encoding="utf-8")
    for _ in range(3):
        run_killed(tmp_path)

    go(tmp_path, killing_transcribe)

    assert status_of(meeting) == "failed" and wavs(meeting) == []
    assert (meeting.directory / "7/000001.txt").read_text(encoding="utf-8") == "κείμενο" and path.is_file()


def test_a_killed_run_deletes_no_audio(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    stray(meeting)
    run_killed(tmp_path)
    assert wavs(meeting) == ["7/000001.wav", "9/000004.wav"]


def test_a_summary_failure_fails_the_meeting_and_deletes_its_audio_but_keeps_its_texts(tmp_path):
    """deploy/README.md: a meeting whose summary failed keeps its segment texts and notes only."""
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    stray(meeting)

    assert go(tmp_path, real_transcribe(Factory()), Summary(error=RuntimeError("ollama 500"))) == 1

    assert status_of(meeting) == "failed" and "ollama 500" in Meeting.load(meeting.directory).error
    assert wavs(meeting) == []
    assert (meeting.directory / "7/000001.txt").exists()


# --- live passes --------------------------------------------------------------------------------------------------

def test_a_live_pass_transcribes_but_never_writes_the_manifest(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0), (7, 2, 3.0, "garbage"), (8, 1, 1.0, "none")],
                           status="recording")
    path = meeting.directory / "manifest.json"
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns

    assert go(tmp_path, real_transcribe(Factory()), live=True) == 0

    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    assert (meeting.directory / "7/000001.txt").exists()


def test_a_failing_live_pass_never_writes_the_manifest_or_fails_the_meeting(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)], status="recording")
    path = meeting.directory / "manifest.json"
    before = path.read_bytes()

    for _ in range(5):
        go(tmp_path, real_transcribe(Factory(fail_load=True)), live=True)

    assert path.read_bytes() == before and wavs(meeting) == ["7/000001.wav"]


def test_live_passes_that_keep_dying_stop_but_never_touch_the_manifest(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)], status="recording")
    path = meeting.directory / "manifest.json"
    before = path.read_bytes()
    for _ in range(3):
        with pytest.raises(Killed):
            go(tmp_path, killing_transcribe, live=True)
    tried = []

    go(tmp_path, lambda d: tried.append(d), live=True)

    assert tried == [] and path.read_bytes() == before and wavs(meeting) == ["7/000001.wav"]
    # once the call ends the full run still processes it
    Meeting.load(meeting.directory).set_status("queued")
    assert go(tmp_path, real_transcribe(Factory())) == 0
    assert status_of(meeting) == "done"


def test_a_live_run_leaves_ended_meetings_alone(tmp_path):
    ended = make_meeting(tmp_path, [(7, 1, 0.0)], status="queued")
    tried = []
    assert go(tmp_path, lambda d: tried.append(d), live=True) == 0
    assert tried == [] and status_of(ended) == "queued" and attempts(ended) == {}


# --- the lock -----------------------------------------------------------------------------------------------------

def test_another_worker_holding_the_lock_returns_75_and_touches_nothing(tmp_path):
    meeting = make_meeting(tmp_path, [(7, 1, 0.0)])
    tried = []
    with open(tmp_path / LOCK_FILE, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = signal.getsignal(signal.SIGTERM)
        assert go(tmp_path, lambda d: tried.append(d)) == LOCK_BUSY == 75
        assert signal.getsignal(signal.SIGTERM) is old
    assert tried == [] and status_of(meeting) == "queued" and attempts(meeting) == {}
    assert go(tmp_path, real_transcribe(Factory())) == 0                 # free again once released

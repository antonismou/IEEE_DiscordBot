import fcntl
import gc
import json
import weakref
from datetime import datetime, timedelta

import pytest

from bot.recording.meeting import Meeting
from worker.run import DEFAULT_MEETINGS_DIR, LOCK_BUSY, LOCK_FILE, Config, WorkerError, main, process, run, wait_for_ollama

NOW = datetime(2026, 10, 25, 18, 30, 5)
SUMMARY = "## Θέματα\n- Workshop\n"


def meeting_with(root, segments, status="queued", minute=30, transcribed=()):
    """A meeting with WAV segments `(speaker, number, start)`; those in `transcribed` already have their text."""
    meeting = Meeting.create(root, 11, 22, NOW.replace(minute=minute))
    meeting.set_speaker(7, "Μαρία")
    for speaker, number, start in segments:
        relative = f"{speaker}/{number:06d}.wav"
        audio = meeting.directory / relative
        audio.parent.mkdir(parents=True, exist_ok=True)
        if (speaker, number) in transcribed:
            audio.with_suffix(".txt").write_text(f"ήδη {number}", encoding="utf-8")
        else:
            audio.write_bytes(b"RIFF")
        meeting.add_segment({"speaker": speaker, "file": relative, "start": start, "duration": 1.0})
    meeting.set_status(status)
    return meeting


def status_of(meeting):
    return json.loads((meeting.directory / "manifest.json").read_text(encoding="utf-8"))["status"]


class Fakes:
    """Fake stages that write what the real ones would and record the order of events."""

    def __init__(self, fail_segments=(), summary_error=None):
        self.events = []
        self.fail_segments = set(fail_segments)  # files that never get a transcript
        self.summary_error = summary_error
        self.model = None

    def transcribe(self, directory):
        meeting = Meeting.load(directory)
        self.events.append(("transcribe", meeting.id, meeting.status))
        model = Model()
        self.model = weakref.ref(model)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        for segment in manifest["segments"]:
            audio = directory / segment["file"]
            if segment["file"] in self.fail_segments or not audio.exists():
                continue
            audio.with_suffix(".txt").write_text("Καλησπέρα.", encoding="utf-8")
            audio.unlink()
        return 0

    def wait(self):
        self.events.append(("wait",))

    def summarize(self, directory):
        meeting = Meeting.load(directory)
        self.events.append(("summarize", meeting.id, meeting.status, self.model is None or self.model() is None,
                            (directory / "transcript.txt").exists()))
        if self.summary_error:
            raise self.summary_error
        (directory / "summary.md").write_text(SUMMARY, encoding="utf-8")
        return SUMMARY

    def run(self, root):
        return run(Config(meetings_dir=root), transcribe=self.transcribe, summarize=self.summarize, wait=self.wait)


class Model:
    """Holds a reference to itself, so only a garbage collection frees it, like a model with internal cycles."""

    def __init__(self):
        self.me = self


def test_a_queued_meeting_goes_through_both_stages_in_order(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0), (7, 2, 9.0)])
    fakes = Fakes()

    assert fakes.run(tmp_path) == 0

    assert fakes.events == [
        ("transcribe", meeting.id, "transcribing"),
        ("wait",),
        ("summarize", meeting.id, "summarizing", True, True),
    ]
    assert status_of(meeting) == "done" and Meeting.load(meeting.directory).error is None
    assert (meeting.directory / "transcript.txt").read_text(encoding="utf-8") == (
        "[00:00:00] Μαρία: Καλησπέρα.\n[00:00:09] Μαρία: Καλησπέρα.\n"
    )


def test_whisper_is_unloaded_before_the_summary_starts(tmp_path):
    meeting_with(tmp_path, [(7, 1, 0.0)])
    fakes = Fakes()
    gc.disable()  # so only the worker's own collection can free the model
    try:
        fakes.run(tmp_path)
    finally:
        gc.enable()
    assert fakes.events[-1][3] is True  # the self-referencing model was collected before summarize ran


def test_transcript_and_summary_exist_before_the_status_says_done(tmp_path, monkeypatch):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)])
    seen = []
    original = Meeting.set_status

    def spy(self, status):
        if status == "done":
            seen.append(((self.directory / "transcript.txt").exists(), (self.directory / "summary.md").exists()))
        original(self, status)
    monkeypatch.setattr(Meeting, "set_status", spy)

    assert Fakes().run(tmp_path) == 0
    assert seen == [(True, True)] and status_of(meeting) == "done"


def test_a_summary_that_did_not_write_its_file_fails_the_meeting(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)])
    fakes = Fakes()
    fakes.summarize = lambda directory: SUMMARY  # returns text but writes nothing

    assert fakes.run(tmp_path) != 0
    assert status_of(meeting) == "failed" and "summary.md" in Meeting.load(meeting.directory).error


def test_a_crash_while_transcribing_resumes_from_the_segments_left(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0), (7, 2, 9.0)], status="transcribing", transcribed={(7, 1)})
    fakes = Fakes()

    assert fakes.run(tmp_path) == 0
    assert [e[0] for e in fakes.events] == ["transcribe", "wait", "summarize"]
    assert (meeting.directory / "transcript.txt").read_text(encoding="utf-8") == (
        "[00:00:00] Μαρία: ήδη 1\n[00:00:09] Μαρία: Καλησπέρα.\n"
    )
    assert status_of(meeting) == "done"


def test_a_crash_while_summarizing_skips_transcription(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)], status="summarizing", transcribed={(7, 1)})
    fakes = Fakes()

    assert fakes.run(tmp_path) == 0
    assert [e[0] for e in fakes.events] == ["wait", "summarize"]
    assert (meeting.directory / "transcript.txt").read_text(encoding="utf-8") == "[00:00:00] Μαρία: ήδη 1\n"
    assert status_of(meeting) == "done"


def test_a_segment_still_missing_its_text_is_retried_once(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0), (7, 2, 9.0)])
    fakes = Fakes()
    calls = []
    first = fakes.transcribe

    def flaky(directory):
        calls.append(directory)
        fakes.fail_segments = {"7/000002.wav"} if len(calls) == 1 else set()
        return first(directory)
    fakes.transcribe = flaky

    assert fakes.run(tmp_path) == 0
    assert len(calls) == 2 and status_of(meeting) == "done"


def test_a_segment_that_fails_twice_fails_the_meeting(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0), (7, 2, 9.0)])
    fakes = Fakes(fail_segments={"7/000002.wav"})

    assert fakes.run(tmp_path) != 0

    assert [e[0] for e in fakes.events] == ["transcribe", "transcribe"]  # never reaches the summary
    loaded = Meeting.load(meeting.directory)
    assert loaded.status == "failed" and "7/000002.wav" in loaded.error
    assert not (meeting.directory / "summary.md").exists()


def test_a_segment_with_neither_audio_nor_text_fails_the_meeting(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)], status="summarizing")
    (meeting.directory / "7/000001.wav").unlink()

    assert Fakes().run(tmp_path) != 0
    assert status_of(meeting) == "failed" and "7/000001.wav" in Meeting.load(meeting.directory).error


def test_a_summary_error_fails_the_meeting_and_records_why(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)])
    fakes = Fakes(summary_error=RuntimeError("Ollama returned HTTP 500"))

    assert fakes.run(tmp_path) != 0

    loaded = Meeting.load(meeting.directory)
    assert loaded.status == "failed" and "Ollama returned HTTP 500" in loaded.error
    assert loaded.speakers == {7: "Μαρία"}  # the rest of the manifest is kept


def test_an_empty_transcript_still_ends_done(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)])
    fakes = Fakes()
    original = fakes.transcribe

    def silent(directory):
        original(directory)
        (directory / "7/000001.txt").write_text("", encoding="utf-8")  # nobody spoke
    fakes.transcribe = silent

    assert fakes.run(tmp_path) == 0
    assert (meeting.directory / "transcript.txt").read_text(encoding="utf-8") == ""
    assert ("wait",) not in fakes.events  # nothing to ask the model, so Ollama is not needed
    assert status_of(meeting) == "done" and (meeting.directory / "summary.md").exists()


def test_nothing_to_do_exits_cleanly(tmp_path):
    fakes = Fakes()
    assert fakes.run(tmp_path) == 0 and fakes.events == []
    assert fakes.run(tmp_path / "missing") == 0


@pytest.mark.parametrize("status", ["recording", "done", "failed", "interrupted"])
def test_meetings_that_need_no_work_are_left_alone(tmp_path, status):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)], status=status)
    fakes = Fakes()

    assert fakes.run(tmp_path) == 0
    assert fakes.events == [] and status_of(meeting) == status


def test_meetings_are_processed_oldest_first_and_a_failure_does_not_stop_the_rest(tmp_path):
    newer = meeting_with(tmp_path, [(7, 1, 0.0)], minute=50)
    older = meeting_with(tmp_path, [(7, 1, 0.0)], minute=10)
    (older.directory / "7/000001.wav").unlink()  # the older one cannot be finished
    fakes = Fakes()

    assert fakes.run(tmp_path) != 0
    assert [e[1] for e in fakes.events if e[0] == "transcribe"] == [older.id, older.id, newer.id]
    assert status_of(older) == "failed" and status_of(newer) == "done"


def test_process_handles_one_meeting(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)])
    fakes = Fakes()
    assert process(meeting.directory, fakes.transcribe, fakes.summarize, fakes.wait) is True
    assert status_of(meeting) == "done"


def test_a_second_worker_refuses_to_run_while_the_lock_is_held(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)])
    fakes = Fakes()
    with open(tmp_path / LOCK_FILE, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert fakes.run(tmp_path) == LOCK_BUSY

    assert fakes.events == [] and status_of(meeting) == "queued"
    assert fakes.run(tmp_path) == 0  # released, so the next run proceeds


def test_config_defaults():
    config = Config.from_env({})
    assert config == Config(
        meetings_dir=DEFAULT_MEETINGS_DIR, whisper_model="large-v3-turbo", whisper_threads=8,
        ollama_url="http://ollama:11434", ollama_model="gemma3:12b", chunk_minutes=12,
    )


def test_config_reads_the_environment():
    config = Config.from_env({
        "MEETINGS_DIR": "/data/m", "WHISPER_MODEL": "large-v3", "WHISPER_THREADS": "4",
        "OLLAMA_URL": "http://localhost:11434", "OLLAMA_MODEL": "gemma3:4b", "CHUNK_MINUTES": "10",
    })
    assert config == Config(
        meetings_dir="/data/m", whisper_model="large-v3", whisper_threads=4,
        ollama_url="http://localhost:11434", ollama_model="gemma3:4b", chunk_minutes=10,
    )


def test_a_bad_number_in_the_environment_is_reported():
    with pytest.raises(ValueError, match="WHISPER_THREADS"):
        Config.from_env({"WHISPER_THREADS": "eight"})


def test_main_takes_the_meetings_dir_from_the_command_line_over_the_environment(tmp_path):
    used = []
    code = main(["--meetings-dir", str(tmp_path)], env={"MEETINGS_DIR": "/nowhere"},
                runner=lambda config: used.append(config) or 0)
    assert code == 0 and used[0].meetings_dir == str(tmp_path)


def test_main_reports_bad_configuration_with_a_nonzero_exit():
    assert main([], env={"CHUNK_MINUTES": "x"}, runner=lambda config: 0) != 0


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1)
        self.sleeps = []

    def time(self):
        return (self.now - datetime(2026, 1, 1)).total_seconds()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def test_waiting_for_ollama_returns_once_it_answers():
    clock = Clock()
    answers = iter([OSError("refused"), False, True])

    def ping(url):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    wait_for_ollama("http://ollama:11434", ping=ping, clock=clock.time, sleep=clock.sleep)
    assert len(clock.sleeps) == 2


def test_waiting_for_ollama_gives_up_after_the_timeout():
    clock = Clock()
    with pytest.raises(WorkerError, match="Ollama"):
        wait_for_ollama("http://ollama:11434", timeout=120, ping=lambda url: False, clock=clock.time,
                        sleep=clock.sleep)
    assert 115 <= sum(clock.sleeps) <= 125

"""deploy/meeting-pending.sh and deploy/meeting-job.sh, run with a fake `docker` that records its arguments."""
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from bot.recording.meeting import Meeting

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def meetings(root, *statuses):
    for minute, status in enumerate(statuses):
        Meeting.create(root, 11, 22, datetime(2026, 10, 25, 18, minute)).set_status(status)
    return root


def sh(script, *args, env=None):
    return subprocess.run(["sh", str(DEPLOY / script), *map(str, args)], env=env, capture_output=True, text=True)


@pytest.mark.parametrize("status", ["recording", "queued", "transcribing", "summarizing"])
def test_a_meeting_that_needs_the_job_is_pending(tmp_path, status):
    assert sh("meeting-pending.sh", meetings(tmp_path, "done", status)).returncode == 0


@pytest.mark.parametrize("statuses", [(), ("done", "failed", "interrupted")])
def test_nothing_pending_skips_the_run(tmp_path, statuses):
    # 1, or 2 from grep when there is no manifest at all; ExecCondition skips the run on any code from 1 to 254
    assert sh("meeting-pending.sh", meetings(tmp_path, *statuses)).returncode in (1, 2)


def manifest(root, text):
    (root / "a").mkdir()
    (root / "a" / "manifest.json").write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize("text", [
    '{"id": "a", "status": "queued", "segments": []}',  # compact, as json.dumps() without indent
    '{"id":"a","status":"queued","segments":[]}',  # no spaces at all
    '{\n    "id": "a",\n    "status" : "queued"\n}',  # indent 4, a space before the colon
    '{\n\t"status":\t"queued"\n}',  # tabs
])
def test_the_status_is_found_whatever_the_json_formatting(tmp_path, text):
    assert sh("meeting-pending.sh", manifest(tmp_path, text)).returncode == 0


@pytest.mark.parametrize("text", [
    '{"id":"a","status":"done","segments":[{"file":"x.wav"}]}',
    # "status" inside a string value (an error message, a speaker name) is escaped, so it does not count
    '{"status": "failed", "error": "bad \\"status\\": \\"queued\\" in input"}',
    '{"status": "done", "speakers": {"1": "status"}}',
])
def test_only_a_real_status_key_counts(tmp_path, text):
    assert sh("meeting-pending.sh", manifest(tmp_path, text)).returncode == 1


def test_the_manifest_has_a_single_status_key(tmp_path):
    # In compact JSON meeting-pending.sh cannot tell a nested key from a top-level one, so it relies on segments,
    # speakers and every other nested object having no "status" key of their own.
    meeting = Meeting.create(tmp_path, 11, 22, datetime(2026, 10, 25, 18, 0))
    meeting.add_segment({"file": "seg.wav", "speaker": 33, "start": 0.0})
    meeting.set_speaker(33, "Maria")
    meeting.set_error("x")
    assert (meeting.directory / "manifest.json").read_text(encoding="utf-8").count('"status"') == 1


RUN = "compose --profile job run --rm -T --name ieee-meeting-worker"
FULL = f"{RUN} worker"
LIVE = f"{RUN} --no-deps worker python -m worker.run --live"
INSPECT = "container inspect ieee-meeting-worker"
PS = "compose --profile job ps -q --status running ollama"
STOP = "compose --profile job stop ollama"
# SIGTERM to the worker first, so it ends its run without counting it as a crash (worker/run.py), then remove it
END = ["stop --time 60 ieee-meeting-worker", "rm -f ieee-meeting-worker"]


def fake_docker(tmp_path, body):
    """A `docker` on PATH that appends its arguments to the returned log, then runs `body` (a sh case on "$*")."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    log.touch()
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\necho "$@" >> {log}\ncase "$*" in\n{body}\nesac\nexit 0\n')
    docker.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, log


def job(tmp_path, *statuses, exit_code=0, ollama_up=False, container_exists=False, expect=None):
    """Runs meeting-job.sh. `compose run` exits with `exit_code`, as the worker would; `ps` reports Ollama running
    if `ollama_up`; `container inspect` finds the job container if `container_exists`. Returns the docker calls."""
    env, log = fake_docker(tmp_path, f"""
    *" run "*) exit {exit_code} ;;
    *" ps "*) {"echo 0123abcd" if ollama_up else ":"} ;;
    "container inspect"*) exit {0 if container_exists else 1} ;;""")
    root = tmp_path / "meetings"
    root.mkdir()
    result = sh("meeting-job.sh", meetings(root, *statuses), env=env)
    assert result.returncode == (exit_code if expect is None else expect), result.stderr
    return log.read_text().splitlines()


@pytest.mark.parametrize("statuses", [("recording",), ("done", "recording"), ("recording", "failed", "recording")])
def test_during_a_call_the_job_runs_live_without_starting_ollama(tmp_path, statuses):
    assert job(tmp_path, *statuses) == [LIVE]


@pytest.mark.parametrize("code", [1, 3, 75])
def test_whatever_the_live_run_returns_nothing_else_is_started(tmp_path, code):
    # exit 3 once meant "start a notes run with Ollama"; summaries are only made after the call now
    assert job(tmp_path, "recording", exit_code=code) == [LIVE]


@pytest.mark.parametrize("statuses", [("queued",), ("transcribing",), ("summarizing",), ("queued", "recording")])
def test_an_ended_meeting_gets_the_full_job_then_ollama_is_stopped(tmp_path, statuses):
    assert job(tmp_path, *statuses) == [INSPECT, PS, FULL, STOP]


@pytest.mark.parametrize("code", [1, 2, 137])
def test_a_failed_full_job_still_stops_ollama(tmp_path, code):
    assert job(tmp_path, "queued", exit_code=code) == [INSPECT, PS, FULL, STOP]


def test_ollama_left_running_by_a_killed_run_is_stopped_after_the_next_one(tmp_path):
    assert job(tmp_path, "queued", ollama_up=True) == [INSPECT, PS, FULL, STOP]


def test_a_busy_worker_leaves_alone_the_ollama_the_other_worker_uses(tmp_path):
    # another worker (for example a manual `docker compose run worker`) holds the lock and summarizes with it
    assert job(tmp_path, "queued", exit_code=75, ollama_up=True) == [INSPECT, PS, FULL]


def test_a_busy_worker_stops_the_ollama_only_this_run_started(tmp_path):
    # the lock holder is a live run (no Ollama), so the Ollama this run's depends_on started would stay up idle
    assert job(tmp_path, "queued", exit_code=75) == [INSPECT, PS, FULL, STOP]


def test_a_second_run_of_the_script_starts_and_stops_nothing(tmp_path):
    # a manual run while the service's run (container ieee-meeting-worker) is going
    assert job(tmp_path, "queued", container_exists=True, expect=75) == [INSPECT]


def test_an_idle_tick_is_one_grep_and_no_docker(tmp_path):
    # systemd runs ExecStopPost after an ExecCondition skip too, so the service must have none: then the whole idle
    # tick is meeting-pending.sh, which execs one grep and never calls docker.
    service = (DEPLOY / "ieee-meeting-worker.service").read_text()
    assert "ExecStopPost" not in [line.split("=")[0].strip() for line in service.splitlines()]
    script = (DEPLOY / "meeting-pending.sh").read_text()
    commands = [line for line in script.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert commands[-1].startswith("exec grep ") and "docker" not in "\n".join(commands)
    env, log = fake_docker(tmp_path, "")
    root = tmp_path / "meetings"
    root.mkdir()
    assert sh("meeting-pending.sh", meetings(root, "done", "failed"), env=env).returncode == 1
    assert log.read_text() == ""


def test_a_stopped_full_job_stops_its_container_and_ollama(tmp_path):
    started = tmp_path / "started"
    # `compose run` stays in the foreground like the real one
    env, log = fake_docker(tmp_path, f'    *" run "*) touch {started}; exec sleep 5 ;;\n    "container inspect"*) exit 1 ;;')
    root = tmp_path / "meetings"
    root.mkdir()
    process = subprocess.Popen(["sh", str(DEPLOY / "meeting-job.sh"), str(meetings(root, "queued"))], env=env)
    try:
        for _ in range(200):
            if started.exists():
                break
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)  # what systemd sends on a stop or a timeout (KillMode=mixed)
        assert process.wait(timeout=10) == 143
    finally:
        process.kill()
    assert log.read_text().splitlines()[-3:] == [*END, STOP]


def test_a_stopped_live_run_stops_its_container_and_leaves_ollama_alone(tmp_path):
    started = tmp_path / "started"
    env, log = fake_docker(tmp_path, f'    *" run "*) touch {started}; exec sleep 5 ;;')
    root = tmp_path / "meetings"
    root.mkdir()
    process = subprocess.Popen(["sh", str(DEPLOY / "meeting-job.sh"), str(meetings(root, "recording"))], env=env)
    try:
        for _ in range(200):
            if started.exists():
                break
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 143
    finally:
        process.kill()
    assert log.read_text().splitlines() == [LIVE, *END]

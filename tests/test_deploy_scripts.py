"""deploy/meeting-pending.sh and deploy/meeting-job.sh, run with a fake `docker` that records its arguments."""
import os
import subprocess
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


RUN = "compose --profile job run --rm -T --name ieee-meeting-worker"


def job(tmp_path, *statuses, live_exit=0, extract_exit=0, expect=0):
    """Runs meeting-job.sh with a fake docker; the --live run exits with `live_exit`, the --extract one with
    `extract_exit`, as the worker would."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(
        f'#!/bin/sh\necho "$@" >> {log}\n'
        f'case "$*" in *--live*) exit {live_exit} ;; *--extract*) exit {extract_exit} ;; esac\n'
    )
    docker.chmod(0o755)
    root = tmp_path / "meetings"
    root.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = sh("meeting-job.sh", meetings(root, *statuses), env=env)
    assert result.returncode == expect, result.stderr
    return log.read_text().splitlines()


def test_during_a_call_the_job_runs_live_without_starting_ollama(tmp_path):
    assert job(tmp_path, "done", "recording") == [f"{RUN} --no-deps worker python -m worker.run --live"]


def test_a_window_ready_for_notes_gets_a_second_run_with_ollama_after_whisper_has_exited(tmp_path):
    assert job(tmp_path, "recording", live_exit=3) == [
        f"{RUN} --no-deps worker python -m worker.run --live",
        f"{RUN} worker python -m worker.run --extract",  # no --no-deps: compose starts Ollama for this one
    ]


def test_the_extract_runs_result_is_the_jobs_result(tmp_path):
    assert len(job(tmp_path, "recording", live_exit=3, extract_exit=1, expect=1)) == 2


@pytest.mark.parametrize("code", [1, 75])
def test_a_failed_or_locked_live_run_starts_nothing_else(tmp_path, code):
    assert job(tmp_path, "recording", live_exit=code, expect=code) == [
        f"{RUN} --no-deps worker python -m worker.run --live",
    ]


@pytest.mark.parametrize("statuses", [("queued",), ("transcribing",), ("summarizing",), ("queued", "recording")])
def test_an_ended_meeting_gets_the_full_job_with_ollama(tmp_path, statuses):
    assert job(tmp_path, *statuses) == ["compose --profile job run --rm -T --name ieee-meeting-worker worker"]

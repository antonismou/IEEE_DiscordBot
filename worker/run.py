"""The processing job: `python -m worker.run [--live] [--meetings-dir DIR]`, started by the host's timer.

It loops until no meeting needs work, oldest first: `queued`, or `transcribing`/`summarizing` left by a crash. Each
meeting is transcribed (Whisper), its transcript checked and written, then summarized (Ollama over HTTP), and only
then marked `done`, so the bot finds transcript.txt and summary.md when it sees the status. A failure marks that
meeting `failed` with an `error` in the manifest and the job moves on. The summary saves its window notes as it goes
and reuses them, so a job killed during the summary only extracts the missing windows next time (worker/summarize.py).

A meeting still `recording` gets one live pass per run: the segments the bot has closed so far are transcribed and
nothing else happens. The bot owns the manifest during the call (it rewrites it from memory on every segment), so
a live pass never writes it, never summarizes and never fails the meeting; whatever is left is retried next run.
Ollama is never asked for during the call: summarizing starts only after the meeting has ended.
With `--live` the job does only live passes and leaves ended meetings alone, because Ollama is not running then.

Exit codes: 0 when everything finished or there was nothing to do, 1 when a meeting (or a live pass) failed, 2 for
bad configuration, 75 when another worker holds the lock.
"""
from __future__ import annotations

import argparse
import fcntl
import gc
import logging
import os
import sys
import time
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Callable, Mapping

import requests

from bot.recording.meeting import SUMMARY, TRANSCRIPT, Meeting, all_meetings
from worker import summarize as summary_stage
from worker import transcribe as transcribe_stage

log = logging.getLogger(__name__)

DEFAULT_MEETINGS_DIR = "/app/data/meetings"
WORK = ("queued", "transcribing", "summarizing")  # the full pipeline; done and failed are left alone
LIVE = "recording"  # transcription only, while the call goes on
LOCK_FILE = ".worker.lock"
LOCK_BUSY = 75  # EX_TEMPFAIL: another worker is running and will pick up whatever is queued
OLLAMA_WAIT = 120  # seconds; the ollama container starts with the job

Stage = Callable[[Path], object]


class WorkerError(Exception):
    """A meeting cannot be finished."""


@dataclass(frozen=True)
class Config:
    meetings_dir: str = DEFAULT_MEETINGS_DIR
    whisper_model: str = "large-v3-turbo"
    whisper_threads: int = 8
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = summary_stage.DEFAULT_MODEL
    chunk_minutes: int = summary_stage.DEFAULT_CHUNK_MINUTES
    live: bool = False  # only transcribe meetings still recording (set by --live, when Ollama is not started)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        def number(name: str, default: int) -> int:
            raw = env.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"{name} must be a whole number, got {raw!r}") from None

        return cls(
            meetings_dir=env.get("MEETINGS_DIR") or cls.meetings_dir,
            whisper_model=env.get("WHISPER_MODEL") or cls.whisper_model,
            whisper_threads=number("WHISPER_THREADS", cls.whisper_threads),
            ollama_url=env.get("OLLAMA_URL") or cls.ollama_url,
            ollama_model=env.get("OLLAMA_MODEL") or cls.ollama_model,
            chunk_minutes=number("CHUNK_MINUTES", cls.chunk_minutes),
        )


# --- Ollama -------------------------------------------------------------------------------------------------------

def ollama_answers(url: str) -> bool:
    return requests.get(f"{url.rstrip('/')}/api/tags", timeout=5).ok


def wait_for_ollama(url: str, timeout: float = OLLAMA_WAIT, interval: float = 2.0,
                    ping: Callable[[str], bool] = ollama_answers, clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep) -> None:
    """Polls until Ollama answers; it may still be starting when the summary stage begins."""
    deadline = clock() + timeout
    while True:
        try:
            if ping(url):
                return
        except Exception as exc:  # not listening yet
            log.debug("Ollama not ready: %s", exc)
        remaining = deadline - clock()
        if remaining <= 0:
            raise WorkerError(f"Ollama at {url} did not answer within {timeout:.0f} s")
        sleep(min(interval, remaining))


# --- one meeting --------------------------------------------------------------------------------------------------

def process(directory: Path, transcribe: Stage, summarize: Stage, wait: Callable[[], None]) -> bool:
    """Finish one meeting. Returns False if it failed (and is now marked so)."""
    directory = Path(directory)
    try:
        if Meeting.load(directory).status != "summarizing":
            _transcribe(directory, transcribe)
        else:
            _check_segments(directory)  # a crash after transcription: the texts are on disk
        transcript = transcribe_stage.write_transcript(directory)
        Meeting.load(directory).set_status("summarizing")
        if transcript.read_text(encoding="utf-8").strip():
            wait()
        # An empty transcript (nobody spoke) still gets a summary: the stage writes the empty headings itself.
        summarize(directory)
        for name in (TRANSCRIPT, SUMMARY):
            if not (directory / name).is_file():
                raise WorkerError(f"{name} was not written")
        Meeting.load(directory).set_status("done")
        log.info("meeting %s done", directory.name)
        return True
    except Exception as exc:
        log.exception("meeting %s failed", directory.name)
        try:
            meeting = Meeting.load(directory)  # fresh, so nothing the stages wrote is overwritten
            meeting.set_error(f"{type(exc).__name__}: {exc}")
            meeting.set_status("failed")
        except Exception:
            log.exception("could not mark meeting %s failed", directory.name)
        return False


def live_pass(directory: Path, transcribe: Stage) -> bool:
    """Transcribe the segments a recording meeting has so far. Returns False if the pass raised.

    Only closed segments are in the manifest (the bot adds each one after its WAV is complete), so all are safe to
    read. transcribe_meeting leaves the manifest alone while the status is `recording`; nothing here writes it
    either, since the bot's next save would overwrite it, or a write here could drop a segment the bot just added.
    """
    try:
        done = transcribe(directory)
        log.info("meeting %s: %s new segment(s) transcribed during the call", Path(directory).name, done)
        return True
    except Exception:
        log.exception("meeting %s: live transcription failed, retrying next run", Path(directory).name)
        return False
    finally:
        gc.collect()


def _transcribe(directory: Path, transcribe: Stage) -> None:
    Meeting.load(directory).set_status("transcribing")
    transcribe(directory)
    if _missing(directory):
        log.warning("meeting %s: retrying %d segment(s)", directory.name, len(_missing(directory)))
        transcribe(directory)
    gc.collect()  # the Whisper model is dropped with the call; free it before Gemma needs the memory
    _check_segments(directory)


def _check_segments(directory: Path) -> None:
    missing = _missing(directory)
    if missing:
        raise WorkerError(f"no transcript for {len(missing)} segment(s): {', '.join(missing)}")


def _missing(directory: Path) -> list[str]:
    segments = transcribe_stage._segments(Meeting.load(directory))
    return [s["file"] for s in segments if not (directory / s["file"]).with_suffix(".txt").exists()]


# --- the job ------------------------------------------------------------------------------------------------------

def run(config: Config, transcribe: Stage | None = None, summarize: Stage | None = None,
        wait: Callable[[], None] | None = None) -> int:
    root = Path(config.meetings_dir)
    transcribe = transcribe or partial(transcribe_stage.transcribe_meeting, model_name=config.whisper_model,
                                       threads=config.whisper_threads)
    summarize = summarize or partial(summary_stage.summarize_meeting, chunk_minutes=config.chunk_minutes,
                                     model=config.ollama_model, base_url=config.ollama_url)
    wait = wait or partial(wait_for_ollama, config.ollama_url)

    root.mkdir(parents=True, exist_ok=True)
    with open(root / LOCK_FILE, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released when the file closes, even on a crash
        except BlockingIOError:
            log.warning("another worker holds %s, not starting", root / LOCK_FILE)
            return LOCK_BUSY
        failed = 0
        tried: set[tuple[str, str]] = set()
        while (found := _next(root, tried, config.live)) is not None:
            meeting, kind = found
            tried.add((meeting.id, kind))
            log.info("processing meeting %s (%s)", meeting.id, meeting.status)
            if kind == "live":
                ok = live_pass(meeting.directory, transcribe)
            else:
                ok = process(meeting.directory, transcribe, summarize, wait)
            if not ok:
                failed += 1
        return 1 if failed else 0


def _next(root: Path, tried: set[tuple[str, str]], live: bool) -> tuple[Meeting, str] | None:
    """Looked up again after each meeting, so one queued meanwhile is not missed. Ids are start times.

    Each meeting gets at most one live pass and one full run per job, so a call that goes on does not keep the job
    looping. A meeting stopped during its live pass is finished by the same job, or, with `live`, by the next one.
    """
    for meeting in all_meetings(root):
        if meeting.status == LIVE:
            kind = "live"
        elif meeting.status in WORK and not live:
            kind = "full"
        else:
            continue
        if (meeting.id, kind) not in tried:
            return meeting, kind
    return None


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         runner: Callable[[Config], int] = run) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker.run", description=__doc__.splitlines()[0])
    parser.add_argument("--meetings-dir", help=f"default: $MEETINGS_DIR or {DEFAULT_MEETINGS_DIR}")
    parser.add_argument("--live", action="store_true",
                        help="only transcribe meetings still recording; never summarize (Ollama is not running)")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env(os.environ if env is None else env)
    except ValueError as exc:
        log.error("bad configuration: %s", exc)
        return 2
    if args.meetings_dir:
        config = replace(config, meetings_dir=args.meetings_dir)
    if args.live:
        config = replace(config, live=True)
    return runner(config)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())

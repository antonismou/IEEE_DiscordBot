"""A meeting's folder and its manifest.json, the single record shared between the bot and the processing job."""
from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import threading
import wave
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"
POSTING = "posting.json"  # what the bot posted about the meeting; the job never touches it
TRANSCRIPT = "transcript.txt"  # written by the job: lines "[hh:mm:ss] Name: text"
SUMMARY = "summary.md"  # written by the job, in Greek
BUSY = ("recording", "queued", "transcribing", "summarizing")  # statuses that block a new recording
STALE = ("recording", "interrupted")  # left by a bot restart during the call ("interrupted" by older versions)
NO_AUDIO = "the bot restarted during the recording and no audio was captured"
NO_AUDIO_STOPPED = "no audio was captured"  # a recording stopped with nothing in it
REQUIRED = ("id", "status", "segments")  # a manifest without these cannot be used
_unreadable_warned: set[Path] = set()  # folders whose manifest could not be read, logged once each


class Meeting:
    def __init__(self, directory: Path, data: dict, posting: dict | None = None):
        self.directory = Path(directory)
        self._data = data
        self._posting = posting or {}
        self._lock = threading.Lock()  # segments are saved from the voice thread, names and status from the loop

    @classmethod
    def create(cls, root: Path, channel_id: int, started_by: int, now: datetime) -> Meeting:
        """The id is the start time. Two meetings started in the same second (or an hour apart when the clocks go
        back) would get the same one, so a clash gets a suffix: 20261025-030000-2. If the manifest cannot be written
        the folder is removed again, so a failed start leaves nothing behind."""
        Path(root).mkdir(parents=True, exist_ok=True)
        base = f"{now:%Y%m%d-%H%M%S}"
        for number in range(1, 100):
            meeting_id = base if number == 1 else f"{base}-{number}"
            directory = Path(root) / meeting_id
            try:
                directory.mkdir()
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError(f"too many meetings started at {base}")
        meeting = cls(directory, {
            "id": meeting_id, "channel_id": channel_id, "started_by": started_by,
            "started_at": now.isoformat(timespec="seconds"), "status": "recording", "speakers": {}, "segments": [],
        })
        try:
            meeting.save()
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return meeting

    @classmethod
    def load(cls, directory: Path) -> Meeting:
        """Raises OSError or ValueError if the manifest (or posting.json) is missing, truncated or not a meeting's."""
        directory = Path(directory)
        posting = directory / POSTING
        data = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(key not in data for key in REQUIRED):
            raise ValueError(f"{directory / MANIFEST} is not a meeting manifest")
        posted = json.loads(posting.read_text(encoding="utf-8")) if posting.exists() else {}
        if not isinstance(posted, dict):
            raise ValueError(f"{posting} is not a posting record")
        return cls(directory, data, posted)

    @property
    def id(self) -> str:
        return self._data["id"]

    @property
    def status(self) -> str:
        return self._data["status"]

    @property
    def channel_id(self) -> int:
        return self._data["channel_id"]

    @property
    def started_at(self) -> datetime:
        return datetime.fromisoformat(self._data["started_at"])

    @property
    def error(self) -> str | None:
        """Why processing failed, if the job said."""
        return self._data.get("error")

    @property
    def transcript_path(self) -> Path:
        return self.directory / TRANSCRIPT

    @property
    def summary_path(self) -> Path:
        return self.directory / SUMMARY

    @property
    def posted(self) -> bool:
        """The final post (summary and transcript, or the failure notice) is in the summaries channel."""
        return self._posting.get("posted", False)

    @property
    def posted_for(self) -> str | None:
        """Which result the final post showed (see posting.result_token); None if posted by an older version."""
        return self._posting.get("posted_for")

    @property
    def interrupted(self) -> bool:
        """The recording was cut short by a bot restart, so the result may be incomplete."""
        return self._posting.get("interrupted", False)

    @property
    def status_message_id(self) -> int | None:
        return self._posting.get("status_message_id")

    @property
    def status_shown(self) -> str | None:
        """The status the status message shows now."""
        return self._posting.get("status_shown")

    @property
    def parts_sent(self) -> int:
        """How many summary messages are already posted, so a retry continues instead of repeating them."""
        return self._posting.get("parts_sent", 0)

    @property
    def speakers(self) -> dict[int, str]:
        return {int(user_id): name for user_id, name in self._data["speakers"].items()}

    @property
    def segments(self) -> list[dict]:
        """The finished audio files, as saved in the manifest (a copy)."""
        with self._lock:
            return [dict(segment) for segment in self._data["segments"]]

    @property
    def has_audio(self) -> bool:
        with self._lock:
            return bool(self._data["segments"])

    def unnamed_speakers(self) -> list[int]:
        """Who has audio in the meeting but no saved name (meetings recorded before names were saved live)."""
        with self._lock:
            named = self._data["speakers"]
            return sorted({s["speaker"] for s in self._data["segments"] if str(s["speaker"]) not in named})

    def add_segment(self, segment: dict) -> None:
        # The whole manifest is rewritten each time: O(segments) per segment, a few hundred small writes per meeting.
        with self._lock:
            self._data["segments"].append(segment)
            self._write()

    def set_speaker(self, user_id: int, name: str) -> None:
        with self._lock:
            self._data["speakers"][str(user_id)] = name
            self._write()

    def set_status(self, status: str) -> None:
        with self._lock:
            self._data["status"] = status
            self._write()

    def set_error(self, text: str) -> None:
        """Set by the job before it marks the meeting failed, so the bot can say why."""
        with self._lock:
            self._data["error"] = text
            self._write()

    def set_status_message(self, message_id: int, status: str) -> None:
        self._set_posting(status_message_id=message_id, status_shown=status)

    def set_parts_sent(self, count: int) -> None:
        self._set_posting(parts_sent=count)

    def mark_posted(self, result: str | None = None) -> None:
        self._set_posting(posted=True, posted_for=result)

    def restart_posting(self) -> None:
        """Processing ran again (a reprocess, or a failed meeting retried): post its progress and result as new.
        Only `interrupted` is kept, since it describes the recording, not the processing."""
        with self._lock:
            self._posting = {key: value for key, value in self._posting.items() if key == "interrupted"}
            _write_json(self.directory / POSTING, self._posting)

    def mark_interrupted(self) -> None:
        self._set_posting(interrupted=True)

    def save(self) -> None:
        with self._lock:
            self._write()

    def _set_posting(self, **changes) -> None:
        # Kept out of the manifest: the job rewrites the manifest and could drop fields it does not know.
        with self._lock:
            self._posting.update(changes)
            _write_json(self.directory / POSTING, self._posting)

    def _write(self) -> None:
        _write_json(self.directory / MANIFEST, self._data)


def _write_json(path: Path, data: dict) -> None:
    """Write atomically, so the job never reads a half-written file, and durably: the data is on disk before the
    rename, and the rename before we return, so a power cut leaves the old file or the new one, never an empty one."""
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        file.write(json.dumps(data, ensure_ascii=False, indent=2))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    try:
        folder = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return  # a filesystem that cannot open a folder; the rename is still atomic
    try:
        os.fsync(folder)
    except OSError:
        pass
    finally:
        os.close(folder)


def all_meetings(root: Path) -> list[Meeting]:
    """Every readable meeting, oldest first. A folder whose manifest cannot be read (a bad hand edit, a truncated
    file) is skipped and logged once, so one broken meeting never stops the others."""
    if not Path(root).is_dir():
        return []
    meetings = []
    for path in sorted(Path(root).glob(f"*/{MANIFEST}"), key=lambda p: p.parent.name):  # "-2" after its base id
        try:
            meetings.append(Meeting.load(path.parent))
        except (OSError, ValueError) as exc:  # json.JSONDecodeError and UnicodeDecodeError are ValueErrors
            if path.parent not in _unreadable_warned:
                _unreadable_warned.add(path.parent)
                log.warning("Skipping meeting folder %s: its manifest cannot be read (%s)", path.parent, exc)
    return meetings


def latest(root: Path) -> Meeting | None:
    """The most recent meeting (ids are start times, so they sort by date)."""
    meetings = all_meetings(root)
    return meetings[-1] if meetings else None


def find_busy(root: Path) -> Meeting | None:
    """A meeting that is recording or still being processed. Only one may exist at a time."""
    return next((m for m in all_meetings(root) if m.status in BUSY), None)


def recover_interrupted(root: Path, hold_unnamed: bool = False) -> list[Meeting]:
    """After a bot restart a recording cannot continue (the voice session is gone), so end it as /record stop would.

    Called once at startup, before any recording begins, so the bot is the only writer of these manifests: the job
    never writes a `recording` manifest (a live pass only adds segment texts) and never touches `interrupted` ones.
    The audio files that were still open when the bot stopped are added first (see adopt_orphans). A meeting with
    audio is queued for the job; one without fails, so nothing waits for it. The flag goes to posting.json first, so
    it is there before the job can pick the meeting up, and a crash in between only repeats it.

    With `hold_unnamed`, a meeting with audio from speakers it has no name for is only flagged and left `recording`,
    which still blocks a new recording and keeps the job from transcribing it for good, so the caller can save the
    names once it can look them up and then finish it with end_interrupted.
    """
    stale = [m for m in all_meetings(root) if m.status in STALE]
    for meeting in stale:
        adopt_orphans(meeting)
        if hold_unnamed and meeting.unnamed_speakers():
            meeting.mark_interrupted()
            if meeting.status != "recording":  # an older version's `interrupted` would not block a new recording
                meeting.set_status("recording")
        else:
            end_interrupted(meeting)
    return stale


def adopt_orphans(meeting: Meeting) -> list[dict]:
    """Add the WAVs on disk that the manifest does not list, or delete them if they hold no usable audio.

    A segment is added to the manifest only when its file is closed, so the files still open when the bot died (or
    whose close failed) would otherwise be kept forever and never transcribed. Each is repaired (the header's sizes
    are recomputed from the file's length, dropping a half-written last sample) and added with:

    - speaker: the folder it is in (`<user id>/000003.wav`);
    - duration: from the audio it holds;
    - start: when it was last written (its mtime, the end of the speech) minus its duration, counted from the
      meeting's start time, but never before the end of that speaker's previous segment (one speaker's files never
      overlap) and never below 0. Pauses inside a segment are shorter than the writer's gap, so this is at most a
      few seconds late, and the transcript is ordered by start, so the line still lands about where it was said.

    Only for a meeting the bot owns (`recording`, or one it is stopping), so nothing else writes the manifest.
    """
    known = {segment["file"] for segment in meeting.segments}
    adopted = []
    for path in sorted(meeting.directory.glob("*/*.wav")):
        relative = path.relative_to(meeting.directory).as_posix()
        if relative in known or not path.parent.name.isdigit():
            continue
        try:
            written = path.stat().st_mtime  # before the repair, which changes it
            duration = _repair_wav(path)
            if duration is None:
                log.warning("Deleting %s: it holds no usable audio", path)
                path.unlink(missing_ok=True)
                continue
        except OSError as exc:
            log.warning("Could not recover the unfinished audio file %s: %s", path, exc)
            continue
        speaker = int(path.parent.name)
        previous_end = max(
            (s["start"] + s["duration"] for s in meeting.segments if s["speaker"] == speaker), default=0.0,
        )
        by_time = written - meeting.started_at.timestamp() - duration
        segment = {"speaker": speaker, "file": relative, "start": round(max(previous_end, by_time, 0.0), 3),
                   "duration": duration}
        meeting.add_segment(segment)
        adopted.append(segment)
        log.warning("Recovered unfinished audio %s of meeting %s (%.1f s)", relative, meeting.id, duration)
    return adopted


_HEADER = 44  # the canonical PCM header the wave module writes: RIFF, a 16-byte fmt chunk, then data


def _repair_wav(path: Path) -> float | None:
    """Fix the sizes in the header of a WAV written by SegmentWriter and return its duration in seconds, or None if
    it is not such a file or holds no audio. The header is written with the first frame and patched after each
    write, so it is usually right already; a crash in the middle of a write leaves sizes that do not match."""
    with open(path, "r+b") as file:
        header = file.read(_HEADER)
        if (len(header) < _HEADER or header[0:4] != b"RIFF" or header[8:16] != b"WAVEfmt "
                or struct.unpack("<I", header[16:20])[0] != 16 or header[36:40] != b"data"):
            return None
        audio_format, channels, rate, _, block, _ = struct.unpack("<HHIIHH", header[20:36])
        if audio_format != 1 or channels < 1 or rate < 1 or block < 1:
            return None
        size = file.seek(0, os.SEEK_END) - _HEADER
        size -= size % block  # a sample cut in half by the crash
        if size <= 0:
            return None
        riff, data = struct.unpack("<I", header[4:8])[0], struct.unpack("<I", header[40:44])[0]
        if riff != _HEADER - 8 + size or data != size:
            file.seek(4)
            file.write(struct.pack("<I", _HEADER - 8 + size))
            file.seek(40)
            file.write(struct.pack("<I", size))
            file.truncate(_HEADER + size)
            file.flush()
            os.fsync(file.fileno())
    try:
        with wave.open(str(path), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
    except (wave.Error, EOFError):
        return None
    return frames / rate if frames else None


def end_interrupted(meeting: Meeting) -> None:
    """Finish a recording cut short by a restart: queued with the audio it has, or failed with none."""
    meeting.mark_interrupted()
    if meeting.has_audio:
        meeting.set_status("queued")
    else:
        meeting.set_error(NO_AUDIO)
        meeting.set_status("failed")

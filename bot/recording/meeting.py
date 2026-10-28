"""A meeting's folder and its manifest.json, the single record shared between the bot and the processing job."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

MANIFEST = "manifest.json"
POSTING = "posting.json"  # what the bot posted about the meeting; the job never touches it
TRANSCRIPT = "transcript.txt"  # written by the job: lines "[hh:mm:ss] Name: text"
SUMMARY = "summary.md"  # written by the job, in Greek
BUSY = ("recording", "queued", "transcribing", "summarizing")  # statuses that block a new recording


class Meeting:
    def __init__(self, directory: Path, data: dict, posting: dict | None = None):
        self.directory = Path(directory)
        self._data = data
        self._posting = posting or {}
        self._lock = threading.Lock()  # segments are saved from the voice thread, names and status from the loop

    @classmethod
    def create(cls, root: Path, channel_id: int, started_by: int, now: datetime) -> Meeting:
        meeting_id = f"{now:%Y%m%d-%H%M%S}"
        directory = Path(root) / meeting_id
        directory.mkdir(parents=True)
        meeting = cls(directory, {
            "id": meeting_id, "channel_id": channel_id, "started_by": started_by,
            "started_at": now.isoformat(timespec="seconds"), "status": "recording", "speakers": {}, "segments": [],
        })
        meeting.save()
        return meeting

    @classmethod
    def load(cls, directory: Path) -> Meeting:
        directory = Path(directory)
        posting = directory / POSTING
        return cls(
            directory, json.loads((directory / MANIFEST).read_text(encoding="utf-8")),
            json.loads(posting.read_text(encoding="utf-8")) if posting.exists() else {},
        )

    @property
    def id(self) -> str:
        return self._data["id"]

    @property
    def status(self) -> str:
        return self._data["status"]

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

    def add_segment(self, segment: dict) -> None:
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

    def mark_posted(self) -> None:
        self._set_posting(posted=True)

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
    """Write atomically, so the job never reads a half-written file."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def all_meetings(root: Path) -> list[Meeting]:
    if not Path(root).is_dir():
        return []
    return [Meeting.load(p.parent) for p in sorted(Path(root).glob(f"*/{MANIFEST}"))]


def latest(root: Path) -> Meeting | None:
    """The most recent meeting (ids are start times, so they sort by date)."""
    meetings = all_meetings(root)
    return meetings[-1] if meetings else None


def find_busy(root: Path) -> Meeting | None:
    """A meeting that is recording or still being processed. Only one may exist at a time."""
    return next((m for m in all_meetings(root) if m.status in BUSY), None)


def interrupt_stale(root: Path) -> list[Meeting]:
    """After a bot restart, recordings cannot continue (the voice session is gone), so mark them interrupted."""
    stale = [m for m in all_meetings(root) if m.status == "recording"]
    for meeting in stale:
        meeting.set_status("interrupted")
    return stale

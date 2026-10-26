"""A meeting's folder and its manifest.json, the single record shared between the bot and the processing job."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

MANIFEST = "manifest.json"
BUSY = ("recording", "queued", "transcribing", "summarizing")  # statuses that block a new recording


class Meeting:
    def __init__(self, directory: Path, data: dict):
        self.directory = Path(directory)
        self._data = data
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
        return cls(directory, json.loads((Path(directory) / MANIFEST).read_text(encoding="utf-8")))

    @property
    def id(self) -> str:
        return self._data["id"]

    @property
    def status(self) -> str:
        return self._data["status"]

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

    def save(self) -> None:
        with self._lock:
            self._write()

    def _write(self) -> None:
        """Write atomically, so the job never reads a half-written manifest."""
        path = self.directory / MANIFEST
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)


def _all(root: Path) -> list[Meeting]:
    if not Path(root).is_dir():
        return []
    return [Meeting.load(p.parent) for p in sorted(Path(root).glob(f"*/{MANIFEST}"))]


def find_busy(root: Path) -> Meeting | None:
    """A meeting that is recording or still being processed. Only one may exist at a time."""
    return next((m for m in _all(root) if m.status in BUSY), None)


def interrupt_stale(root: Path) -> list[Meeting]:
    """After a bot restart, recordings cannot continue (the voice session is gone), so mark them interrupted."""
    stale = [m for m in _all(root) if m.status == "recording"]
    for meeting in stale:
        meeting.set_status("interrupted")
    return stale

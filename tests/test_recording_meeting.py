import json
from datetime import datetime

from bot.recording.meeting import Meeting, find_busy, interrupt_stale

NOW = datetime(2026, 10, 25, 18, 30, 5)


def manifest(meeting):
    return json.loads((meeting.directory / "manifest.json").read_text(encoding="utf-8"))


def test_creating_a_meeting_makes_its_folder_and_a_recording_manifest(tmp_path):
    meeting = Meeting.create(tmp_path, channel_id=11, started_by=22, now=NOW)

    assert meeting.directory == tmp_path / "20261025-183005"
    assert manifest(meeting) == {
        "id": "20261025-183005", "channel_id": 11, "started_by": 22,
        "started_at": "2026-10-25T18:30:05", "status": "recording", "speakers": {}, "segments": [],
    }


def test_segments_speakers_and_status_are_saved_as_they_change(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    meeting.add_segment({"speaker": 7, "file": "7/000001.wav", "start": 1.5, "duration": 3.0})
    meeting.set_speaker(7, "Μαρία")
    meeting.set_status("queued")

    saved = manifest(meeting)
    assert saved["segments"] == [{"speaker": 7, "file": "7/000001.wav", "start": 1.5, "duration": 3.0}]
    assert saved["speakers"] == {"7": "Μαρία"} and saved["status"] == "queued"


def test_a_meeting_can_be_loaded_back_from_its_folder(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    meeting.set_speaker(7, "Μαρία")

    loaded = Meeting.load(meeting.directory)
    assert loaded.id == meeting.id and loaded.status == "recording" and loaded.speakers == {7: "Μαρία"}


def test_find_busy_ignores_finished_meetings(tmp_path):
    for status, now in (("done", NOW), ("failed", NOW.replace(second=6)), ("interrupted", NOW.replace(second=7))):
        Meeting.create(tmp_path, 11, 22, now).set_status(status)
    assert find_busy(tmp_path) is None


def test_find_busy_reports_a_meeting_that_is_still_in_progress(tmp_path):
    Meeting.create(tmp_path, 11, 22, NOW).set_status("done")
    waiting = Meeting.create(tmp_path, 11, 22, NOW.replace(second=9))
    waiting.set_status("transcribing")

    assert find_busy(tmp_path).id == waiting.id


def test_find_busy_on_a_missing_folder_is_none(tmp_path):
    assert find_busy(tmp_path / "nope") is None


def test_after_a_restart_a_recording_cannot_continue_so_it_is_marked_interrupted(tmp_path):
    recording = Meeting.create(tmp_path, 11, 22, NOW)
    queued = Meeting.create(tmp_path, 11, 22, NOW.replace(second=6))
    queued.set_status("queued")

    interrupted = interrupt_stale(tmp_path)

    assert [m.id for m in interrupted] == [recording.id]
    assert Meeting.load(recording.directory).status == "interrupted"
    assert Meeting.load(queued.directory).status == "queued"   # the job owns it, not the bot
    assert find_busy(tmp_path).id == queued.id


def test_saving_from_two_threads_at_once_never_corrupts_or_loses_the_manifest(tmp_path):
    import threading

    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    errors = []

    def add_segments():
        try:
            for i in range(150):
                meeting.add_segment({"speaker": 1, "file": f"1/{i}.wav", "start": i, "duration": 1})
        except Exception as exc:
            errors.append(exc)

    def change_names():
        try:
            for i in range(150):
                meeting.set_speaker(i, f"name {i}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=add_segments), threading.Thread(target=change_names)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    saved = manifest(meeting)
    assert errors == [] and len(saved["segments"]) == 150 and len(saved["speakers"]) == 150

import json
from datetime import datetime

from bot.recording.meeting import Meeting, find_busy, interrupt_stale, latest

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


def test_latest_is_the_most_recent_meeting(tmp_path):
    assert latest(tmp_path / "nope") is None
    Meeting.create(tmp_path, 11, 22, NOW)
    newer = Meeting.create(tmp_path, 11, 22, NOW.replace(minute=45))
    assert latest(tmp_path).id == newer.id


def test_a_new_meeting_has_nothing_posted_yet(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    assert meeting.posted is False and meeting.status_message_id is None
    assert meeting.status_shown is None and meeting.parts_sent == 0
    assert meeting.started_at == NOW and meeting.error is None
    assert meeting.summary_path == meeting.directory / "summary.md"
    assert meeting.transcript_path == meeting.directory / "transcript.txt"


def test_what_the_bot_posted_survives_a_reload(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    meeting.set_status_message(555, "queued")
    meeting.set_parts_sent(2)
    meeting.mark_posted()

    loaded = Meeting.load(meeting.directory)
    assert loaded.status_message_id == 555 and loaded.status_shown == "queued"
    assert loaded.parts_sent == 2 and loaded.posted is True


def test_the_job_rewriting_the_manifest_cannot_erase_what_the_bot_posted(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    meeting.set_status_message(555, "queued")
    meeting.mark_posted()
    job_copy = manifest(meeting)                     # the job writes its own copy of the manifest back
    job_copy["status"] = "done"
    (meeting.directory / "manifest.json").write_text(json.dumps(job_copy), encoding="utf-8")

    loaded = Meeting.load(meeting.directory)
    assert loaded.status == "done" and loaded.posted is True and loaded.status_message_id == 555
    assert "posted" not in job_copy                 # the bot's own state is kept out of the shared manifest


def test_the_job_s_error_message_is_readable(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    data = manifest(meeting)
    data.update(status="failed", error="Ollama 500")
    (meeting.directory / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    assert Meeting.load(meeting.directory).error == "Ollama 500"

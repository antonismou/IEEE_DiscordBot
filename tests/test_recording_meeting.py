import json
import os
import struct
import wave
from datetime import datetime

import pytest

from bot.recording.meeting import (
    Meeting, adopt_orphans, all_meetings, end_interrupted, find_busy, latest, recover_interrupted,
)

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


SEGMENT = {"speaker": 7, "file": "7/000001.wav", "start": 1.5, "duration": 3.0}


def test_after_a_restart_a_recording_with_audio_is_queued_and_flagged_as_interrupted(tmp_path):
    recording = Meeting.create(tmp_path, 11, 22, NOW)
    recording.add_segment(SEGMENT)

    recovered = recover_interrupted(tmp_path)

    loaded = Meeting.load(recording.directory)
    assert [m.id for m in recovered] == [recording.id]
    assert loaded.status == "queued" and loaded.interrupted and loaded.error is None
    assert manifest(loaded)["segments"] == [SEGMENT]                 # the chunks on disk are still processed
    assert find_busy(tmp_path).id == recording.id                    # it blocks a new recording until processed


def test_after_a_restart_a_recording_with_no_audio_fails_with_a_reason(tmp_path):
    empty = Meeting.create(tmp_path, 11, 22, NOW)

    recover_interrupted(tmp_path)

    loaded = Meeting.load(empty.directory)
    assert loaded.status == "failed" and loaded.interrupted
    assert "no audio was captured" in loaded.error
    assert find_busy(tmp_path) is None                               # nothing to process, nothing blocks


def test_a_meeting_left_interrupted_by_an_older_version_is_recovered_too(tmp_path):
    old = Meeting.create(tmp_path, 11, 22, NOW)
    old.add_segment(SEGMENT)
    old.set_status("interrupted")

    recover_interrupted(tmp_path)

    assert Meeting.load(old.directory).status == "queued"


def test_recovery_leaves_meetings_that_were_not_recording_alone(tmp_path):
    others = {}
    for second, status in enumerate(("queued", "transcribing", "summarizing", "done", "failed")):
        meeting = Meeting.create(tmp_path, 11, 22, NOW.replace(second=second))
        meeting.add_segment(SEGMENT)
        meeting.set_status(status)
        others[meeting.id] = (status, (meeting.directory / "manifest.json").read_bytes())

    assert recover_interrupted(tmp_path) == []
    for meeting in [Meeting.load(tmp_path / meeting_id) for meeting_id in others]:
        status, before = others[meeting.id]
        assert meeting.status == status and not meeting.interrupted
        assert (meeting.directory / "manifest.json").read_bytes() == before   # the job owns these, not the bot


def test_recovery_run_twice_changes_nothing_the_second_time(tmp_path):
    Meeting.create(tmp_path, 11, 22, NOW).add_segment(SEGMENT)
    Meeting.create(tmp_path, 11, 22, NOW.replace(second=9))

    recover_interrupted(tmp_path)
    before = {p: p.read_bytes() for p in sorted(tmp_path.glob("*/*.json"))}

    assert recover_interrupted(tmp_path) == []
    assert {p: p.read_bytes() for p in sorted(tmp_path.glob("*/*.json"))} == before


def test_a_meeting_knows_which_speakers_it_has_no_name_for(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    meeting.add_segment(SEGMENT)
    meeting.add_segment({**SEGMENT, "speaker": 8, "file": "8/000001.wav"})
    meeting.set_speaker(8, "Νίκος")
    assert meeting.channel_id == 11 and meeting.unnamed_speakers() == [7]


def test_recovery_can_hold_a_meeting_whose_speakers_have_no_names_until_they_are_looked_up(tmp_path):
    unnamed = Meeting.create(tmp_path, 11, 22, NOW)
    unnamed.add_segment(SEGMENT)
    old = Meeting.create(tmp_path, 11, 22, NOW.replace(second=7))
    old.add_segment(SEGMENT)
    old.set_status("interrupted")
    named = Meeting.create(tmp_path, 11, 22, NOW.replace(second=8))
    named.add_segment(SEGMENT)
    named.set_speaker(7, "Μαρία")
    empty = Meeting.create(tmp_path, 11, 22, NOW.replace(second=9))

    held = {m.id: m for m in recover_interrupted(tmp_path, hold_unnamed=True)}

    for meeting in (unnamed, old):                    # held as `recording`: blocks a new recording, the job waits
        loaded = Meeting.load(meeting.directory)
        assert loaded.status == "recording" and loaded.interrupted
    assert find_busy(tmp_path).id == unnamed.id
    assert Meeting.load(named.directory).status == "queued"            # names already saved during the call
    assert Meeting.load(named.directory).speakers == {7: "Μαρία"}
    assert Meeting.load(empty.directory).status == "failed"

    held[unnamed.id].set_speaker(7, "Μαρία")
    end_interrupted(held[unnamed.id])
    loaded = Meeting.load(unnamed.directory)
    assert loaded.status == "queued" and loaded.interrupted and loaded.speakers == {7: "Μαρία"}


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


def test_the_job_can_record_why_it_failed(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    meeting.set_error("Ollama 500")
    meeting.set_status("failed")

    assert manifest(meeting)["error"] == "Ollama 500"
    assert Meeting.load(meeting.directory).error == "Ollama 500"


def test_two_meetings_started_in_the_same_second_get_different_folders(tmp_path):
    """Also the autumn clock change, when the same local hour happens twice."""
    first = Meeting.create(tmp_path, 11, 22, NOW)
    second = Meeting.create(tmp_path, 11, 22, NOW)

    assert (first.id, second.id) == ("20261025-183005", "20261025-183005-2")
    assert manifest(second)["id"] == second.id and latest(tmp_path).id == second.id


def test_a_meeting_whose_manifest_cannot_be_written_leaves_no_folder(tmp_path, monkeypatch):
    def disk_full(self):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Meeting, "save", disk_full)
    with pytest.raises(OSError):
        Meeting.create(tmp_path, 11, 22, NOW)
    assert list(tmp_path.iterdir()) == []


def test_an_unreadable_manifest_is_skipped_and_logged_once(tmp_path, caplog):
    good = Meeting.create(tmp_path, 11, 22, NOW)
    good.set_status("queued")
    for name, text in (("20261026-100000", '{"id": "20261026-100000", "status": "queued",'),   # truncated
                       ("20261026-110000", "[1, 2]"),                                           # not a meeting
                       ("20261026-120000", '{"id": "x"}')):                                     # fields missing
        (tmp_path / name).mkdir()
        (tmp_path / name / "manifest.json").write_text(text, encoding="utf-8")

    assert find_busy(tmp_path).id == good.id
    assert [m.id for m in all_meetings(tmp_path)] == [good.id]
    assert recover_interrupted(tmp_path) == []
    assert latest(tmp_path).id == good.id
    assert sum("cannot be read" in record.message for record in caplog.records) == 3   # once per folder


def write_wav(path, seconds, *, close=True):
    """A WAV as SegmentWriter writes it; with close=False it is left as the bot leaves it when it dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wav = wave.open(str(path), "wb")
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(16000)
    wav.writeframes(b"\x01\x00" * int(16000 * seconds))
    if close:
        wav.close()
    return wav


def written_at(meeting, path, seconds_after_start):
    moment = meeting.started_at.timestamp() + seconds_after_start
    os.utime(path, (moment, moment))


def test_recovery_adds_the_audio_that_was_still_being_written_when_the_bot_died(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    write_wav(meeting.directory / "7/000001.wav", 2.0)
    meeting.add_segment({"speaker": 7, "file": "7/000001.wav", "start": 1.0, "duration": 2.0})
    left_open = write_wav(meeting.directory / "7/000002.wav", 3.0, close=False)     # the bot is killed here
    written_at(meeting, meeting.directory / "7/000002.wav", 60.0)

    recover_interrupted(tmp_path)
    left_open.close()

    loaded = Meeting.load(meeting.directory)
    assert loaded.status == "queued"
    assert loaded.segments[1] == {"speaker": 7, "file": "7/000002.wav", "start": 57.0, "duration": 3.0}


def test_a_recovered_file_whose_header_was_not_patched_is_repaired(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    path = meeting.directory / "8/000001.wav"
    write_wav(path, 1.0)
    data = bytearray(path.read_bytes())
    data[4:8] = data[40:44] = struct.pack("<I", 0)                  # the header still says it is empty
    path.write_bytes(bytes(data) + b"\x01")                          # and the last sample is only half written
    written_at(meeting, path, 10.0)

    [segment] = adopt_orphans(meeting)

    assert segment == {"speaker": 8, "file": "8/000001.wav", "start": 9.0, "duration": 1.0}
    with wave.open(str(path), "rb") as wav:
        assert wav.getnframes() == 16000 and len(wav.readframes(16000)) == 32000
    assert Meeting.load(meeting.directory).segments == [segment]


def test_a_recovered_file_never_starts_before_the_same_speakers_previous_one_ends(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    meeting.add_segment({"speaker": 7, "file": "7/000001.wav", "start": 100.0, "duration": 5.0})
    write_wav(meeting.directory / "7/000002.wav", 2.0)
    written_at(meeting, meeting.directory / "7/000002.wav", 50.0)            # a clock that does not fit

    [segment] = adopt_orphans(meeting)
    assert segment["start"] == 105.0


def test_recovery_deletes_audio_files_that_hold_nothing_usable(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    (meeting.directory / "7").mkdir()
    (meeting.directory / "7/000001.wav").write_bytes(b"")                      # killed before the first frame
    (meeting.directory / "7/000002.wav").write_bytes(b"RIFF\x00\x00")          # header cut short
    write_wav(meeting.directory / "7/000003.wav", 0.0)                          # a header and no audio
    (meeting.directory / "7/000004.wav").write_bytes(b"not a wav at all" * 10)

    recover_interrupted(tmp_path)

    assert list(meeting.directory.rglob("*.wav")) == []
    loaded = Meeting.load(meeting.directory)
    assert loaded.status == "failed" and loaded.segments == []


def test_recovery_leaves_listed_files_and_files_outside_speaker_folders_alone(tmp_path):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    write_wav(meeting.directory / "7/000001.wav", 1.0)
    meeting.add_segment({"speaker": 7, "file": "7/000001.wav", "start": 4.0, "duration": 1.0})
    write_wav(meeting.directory / "notes/x.wav", 1.0)

    assert adopt_orphans(meeting) == []
    assert (meeting.directory / "notes/x.wav").exists()

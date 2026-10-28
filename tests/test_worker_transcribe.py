from datetime import datetime
from types import SimpleNamespace

from bot.recording.meeting import Meeting
from worker.transcribe import format_offset, transcribe_meeting, write_transcript

NOW = datetime(2026, 10, 25, 18, 30, 5)


class FakeModel:
    """Stands in for a faster-whisper WhisperModel: the text for each file comes from `texts`."""

    def __init__(self, texts, fail=()):
        self.texts = texts
        self.fail = set(fail)
        self.calls = []

    def transcribe(self, path, **options):
        self.calls.append((path, options))
        name = "/".join(path.split("/")[-2:])
        if name in self.fail:
            raise RuntimeError("decode failed")
        pieces = [SimpleNamespace(text=t) for t in self.texts.get(name, "").split("|")]
        return iter(pieces), SimpleNamespace(language="el")


def meeting_with(tmp_path, segments, speakers=None, status="recording"):
    meeting = Meeting.create(tmp_path, 11, 22, NOW)
    for user_id, name in (speakers or {}).items():
        meeting.set_speaker(user_id, name)
    for speaker, number, start in segments:
        relative = f"{speaker}/{number:06d}.wav"
        (meeting.directory / relative).parent.mkdir(parents=True, exist_ok=True)
        (meeting.directory / relative).write_bytes(b"RIFF")
        meeting.add_segment({"speaker": speaker, "file": relative, "start": start, "duration": 1.0})
    meeting.set_status(status)
    return meeting


def factory(model):
    created = []

    def make(name, threads):
        created.append((name, threads))
        return model
    make.created = created
    return make


def transcript(meeting):
    return (meeting.directory / "transcript.txt").read_text(encoding="utf-8")


def test_offsets_are_formatted_as_hours_minutes_seconds():
    assert format_offset(0) == "00:00:00"
    assert format_offset(9.9) == "00:00:09"
    assert format_offset(3725.4) == "01:02:05"
    assert format_offset(36000) == "10:00:00"


def test_speakers_are_merged_by_start_offset(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0), (7, 2, 20.0), (8, 1, 9.0)], {7: "Μαρία", 8: "Νίκος"})
    model = FakeModel({"7/000001.wav": " Καλησπέρα.", "7/000002.wav": "Συμφωνώ.", "8/000001.wav": "Γεια σας."})

    count = transcribe_meeting(meeting.directory, model_factory=factory(model))

    assert count == 3
    assert transcript(meeting) == (
        "[00:00:00] Μαρία: Καλησπέρα.\n"
        "[00:00:09] Νίκος: Γεια σας.\n"
        "[00:00:20] Μαρία: Συμφωνώ.\n"
    )


def test_the_pieces_of_a_segment_are_joined_into_one_line(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 3725.0)], {7: "Μαρία"})
    model = FakeModel({"7/000001.wav": " Πρώτα αυτό.| Μετά εκείνο. "})

    transcribe_meeting(meeting.directory, model_factory=factory(model))

    assert transcript(meeting) == "[01:02:05] Μαρία: Πρώτα αυτό. Μετά εκείνο.\n"


def test_the_whisper_settings_are_passed_to_the_model(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)])
    model = FakeModel({"7/000001.wav": "Γεια."})
    make = factory(model)

    transcribe_meeting(meeting.directory, model_factory=make)

    assert make.created == [("large-v3-turbo", 8)]
    [(path, options)] = model.calls
    assert path == str(meeting.directory / "7/000001.wav")
    assert options["language"] == "el" and options["beam_size"] == 1 and options["vad_filter"] is True
    assert "IEEE Student Branch" in options["initial_prompt"] and "workshop" in options["initial_prompt"]


def test_each_transcript_is_saved_next_to_its_audio_and_the_audio_deleted(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)])

    transcribe_meeting(meeting.directory, model_factory=factory(FakeModel({"7/000001.wav": "Γεια."})))

    assert (meeting.directory / "7/000001.txt").read_text(encoding="utf-8") == "Γεια."
    assert not (meeting.directory / "7/000001.wav").exists()


def test_segments_that_already_have_a_transcript_are_skipped(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0), (7, 2, 5.0)], {7: "Μαρία"})
    (meeting.directory / "7/000001.txt").write_text("Από πριν.", encoding="utf-8")
    model = FakeModel({"7/000002.wav": "Καινούργιο."})

    count = transcribe_meeting(meeting.directory, model_factory=factory(model))

    assert count == 1
    assert [path for path, _ in model.calls] == [str(meeting.directory / "7/000002.wav")]
    assert not (meeting.directory / "7/000001.wav").exists()  # left over from a crash before the delete
    assert transcript(meeting) == "[00:00:00] Μαρία: Από πριν.\n[00:00:05] Μαρία: Καινούργιο.\n"


def test_nothing_to_do_does_not_load_the_model(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)])
    make = factory(FakeModel({"7/000001.wav": "Γεια."}))
    transcribe_meeting(meeting.directory, model_factory=make)

    assert transcribe_meeting(meeting.directory, model_factory=make) == 0
    assert len(make.created) == 1


def test_a_failed_segment_keeps_its_audio_and_the_others_go_on(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0), (8, 1, 2.0)], {7: "Μαρία", 8: "Νίκος"})
    model = FakeModel({"8/000001.wav": "Εντάξει."}, fail={"7/000001.wav"})

    count = transcribe_meeting(meeting.directory, model_factory=factory(model))

    assert count == 1
    assert (meeting.directory / "7/000001.wav").exists()
    assert not (meeting.directory / "7/000001.txt").exists()
    assert not (meeting.directory / "8/000001.wav").exists()
    assert transcript(meeting) == "[00:00:02] Νίκος: Εντάξει.\n"

    retry = FakeModel({"7/000001.wav": "Τελικά."})
    assert transcribe_meeting(meeting.directory, model_factory=factory(retry)) == 1
    assert transcript(meeting) == "[00:00:00] Μαρία: Τελικά.\n[00:00:02] Νίκος: Εντάξει.\n"


def test_silent_segments_are_left_out_of_the_transcript(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0), (7, 2, 4.0)], {7: "Μαρία"})
    model = FakeModel({"7/000001.wav": "  |  ", "7/000002.wav": "Ναι."})

    assert transcribe_meeting(meeting.directory, model_factory=factory(model)) == 2

    assert (meeting.directory / "7/000001.txt").exists()  # done, so it is not transcribed again
    assert not (meeting.directory / "7/000001.wav").exists()
    assert transcript(meeting) == "[00:00:04] Μαρία: Ναι.\n"


def test_an_unknown_speaker_gets_a_numbered_label(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0), (99, 1, 1.0)], {7: "Μαρία"})
    model = FakeModel({"7/000001.wav": "Ποιος είναι;", "99/000001.wav": "Εγώ."})

    transcribe_meeting(meeting.directory, model_factory=factory(model))

    assert transcript(meeting) == "[00:00:00] Μαρία: Ποιος είναι;\n[00:00:01] Ομιλητής 99: Εγώ.\n"


def test_status_is_transcribing_after_the_meeting_ended(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)], status="queued")

    transcribe_meeting(meeting.directory, model_factory=factory(FakeModel({"7/000001.wav": "Γεια."})))

    assert Meeting.load(meeting.directory).status == "transcribing"


def test_during_the_call_the_manifest_is_left_to_the_bot(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)], status="recording")

    transcribe_meeting(meeting.directory, model_factory=factory(FakeModel({"7/000001.wav": "Γεια."})))

    assert Meeting.load(meeting.directory).status == "recording"


def test_it_can_run_again_as_new_segments_arrive(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0)], {7: "Μαρία"})
    transcribe_meeting(meeting.directory, model_factory=factory(FakeModel({"7/000001.wav": "Πρώτο."})))
    (meeting.directory / "7/000002.wav").write_bytes(b"RIFF")
    meeting.add_segment({"speaker": 7, "file": "7/000002.wav", "start": 8.0, "duration": 1.0})

    assert transcribe_meeting(meeting.directory, model_factory=factory(FakeModel({"7/000002.wav": "Δεύτερο."}))) == 1
    assert transcript(meeting) == "[00:00:00] Μαρία: Πρώτο.\n[00:00:08] Μαρία: Δεύτερο.\n"


def test_write_transcript_uses_only_finished_segments(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 0.0), (7, 2, 3.0)], {7: "Μαρία"})
    (meeting.directory / "7/000002.txt").write_text("Μόνο αυτό.", encoding="utf-8")

    path = write_transcript(meeting.directory)

    assert path == meeting.directory / "transcript.txt"
    assert transcript(meeting) == "[00:00:03] Μαρία: Μόνο αυτό.\n"


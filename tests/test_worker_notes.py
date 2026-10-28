"""worker/notes.py: which fixed windows of a meeting can get their notes, during the call and after it."""
from datetime import datetime, timedelta, timezone

import pytest

from bot.recording.meeting import Meeting
from worker.notes import MARGIN, MAX_SEGMENT, complete_windows, extract, pending_windows
from worker.summarize import notes_path

START = datetime(2026, 10, 25, 18, 0, 0)  # naive local time, as the bot writes started_at
W = 12 * 60


def meeting_with(root, segments, status="recording", transcribed=None, texts=None):
    """Segments `(speaker, number, start, duration)`; all transcribed unless `transcribed` lists the ones that are."""
    meeting = Meeting.create(root, 11, 22, START)
    for speaker, number, start, duration in segments:
        relative = f"{speaker}/{number:06d}.wav"
        audio = meeting.directory / relative
        audio.parent.mkdir(parents=True, exist_ok=True)
        if transcribed is None or (speaker, number) in transcribed:
            text = (texts or {}).get((speaker, number), f"λόγια {number}")
            audio.with_suffix(".txt").write_text(text, encoding="utf-8")
        else:
            audio.write_bytes(b"RIFF")
        meeting.add_segment({"speaker": speaker, "file": relative, "start": start, "duration": duration})
    if status != "recording":
        meeting.set_status(status)
    return meeting


def at(seconds):
    return START + timedelta(seconds=seconds)


def ready_after(k):
    """The first moment window k can be complete during the call."""
    return (k + 1) * W + MAX_SEGMENT + MARGIN


# --- during the call --------------------------------------------------------------------------------------------

def test_a_window_is_complete_only_once_no_segment_can_still_arrive_for_it(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 10.0, 5.0), (7, 2, 800.0, 5.0)])
    assert MAX_SEGMENT == 300 and MARGIN >= 30  # SegmentWriter closes a file at 5 minutes of audio
    assert complete_windows(meeting.directory, 12, now=at(W + 10)) == []  # window 0 ended, a segment may be open
    assert complete_windows(meeting.directory, 12, now=at(ready_after(0) - 1)) == []
    assert complete_windows(meeting.directory, 12, now=at(ready_after(0))) == [0]
    assert complete_windows(meeting.directory, 12, now=at(ready_after(1))) == [0, 1]


def test_an_untranscribed_segment_blocks_its_window_and_the_later_ones(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 10.0, 5.0), (8, 1, 700.0, 5.0), (7, 2, 800.0, 5.0)],
                           transcribed={(7, 1), (7, 2)})
    assert complete_windows(meeting.directory, 12, now=at(ready_after(1))) == []


def test_a_segment_still_open_on_disk_blocks_the_windows_it_may_belong_to(tmp_path):
    # A segment's wall-clock span can exceed 5 minutes (short pauses do not close it), so an open WAV the bot
    # has not registered yet is checked as well: it started after the speaker's previous segment ended.
    meeting = meeting_with(tmp_path, [(7, 1, 10.0, 5.0), (8, 1, 1500.0, 5.0)])
    open_wav = meeting.directory / "7/000002.wav"
    open_wav.write_bytes(b"RIFF")  # opened after 7/000001 ended (15 s), still being written
    late = at(ready_after(2) + 3600)
    assert complete_windows(meeting.directory, 12, now=late) == []

    (meeting.directory / "9").mkdir()
    (meeting.directory / "9/000001.wav").write_bytes(b"RIFF")  # a first segment: could have started at 0
    open_wav.unlink()
    assert complete_windows(meeting.directory, 12, now=late) == []


def test_an_open_segment_that_started_after_a_window_does_not_block_it(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 10.0, 5.0), (7, 2, 1000.0, 300.0)])
    (meeting.directory / "7/000003.wav").write_bytes(b"RIFF")  # started after 1300 s, so in window 1 or later
    assert complete_windows(meeting.directory, 12, now=at(ready_after(1) + 600)) == [0]


def test_elapsed_time_comes_from_started_at_whatever_the_clock_zone(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 10.0, 5.0)])
    aware_now = at(ready_after(0)).astimezone(timezone.utc)  # the same instant, given in UTC
    assert complete_windows(meeting.directory, 12, now=aware_now) == [0]
    assert complete_windows(meeting.directory, 12, now=aware_now - timedelta(seconds=2)) == []


def test_empty_windows_are_not_listed(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 10.0, 5.0), (7, 2, 3000.0, 5.0)])
    assert complete_windows(meeting.directory, 12, now=at(ready_after(4))) == [0, 4]


# --- after the call ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["queued", "transcribing", "summarizing"])
def test_after_the_call_every_window_is_complete_including_the_last_partial_one(tmp_path, status):
    meeting = meeting_with(tmp_path, [(7, 1, 10.0, 5.0), (7, 2, 800.0, 5.0)], status=status,
                           transcribed={(7, 1)})
    (meeting.directory / "8").mkdir()
    (meeting.directory / "8/000001.wav").write_bytes(b"RIFF")  # left behind by a bot restart, never registered
    assert complete_windows(meeting.directory, 12, now=at(60)) == [0, 1]


# --- what still needs notes -------------------------------------------------------------------------------------

def test_pending_windows_skip_the_ones_with_notes_and_the_silent_ones(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 10.0, 5.0), (7, 2, 800.0, 5.0), (7, 3, 1500.0, 5.0)],
                           texts={(7, 2): ""})  # window 1 only has silence: nothing to extract
    path = notes_path(meeting.directory, 0, 12)
    path.parent.mkdir(parents=True)
    path.write_text("σημειώσεις\n", encoding="utf-8")
    assert pending_windows(meeting.directory, 12, now=at(ready_after(2))) == [2]


def test_an_idle_check_writes_nothing(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 10.0, 5.0)])
    before = sorted(p.name for p in meeting.directory.rglob("*"))
    assert pending_windows(meeting.directory, 12, now=at(60)) == []
    assert sorted(p.name for p in meeting.directory.rglob("*")) == before


def test_extract_uses_the_segment_texts_on_disk_and_saves_the_notes(tmp_path):
    meeting = meeting_with(tmp_path, [(7, 1, 10.0, 5.0), (7, 2, 800.0, 5.0), (8, 1, 900.0, 5.0)],
                           texts={(7, 2): "η αφίσα", (8, 1): "μέχρι Παρασκευή"})
    prompts = []

    def generate(prompt):
        prompts.append(prompt)
        return "σημειώσεις 1"
    assert extract(meeting.directory, 1, 12, generate) == "σημειώσεις 1"

    assert len(prompts) == 1
    assert "[00:13:20] Ομιλητής 7: η αφίσα" in prompts[0] and "[00:15:00] Ομιλητής 8: μέχρι Παρασκευή" in prompts[0]
    assert "λόγια 1" not in prompts[0]
    assert notes_path(meeting.directory, 1, 12).read_text(encoding="utf-8") == "σημειώσεις 1\n"
    assert pending_windows(meeting.directory, 12, now=at(ready_after(1))) == [0]

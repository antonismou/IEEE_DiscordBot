import pytest
import wave

from bot.recording.segments import SegmentWriter

FRAME = b"\x01\x00" * 320  # 20 ms of 16 kHz mono
FRAME_SECONDS = 0.02


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        return w.getnchannels(), w.getframerate(), w.getsampwidth(), w.readframes(w.getnframes())


def feed(writer, speaker, start, count):
    for i in range(count):
        writer.add(speaker, FRAME, start + i * FRAME_SECONDS)


def test_continuous_speech_becomes_one_playable_file(tmp_path):
    writer = SegmentWriter(tmp_path)
    feed(writer, 7, 10.0, 5)
    writer.close()

    [segment] = writer.segments
    assert segment["speaker"] == 7 and segment["start"] == 10.0
    assert abs(segment["duration"] - 0.1) < 1e-6
    channels, rate, width, data = read_wav(tmp_path / segment["file"])
    assert (channels, rate, width, data) == (1, 16000, 2, FRAME * 5)


def test_a_pause_longer_than_the_gap_starts_a_new_segment(tmp_path):
    writer = SegmentWriter(tmp_path, gap_seconds=2.0)
    feed(writer, 7, 0.0, 3)
    feed(writer, 7, 5.0, 3)  # the speaker stopped for ~5 s
    writer.close()

    assert [s["start"] for s in writer.segments] == [0.0, 5.0]
    assert writer.segments[0]["file"] != writer.segments[1]["file"]


def test_a_pause_shorter_than_the_gap_stays_in_the_same_segment(tmp_path):
    writer = SegmentWriter(tmp_path, gap_seconds=2.0)
    feed(writer, 7, 0.0, 3)
    feed(writer, 7, 1.0, 3)
    writer.close()

    assert len(writer.segments) == 1


def test_speakers_are_independent_and_get_their_own_folders(tmp_path):
    writer = SegmentWriter(tmp_path)
    for i in range(3):  # interleaved, as they arrive in a real call
        writer.add(1, FRAME, i * FRAME_SECONDS)
        writer.add(2, FRAME, i * FRAME_SECONDS)
    writer.close()

    assert sorted(s["speaker"] for s in writer.segments) == [1, 2]
    assert {s["file"].split("/")[0] for s in writer.segments} == {"1", "2"}


def test_long_speech_is_split_at_the_maximum_length(tmp_path):
    writer = SegmentWriter(tmp_path, max_seconds=0.1)
    feed(writer, 7, 0.0, 12)  # 0.24 s of continuous speech
    writer.close()

    assert [round(s["duration"], 2) for s in writer.segments] == [0.1, 0.1, 0.04]
    assert [round(s["start"], 2) for s in writer.segments] == [0.0, 0.1, 0.2]


def test_flush_idle_closes_only_segments_that_have_gone_quiet(tmp_path):
    writer = SegmentWriter(tmp_path, gap_seconds=2.0)
    feed(writer, 1, 0.0, 3)   # last frame at 0.04
    feed(writer, 2, 9.0, 3)   # still talking at "now" = 9.1
    writer.flush_idle(now=9.1)

    assert [s["speaker"] for s in writer.segments] == [1]
    writer.close()
    assert sorted(s["speaker"] for s in writer.segments) == [1, 2]


def test_on_close_is_called_once_the_file_is_complete(tmp_path):
    seen = []
    writer = SegmentWriter(tmp_path, on_close=lambda s: seen.append((s["file"], read_wav(tmp_path / s["file"])[3])))
    feed(writer, 7, 0.0, 2)
    writer.close()

    assert seen == [(writer.segments[0]["file"], FRAME * 2)]


def test_audio_arriving_on_one_thread_while_another_flushes_idle_segments_loses_nothing(tmp_path):
    import threading

    writer = SegmentWriter(tmp_path, gap_seconds=0.0)  # every flush closes whatever is open
    errors = []
    done = threading.Event()

    def audio():
        try:
            for i in range(1500):
                writer.add(i % 25, FRAME, i * FRAME_SECONDS)
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    def flusher():
        try:
            while not done.is_set():
                writer.flush_idle(now=1e9)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=audio), threading.Thread(target=flusher)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    writer.close()

    assert errors == []
    assert sum(s["duration"] for s in writer.segments) == pytest.approx(1500 * FRAME_SECONDS)


def test_audio_arriving_after_close_is_dropped_instead_of_opening_a_file_nobody_closes(tmp_path):
    """The library's stop_listening() only signals its receiving thread, so a frame it had already taken can reach
    the writer after the recording was closed."""
    writer = SegmentWriter(tmp_path)
    writer.add(7, FRAME, 0.0)
    writer.close()
    writer.add(7, FRAME, 0.02)                                       # the late frame
    writer.flush_idle(100.0)

    assert writer.closed and sorted(p.name for p in tmp_path.rglob("*.wav")) == ["000001.wav"]
    assert len(writer.segments) == 1


def test_a_file_that_cannot_be_finished_does_not_stop_the_others_from_being_finished(tmp_path):
    closed = []
    writer = SegmentWriter(tmp_path, on_close=closed.append)
    writer.add(7, FRAME, 0.0)
    writer.add(8, FRAME, 0.0)

    def disk_full():
        raise OSError(28, "No space left on device")
    wav = writer._open[7].wav
    wav.close = disk_full

    with pytest.raises(OSError):
        writer.close()
    del wav.close                                                    # the real one again, for the cleanup
    wav.close()
    assert [s["speaker"] for s in closed] == [8] and writer.closed

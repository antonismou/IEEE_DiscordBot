from types import SimpleNamespace

from bot.recording.sink import MeetingSink


class FakePipeline:
    def __init__(self):
        self.frames = []

    def handle(self, user_id, opus):
        self.frames.append((user_id, opus))


def data(opus=b"opus", ssrc=99):
    return SimpleNamespace(packet=SimpleNamespace(decrypted_data=opus, ssrc=ssrc))


def test_the_sink_asks_for_raw_opus_so_the_dave_layer_can_be_removed_before_decoding():
    assert MeetingSink(FakePipeline(), lambda ssrc: None).wants_opus() is True


def test_a_frame_from_a_known_member_goes_to_the_pipeline_under_their_id():
    pipeline = FakePipeline()
    MeetingSink(pipeline, lambda ssrc: None).write(SimpleNamespace(id=5), data())
    assert pipeline.frames == [(5, b"opus")]


def test_a_speaker_missing_from_the_member_cache_is_identified_by_their_audio_stream():
    pipeline = FakePipeline()
    MeetingSink(pipeline, {99: 7}.get).write(None, data(ssrc=99))
    assert pipeline.frames == [(7, b"opus")]


def test_a_speaker_who_cannot_be_identified_at_all_is_skipped():
    pipeline = FakePipeline()
    MeetingSink(pipeline, lambda ssrc: None).write(None, data())
    assert pipeline.frames == []


def test_a_lost_packet_has_no_audio_and_is_skipped():
    pipeline = FakePipeline()
    sink = MeetingSink(pipeline, lambda ssrc: None)
    sink.write(SimpleNamespace(id=5), SimpleNamespace(packet=SimpleNamespace(ssrc=99)))  # no decrypted_data at all
    sink.write(SimpleNamespace(id=5), data(opus=b""))
    assert pipeline.frames == []


def test_each_speaker_is_reported_once_with_whatever_the_library_knows_about_them():
    heard = []
    member = SimpleNamespace(id=5, display_name="Μαρία")
    sink = MeetingSink(FakePipeline(), {99: 7}.get, on_new_speaker=lambda user_id, user: heard.append((user_id, user)))
    sink.write(member, data())
    sink.write(member, data())
    sink.write(None, data(ssrc=99))           # known only by their audio stream
    sink.write(None, data(ssrc=1))            # cannot be identified: not reported
    assert heard == [(5, member), (7, None)]


def test_the_sink_never_raises_because_that_would_end_receiving_for_everyone():
    """The library calls write() with no error handling: an exception ends its receiving thread, silently ending
    the recording for the rest of the meeting while the bot stays in the call."""
    class Broken:
        def handle(self, user_id, opus):
            raise OSError(5, "Input/output error")

    sink = MeetingSink(Broken(), lambda ssrc: None)
    sink.write(SimpleNamespace(id=7), data())
    sink.write(SimpleNamespace(id=7), data())
    assert sink.errors == 2


def test_a_write_error_in_the_real_pipeline_does_not_escape_the_sink(tmp_path):
    from bot.recording.pipeline import SpeakerPipeline

    class Passthrough:
        def decrypt(self, user_id, payload):
            return payload

    class Decoder:
        def decode(self, payload, fec=False):
            return b"\x01\x00" * 1920

    class FullDisk:
        def add(self, *args):
            raise OSError(28, "No space left on device")

    pipeline = SpeakerPipeline(Passthrough(), Decoder, FullDisk(), lambda: 1.0)
    sink = MeetingSink(pipeline, lambda ssrc: None)
    sink.write(SimpleNamespace(id=7), data(opus=b"\xfc\x00"))
    assert pipeline.write_errors == {7: 1} and sink.errors == 0     # counted where it happened

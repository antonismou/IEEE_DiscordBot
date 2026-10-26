import wave
from array import array

from bot.recording.pipeline import SpeakerPipeline
from bot.recording.segments import SegmentWriter


def stereo_frame(level: int) -> bytes:
    samples = array("h")
    for _ in range(960):
        samples.extend((level, level))
    return samples.tobytes()


class FakeDecryptor:
    def __init__(self, result="same"):
        self.result = result
        self.calls = []

    def decrypt(self, user_id, payload):
        self.calls.append((user_id, payload))
        return payload if self.result == "same" else self.result


class FakeDecoder:
    def __init__(self, level=500, error=None):
        self.level, self.error = level, error

    def decode(self, data, *, fec=False):
        if self.error:
            raise self.error
        return stereo_frame(self.level)


def make(tmp_path, decryptor=None, decoder_factory=None, now=lambda: 12.0):
    writer = SegmentWriter(tmp_path)
    pipeline = SpeakerPipeline(decryptor or FakeDecryptor(), decoder_factory or (lambda: FakeDecoder()), writer, now)
    return pipeline, writer


def test_a_frame_is_decrypted_decoded_converted_and_written_at_the_current_time(tmp_path):
    pipeline, writer = make(tmp_path)
    pipeline.handle(7, b"opus")
    writer.close()

    [segment] = writer.segments
    assert segment["speaker"] == 7 and segment["start"] == 12.0
    with wave.open(str(tmp_path / segment["file"]), "rb") as w:
        samples = array("h")
        samples.frombytes(w.readframes(w.getnframes()))
    assert len(samples) == 320 and set(samples) == {500}


def test_a_frame_that_cannot_be_decrypted_is_skipped_without_decoding(tmp_path):
    decoders = []
    pipeline, writer = make(tmp_path, FakeDecryptor(result=None), lambda: decoders.append(1) or FakeDecoder())
    pipeline.handle(7, b"opus")
    writer.close()

    assert writer.segments == [] and decoders == []


def test_each_speaker_gets_their_own_decoder_because_opus_decoding_keeps_per_stream_state(tmp_path):
    made = []
    pipeline, writer = make(tmp_path, decoder_factory=lambda: made.append(1) or FakeDecoder())
    for user in (1, 2, 1, 2, 1):
        pipeline.handle(user, b"opus")
    assert len(made) == 2


def test_a_corrupt_frame_is_counted_and_does_not_stop_the_recording(tmp_path):
    pipeline, writer = make(tmp_path, decoder_factory=lambda: FakeDecoder(error=ValueError("corrupted stream")))
    pipeline.handle(7, b"opus")
    pipeline.handle(7, b"opus")
    writer.close()

    assert pipeline.decode_errors == 2 and writer.segments == []

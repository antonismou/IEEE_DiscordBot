from array import array

from bot.recording.pcm import FRAME_BYTES_16K_MONO, to_16k_mono


def stereo_frame(left: int, right: int, frames: int = 960) -> bytes:
    """20 ms of Discord audio: 48 kHz, 16-bit, interleaved stereo."""
    samples = array("h")
    for _ in range(frames):
        samples.extend((left, right))
    return samples.tobytes()


def test_a_20ms_discord_frame_becomes_20ms_of_16k_mono():
    assert len(to_16k_mono(stereo_frame(100, 100))) == FRAME_BYTES_16K_MONO == 640


def test_identical_channels_keep_their_level():
    out = array("h")
    out.frombytes(to_16k_mono(stereo_frame(3000, 3000)))
    assert set(out) == {3000}


def test_opposite_channels_downmix_to_silence():
    out = array("h")
    out.frombytes(to_16k_mono(stereo_frame(1000, -1000)))
    assert set(out) == {0}


def test_whole_stereo_frames_only_so_a_ragged_tail_is_dropped_not_misaligned():
    ragged = stereo_frame(5, 5) + b"\x01\x02\x03"
    assert len(to_16k_mono(ragged)) == FRAME_BYTES_16K_MONO

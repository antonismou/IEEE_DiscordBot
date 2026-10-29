from types import SimpleNamespace

from bot.recording.dave import DaveDecryptor

AUDIO = object()  # stands in for davey.MediaType.audio


class FakeSession:
    def __init__(self, ready=True, error=None):
        self.ready = ready
        self.error = error
        self.calls = []

    def decrypt(self, user_id, media_type, payload):
        self.calls.append((user_id, media_type, payload))
        if self.error:
            raise self.error
        return b"opus:" + payload


def decryptor(protocol=1, session=None):
    state = SimpleNamespace(dave_protocol_version=protocol, dave_session=session)
    return DaveDecryptor(lambda: state, media_type=AUDIO)


def test_a_call_without_e2ee_passes_frames_through_untouched():
    d = decryptor(protocol=0, session=None)
    assert d.decrypt(5, b"frame") == b"frame"
    assert d.stats["passthrough"] == 1


def test_an_e2ee_frame_is_decrypted_with_the_speakers_user_id():
    session = FakeSession()
    d = decryptor(session=session)
    assert d.decrypt(5, b"frame") == b"opus:frame"
    assert session.calls == [(5, AUDIO, b"frame")]
    assert d.stats["decrypted"] == 1


def test_frames_are_dropped_while_the_session_is_not_ready():
    d = decryptor(session=FakeSession(ready=False))
    assert d.decrypt(5, b"frame") is None
    assert d.stats["dropped"] == 1


def test_frames_are_dropped_when_the_call_is_e2ee_but_there_is_no_session():
    d = decryptor(session=None)
    assert d.decrypt(5, b"frame") is None
    assert d.stats["dropped"] == 1


def test_frames_from_an_unknown_speaker_are_dropped_because_the_key_cannot_be_chosen():
    session = FakeSession()
    d = decryptor(session=session)
    assert d.decrypt(None, b"frame") is None
    assert session.calls == []


def test_an_unencrypted_frame_during_passthrough_gaps_is_used_as_plain_audio():
    error = RuntimeError("DecryptionFailed(UnencryptedWhenPassthroughDisabled)")
    d = decryptor(session=FakeSession(error=error))
    assert d.decrypt(5, b"silence") == b"silence"
    assert d.stats["passthrough"] == 1


def test_any_other_decrypt_error_drops_the_frame_and_remembers_why():
    d = decryptor(session=FakeSession(error=RuntimeError("bad key")))
    assert d.decrypt(5, b"frame") is None
    assert d.stats["errors"] == 1 and d.last_error == "bad key"


def test_the_session_is_looked_up_for_every_frame_because_discord_replaces_it_between_epochs():
    state = SimpleNamespace(dave_protocol_version=1, dave_session=FakeSession(ready=False))
    d = DaveDecryptor(lambda: state, media_type=AUDIO)
    assert d.decrypt(5, b"a") is None
    state.dave_session = FakeSession(ready=True)
    assert d.decrypt(5, b"a") == b"opus:a"

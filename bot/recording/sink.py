"""The receive extension's entry point: hands every received frame, with its speaker, to the pipeline."""
from __future__ import annotations

from typing import Any, Callable

from discord.ext import voice_recv


class MeetingSink(voice_recv.AudioSink):
    """Runs on the library's voice thread. `id_from_ssrc` finds a speaker the member cache does not know
    (the bot has no members intent), using the audio stream's id that Discord announces.

    `on_new_speaker(user_id, user)` is called once per speaker, on their first frame, with the Member (or User) the
    library found, or None. It runs on the voice thread too, so it must return quickly."""

    def __init__(self, pipeline, id_from_ssrc: Callable[[int], int | None],
                 on_new_speaker: Callable[[int, Any], None] | None = None):
        super().__init__()
        self._pipeline = pipeline
        self._id_from_ssrc = id_from_ssrc
        self._on_new_speaker = on_new_speaker
        self._heard: set[int] = set()  # only the voice thread touches it

    def wants_opus(self) -> bool:
        return True  # we remove the end-to-end encryption ourselves, then decode

    def write(self, user, data) -> None:
        opus = getattr(data.packet, "decrypted_data", None)  # a lost packet has none
        if not opus:
            return
        user_id = user.id if user is not None else self._id_from_ssrc(data.packet.ssrc)
        if user_id is None:
            return
        if user_id not in self._heard:
            self._heard.add(user_id)
            if self._on_new_speaker:
                self._on_new_speaker(user_id, user)
        self._pipeline.handle(user_id, opus)

    def cleanup(self) -> None:
        pass

"""The receive extension's entry point: hands every received frame, with its speaker, to the pipeline."""
from __future__ import annotations

from typing import Callable

from discord.ext import voice_recv


class MeetingSink(voice_recv.AudioSink):
    """Runs on the library's voice thread. `id_from_ssrc` finds a speaker the member cache does not know
    (the bot has no members intent), using the audio stream's id that Discord announces."""

    def __init__(self, pipeline, id_from_ssrc: Callable[[int], int | None]):
        super().__init__()
        self._pipeline = pipeline
        self._id_from_ssrc = id_from_ssrc

    def wants_opus(self) -> bool:
        return True  # we remove the end-to-end encryption ourselves, then decode

    def write(self, user, data) -> None:
        opus = getattr(data.packet, "decrypted_data", None)  # a lost packet has none
        if not opus:
            return
        user_id = user.id if user is not None else self._id_from_ssrc(data.packet.ssrc)
        if user_id is None:
            return
        self._pipeline.handle(user_id, opus)

    def cleanup(self) -> None:
        pass

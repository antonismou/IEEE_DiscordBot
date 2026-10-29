"""Removes Discord's end-to-end (DAVE) encryption from received audio frames.

discord.py negotiates DAVE when the bot joins a call and keeps the session for sending, but the receive
extension only removes the transport encryption. The frames it hands over are still DAVE-encrypted, so opus
decoding fails unless this step runs first. The approach follows the open pull requests on discord-ext-voice-recv.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

_PLAIN_FRAME_ERROR = "UnencryptedWhenPassthroughDisabled"  # a sender not (yet) in the encrypted group


def _audio_media_type() -> Any:
    import davey

    return davey.MediaType.audio


class DaveDecryptor:
    """`state_getter` returns discord.py's voice connection state, looked up per frame because it replaces the
    DAVE session whenever the group changes. `stats` counts what happened, for diagnosing a live call."""

    def __init__(self, state_getter: Callable[[], Any], media_type: Any = None):
        self._state = state_getter
        self._media_type = media_type if media_type is not None else _audio_media_type()
        self.stats = {"decrypted": 0, "passthrough": 0, "dropped": 0, "errors": 0}
        self.last_error: str | None = None

    def decrypt(self, user_id: int | None, payload: bytes) -> bytes | None:
        """The opus data for a received frame, or None when the frame cannot be used."""
        state = self._state()
        if not getattr(state, "dave_protocol_version", 0):
            self.stats["passthrough"] += 1
            return payload
        session = getattr(state, "dave_session", None)
        if session is None or not session.ready or user_id is None:
            self.stats["dropped"] += 1
            return None
        try:
            result = session.decrypt(int(user_id), self._media_type, bytes(payload))
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            if _PLAIN_FRAME_ERROR in message or _PLAIN_FRAME_ERROR in repr(exc):
                self.stats["passthrough"] += 1
                return payload
            self.stats["errors"] += 1
            self.last_error = message
            if self.stats["errors"] == 1:
                log.warning("DAVE decrypt failed: %s", message)
            return None
        self.stats["decrypted"] += 1
        return result

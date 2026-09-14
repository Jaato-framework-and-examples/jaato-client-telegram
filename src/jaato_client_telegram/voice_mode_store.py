"""Per-chat voice-reply mode + the directive that drives the `voz` tier.

Three states per chat, controlled by ``/voice``:
- ``on``   → speak every reply (even to text messages)
- ``off``  → never speak (text only)
- ``auto`` (default, when unset) → **reply-in-kind**: speak when the user sent a
  voice note, text otherwise.

When a turn should be spoken, the handlers append ``VOICE_HINT`` to the user
message. The model answers normally, then (per the persona + this directive)
enters the ``voz`` gpt-audio tier and says the answer aloud; the server streams
that as model-media and the renderer turns it into a Telegram voice note.

Persisted to JSON when a path is given (survives restarts); in-memory otherwise
— no hardcoded default path (repo convention: empty path ⇒ per-process).
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# Appended to the user message when the turn should be spoken. Kept explicit so
# voice-out works even before the Langfuse persona names the tier; the persona
# reinforces it. Short spoken replies keep gpt-audio cost + note length down.
VOICE_HINT = (
    "\n\n[SYSTEM — the user sent a voice note. You are already in the voz tier, which "
    "HEARS them and SPEAKS: just answer by SPEAKING your reply aloud, naturally and "
    "concise. Only if you need a tool or to see an attached image, enter the executor "
    "tier for that, then you return to voz automatically and speak your answer.]"
)


class VoiceModeStore:
    """Tracks each chat's voice-reply preference (on / off / unset=auto)."""

    def __init__(self, path: str = "") -> None:
        self._path = Path(path) if path else None
        self._mode: dict[int, bool] = {}
        self._load()

    def _load(self) -> None:
        if not self._path or not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text() or "{}")
            self._mode = {int(k): bool(v) for k, v in raw.items()}
        except Exception:
            logger.warning("VoiceModeStore: failed to load %s", self._path, exc_info=True)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({str(k): v for k, v in self._mode.items()}))
        except Exception:
            logger.warning("VoiceModeStore: failed to save %s", self._path, exc_info=True)

    def get(self, chat_id: int) -> bool | None:
        """The explicit preference, or None when unset (auto / reply-in-kind)."""
        return self._mode.get(chat_id)

    def set(self, chat_id: int, on: bool) -> None:
        self._mode[chat_id] = on
        self._save()

    def clear(self, chat_id: int) -> None:
        """Back to auto (reply-in-kind)."""
        if self._mode.pop(chat_id, None) is not None:
            self._save()

    def wants_voice(self, chat_id: int, inbound_is_audio: bool) -> bool:
        """Whether THIS turn should be spoken: the explicit toggle if set, else
        reply-in-kind (speak iff the inbound message was a voice note)."""
        mode = self._mode.get(chat_id)
        return mode if mode is not None else inbound_is_audio

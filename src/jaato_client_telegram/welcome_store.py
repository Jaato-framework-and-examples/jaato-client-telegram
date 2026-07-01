"""First-contact welcome tracking.

Records which chats have already received the one-time, model-generated welcome,
so a returning user is never re-greeted. Persisted to JSON when a path is given
(survives restarts); in-memory only otherwise — there is no hardcoded default
path (repo convention: an empty path means per-process tracking, deliberately).

The welcome itself is produced by the AGENT, not canned here: on a chat's first
turn we inject a hidden system directive so the model opens by introducing itself
and the capabilities/tools it actually has (which stays accurate as tools change).
Two shapes — a standalone greeting for a bare ``/start`` (no question yet), and a
prefix that rides on the user's first real message so the intro precedes the answer.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


WELCOME_START = (
    "[SYSTEM — first contact: this person just opened you for the first time and "
    "hasn't asked anything yet. Greet them warmly and briefly introduce yourself: "
    "who you are, the main things you can help them with, and a few of the tools / "
    "capabilities you have. Keep it friendly and concise — a few short lines. Do "
    "not invent a question to answer; there isn't one yet.]"
)
WELCOME_PREFIX = (
    "[SYSTEM — first contact: this is this person's first message to you ever. "
    "Begin your reply with a brief, warm one- or two-line introduction of who you "
    "are and what you can help with, then answer their message normally.]\n\n"
)


class WelcomeStore:
    """Tracks which chat_ids have received the first-contact welcome."""

    def __init__(self, path: str = "") -> None:
        self._path = Path(path) if path else None
        self._welcomed: set[int] = set()
        self._load()

    def _load(self) -> None:
        if not self._path or not self._path.is_file():
            return
        try:
            self._welcomed = {int(c) for c in json.loads(self._path.read_text() or "[]")}
        except Exception:
            logger.warning("WelcomeStore: failed to load %s", self._path, exc_info=True)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(sorted(self._welcomed)))
        except Exception:
            logger.warning("WelcomeStore: failed to save %s", self._path, exc_info=True)

    def claim_first_contact(self, chat_id: int) -> bool:
        """Atomically mark a chat welcomed. Returns True iff this is the FIRST
        contact (caller should send the welcome); False if already welcomed.

        Marks + persists BEFORE the welcome is sent so a crash/restart between the
        claim and the reply can't re-welcome — a rare 'claimed but send failed'
        (no welcome) is preferable to greeting the same user twice."""
        if chat_id in self._welcomed:
            return False
        self._welcomed.add(chat_id)
        self._save()
        return True

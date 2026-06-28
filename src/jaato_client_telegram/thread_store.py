"""Per-chat Telegram message-thread continuity.

The bot follows whichever thread the user is writing in: each inbound message's
``message_thread_id`` is synced here, and every bot send for that chat (the
renderer's model output AND host-tool messages) goes into ``current``. The model
can branch to a NEW thread via the ``open_thread`` host tool, which mints an id
distinct from every thread seen so far (``known``) and makes it current.

Persisted to JSON when a path is given (continuity survives restarts); in-memory
only otherwise — there is no hardcoded default path (repo convention: an empty
path means the feature degrades to per-process, deliberately).
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ChatThreadStore:
    """Tracks, per chat_id, the current send thread and every thread id seen."""

    def __init__(self, path: str = "") -> None:
        self._path = Path(path) if path else None
        self._current: dict[int, int | None] = {}
        self._known: dict[int, set[int]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path or not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text() or "{}")
            for cid, rec in raw.items():
                self._current[int(cid)] = rec.get("current")
                self._known[int(cid)] = {int(t) for t in rec.get("known", [])}
        except Exception:
            logger.warning("ChatThreadStore: failed to load %s", self._path, exc_info=True)

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                str(cid): {
                    "current": self._current.get(cid),
                    "known": sorted(self._known.get(cid, set())),
                }
                for cid in set(self._current) | set(self._known)
            }
            self._path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.warning("ChatThreadStore: failed to save %s", self._path, exc_info=True)

    def current(self, chat_id: int) -> int | None:
        """The thread id the bot should send into for this chat (``None`` = main)."""
        return self._current.get(chat_id)

    def sync_inbound(self, chat_id: int, thread_id: int | None) -> None:
        """Follow the user: the thread their latest message was in becomes the
        thread the bot replies into. ``None`` means the main (no-thread) view."""
        self._current[chat_id] = thread_id
        if thread_id is not None:
            self._known.setdefault(chat_id, set()).add(thread_id)
        self._save()

    def open_new(self, chat_id: int) -> int:
        """Mint a thread id distinct from every id seen in this chat, make it the
        current send thread, and return it. The ``open_thread`` tool sends its
        titled root message with this id."""
        known = self._known.setdefault(chat_id, set())
        candidates = set(known)
        cur = self._current.get(chat_id)
        if cur:
            candidates.add(cur)
        new_id = (max(candidates) if candidates else 0) + 1
        known.add(new_id)
        self._current[chat_id] = new_id
        self._save()
        return new_id

    def set_current(self, chat_id: int, thread_id: int | None) -> None:
        """Force the current thread (e.g. back to main with ``None``)."""
        self._current[chat_id] = thread_id
        if thread_id is not None:
            self._known.setdefault(chat_id, set()).add(thread_id)
        self._save()

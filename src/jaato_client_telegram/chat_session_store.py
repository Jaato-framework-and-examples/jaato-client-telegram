"""Persistent ``chat_id -> session_id`` map for session re-attachment.

The bot keeps one daemon session per Telegram chat, but that mapping lives in
memory and is lost when the bot process restarts.  This store persists it to a
JSON file so that, after a restart, the bot can RE-ATTACH to the same daemon
session (``session.attach``, which loads from disk if needed) instead of
starting a fresh conversation.

The store is only constructed when ``session_store_path`` is configured; an
empty path disables re-attachment entirely (the pool simply never creates a
store).  There is no hardcoded default path.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class ChatSessionStore:
    """A tiny JSON-backed ``{chat_id: session_id}`` map with atomic writes."""

    def __init__(self, path: str) -> None:
        if not path:
            # The pool gates on the config; constructing with an empty path is a
            # programming error, not a runtime condition to paper over.
            raise ValueError("ChatSessionStore requires a non-empty path")
        self._path = Path(path).expanduser()
        self._map: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt/unreadable store is a real I/O failure mode; recover
            # deterministically by starting empty and re-persisting on the next
            # write. Logged loudly so it is never silent.
            log.warning("ChatSessionStore: cannot read %s (%s); starting empty", self._path, exc)
            return
        if isinstance(data, dict):
            self._map = {str(k): str(v) for k, v in data.items()}
        else:
            log.warning("ChatSessionStore: %s is not a JSON object; starting empty", self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._map, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # atomic on POSIX

    def get(self, chat_id: int) -> str | None:
        """Return the persisted session_id for ``chat_id``, or None."""
        return self._map.get(str(chat_id))

    def set(self, chat_id: int, session_id: str) -> None:
        """Persist ``chat_id -> session_id`` (overwrites any prior mapping)."""
        self._map[str(chat_id)] = session_id
        self._save()

    def remove(self, chat_id: int) -> None:
        """Forget the mapping for ``chat_id`` (no-op if absent)."""
        if self._map.pop(str(chat_id), None) is not None:
            self._save()

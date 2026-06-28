"""A thin proxy over the aiogram ``Bot`` that keeps host-tool messages in the
chat's active thread.

Host tools (built-in ``send_to_telegram`` / ``show_image`` and every dynamic
tool's ``ctx.bot``) call ``bot.send_message`` / ``send_photo`` / ``send_document``
with only a ``chat_id`` — no ``message_thread_id`` — so their messages fall out
of whatever thread the conversation is in (visible in the whole-chat view but not
the per-thread view). Wrapping the bot in this proxy injects the chat's current
``message_thread_id`` into any ``send_*`` call that targets this chat and didn't
set one explicitly. Tools need no changes; an explicit ``message_thread_id`` (or
a different chat) is never overridden.
"""

from typing import Any, Callable, Optional


class ThreadAwareBot:
    """Proxy injecting the chat's current thread id into ``send_*`` calls.

    Args:
        bot: the real aiogram ``Bot``.
        chat_id: the chat this proxy is scoped to (only sends to it are threaded).
        thread_getter: called at send time, returns the current ``message_thread_id``
            for the chat (``None`` = main view → no injection).
    """

    def __init__(self, bot: Any, chat_id: int, thread_getter: Callable[[], Optional[int]]):
        self._bot = bot
        self._chat_id = chat_id
        self._thread_getter = thread_getter

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._bot, name)
        if not (name.startswith("send_") and callable(attr)):
            return attr

        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            injected = False
            if "message_thread_id" not in kwargs:
                cid = kwargs.get("chat_id", args[0] if args else None)
                if cid == self._chat_id:
                    tid = self._thread_getter()
                    if tid is not None:
                        kwargs["message_thread_id"] = tid
                        injected = True
            try:
                return await attr(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — only swallow the thread case
                # A stale/invalid thread id we injected must not break a host-tool
                # send (private chats give the bot no way to create a thread) —
                # retry without it. Anything else propagates.
                if injected and "thread not found" in str(e).lower():
                    kwargs.pop("message_thread_id", None)
                    return await attr(*args, **kwargs)
                raise

        return wrapped

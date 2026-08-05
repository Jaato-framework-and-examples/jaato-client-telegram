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

from jaato_client_telegram.host_tool_loader import (
    flush_before_prompt,
    open_fold_slot,
    record_delivery,
)


def _visible_text(method: str, args: tuple, kwargs: dict) -> str:
    """The user-visible text a ``send_*`` call delivered: ``text`` for
    ``send_message`` (positional or keyword), ``caption`` for media sends
    (``send_photo``/``send_document``/…). ``""`` when there is none (e.g. a bare
    photo) — record_delivery treats that as nothing to record."""
    if method == "send_message":
        text = kwargs.get("text", args[1] if len(args) > 1 else None)
    else:
        text = kwargs.get("caption")
    return str(text) if text else ""


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
            cid = kwargs.get("chat_id", args[0] if args else None)
            to_this_chat = cid == self._chat_id
            # Flush the renderer's pending narration BEFORE an out-of-band tool send to
            # THIS chat, so the send lands AFTER the narration. Every host-tool send —
            # send_to_telegram / show_image / ask_user / any dynamic ctx.bot.send_* —
            # funnels through here, so this ONE flush point keeps prompt/result-after-
            # narration order (the renderer's throttled narration otherwise loses the
            # race to the tool's direct send). No-op when no renderer is streaming.
            if to_this_chat:
                await flush_before_prompt(self._chat_id)
            injected = False
            if to_this_chat and "message_thread_id" not in kwargs:
                tid = self._thread_getter()
                if tid is not None:
                    kwargs["message_thread_id"] = tid
                    injected = True
            try:
                sent = await attr(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — only swallow the thread case
                # A stale/invalid thread id we injected must not break a host-tool
                # send (private chats give the bot no way to create a thread) —
                # retry without it. Anything else propagates.
                if injected and "thread not found" in str(e).lower():
                    kwargs.pop("message_thread_id", None)
                    sent = await attr(*args, **kwargs)
                else:
                    raise
            # Record what a dynamic tool just delivered to its own chat so the model
            # sees it even if the tool returned only a status (no-op unless a
            # make_executor recorder is active — see host_tool_loader._DELIVERY_RECORDER).
            if to_this_chat:
                record_delivery(_visible_text(name, args, kwargs))
            # After an image lands in THIS chat, ask the renderer to open a fold slot
            # right here so the turn's trailing narration edits into this position
            # instead of appending below a reply the user slips in (no-op unless the
            # renderer registered a fold hook — i.e. rendering.fold_post_image_text on).
            if to_this_chat and name == "send_photo":
                await open_fold_slot(self._chat_id)
            return sent

        return wrapped

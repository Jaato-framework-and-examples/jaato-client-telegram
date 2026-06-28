"""Clarification UI for jaato-client-telegram.

Surfaces the agent's ``request_clarification`` questions to Telegram and routes
the user's answers back. WS clients receive every question at once
(``ClarificationBatchEvent``) and reply in one batch
(``ClarificationBatchResponseEvent``); the server feeds each answer into the
channel input queue in order.

Answer format per question (what the server's channel parser expects, the same
strings a TUI user would type):

- ``single_choice``   → the 1-based ordinal of the chosen option, e.g. ``"2"``
- ``multiple_choice`` → comma-separated ordinals, e.g. ``"1,3"``
- ``free_text``       → the literal text

This mirrors :class:`~jaato_client_telegram.permissions.PermissionHandler`:
pure UI/state logic, no Telegram I/O — callers send the messages. Single-choice
questions get one-tap inline buttons (answered via a ``clar:`` callback, like
permissions); multiple-choice and free-text questions are answered by a text
reply (routed by the private message handler before it acquires the per-user
lock, so the in-flight stream keeps rendering the continuation).
"""

import html
import logging
from dataclasses import dataclass, field

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


logger = logging.getLogger(__name__)


@dataclass
class PendingClarification:
    """A clarification batch awaiting the user's answers."""

    request_id: str
    questions: list[dict]            # batch question dicts, in order
    chat_id: int
    context: str = ""                # why the agent needs this (shown once)
    answers: list[str] = field(default_factory=list)  # accumulated, one per question
    current: int = 0                 # 0-based index of the question awaiting an answer


class ClarificationHandler:
    """Tracks pending clarification batches per chat and builds their UI."""

    def __init__(self) -> None:
        self._pending: dict[int, PendingClarification] = {}

    def store_pending(self, event, chat_id: int) -> PendingClarification:
        """Store a ClarificationBatchEvent as the pending request for a chat."""
        pending = PendingClarification(
            request_id=event.request_id,
            questions=list(event.questions or []),
            chat_id=chat_id,
            context=getattr(event, "context", "") or "",
        )
        self._pending[chat_id] = pending
        logger.info(
            "Stored pending clarification: request_id=%s chat_id=%d questions=%d",
            event.request_id, chat_id, len(pending.questions),
        )
        return pending

    def get_pending(self, chat_id: int) -> PendingClarification | None:
        return self._pending.get(chat_id)

    def remove_pending(self, chat_id: int) -> None:
        if self._pending.pop(chat_id, None) is not None:
            logger.info("Removed pending clarification for chat_id=%d", chat_id)

    def current_question(self, chat_id: int) -> dict | None:
        pending = self._pending.get(chat_id)
        if not pending or pending.current >= len(pending.questions):
            return None
        return pending.questions[pending.current]

    @staticmethod
    def is_single_choice(question: dict) -> bool:
        return (
            question.get("question_type") == "single_choice"
            and bool(question.get("choices"))
        )

    def build_question_ui(
        self, pending: PendingClarification, question: dict, include_context: bool,
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        """Render one question. Returns (html_text, keyboard_or_None).

        A keyboard is returned only for single-choice questions; multiple-choice
        and free-text questions are answered by a text reply.
        """
        qtype = question.get("question_type", "single_choice")
        idx = question.get("index", pending.current + 1)
        total = len(pending.questions)

        lines = [f"❓ <b>Clarification</b> ({idx}/{total})", ""]
        if include_context and pending.context:
            lines.append(f"<i>{html.escape(pending.context, quote=False)}</i>")
            lines.append("")

        lines.append(html.escape(question.get("text", ""), quote=False))

        choices = question.get("choices") or []
        keyboard: InlineKeyboardMarkup | None = None
        if qtype == "single_choice" and choices:
            # The options ARE the tappable buttons — don't also list them as prose
            # (that rendered every option twice: once in the text, once as a button).
            keyboard = self._build_choice_keyboard(pending.request_id, pending.current, choices)
        elif qtype == "multiple_choice" and choices:
            # No buttons here — the user types the numbers, so they must see them.
            lines.append("")
            for j, choice in enumerate(choices, 1):
                lines.append(f"  {j}. {html.escape(choice.get('text', ''), quote=False)}")
            lines += ["", "<i>Reply with the option numbers, comma-separated (e.g. 1,3).</i>"]
        else:  # free_text (or single_choice with no choices — treat as text)
            lines += ["", "<i>Reply with your answer.</i>"]

        return "\n".join(lines), keyboard

    def _build_choice_keyboard(
        self, request_id: str, question_index: int, choices: list[dict],
    ) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for ordinal, choice in enumerate(choices, 1):
            label = choice.get("text", "")[:40]
            # callback data: clar:request_id:question_index:choice_ordinal
            builder.add(InlineKeyboardButton(
                text=f"{ordinal}. {label}",
                callback_data=f"clar:{request_id}:{question_index}:{ordinal}",
            ))
        builder.adjust(1)
        return builder.as_markup()

    @staticmethod
    def parse_callback_data(callback_data: str) -> tuple[str, int, int] | None:
        """Parse ``clar:request_id:question_index:choice_ordinal``."""
        parts = callback_data.split(":")
        if len(parts) != 4 or parts[0] != "clar":
            return None
        try:
            return parts[1], int(parts[2]), int(parts[3])
        except ValueError:
            return None

    def record_answer(self, chat_id: int, answer: str) -> tuple[str, object]:
        """Record the current question's answer and advance.

        Returns one of:
        - ``("next", question_dict)``  — more questions remain
        - ``("done", answers_list)``   — all questions answered (send the batch)
        - ``("error", message)``       — no pending clarification
        """
        pending = self._pending.get(chat_id)
        if not pending:
            return ("error", "no pending clarification")
        pending.answers.append(answer)
        pending.current += 1
        if pending.current >= len(pending.questions):
            return ("done", list(pending.answers))
        return ("next", pending.questions[pending.current])


async def advance_clarification(message, chat_id, status, payload, handler, pool):
    """Send the next question, or submit the batch once all are answered.

    Shared by the button-callback (single_choice) and text-reply
    (free_text / multiple_choice) answer paths. ``message`` is any aiogram
    Message used only to ``.answer()``; ``pool`` is the SessionPool.
    """
    if status == "next":
        pending = handler.get_pending(chat_id)
        if pending is None:
            return
        text, keyboard = handler.build_question_ui(pending, payload, include_context=False)
        if keyboard is not None:
            await message.answer(text, reply_markup=keyboard)
        else:
            await message.answer(text)
    elif status == "done":
        pending = handler.get_pending(chat_id)
        request_id = pending.request_id if pending else ""
        answers = payload
        handler.remove_pending(chat_id)
        session_id = pool.get_session_id(chat_id)
        if session_id and request_id:
            await pool.respond_to_clarification(session_id, request_id, answers)
            logger.info("Clarification submitted: request_id=%s answers=%s", request_id, answers)
        else:
            await message.answer("❌ No active session to submit the clarification answer.")
    elif status == "error":
        logger.warning("advance_clarification: %s (chat_id=%d)", payload, chat_id)

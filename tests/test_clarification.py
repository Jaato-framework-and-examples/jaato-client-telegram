"""Unit tests for the clarification handler + batch-response wiring.

Exercises the pure logic against the real SDK events (no Telegram I/O):
- batch event -> per-question UI (single_choice keyboard / text prompt)
- callback data round-trip
- record/advance across multiple questions -> done
- session_pool.respond_to_clarification calls the client's respond_to_clarification_batch
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from jaato_sdk.events import ClarificationBatchEvent

from jaato_client_telegram.clarification import ClarificationHandler


def _batch():
    return ClarificationBatchEvent(
        request_id="req-1",
        tool_name="request_clarification",
        context="Need details to proceed.",
        questions=[
            {
                "index": 1,
                "text": "Which format?",
                "question_type": "single_choice",
                "required": True,
                "choices": [{"text": "JSON"}, {"text": "YAML"}],
            },
            {
                "index": 2,
                "text": "Any extra notes?",
                "question_type": "free_text",
                "required": False,
            },
        ],
    )


def test_single_choice_builds_keyboard_with_ordinal_callbacks():
    h = ClarificationHandler()
    pending = h.store_pending(_batch(), chat_id=7)
    q = h.current_question(7)
    assert q["index"] == 1
    text, keyboard = h.build_question_ui(pending, q, include_context=True)
    assert "Which format?" in text
    assert "Need details to proceed." in text  # context shown once
    assert keyboard is not None
    datas = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    assert datas == ["clar:req-1:0:1", "clar:req-1:0:2"]


def test_free_text_has_no_keyboard():
    h = ClarificationHandler()
    pending = h.store_pending(_batch(), chat_id=7)
    _, kb1 = h.build_question_ui(pending, pending.questions[0], include_context=False)
    _, kb2 = h.build_question_ui(pending, pending.questions[1], include_context=False)
    assert kb1 is not None      # single_choice
    assert kb2 is None          # free_text -> text reply


def test_callback_roundtrip():
    h = ClarificationHandler()
    assert h.parse_callback_data("clar:req-1:0:2") == ("req-1", 0, 2)
    assert h.parse_callback_data("perm:x:y") is None
    assert h.parse_callback_data("clar:req-1:0:notint") is None


def test_record_answer_advances_then_done():
    h = ClarificationHandler()
    h.store_pending(_batch(), chat_id=7)
    # Answer Q1 with the chosen ordinal "1" -> next question
    status, payload = h.record_answer(7, "1")
    assert status == "next"
    assert payload["index"] == 2
    # Answer Q2 free text -> done, answers in order
    status, payload = h.record_answer(7, "see the README")
    assert status == "done"
    assert payload == ["1", "see the README"]


def test_respond_to_clarification_builds_batch_response():
    from jaato_client_telegram.session_pool import SessionPool, SessionMetadata
    from datetime import datetime

    calls = []

    class _FakeClient:
        async def respond_to_clarification_batch(self, request_id, answers):
            calls.append((request_id, answers))

    pool = SessionPool.__new__(SessionPool)  # bypass __init__ (needs config)
    pool._sessions = {7: SessionMetadata(
        session_id="sess-1", created_at=datetime.now(),
        last_activity=datetime.now(), client=_FakeClient(),
    )}

    asyncio.run(pool.respond_to_clarification("sess-1", "req-1", ["1", "hello"]))
    assert calls == [("req-1", ["1", "hello"])]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"✓ {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} clarification tests passed")

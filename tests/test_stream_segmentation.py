"""Tests for the human-paced streaming segmenter (segment_stream_text).

Cases are seeded from the real session that motivated the feature
(runtime/.jaato/sessions/20260625_164525.json): the model self-segments into one
narration per action, with a multi-paragraph final summary containing a ```json
code fence, and a 3-char "Let" false-start.
"""

from jaato_client_telegram.renderer import segment_stream_text, _fence_balanced


def test_empty():
    assert segment_stream_text("", flush=True, final=True) == ([], "")
    assert segment_stream_text("   ", flush=False) == ([], "")


def test_fence_balanced_helper():
    assert _fence_balanced("no fence here")
    assert _fence_balanced("```code```")
    assert not _fence_balanced("```open fence")


def test_midstream_holds_incomplete_trailing_paragraph():
    # "A" is terminated by \n\n (complete, and >= min_unit); "B in progress" has
    # no terminator, so it is held back as remainder.
    units, rem = segment_stream_text(
        "First complete paragraph, definitely long enough to stand alone.\n\n"
        "Second still being written",
        flush=False,
    )
    assert units == ["First complete paragraph, definitely long enough to stand alone."]
    assert rem == "Second still being written"


def test_flush_emits_trailing_at_tool_boundary():
    # At a tool boundary the model has moved on -> emit the trailing narration too.
    units, rem = segment_stream_text(
        "Now I'll fix the STORE_PATH to use the workspace directory explicitly.",
        flush=True,
    )
    assert units == ["Now I'll fix the STORE_PATH to use the workspace directory explicitly."]
    assert rem == ""


def test_never_splits_inside_code_fence():
    # The \n\n INSIDE the ```json block must NOT create a boundary.
    final_summary = (
        "**Persistence is now working.** The file exists on disk.\n\n"
        "```json\n"
        "[\n"
        "  {\n"
        '    "id": "r1",\n'
        '    "text": "Disk persistence verified!"\n'
        "  }\n"
        "]\n"
        "```\n\n"
        "**What was wrong:** STORE_PATH used Path(__file__).parent.\n\n"
        "**The fix:** changed it to an absolute workspace path."
    )
    units, rem = segment_stream_text(final_summary, flush=True, final=True)
    # The json block stays intact as exactly one unit.
    json_units = [u for u in units if "```json" in u]
    assert len(json_units) == 1
    assert json_units[0].count("```") == 2  # fence opened AND closed in one unit
    assert "id" in json_units[0] and "r1" in json_units[0]
    # And it split into the expected 4 natural pieces.
    assert len(units) == 4
    assert units[0].startswith("**Persistence is now working.**")
    assert units[-1].startswith("**The fix:**")
    assert rem == ""


def test_submin_fragment_held_then_coalesced():
    # The "Let" false-start (3 chars) must NOT become its own bubble at a boundary.
    units, rem = segment_stream_text("Let", flush=True, final=False)
    assert units == []
    assert rem == "Let"
    # …but at turn end it is emitted rather than dropped (never lose content).
    units2, rem2 = segment_stream_text("Let", flush=True, final=True)
    assert units2 == ["Let"]
    assert rem2 == ""


def test_submin_coalesces_forward_into_next_paragraph():
    units, rem = segment_stream_text(
        "Hi\n\nThis is a sufficiently long second paragraph to stand on its own.",
        flush=True,
    )
    # "Hi" (2 chars) is folded into the next unit, not emitted alone.
    assert len(units) == 1
    assert units[0].startswith("Hi")
    assert "second paragraph" in units[0]


async def _run(events):
    """Drive stream_response with a mock Message; return the list of texts sent."""
    import pytest  # noqa
    from unittest.mock import AsyncMock, MagicMock
    from jaato_client_telegram.renderer import ResponseRenderer

    msg = MagicMock()
    msg.answer = AsyncMock(return_value=MagicMock())
    msg.bot.send_chat_action = AsyncMock()
    msg.chat.id = 1

    class Ev:
        def __init__(self, **k): self.__dict__.update(k)

    async def gen():
        for e in events:
            yield Ev(**e)

    renderer = ResponseRenderer()
    await renderer.stream_response(msg, gen())
    return [str(c.args[0]) for c in msg.answer.call_args_list if c.args]


import pytest


@pytest.mark.asyncio
async def test_two_narrations_split_by_tool_emit_two_messages():
    # The core regression this feature fixes: narration → tool → narration must
    # stream as TWO messages, not one blob at the end.
    sent = await _run([
        {"type": "agent.output", "source": "model", "mode": "write",
         "text": "Looking at the tool source to find the bug now."},
        {"type": "agent.output", "source": "tool", "mode": "write",
         "tool_name": "notebook_execute", "tool_args": {}, "text": "ok"},
        {"type": "agent.output", "source": "model", "mode": "write",
         "text": "Fixed it — re-registering the tool so the change takes effect."},
        {"type": "agent.completed"},
    ])
    narration = [s for s in sent if "Looking at the tool source" in s or "Fixed it" in s]
    assert any("Looking at the tool source" in s for s in narration)
    assert any("Fixed it" in s for s in narration)
    # The two narrations are in SEPARATE messages, not glued into one.
    assert not any("Looking at the tool source" in s and "Fixed it" in s for s in sent)


def test_multi_paragraph_all_complete():
    text = (
        "Paragraph one is long enough to be its own unit here.\n\n"
        "Paragraph two is also long enough to be its own unit.\n\n"
    )
    units, rem = segment_stream_text(text, flush=False)
    assert len(units) == 2
    assert rem == ""

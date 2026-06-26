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


def test_midstream_holds_short_content_until_target():
    # Under target, mid-stream emits NOTHING (no premature tiny messages); the
    # whole thing is held as remainder.
    units, rem = segment_stream_text(
        "Short first line.\nSecond still being written", flush=False, target=350,
    )
    assert units == []
    assert "Short first line." in rem and "Second still being written" in rem


def test_midstream_emits_once_over_target():
    # Once committed lines exceed target a unit is emitted; the in-progress
    # trailing line is held back.
    big = "X" * 200
    units, rem = segment_stream_text(
        f"{big}\n{big}\ntrailing in progress", flush=False, target=150,
    )
    assert units, "something should have been emitted past target"
    assert "trailing in progress" in rem


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
    # Small target forces splitting — but the fence must still never be cut.
    units, rem = segment_stream_text(final_summary, flush=True, final=True, target=60)
    json_units = [u for u in units if "```json" in u]
    assert len(json_units) == 1
    assert json_units[0].count("```") == 2  # fence opened AND closed in one unit
    assert "id" in json_units[0] and "r1" in json_units[0]
    assert len(units) >= 2  # it DID split (at the small target), just not in the fence
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
async def test_first_token_not_split_off_per_delta_flush():
    # Regression: per-delta flushing exposed a latent _flush_text_buffer bug that
    # inserted a paragraph break after the first delta ("No\n\n, the ..."). The
    # emitted message must contain the text contiguously, no break after token 1.
    sent = await _run([
        {"type": "agent.output", "source": "model", "mode": "write", "text": "No"},
        {"type": "agent.output", "source": "model", "mode": "append",
         "text": ", the approval server is not running. Want me to start it?"},
        {"type": "agent.completed"},
    ])
    joined = "\n----\n".join(sent)
    assert "No, the approval server" in joined, joined
    assert "No\n" not in joined  # no stray break after "No"


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


def test_never_splits_html_pre_code_block():
    # The model emits code as HTML <pre><code>…</code></pre> (not ``` markdown).
    # A split there breaks the HTML and Telegram shows raw tags. The gate must
    # keep the whole block in one unit even though it spans many lines.
    text = (
        "Here's the code — take a look:\n"
        "<pre><code>   1 | import json, os\n"
        "   2 | \n"
        "   3 | TOOL_SCHEMA = {\n"
        '   4 | "name": "shopping_list",\n'
        "   5 | }\n"
        "</code></pre>\n"
        "That's the whole thing — want me to register it?"
    )
    units, rem = segment_stream_text(text, flush=True, final=True, target=80)
    code_units = [u for u in units if "<pre><code>" in u]
    assert len(code_units) == 1, units
    u = code_units[0]
    assert u.count("<pre") == u.count("</pre") == 1
    assert u.count("<code") == u.count("</code") == 1
    assert "shopping_list" in u  # whole block intact despite tiny target


def test_short_multiparagraph_stays_one_unit():
    # Regression: a short answer with a blank-line break (list + a trailing
    # comment) must stay ONE message — splitting it produced "shampoo(Same..."
    # gluing across message boundaries.
    text = (
        "🛒 **Shopping list (3 items):**\n  1. iogurt\n  2. lettuce\n  3. shampoo\n\n"
        "(Same as before — nothing changed since you last checked.)"
    )
    units, rem = segment_stream_text(text, flush=True, final=True)
    assert len(units) == 1, units
    assert "shampoo" in units[0] and "Same as before" in units[0]
    assert "\n\n" in units[0]  # the paragraph break is preserved inside the unit


def test_submin_tail_never_lone_bubble():
    # A stray sub-min tail at turn end must merge into the previous unit, not be
    # emitted as its own 1-char message (the "I" hiccup).
    units, rem = segment_stream_text(
        "This first line is comfortably above the minimum unit size.\nI",
        flush=True, final=True,
    )
    assert rem == ""
    assert "I" not in units  # not a lone unit
    assert units[-1].endswith("I")  # merged into the previous one


def test_fence_gate_helper_covers_html():
    assert not _fence_balanced("<pre><code>open")
    assert _fence_balanced("<pre><code>x</code></pre>")
    assert not _fence_balanced("<code>inline never closed")


def test_short_paragraphs_stay_together_under_target():
    # Short multi-paragraph content stays ONE unit (blank line preserved inside),
    # rather than one message per paragraph.
    text = (
        "Paragraph one here.\n\n"
        "Paragraph two here."
    )
    units, rem = segment_stream_text(text, flush=True, final=True, target=350)
    assert len(units) == 1
    assert "Paragraph one" in units[0] and "Paragraph two" in units[0]
    assert "\n\n" in units[0]
    assert rem == ""

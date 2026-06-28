"""
Response renderer for jaato events.

Handles progressive rendering of streamed events to Telegram messages,
including long message splitting and edit-in-place updates.
"""

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from jaato_client_telegram.semantic_markup import render_semantic_markup

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from jaato_sdk.events import EventType

if TYPE_CHECKING:
    from jaato_client_telegram.file_handler import FileHandler
    from jaato_client_telegram.permissions import PermissionHandler
    from jaato_client_telegram.clarification import ClarificationHandler
    from jaato_client_telegram.session_pool import SessionPool


log = logging.getLogger(__name__)


# ANSI escape code pattern - matches terminal color codes like [1;38;5;253;48;5;235m
ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-9;]*m|\[\d+(?:;\d+)*m')


def strip_ansi_codes(text: str) -> str:
    """
    Remove ANSI escape codes from text.
    
    These terminal color codes can appear in command output and will
    cause issues when sent to Telegram.
    
    Args:
        text: Text that may contain ANSI codes
        
    Returns:
        Text with ANSI codes removed
    """
    return ANSI_ESCAPE_PATTERN.sub('', text)


def escape_html_content(text: str) -> str:
    """
    Escape HTML special characters and strip ANSI codes from text content.
    
    This ensures that:
    1. Any angle brackets, ampersands, etc. in the model's output don't 
       get interpreted as HTML by Telegram.
    2. Terminal color codes (ANSI escape sequences) are removed as they
       would display as garbage in Telegram.
    3. Filenames like README.md don't get treated as URLs by Telegram.
    
    Args:
        text: Raw text that may contain HTML-like characters or ANSI codes
        
    Returns:
        Clean text with HTML special characters escaped and ANSI codes removed
    """
    # First strip ANSI escape codes
    clean_text = strip_ansi_codes(text)
    # Then escape HTML
    escaped = html.escape(clean_text, quote=False)
    # Add zero-width non-joiner after dots in common filename extensions
    # to prevent Telegram from treating them as URLs
    # This affects: .md, .txt, .py, .js, .json, .yaml, .xml, .html, .css, etc.
    ZWNJ = '\u200c'  # Zero-width non-joiner character
    FILENAME_PATTERN = re.compile(r'\.(md|txt|py|js|json|yaml|yml|xml|html|css|java|kt|rs|go|ts|sh|bash|zsh|cfg|conf|ini|toml|lock)\b', re.IGNORECASE)
    escaped = FILENAME_PATTERN.sub(f'.{ZWNJ}\\1', escaped)
    return escaped


# If the runner goes completely silent for this long mid-turn — no events at all —
# the session is treated as stalled (e.g. a stuck/broken re-attach) so the bot can
# surface a clear error and self-heal instead of hanging forever. Generous on
# purpose: a healthy first response (runner re-spawn + first token) arrives well
# inside this, and any active turn keeps emitting events that reset the timer.
_STALL_TIMEOUT_SECS = 120.0


# --- Markdown -> Telegram HTML --------------------------------------------------
# The model is told the client supports markdown (presentation context), so it
# emits **bold**, *italic*, `code`, ```blocks```, [text](url) — but the bot sends
# parse_mode=HTML, which never rendered them (they showed literally). We convert
# the common, unambiguous markdown to Telegram's HTML subset at EMIT time, on the
# already-escaped, fully-assembled unit (so multi-delta spans are whole).
#
# DELIBERATELY conservative: no single-`_`/`__` emphasis (would mangle
# snake_case / file_paths), and code regions are protected so markdown inside
# them is left literal.
_MD_FENCE = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
_MD_INLINE_CODE = re.compile(r"`([^`\n]+?)`")
_HTML_PRE = re.compile(r"<pre\b.*?</pre>", re.DOTALL | re.IGNORECASE)
_HTML_CODE = re.compile(r"<code\b.*?</code>", re.DOTALL | re.IGNORECASE)
_MD_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_MD_ITALIC = re.compile(r"(?<!\w)\*(?=\S)([^*\n]+?)(?<=\S)\*(?!\w)")
_MD_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]\n]+?)\]\((https?://[^\s)]+)\)")
_PLACEHOLDER = "\x00%d\x00"


def markdown_to_telegram_html(text: str) -> str:
    """Convert common markdown in ``text`` to Telegram's HTML subset. Runs on the
    already-HTML-escaped, fully-assembled message (markdown markers aren't escaped
    by escape_html_content, so they survive intact). Code regions — existing
    <pre>/<code> and markdown ``` / `…` — are protected so their contents are not
    treated as markdown."""
    if not text or ("*" not in text and "`" not in text and "[" not in text and "~" not in text and "<pre" not in text):
        return text

    stash: list[str] = []

    def _stash(s: str) -> str:
        stash.append(s)
        return _PLACEHOLDER % (len(stash) - 1)

    # Protect code regions FIRST (markdown inside them must stay literal).
    text = _HTML_PRE.sub(lambda m: _stash(m.group(0)), text)
    text = _HTML_CODE.sub(lambda m: _stash(m.group(0)), text)
    text = _MD_FENCE.sub(lambda m: _stash(f"<pre>{m.group(1).rstrip(chr(10))}</pre>"), text)
    text = _MD_INLINE_CODE.sub(lambda m: _stash(f"<code>{m.group(1)}</code>"), text)

    # Inline emphasis on what's left (bold before italic so ** isn't eaten by *).
    text = _MD_BOLD.sub(r"<b>\1</b>", text)
    text = _MD_ITALIC.sub(r"<i>\1</i>", text)
    text = _MD_STRIKE.sub(r"<s>\1</s>", text)
    text = _MD_LINK.sub(r'<a href="\2">\1</a>', text)

    for i, s in enumerate(stash):
        text = text.replace(_PLACEHOLDER % i, s, 1)
    return text


@dataclass
class StreamingContext:
    """State for edit-in-place streaming of responses."""

    sent_message: Message | None = None
    accumulated_text: str = ""
    last_edit_time: float = 0.0
    edits_count: int = 0
    seen_model_output: bool = False  # Track if we've received any model output yet
    permission_sent: bool = False  # Track if permission UI was sent as separate message
    content_sent: bool = False  # Track if content was already sent (prevents final duplicate)
    last_final_text: str = ""   # Last text sent via send_final_response (dedups repeat TURN_COMPLETED)
    produced_output: bool = False  # Did this turn render ANY non-empty content to the user?
    stalled: bool = False  # Did the stream go silent (no events) past the stall timeout?

    # Buffer for text chunks in arrival order
    text_buffer: list[str] = field(default_factory=list)

    # Returns the message_thread_id the bot should currently send into for this
    # chat (read live so an open_thread mid-turn is reflected). None => follow the
    # incoming message's thread via Message.answer() as before.
    thread_id_getter: "Callable[[], int | None] | None" = None


def _has_telegram_html(text: str) -> bool:
    """Check if text contains Telegram-compatible HTML tags.

    Telegram parse_mode=HTML supports: <b>, <i>, <u>, <s>, <code>,
    <pre>, <blockquote>, <a>, <spoiler>. We check for the ones
    our pipeline actually emits.
    """
    return bool(re.search(r"<(?:pre|code|b|i|u|s|blockquote|a\s)[\s>]", text))


def split_preserving_paragraphs(text: str, max_len: int) -> list[str]:
    """
    Split text on paragraph boundaries without exceeding max_len.

    Tries to preserve paragraph structure. If a single paragraph
    exceeds max_len, it's split at character boundaries.

    Args:
        text: The text to split
        max_len: Maximum length per chunk (Telegram limit is 4096)

    Returns:
        List of text chunks, each <= max_len
    """
    if not text:
        return []
    
    # If text fits in one chunk, return it
    if len(text) <= max_len:
        return [text]
    
    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current = ""

    for para in paragraphs:
        # Preserve double newline between paragraphs
        candidate = f"{current}\n\n{para}".strip() if current else para

        if len(candidate) <= max_len:
            current = candidate
        else:
            # Current chunk is full, save it
            if current:
                chunks.append(current)
                current = ""

            # Check if single paragraph is too long
            if len(para) > max_len:
                # Split long paragraph at character boundaries
                for i in range(0, len(para), max_len):
                    chunk = para[i : i + max_len]
                    if chunk:  # Only add non-empty chunks
                        chunks.append(chunk)
            else:
                current = para

    # Add final chunk
    if current:
        chunks.append(current)

    return chunks


# --- Human-paced streaming segmentation -------------------------------------
# Model output arrives as a delta stream; rendering it in one blob at turn end
# reads as "frozen, then a wall of text". We segment it into natural units and
# emit each as it completes. The model already self-segments (one narration per
# action), so the primary boundary is the text->tool transition; within a long
# part we split on blank lines (\n\n), never inside a ``` code fence, and we
# never emit a fragment shorter than _MIN_UNIT_CHARS alone (coalesce it forward)
# to avoid single-word "stutter" bubbles. Validated against real session output.
_MIN_UNIT_CHARS = 40
# Target unit size. The model is inconsistent about paragraphing (sometimes \n\n,
# often just single \n between lines), so we group LINES up to ~this size rather
# than relying on blank-line paragraphs. Tuned for chat: big enough to keep a
# short list together, small enough that a long explanation streams in pieces.
_UNIT_TARGET_CHARS = 350


def _fence_balanced(s: str) -> bool:
    """True when no code block is left open in s — neither a ``` markdown fence
    NOR an HTML <pre>/<code> block. A boundary is only legal where this holds:
    splitting an open block cracks it in two. The model emits code as
    <pre><code>…</code></pre> (HTML), so a fence check alone let line-grouping
    break the tags, Telegram's HTML parse then failed, and it fell back to
    showing the raw tags as plain text."""
    if s.count("```") % 2:
        return False
    if s.count("<pre") != s.count("</pre"):
        return False
    if s.count("<code") != s.count("</code"):
        return False
    return True


def _group_lines(text: str, target: int, min_unit: int) -> tuple[list[str], str]:
    """Greedily group lines into ~``target``-sized units, splitting at a line
    boundary only once a group exceeds ``target`` — NOT at every blank line.
    Blank lines (paragraph breaks) are kept inside the unit so short, multi-
    paragraph answers stay a single message (splitting them produced spurious
    extra messages whose boundaries glued together, e.g. "shampoo(Same as ...").
    Never breaks inside an open ``` / <pre> / <code> block, never closes a group
    below ``min_unit``. Returns ``(units, leftover)`` — the last open group."""
    units: list[str] = []
    cur = ""
    for ln in text.split("\n"):
        cand = f"{cur}\n{ln}" if cur else ln
        if cur and not _fence_balanced(cur):
            cur = cand  # inside a code block: must not break
            continue
        over = len(cand) > target
        if over and len(cur.strip()) >= min_unit and _fence_balanced(cur):
            units.append(cur.strip())
            cur = ln
        else:
            cur = cand
    return units, cur.strip()


def segment_stream_text(
    text: str, *, flush: bool, final: bool = False,
    min_unit: int = _MIN_UNIT_CHARS, target: int = _UNIT_TARGET_CHARS,
) -> tuple[list[str], str]:
    """Split accumulated model text into emit-ready, ~``target``-sized units at
    line boundaries (blank-line OR single-newline), never cutting inside a ```
    fence, never emitting below ``min_unit``.

    Returns ``(units, remainder)``:
    - ``flush=False`` (mid-stream) — only fully-terminated lines are considered;
      the in-progress trailing line and the last not-yet-full group are held back.
    - ``flush=True`` (tool boundary / turn pause) — the trailing group is emitted,
      EXCEPT a sub-``min_unit`` tail is held to merge with what comes next…
    - ``final=True`` (turn end) — …unless this is the end, where all is emitted.

    Pure function: no I/O, fully unit-testable.
    """
    if not text or not text.strip():
        return [], ""

    if flush:
        units, leftover = _group_lines(text, target, min_unit)
        if leftover:
            if len(leftover) >= min_unit:
                units.append(leftover)
                leftover = ""
            elif final:
                # Sub-min tail at the very end: never emit a lone tiny bubble (e.g.
                # a stray "I"). Merge it into the previous unit if there is one;
                # only stand alone when it is the turn's entire content.
                if units:
                    units[-1] = f"{units[-1]}\n{leftover}".rstrip()
                else:
                    units.append(leftover)
                leftover = ""
        return units, leftover

    # Mid-stream: hold the in-progress (un-terminated) last line.
    nl = text.rfind("\n")
    if nl == -1:
        return [], text
    committed, tail = text[:nl], text[nl + 1:]
    units, leftover = _group_lines(committed, target, min_unit)
    if not units:
        # Nothing crossed the target yet, so nothing is emitted this tick. Hold
        # the WHOLE accumulated text VERBATIM rather than the strip/rejoin that
        # _group_lines applies to its leftover — that normalisation collapses
        # interior blank lines (a paragraph break between sections "\n\n") into a
        # single "\n", or drops a trailing one entirely, as more deltas append to
        # it. Lossless hold keeps the structure the model actually sent.
        return [], text
    remainder = f"{leftover}\n{tail}" if leftover else tail
    return units, remainder


class ResponseRenderer:
    """
    Renders jaato events as Telegram messages.

    Supports progressive streaming via edit-in-place and handles
    long messages by splitting at paragraph boundaries.
    """

    def __init__(
        self,
        max_message_length: int = 4096,
        edit_throttle_ms: int = 500,
        permission_handler: "PermissionHandler | None" = None,
        file_handler: "FileHandler | None" = None,
        clarification_handler: "ClarificationHandler | None" = None,
    ):
        """
        Initialize the renderer.

        Args:
            max_message_length: Telegram message length limit (default 4096)
            edit_throttle_ms: Minimum time between edit_message_text calls (currently unused - reserved for future progressive streaming)
            permission_handler: Optional handler for permission requests
            file_handler: Optional handler for file sending (ATTACH/SHARE)
            clarification_handler: Optional handler for clarification requests
        """
        self._max_message_length = max_message_length
        self._edit_throttle_seconds = edit_throttle_ms / 1000.0
        self._permission_handler = permission_handler
        self._file_handler = file_handler
        self._clarification_handler = clarification_handler

    def _flush_text_buffer(self, ctx: StreamingContext) -> None:
        """
        Flush accumulated text chunks to accumulated_text.
        
        Should be called before displaying tool calls or at turn completion.
        """
        if ctx.text_buffer:
            combined = "".join(ctx.text_buffer)
            # Guard's ONLY job: don't lead the message with whitespace. Once any
            # real model text has been emitted, append everything — INCLUDING a
            # whitespace-only delta. Streaming tokenizers routinely emit a lone
            # "\n" between list items or a lone " " after an em-dash as its own
            # delta; because we now flush per-delta, "if combined.strip()" alone
            # discarded those (and cleared the buffer), silently gluing
            # "stats- Bookmark", "plan🛠️ Productivity", "Timer —25/5".
            if combined.strip() or ctx.seen_model_output:
                # Add a blank line before the FIRST model output (to separate it
                # from any prior system/init text). seen_model_output MUST be set
                # on that first output regardless of whether the break was added —
                # otherwise, now that we flush per-delta, the 2nd delta sees a
                # non-empty accumulated_text with the flag still false and inserts
                # a spurious break, splitting the first token ("No\n\n, the ...").
                if not ctx.seen_model_output and ctx.accumulated_text:
                    if not ctx.accumulated_text.endswith("\n\n"):
                        ctx.accumulated_text += "\n\n"
                ctx.seen_model_output = True
                ctx.accumulated_text += combined
            ctx.text_buffer.clear()

    def _flush_all_buffers(self, ctx: StreamingContext) -> None:
        """Flush all pending buffers."""
        self._flush_text_buffer(ctx)

    async def _emit_one(self, initial_message: Message, ctx: StreamingContext, text: str) -> None:
        """Send one segmented unit as its own Telegram message (splitting at 4096
        if a single unit is huge, e.g. a giant code block)."""
        tid = ctx.thread_id_getter() if ctx.thread_id_getter else None
        for chunk in split_preserving_paragraphs(text, self._max_message_length):
            if not chunk.strip():
                continue
            # Render the model's markdown (**bold**, `code`, links, …) into
            # Telegram HTML on the whole, assembled chunk — per-delta rendering
            # could never see a marker that spanned two deltas.
            chunk = markdown_to_telegram_html(chunk)
            has_html = _has_telegram_html(chunk)
            sent = await self._send_with_typing_indicator(
                initial_message, chunk, parse_mode="HTML" if has_html else None,
                message_thread_id=tid,
            )
            if sent:
                ctx.sent_message = sent
            ctx.produced_output = True

    async def _emit_segments(
        self, initial_message: Message, ctx: StreamingContext,
        *, flush: bool = True, final: bool = False,
    ) -> None:
        """Progressive streaming: flush buffered model text and emit each completed
        segment as its own message (see segment_stream_text). The in-progress tail
        is held back unless ``final``. This replaces the old "accumulate the whole
        turn, send once" behaviour at every model/tool/turn boundary."""
        self._flush_text_buffer(ctx)
        if not ctx.accumulated_text.strip():
            ctx.accumulated_text = ""
            return
        units, remainder = segment_stream_text(
            ctx.accumulated_text, flush=flush, final=final,
        )
        ctx.accumulated_text = remainder
        if units:
            logging.getLogger(__name__).debug(
                "stream emit: flush=%s final=%s sizes=%s remainder=%dch",
                flush, final, [len(u) for u in units], len(remainder),
            )
        for unit in units:
            await self._emit_one(initial_message, ctx, unit)

    async def stream_response(
        self,
        initial_message: Message,
        event_stream,  # AsyncIterator[Event] from SDK
        thread_id_getter: "Callable[[], int | None] | None" = None,
    ) -> StreamingContext:
        """
        Stream events progressively, editing the message in place.

        Accumulates AGENT_OUTPUT events and updates the message
        at throttled intervals to respect Telegram rate limits.

        Args:
            initial_message: The user's message (for context)
            event_stream: Async iterator of events from SDK
            thread_id_getter: Optional callable returning the chat's current
                message_thread_id (so model output follows an open_thread branch).

        Returns:
            StreamingContext with final accumulated text
        """
        import logging
        log = logging.getLogger(__name__)

        ctx = StreamingContext()
        ctx.thread_id_getter = thread_id_getter
        ctx.last_edit_time = time.monotonic()

        init_progress_count = 0
        # On re-attach the daemon emits AGENT_STATUS_CHANGED "idle" during the restore
        # (the restored agent is idle BEFORE the user's turn runs). Treating that
        # pre-turn idle as turn-complete makes the renderer exit before the real
        # response is generated. Only honor done/idle once the turn has actually
        # started ("active" status, or model output).
        turn_started = False
        
        event_iter = event_stream.__aiter__()
        while True:
            try:
                event = await asyncio.wait_for(
                    event_iter.__anext__(), timeout=_STALL_TIMEOUT_SECS
                )
            except asyncio.TimeoutError:
                log.warning(
                    "stream_response: no event for %.0fs — treating session as stalled",
                    _STALL_TIMEOUT_SECS,
                )
                ctx.stalled = True
                break
            except StopAsyncIteration:
                break
            # Get event type - handle both enum and string
            event_type = getattr(event, "type", None)
            # Convert enum to string if needed
            if hasattr(event_type, "value"):
                event_type = event_type.value
            
            # Log session_id from events (for debugging multi-user issues)
            event_session_id = getattr(event, "session_id", None)
            if event_session_id:
                log.debug(f"Event session_id: {event_session_id}")

            # Handle different event types
            if event_type == EventType.AGENT_OUTPUT:
                # Agent output - check source and mode
                source = getattr(event, "source", None)
                mode = getattr(event, "mode", None)
                content = getattr(event, "text", "")

                # DEBUG: Log source, mode, and content
                log.debug(f"AGENT_OUTPUT received: source={source}, mode={mode}, text={repr(content[:200])}")

                # Handle flush signal FIRST - regardless of source
                # The SDK emits flush as source="system" with mode="flush" and empty text
                if mode == "flush":
                    log.debug("Flush signal received (source=%s) - emitting completed segments", source)
                    # Emit completed paragraphs now (hold a sub-min / in-progress tail).
                    await self._emit_segments(initial_message, ctx, flush=True, final=False)
                # Buffer model output for later display
                elif source == "model" and mode in ("write", "append"):
                    # Model output means the user's turn is underway — a backup to the
                    # "active" status for gating the done/idle exit on re-attach.
                    turn_started = True
                    # Process semantic markup tags first, then escape remaining HTML
                    rendered = render_semantic_markup(content)
                    if rendered != content:
                        # Semantic tags were found and rendered; don't double-escape
                        ctx.text_buffer.append(rendered)
                    else:
                        # No semantic tags; escape HTML as before
                        ctx.text_buffer.append(escape_html_content(content))
                    # Progressive streaming: emit any paragraph(s) that just completed
                    # (\n\n). A single-paragraph narration stays buffered until its
                    # tool boundary; a multi-paragraph block streams as it goes.
                    await self._emit_segments(initial_message, ctx, flush=False)

                elif source == "tool":
                    # Tool boundary: the model finished its narration before acting —
                    # emit it as a unit now (the primary, validated boundary).
                    await self._emit_segments(initial_message, ctx, flush=True, final=False)
                    # Tool output - format with tool name and parameters
                    tool_name = getattr(event, "tool_name", None)
                    tool_args = getattr(event, "tool_args", None)
                    
                    # If tool_name not in event, try to get from permission handler
                    # (for tools that required permission approval)
                    if not tool_name and self._permission_handler:
                        chat_id = initial_message.chat.id
                        approved_info = self._permission_handler.get_last_approved(chat_id)
                        if approved_info:
                            tool_name, tool_args = approved_info
                            # Clear after use so it doesn't affect subsequent tool outputs
                            self._permission_handler.clear_approved(chat_id)
                    
                    if tool_name:
                        # Format as structured tool block with name, params, and output
                        formatted = self._format_tool_block(tool_name, tool_args, content if content else None)
                        ctx.text_buffer.append(formatted)
                    elif self._is_wide_content(content):
                        # Fallback: no tool_name, but wide content - use expandable blockquote
                        expandable = self._format_expandable_blockquote(content)
                        ctx.text_buffer.append(expandable)
                    else:
                        # Fallback: regular output without structured formatting
                        # Escape HTML to prevent parsing errors
                        ctx.text_buffer.append(escape_html_content(content))
                else:
                    # Non-model output or unknown mode - log but don't buffer
                    if source != "system" or content:  # Skip system flush events (already handled)
                        log.debug(f"Non-model output: source={source}, mode={mode}, buffering anyway")

            elif event_type == "tool.call_start":
                # THE primary boundary: the model finished its narration and is
                # invoking a tool. Tool calls arrive as their own event type (NOT
                # source="tool" agent output), so this is where a multi-step turn
                # gets its natural per-step segmentation. Emit the narration now.
                await self._emit_segments(initial_message, ctx, flush=True, final=False)

            elif event_type == EventType.AGENT_COMPLETED:
                # Agent completed - emit everything remaining, including the tail.
                await self._emit_segments(initial_message, ctx, flush=True, final=True)
                ctx.content_sent = True
                break

            elif event_type == EventType.TURN_COMPLETED:
                # Emit completed paragraphs now, but HOLD the in-progress tail: in
                # multi-turn agentic flows more turns follow, and the true final
                # flush happens at AGENT_COMPLETED / status done|idle / post-loop.
                # (The old formatted_text "delete streaming msg + resend" dance is
                # gone — we stream discrete units, there is no single message to
                # replace, and the code already preferred accumulated text anyway.)
                await self._emit_segments(initial_message, ctx, flush=True, final=False)

                # NOTE: Do NOT break on turn.completed!
                # Multi-turn agentic flows have multiple turn.completed events
                # before the final agent.completed. Breaking here would truncate
                # the response after the first turn.
                log.debug("Turn completed, continuing to stream events...")

            elif event_type == EventType.AGENT_STATUS_CHANGED:
                # Agent status changed - check for completion signals
                status = getattr(event, "status", "")
                log.debug(f"Agent status changed: {status}")

                if status == "active":
                    # The user's turn has started; a subsequent done/idle is real.
                    turn_started = True
                elif status in ("done", "idle"):
                    if not turn_started:
                        # Pre-turn idle emitted by a re-attach restore (the restored
                        # agent is idle before the user's turn runs). Ignore it and
                        # keep streaming — the real turn starts with "active" next.
                        log.debug("Ignoring pre-turn status=%s (turn not started yet)", status)
                    else:
                        # Main agent finished processing - flush and exit
                        # "done" = agent completed all work
                        # "idle" = agent waiting for next user input
                        log.debug(f"Agent finished with status={status}, emitting remaining segments and completing stream")
                        await self._emit_segments(initial_message, ctx, flush=True, final=True)
                        ctx.content_sent = True
                        break

            elif event_type == EventType.INIT_PROGRESS:
                # Initialization progress - show to user with in-place updates
                init_progress_count += 1
                step = getattr(event, "step", "")
                status = getattr(event, "status", "running")
                
                # Only show progress every 10 events to avoid spam
                if init_progress_count % 10 == 0 or status == "done":
                    # Update the initialization progress in-place
                    if status == "done":
                        # Don't show "Ready!" - just remove all progress messages
                        # The system messages and agent response will follow
                        if "⏳ Initializing..." in ctx.accumulated_text:
                            # Remove all progress messages
                            lines = ctx.accumulated_text.split('\n')
                            filtered_lines = [
                                line for line in lines 
                                if not line.strip().startswith("⏳ Initializing...")
                                and not line.strip() == "✅ Ready!"
                            ]
                            ctx.accumulated_text = '\n'.join(filtered_lines).strip()
                    else:
                        # Show current step
                        progress_text = f"⏳ Initializing... {step}" if step else "⏳ Initializing..."
                        
                        # Find and remove previous progress text
                        if "⏳ Initializing..." in ctx.accumulated_text:
                            # Split by lines and filter out old progress messages
                            lines = ctx.accumulated_text.split('\n')
                            filtered_lines = [
                                line for line in lines 
                                if not line.strip().startswith("⏳ Initializing...")
                            ]
                            # Reconstruct with new progress at the end
                            ctx.accumulated_text = '\n'.join(filtered_lines)
                            if ctx.accumulated_text:
                                ctx.accumulated_text += f"\n{progress_text}"
                            else:
                                ctx.accumulated_text = progress_text
                        else:
                            # First time showing progress
                            if ctx.accumulated_text:
                                ctx.accumulated_text += f"\n\n{progress_text}"
                            else:
                                ctx.accumulated_text = progress_text
                    
                    # Only update if we have content
                    if ctx.accumulated_text.strip():
                        await self._edit_or_send(initial_message, ctx)
                        ctx.last_edit_time = time.monotonic()

            elif event_type == EventType.SYSTEM_MESSAGE:
                # System message - add to output
                msg = getattr(event, "message", "")
                style = getattr(event, "style", "info")

                # Swallow session-bootstrap chatter the daemon re-emits on EVERY
                # session create/restore ("Session created", "Connected to ...",
                # "Attached to session", the API-key notice, "Loading plugins").
                # It's internal noise that makes a *resumed* conversation look
                # brand-new; the handler shows its own "Resumed" cue instead.
                # Errors/warnings (non-info styles) always render.
                if msg and style in ("info", "success") and any(
                    m in msg for m in (
                        "Session created", "Attached to session", "Connected to ",
                        "API key", "Initializing", "Loading plugins",
                    )
                ):
                    msg = ""

                if msg:
                    # Format based on style
                    if style == "error":
                        formatted = f"❌ **System**: {msg}"
                    elif style == "warning":
                        formatted = f"⚠️ **System**: {msg}"
                    elif style == "success":
                        formatted = f"✅ **System**: {msg}"
                    else:
                        formatted = f"ℹ️ **System**: {msg}"
                    
                    if ctx.accumulated_text:
                        ctx.accumulated_text += f"\n\n{formatted}"
                    else:
                        ctx.accumulated_text = formatted
                    
                    await self._edit_or_send(initial_message, ctx)
                    ctx.last_edit_time = time.monotonic()

            elif event_type == EventType.ERROR:
                # Error event - extract error details
                error_msg = getattr(event, "error", "Unknown error")
                error_type = getattr(event, "error_type", "")
                
                log.error(f"Error from jaato: {error_type}: {error_msg}")
                
                # Add error to accumulated text
                if ctx.accumulated_text:
                    ctx.accumulated_text += f"\n\n❌ **Error**: {error_msg}"
                    if error_type:
                        ctx.accumulated_text += f"\n\nType: `{error_type}`"
                else:
                    ctx.accumulated_text = f"❌ **Error**: {error_msg}"
                    if error_type:
                        ctx.accumulated_text += f"\n\nType: `{error_type}`"
                
                # Stop streaming on error
                break

            elif event_type == EventType.PERMISSION_INPUT_MODE:
                # Permission input mode - flush text first, then show permission UI
                if self._permission_handler:
                    log.debug(f"Permission input mode: request_id={getattr(event, 'request_id', 'unknown')}")
                    
                    # Emit the model's lead-in narration (fully) BEFORE the permission UI.
                    await self._emit_segments(initial_message, ctx, flush=True, final=True)

                    # Mark that permission was sent - stop editing streaming message
                    ctx.permission_sent = True
                    
                    # Show the permission UI as a separate message with choices
                    text, keyboard = self._permission_handler.create_permission_ui(
                        event,
                        initial_message.chat.id,
                    )
                    
                    # Send permission request message with typing indicator
                    await initial_message.bot.send_chat_action(chat_id=initial_message.chat.id, action="typing")
                    await asyncio.sleep(0.1)
                    perm_message = await initial_message.answer(text, reply_markup=keyboard)
                    
                    # Store pending permission
                    self._permission_handler.store_pending(
                        request_id=event.request_id,
                        event=event,
                        chat_id=initial_message.chat.id,
                        message_id=perm_message.message_id,
                    )
                    
                    # Don't break streaming - server is blocked but events continues
                    log.debug(f"Permission UI shown, continuing to stream events")

            elif event_type == EventType.CLARIFICATION_BATCH:
                # Clarification batch - surface the agent's questions. WS clients
                # receive every question at once; we ask them one at a time and the
                # answer path (button callback / text reply) sends the batch
                # response once all are answered. Server blocks until then.
                if self._clarification_handler:
                    log.debug(f"Clarification batch: request_id={getattr(event, 'request_id', 'unknown')}")

                    # Emit the model's lead-in narration before showing questions.
                    await self._emit_segments(initial_message, ctx, flush=True, final=True)
                    ctx.permission_sent = True  # stop editing the streaming message

                    chat_id = initial_message.chat.id
                    pending = self._clarification_handler.store_pending(event, chat_id)
                    question = self._clarification_handler.current_question(chat_id)
                    if question is not None:
                        text, keyboard = self._clarification_handler.build_question_ui(
                            pending, question, include_context=True,
                        )
                        await initial_message.bot.send_chat_action(
                            chat_id=chat_id, action="typing")
                        await asyncio.sleep(0.1)
                        if keyboard is not None:
                            await self._safe_answer(initial_message, text, reply_markup=keyboard)
                        else:
                            await self._safe_answer(initial_message, text)
                    # Don't break - server is blocked on channel input until answered
                    log.debug("Clarification UI shown, continuing to stream events")

            # NOTE (drift): there is no EventType.FILE_GENERATED and the current
            # server never emits "file.generated" — this branch is dead. File
            # delivery now flows through host tools (e.g. send_document) /
            # WORKSPACE_FILES events. Kept (string-matched, not typed) so the
            # FileHandler wiring survives until it's re-pointed at the real event.
            elif event_type == "file.generated":
                # File generated event - send file to user
                if self._file_handler:
                    log.debug(f"File generated: {getattr(event, 'path', 'unknown')}")
                    
                    # Emit any model output before the file.
                    await self._emit_segments(initial_message, ctx, flush=True, final=True)

                    # Handle file event
                    # Convert event to dict for FileHandler
                    event_dict = {
                        'path': getattr(event, 'path', None),
                        'content_type': getattr(event, 'content_type', None),
                        'size': getattr(event, 'size', None),
                    }
                    success = await self._file_handler.handle_file_event(
                        event_dict,
                        initial_message
                    )
                    
                    if success:
                        log.info(f"File sent successfully")
                    else:
                        log.warning(f"Failed to send file")
                else:
                    log.warning("File generated event received but no file_handler configured")

        # Final flush: emit any remaining segments (incl. the in-progress tail) if
        # no terminal event already did. Covers stream end via StopAsyncIteration.
        if not ctx.content_sent:
            await self._emit_segments(initial_message, ctx, flush=True, final=True)

        # Empty-turn fallback: if the whole turn rendered NO visible content (e.g.
        # interrupted before any output), the empty-send guards spare us a Telegram
        # "message text is empty" error — but would leave the user with silence.
        # Surface a calm notice instead.
        if not ctx.produced_output and not ctx.stalled:
            await self._safe_answer(
                initial_message,
                "⚠️ I didn't get a response — please try again, or /reset if it persists.",
                parse_mode=None,
            )

        return ctx

    async def _send_with_typing_indicator(
        self,
        initial_message: Message,
        text: str,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
    ) -> Message:
        """
        Send a message with a typing indicator action right before.

        This ensures the "typing..." animation shows before each message,
        making it clear the bot is actively working.

        Args:
            initial_message: The user's message (for context)
            text: Text to send
            parse_mode: Optional parse mode (HTML, etc.)

        Returns:
            The sent message
        """
        # Send typing action immediately before the message (in the same thread)
        await initial_message.bot.send_chat_action(
            chat_id=initial_message.chat.id,
            action="typing",
            message_thread_id=message_thread_id,
        )

        # Small delay to ensure the typing indicator is seen
        await asyncio.sleep(0.1)

        # Send the actual message — route BOTH paths through _safe_answer so the
        # current-thread override (open_thread) applies whether or not it's HTML.
        return await self._safe_answer(
            initial_message, text, parse_mode=parse_mode or None,
            message_thread_id=message_thread_id,
        )

    @staticmethod
    def _is_html_parse_error(exc: TelegramBadRequest) -> bool:
        """True for Telegram 'can't parse entities' / bad-tag errors — i.e. the
        text isn't valid HTML (e.g. unescaped '<' / '<=' in agent code output).
        These must fall back to plain text; other BadRequests propagate."""
        s = str(exc).lower()
        return "parse entities" in s or "can't parse" in s or "unsupported start tag" in s or "tag" in s

    async def _safe_answer(
        self, target: Message, text: str, parse_mode: str | None = "HTML",
        message_thread_id: int | None = None, **kwargs,
    ):
        """answer() that falls back to plain text if Telegram rejects the HTML.

        NOTE: the bot is configured with default parse_mode=HTML, so the
        plain-text paths MUST pass parse_mode=None explicitly — otherwise the
        "fallback" re-sends as HTML, hits the same parse error, and (unwrapped)
        raises out to the caller.

        ``message_thread_id``: the thread the bot is currently sending into. When
        it differs from the thread the incoming message is in (i.e. open_thread
        branched), we must send EXPLICITLY via bot.send_message — Message.answer()
        derives the thread from is_topic_message and can't be overridden. When it
        matches (the common case), answer() already follows it, so we keep using
        it (no behaviour change, no test churn).
        """
        if not (text and text.strip()):
            return None  # Telegram rejects empty text ("message text is empty")
        inbound = target.message_thread_id if getattr(target, "is_topic_message", False) else None
        override = message_thread_id is not None and message_thread_id != inbound

        async def _send(pm: str | None):
            if override:
                return await target.bot.send_message(
                    chat_id=target.chat.id, text=text,
                    message_thread_id=message_thread_id, parse_mode=pm, **kwargs,
                )
            return await target.answer(text, parse_mode=pm, **kwargs)

        if not parse_mode:
            m = await _send(None)
        else:
            try:
                m = await _send(parse_mode)
            except TelegramBadRequest as e:
                if self._is_html_parse_error(e):
                    m = await _send(None)  # real plain text
                else:
                    raise
        log.info(
            "RENDER send msg id=%s len=%d head=%r",
            getattr(m, "message_id", None), len(text), text[:50],
        )
        return m

    async def _safe_edit(self, msg: Message, text: str) -> None:
        """edit_text() that falls back to plain text on HTML parse errors and
        silently ignores 'message is not modified'."""
        if not (text and text.strip()):
            return  # never edit to empty text ("message text is empty")
        try:
            await msg.edit_text(text, parse_mode="HTML")
        except TelegramBadRequest as e:
            if self._is_html_parse_error(e):
                try:
                    await msg.edit_text(text, parse_mode=None)  # real plain text
                except TelegramBadRequest:
                    pass
            # else: 'message is not modified' / other — ignore

    async def _edit_or_send(
        self,
        initial_message: Message,
        ctx: StreamingContext,
    ) -> None:
        """Edit existing message or send new one if needed.

        Once a permission UI has been sent as a separate message, we stop editing
        the streaming message to avoid pushing the permission out of position.
        Instead, new content is sent as separate messages that appear after.
        """
        display_text = ctx.accumulated_text[: self._max_message_length]
        if display_text.strip():
            ctx.produced_output = True

        # Guard against truly empty messages - Telegram rejects them
        # Check both accumulated_text AND text_buffer
        # This allows displaying content even when accumulated_text is empty but text_buffer has content
        if not display_text and not ctx.text_buffer:
            return

        # Check if text contains Telegram HTML formatting
        has_html = _has_telegram_html(display_text)

        # If permission was sent, don't edit the streaming message anymore
        # Send new content as separate messages instead
        if ctx.permission_sent and ctx.sent_message is not None:
            # Only send if there's actual content
            if display_text:
                await self._safe_answer(
                    initial_message, display_text,
                    parse_mode="HTML" if has_html else None,
                )
            # Clear accumulated text after sending to prevent duplication
            ctx.accumulated_text = ""
            return

        # Normal editing behavior (no permission sent yet)
        if ctx.sent_message is None:
            # First time - send new message
            ctx.sent_message = await self._safe_answer(
                initial_message, display_text,
                parse_mode="HTML" if has_html else None,
            )
        else:
            # Edit in place
            if has_html:
                await self._safe_edit(ctx.sent_message, display_text)
                ctx.edits_count += 1
            else:
                try:
                    await ctx.sent_message.edit_text(display_text, parse_mode=None)
                    ctx.edits_count += 1
                except TelegramBadRequest:
                    # Text unchanged or other Telegram error - ignore
                    pass

    async def send_final_response(
        self,
        initial_message: Message,
        streaming_context: StreamingContext,
    ) -> None:
        """
        Send the final response, handling long messages properly.

        If accumulated text fits in one message, updates the streaming message.
        If too long, deletes the streaming message and sends split chunks.

        Args:
            initial_message: The user's message (for context)
            streaming_context: Context from stream_response
        """
        if not streaming_context.accumulated_text:
            return

        text = streaming_context.accumulated_text
        streaming_context.produced_output = True
        # Dedup: multi-turn agentic flows fire TURN_COMPLETED more than once (e.g.
        # after a host-tool call like show_image). Without this guard the SAME
        # accumulated text is sent again on the second TURN_COMPLETED — a visible
        # duplicate message. Only send when the final text actually changed.
        if text == streaming_context.last_final_text:
            return
        streaming_context.last_final_text = text

        # Check if text contains Telegram HTML formatting
        has_html = _has_telegram_html(text)

        # If fits in one message, just update
        if len(text) <= self._max_message_length:
            if streaming_context.sent_message:
                if has_html:
                    await self._safe_edit(streaming_context.sent_message, text)
                    return
                try:
                    await streaming_context.sent_message.edit_text(text, parse_mode=None)
                    return
                except TelegramBadRequest:
                    pass
            await self._safe_answer(
                initial_message, text, parse_mode="HTML" if has_html else None,
            )
            return

        # Too long - split into multiple messages
        # Delete the streaming message first if it exists
        if streaming_context.sent_message:
            try:
                log.info(
                    "RENDER delete streaming id=%s (send_final_response too-long split)",
                    streaming_context.sent_message.message_id,
                )
                await streaming_context.sent_message.delete()
            except TelegramBadRequest:
                pass

        # Send chunks
        chunks = split_preserving_paragraphs(text, self._max_message_length)
        for chunk in chunks:
            # Check if chunk has HTML
            await self._safe_answer(
                initial_message, chunk,
                parse_mode="HTML" if "<blockquote>" in chunk else None,
            )

    async def send_simple_response(
        self,
        message: Message,
        text: str,
    ) -> None:
        """
        Send a simple response without streaming, handling long messages.

        Args:
            message: The message to respond to
            text: Response text
        """
        if len(text) <= self._max_message_length:
            await self._safe_answer(message, text)
            return

        # Split long messages
        chunks = split_preserving_paragraphs(text, self._max_message_length)
        for chunk in chunks:
            await self._safe_answer(message, chunk)

    def split_text(self, text: str, max_len: int | None = None) -> list[str]:
        """
        Split text into chunks respecting Telegram limits.

        Args:
            text: Text to split
            max_len: Maximum chunk length (defaults to renderer's max)

        Returns:
            List of text chunks
        """
        max_length = max_len or self._max_message_length
        return split_preserving_paragraphs(text, max_length)

    def _format_tool_params(self, params: dict, max_width: int = 40) -> list[str]:
        """
        Format tool parameters for display, one per line, ellipsized.

        Args:
            params: Dictionary of parameter names to values
            max_width: Maximum line width before ellipsis (default 40 chars)

        Returns:
            List of formatted parameter lines
        """
        if not params:
            return []

        lines = []
        for key, value in params.items():
            value_str = str(value)
            # Calculate available space: "  • key: " prefix
            prefix_len = 4 + len(key) + 2  # "  • " + key + ": "
            available = max_width - prefix_len

            if available < 10:
                # Very long key - just show key with ellipsis
                lines.append(f"  • <code>{key}</code>: …")
            elif len(value_str) > available:
                # Ellipsize the value first, then escape
                ellipsis_len = 1  # "…" character
                truncated = value_str[:available - ellipsis_len - 2] + "…"
                # Escape HTML after truncation
                escaped_truncated = escape_html_content(truncated)
                lines.append(f"  • <code>{key}</code>: {escaped_truncated}")
            else:
                # Escape HTML in value to prevent parsing errors
                escaped_value = escape_html_content(value_str)
                lines.append(f"  • <code>{key}</code>: {escaped_value}")

        return lines

    def _format_tool_block(self, tool_name: str, tool_args: dict | None, output: str | None = None) -> str:
        """
        Format a tool block with name, parameters, and optional output.

        Args:
            tool_name: Name of the tool
            tool_args: Tool parameters (can be None)
            output: Tool output content (can be None for pending state)

        Returns:
            Formatted HTML string for Telegram
        """
        lines = [f"🔧 <code>{tool_name}</code>"]

        if tool_args:
            param_lines = self._format_tool_params(tool_args)
            lines.extend(param_lines)

        if output:
            lines.append("")  # Blank line before output
            # Check if output is wide content
            if self._is_wide_content(output):
                lines.append(self._format_expandable_blockquote(output))
            else:
                # Escape HTML in output to prevent parsing errors
                lines.append(escape_html_content(output))

        return "\n".join(lines)

    def _is_wide_content(self, text: str) -> bool:
        """
        Check if text contains wide content that would overflow chat width.

        Wide content indicators:
        - Very long lines (>100 chars without newlines)
        - JSON objects (contains { and })
        - Code blocks (contains ``` or indented blocks)
        - Tables (contains | separators)
        - Long URLs (>80 chars)

        Args:
            text: Text to check

        Returns:
            True if content is wide and should be in expandable blockquote
        """
        if not text:
            return False

        lines = text.split("\n")

        # Check for very long lines (indicates JSON, code, tables)
        for line in lines:
            if len(line) > 100:
                return True

        # Check for JSON indicators
        if "{" in text and "}" in text:
            return True

        # Check for code block markers
        if "```" in text or "`" in text:
            return True

        # Check for table separators
        if "|" in text and len([l for l in lines if "|" in l]) > 2:
            return True

        # Check for very long URLs
        import re
        if re.search(r'https?://[^\s]{80,}', text):
            return True
    def _format_expandable_blockquote(self, content: str) -> str:
        """
        Format content as an expandable blockquote.

        Uses HTML blockquote syntax with || markers to create
        collapsed-by-default content that expands on tap.

        Args:
            content: The content to wrap

        Returns:
            Formatted string with expandable blockquote syntax
        """
        # Clean the content - remove trailing whitespace and tabs
        lines = [line.rstrip().replace("\t", "  ") for line in content.split("\n")]
        cleaned = "\n".join(lines)
        
        # Escape HTML to prevent parsing issues with content like <dependency>
        escaped = escape_html_content(cleaned)

        # Format as expandable blockquote
        # The || markers create the expandable section
        return f"<blockquote>||{escaped}||</blockquote>"

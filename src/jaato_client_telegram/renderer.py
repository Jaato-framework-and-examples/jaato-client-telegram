"""
Response renderer for jaato events.

Handles progressive rendering of streamed events to Telegram messages,
including long message splitting and edit-in-place updates.
"""

import asyncio
import html
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

if TYPE_CHECKING:
    from jaato_client_telegram.file_handler import FileHandler
    from jaato_client_telegram.permissions import PermissionHandler
    from jaato_client_telegram.session_pool import SessionPool


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


@dataclass
class StreamingContext:
    """State for edit-in-place streaming of responses."""

    sent_message: Message | None = None
    accumulated_text: str = ""
    last_edit_time: float = 0.0
    edits_count: int = 0
    seen_model_output: bool = False  # Track if we've received any model output yet
    permission_sent: bool = False  # Track if permission UI was sent as separate message

    # Buffer for text chunks in arrival order
    text_buffer: list[str] = field(default_factory=list)


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

    ):
        """
        Initialize the renderer.

        Args:
            max_message_length: Telegram message length limit (default 4096)
            edit_throttle_ms: Minimum time between edit_message_text calls (currently unused - reserved for future progressive streaming)
            permission_handler: Optional handler for permission requests
            file_handler: Optional handler for file sending (ATTACH/SHARE)
        """
        self._max_message_length = max_message_length
        self._edit_throttle_seconds = edit_throttle_ms / 1000.0
        self._permission_handler = permission_handler
        self._file_handler = file_handler

    def _flush_text_buffer(self, ctx: StreamingContext) -> None:
        """
        Flush accumulated text chunks to accumulated_text.
        
        Should be called before displaying tool calls or at turn completion.
        """
        if ctx.text_buffer:
            combined = "".join(ctx.text_buffer)
            if combined.strip():  # Only add if non-whitespace
                # Add blank line before first model output
                if not ctx.seen_model_output and ctx.accumulated_text:
                    if not ctx.accumulated_text.endswith("\n\n"):
                        ctx.accumulated_text += "\n\n"
                    ctx.seen_model_output = True
                ctx.accumulated_text += combined
            ctx.text_buffer.clear()

    def _flush_all_buffers(self, ctx: StreamingContext) -> None:
        """Flush all pending buffers."""
        self._flush_text_buffer(ctx)

    async def stream_response(
        self,
        initial_message: Message,
        event_stream,  # AsyncIterator[Event] from SDK
    ) -> StreamingContext:
        """
        Stream events progressively, editing the message in place.

        Accumulates AGENT_OUTPUT events and updates the message
        at throttled intervals to respect Telegram rate limits.

        Args:
            initial_message: The user's message (for context)
            event_stream: Async iterator of events from SDK

        Returns:
            StreamingContext with final accumulated text
        """
        import logging
        log = logging.getLogger(__name__)

        ctx = StreamingContext()
        ctx.last_edit_time = time.monotonic()

        init_progress_count = 0
        
        async for event in event_stream:
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
            if event_type == "agent.output" or event_type == "AGENT_OUTPUT":
                # Agent output - check source and mode
                source = getattr(event, "source", None)
                mode = getattr(event, "mode", None)
                content = getattr(event, "text", "")

                # DEBUG: Log source, mode, and content
                log.debug(f"AGENT_OUTPUT received: source={source}, mode={mode}, text={repr(content[:200])}")

                # Handle flush signal FIRST - regardless of source
                # The SDK emits flush as source="system" with mode="flush" and empty text
                if mode == "flush":
                    log.debug("Flush signal received (source=%s) - flushing text buffer and sending as new message", source)
                    # Flush all accumulated text to display before tools start
                    self._flush_text_buffer(ctx)
                    
                    # Send as a NEW message instead of editing
                    # This ensures each flush creates a separate message in the chat
                    display_text = ctx.accumulated_text[: self._max_message_length]
                    if display_text:  # Only send if there's content
                        has_html = "<blockquote>" in display_text
                        # Use the new helper that sends typing action before the message
                        if has_html:
                            sent_msg = await self._send_with_typing_indicator(initial_message, display_text, parse_mode="HTML")
                        else:
                            sent_msg = await self._send_with_typing_indicator(initial_message, display_text)
                        
                        # Mark that we've sent a message during this turn
                        # This prevents the final _edit_or_send from duplicating it
                        ctx.sent_message = sent_msg
                    
                    # Always clear accumulated text after flush, even if nothing was sent
                    # This prevents old content from appearing in subsequent messages
                    ctx.accumulated_text = ""
                    
                    # Don't use _edit_or_send - we want new messages, not edits
                # Buffer model output for later display
                elif source == "model" and mode in ("write", "append"):
                    # Buffer text chunks for later display
                    # Escape HTML to prevent parsing errors with content like <dependency>
                    ctx.text_buffer.append(escape_html_content(content))

                elif source == "tool":
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

            elif event_type == "agent.completed" or event_type == "AGENT_COMPLETED":
                # Agent completed - flush all buffers before finishing
                self._flush_all_buffers(ctx)
                await self._edit_or_send(initial_message, ctx)
                break

            elif event_type == "turn.completed" or event_type == "TURN_COMPLETED":
                # Turn completed - flush all buffers first
                self._flush_all_buffers(ctx)


                # Check for formatted text - this replaces the streaming text
                formatted_text = getattr(event, "formatted_text", None)
                if formatted_text:
                    # formatted_text contains the full conversation including user input
                    # We prefer to use only the accumulated streaming text (agent response only)
                    # Delete the streaming message and send final response with accumulated text
                    if ctx.sent_message:
                        try:
                            await ctx.sent_message.delete()
                            ctx.sent_message = None
                        except Exception:
                            pass
                    # Send final response using accumulated text (agent response only, no user message)
                    await self.send_final_response(initial_message, ctx)
                else:
                    # No formatted_text provided, ensure we have the final response displayed
                    await self._edit_or_send(initial_message, ctx)

                # NOTE: Do NOT break on turn.completed!
                # Multi-turn agentic flows have multiple turn.completed events
                # before the final agent.completed. Breaking here would truncate
                # the response after the first turn.
                log.debug("Turn completed, continuing to stream events...")

            elif event_type == "agent.status_changed" or event_type == "AGENT_STATUS_CHANGED":
                # Agent status changed - check for completion signals
                status = getattr(event, "status", "")
                log.debug(f"Agent status changed: {status}")

                if status in ("done", "idle"):
                    # Main agent finished processing - flush and exit
                    # "done" = agent completed all work
                    # "idle" = agent waiting for next user input
                    # Both mean the current response is complete
                    log.debug(f"Agent finished with status={status}, flushing buffers and completing stream")
                    self._flush_all_buffers(ctx)
                    await self._edit_or_send(initial_message, ctx)
                    break
                # Ignore "active" status - that's the start signal

            elif event_type == "init.progress" or event_type == "INIT_PROGRESS":
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

            elif event_type == "system.message" or event_type == "SYSTEM_MESSAGE":
                # System message - add to output
                msg = getattr(event, "message", "")
                style = getattr(event, "style", "info")
                
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

            elif event_type == "error" or event_type == "ERROR":
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

            elif event_type == "permission.input_mode" or event_type == "PERMISSION_INPUT_MODE":
                # Permission input mode - flush text first, then show permission UI
                if self._permission_handler:
                    log.debug(f"Permission input mode: request_id={getattr(event, 'request_id', 'unknown')}")
                    
                    # Flush text buffer first to show what the model said BEFORE the permission
                    self._flush_text_buffer(ctx)
                    
                    # Update the message to show the flushed text (without any permission placeholder)
                    await self._edit_or_send(initial_message, ctx)
                    
                    # Clear accumulated text after sending - the model's text has been displayed
                    # and we don't want to repeat it after the permission is approved
                    ctx.accumulated_text = ""
                    
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

            elif event_type == "file.generated" or event_type == "FILE_GENERATED":
                # File generated event - send file to user
                if self._file_handler:
                    log.debug(f"File generated: {getattr(event, 'path', 'unknown')}")
                    
                    # Flush text buffer first to show any model output before the file
                    self._flush_text_buffer(ctx)
                    
                    # Update the message to show the flushed text
                    await self._edit_or_send(initial_message, ctx)
                    
                    # Clear accumulated text after sending
                    ctx.accumulated_text = ""
                    
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

        # Final edit with complete response
        await self._edit_or_send(initial_message, ctx)

        return ctx

    async def _send_with_typing_indicator(
        self,
        initial_message: Message,
        text: str,
        parse_mode: str | None = None,
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
        # Send typing action immediately before the message
        await initial_message.bot.send_chat_action(
            chat_id=initial_message.chat.id,
            action="typing"
        )

        # Small delay to ensure the typing indicator is seen
        await asyncio.sleep(0.1)

        # Send the actual message
        if parse_mode:
            return await initial_message.answer(text, parse_mode=parse_mode)
        else:
            return await initial_message.answer(text)

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

        # Guard against truly empty messages - Telegram rejects them
        # Check both accumulated_text AND text_buffer
        # This allows displaying content even when accumulated_text is empty but text_buffer has content
        if not display_text and not ctx.text_buffer:
            return

        # Check if text contains HTML formatting (expandable blockquotes)
        has_html = "<blockquote>" in display_text

        # If permission was sent, don't edit the streaming message anymore
        # Send new content as separate messages instead
        if ctx.permission_sent and ctx.sent_message is not None:
            # Only send if there's actual content
            if display_text:
                if has_html:
                    await initial_message.answer(display_text, parse_mode="HTML")
                else:
                    await initial_message.answer(display_text)
            # Clear accumulated text after sending to prevent duplication
            ctx.accumulated_text = ""
            return

        # Normal editing behavior (no permission sent yet)
        if ctx.sent_message is None:
            # First time - send new message
            if has_html:
                ctx.sent_message = await initial_message.answer(display_text, parse_mode="HTML")
            else:
                ctx.sent_message = await initial_message.answer(display_text)
        else:
            # Edit in place
            try:
                if has_html:
                    await ctx.sent_message.edit_text(display_text, parse_mode="HTML")
                else:
                    await ctx.sent_message.edit_text(display_text)
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

        # Check if text contains HTML formatting (expandable blockquotes)
        has_html = "<blockquote>" in text

        # If fits in one message, just update
        if len(text) <= self._max_message_length:
            if streaming_context.sent_message:
                try:
                    if has_html:
                        await streaming_context.sent_message.edit_text(text, parse_mode="HTML")
                    else:
                        await streaming_context.sent_message.edit_text(text)
                    return
                except TelegramBadRequest:
                    pass
            if has_html:
                await initial_message.answer(text, parse_mode="HTML")
            else:
                await initial_message.answer(text)
            return

        # Too long - split into multiple messages
        # Delete the streaming message first if it exists
        if streaming_context.sent_message:
            try:
                await streaming_context.sent_message.delete()
            except TelegramBadRequest:
                pass

        # Send chunks
        chunks = split_preserving_paragraphs(text, self._max_message_length)
        for chunk in chunks:
            # Check if chunk has HTML
            if "<blockquote>" in chunk:
                await initial_message.answer(chunk, parse_mode="HTML")
            else:
                await initial_message.answer(chunk)

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
            await message.answer(text)
            return

        # Split long messages
        chunks = split_preserving_paragraphs(text, self._max_message_length)
        for chunk in chunks:
            await message.answer(chunk)

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

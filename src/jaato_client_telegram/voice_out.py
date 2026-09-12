"""Voice OUT — turn model-generated speech into a Telegram voice note.

When the profile's audio tier (``voz`` = ``openai/gpt-audio``, ``modalities:
{audio: outbound}``) speaks, the server streams the audio to the client as
model-media on ``EventType.TOOL_OUTPUT`` events (``call_id == "model-output"``):
headerless **PCM16 @ 24 kHz mono** (``audio/pcm;rate=24000;channels=1;encoding=
s16le``), one base64 chunk per event, ``final`` on the last. Telegram voice notes
must be **OGG/Opus**, so this module accumulates the PCM and transcodes it with
``ffmpeg``. The renderer consumes the events and calls these helpers; keeping the
audio plumbing here keeps the renderer's event loop readable.

``ffmpeg`` is a system dependency (installed by deploy-vps.sh). If it is missing
or the transcode fails, ``pcm_to_ogg_opus`` returns ``None`` and the caller falls
back to the text reply — a voiced turn degrades to text, never crashes.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# The shape OpenAI streams model audio in (jaato-server _media_deltas.py):
# headerless signed 16-bit little-endian PCM, 24 kHz, mono. Used as the fallback
# when a chunk's mime does not spell the parameters out.
_DEFAULT_RATE = 24000
_DEFAULT_CHANNELS = 1


def parse_pcm_mime(mime: str | None) -> tuple[int, int]:
    """Read ``(rate, channels)`` out of a ``audio/pcm;rate=…;channels=…`` mime.

    Falls back to 24 kHz mono (OpenAI's streaming shape) for any part that is
    absent or unparseable — the transcode still runs rather than guessing wrong
    and failing."""
    rate, channels = _DEFAULT_RATE, _DEFAULT_CHANNELS
    for part in (mime or "").split(";"):
        part = part.strip()
        try:
            if part.startswith("rate="):
                rate = int(part[5:]) or _DEFAULT_RATE
            elif part.startswith("channels="):
                channels = int(part[9:]) or _DEFAULT_CHANNELS
        except ValueError:
            continue
    return rate, channels


async def pcm_to_ogg_opus(
    pcm: bytes, rate: int = _DEFAULT_RATE, channels: int = _DEFAULT_CHANNELS,
) -> bytes | None:
    """Transcode raw PCM16 → OGG/Opus (Telegram voice-note format) via ffmpeg.

    Returns the OGG bytes, or ``None`` if ffmpeg is missing/fails or produced
    nothing — the caller then keeps the text reply only. Opus at 24 kbps is
    plenty for speech and keeps the note small."""
    if not pcm:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(rate), "-ac", str(channels), "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning("voice_out: ffmpeg not found — cannot send a voice note (falling back to text)")
        return None
    try:
        out, err = await proc.communicate(pcm)
    except Exception:  # noqa: BLE001 — transcode boundary
        log.exception("voice_out: ffmpeg transcode failed")
        return None
    if proc.returncode != 0 or not out:
        log.warning(
            "voice_out: ffmpeg returned %s, %d bytes out (%s)",
            proc.returncode, len(out or b""), (err or b"")[:200].decode("utf-8", "ignore"),
        )
        return None
    return out

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


async def audio_to_mp3(data: bytes) -> bytes | None:
    """Transcode an inbound audio note (Telegram voice = OGG/Opus) → MP3 via ffmpeg.

    An audio-INPUT tier backed by OpenAI ``gpt-audio`` rejects ``input_audio.format:
    'ogg'`` (it accepts only ``wav``/``mp3``), and Telegram voice notes are always
    OGG. We transcode to **MP3, not WAV, deliberately**: WAV is uncompressed PCM
    (~32 KB/s at 16 kHz mono → a 41 s note is ~1.3 MB), and the server echoes the
    user turn back to the client as a ``source="user"`` event — so a large audio
    turn produces a >1 MiB frame that blows the client's default WebSocket
    ``max_size`` (1 MiB) and drops the connection mid-turn. MP3 at 64 kbps mono is
    ~10× smaller (a 41 s note is ~0.3 MB), keeping the echo well under the cap while
    staying plenty for speech.

    STILL LOAD-BEARING — do not drop this transcode because the server limit was
    raised. The daemon gained ``--ws-max-message-size`` (this deployment runs
    16 MiB), but the ceiling that actually breaks is the CLIENT's: the SDK passes
    no ``max_size`` to ``websockets.connect``, so receives are still capped at the
    1 MiB default and a larger echo closes the socket with 1009 exactly as before.
    The two ends cannot currently be set consistently — tracked as jaato#1279.

    Down-mix to mono 16 kHz. Returns MP3 bytes, or
    ``None`` if ffmpeg is missing or fails (the caller then sends the original
    bytes, which 400s — no worse than doing nothing)."""
    if not data:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k",
            "-f", "mp3", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning("voice_in: ffmpeg not found — cannot transcode the voice note to MP3")
        return None
    try:
        out, err = await proc.communicate(data)
    except Exception:  # noqa: BLE001 — transcode boundary
        log.exception("voice_in: ffmpeg transcode failed")
        return None
    if proc.returncode != 0 or not out:
        log.warning(
            "voice_in: ffmpeg returned %s, %d bytes out (%s)",
            proc.returncode, len(out or b""), (err or b"")[:200].decode("utf-8", "ignore"),
        )
        return None
    return out

"""WebSocket transport for jaato-server.

Manages a single WebSocket connection to JaatoWSServer,
multiplexing events for multiple sessions over one connection.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import websockets
import websockets.exceptions
from jaato_sdk.events import serialize_event, deserialize_event

if TYPE_CHECKING:
    from jaato_client_telegram.config import TLSConfig

logger = logging.getLogger(__name__)


class WSTransport:
    """WebSocket transport that multiplexes events for multiple sessions."""

    def __init__(self, url: str, tls_config: TLSConfig | None = None,
                 secret_token: str | None = None) -> None:
        self._url = url
        self._tls_config = tls_config
        self._secret_token = secret_token
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._receiver_task: asyncio.Task | None = None
        self._session_queues: dict[str, asyncio.Queue] = {}
        self._connected = False
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        if not self._tls_config or not self._tls_config.enabled:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if self._tls_config.ca_cert_path:
            ctx.load_verify_locations(self._tls_config.ca_cert_path)
        if self._tls_config.cert_path and self._tls_config.key_path:
            ctx.load_cert_chain(self._tls_config.cert_path, self._tls_config.key_path)
        return ctx

    async def connect(self) -> None:
        """Connect to the WebSocket server and start the receiver loop."""
        async with self._connect_lock:
            if self._connected:
                return
            ssl_context = self._build_ssl_context()
            extra_headers = {}
            if self._secret_token:
                extra_headers["Authorization"] = f"Bearer {self._secret_token}"
            self._ws = await websockets.connect(
                self._url,
                ssl=ssl_context,
                additional_headers=extra_headers or None,
            )
            self._connected = True
            self._receiver_task = asyncio.create_task(self._receiver_loop())
            logger.info("Connected to WS server at %s", self._url)

    async def disconnect(self) -> None:
        """Close the WebSocket connection and stop the receiver."""
        self._connected = False
        if self._receiver_task:
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
            self._receiver_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        logger.info("Disconnected from WS server")

    async def send(self, event) -> None:
        """Serialize and send an event over the WebSocket."""
        if not self._ws or not self._connected:
            raise RuntimeError("Not connected to WS server")
        payload = serialize_event(event)
        await self._ws.send(payload)

    def register_session(self, session_id: str) -> asyncio.Queue:
        """Create an event queue for a session. Returns the queue."""
        queue: asyncio.Queue = asyncio.Queue()
        self._session_queues[session_id] = queue
        return queue

    def unregister_session(self, session_id: str) -> None:
        """Remove the event queue for a session."""
        queue = self._session_queues.pop(session_id, None)
        if queue is not None:
            queue.put_nowait(None)  # sentinel to unblock consumers

    async def events(self, session_id: str) -> AsyncIterator:
        """Yield events for a specific session. Ends on sentinel (None)."""
        queue = self._session_queues.get(session_id)
        if not queue:
            return
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event

    async def _receiver_loop(self) -> None:
        """Background task: read WS messages and dispatch to session queues."""
        try:
            async for raw in self._ws:
                try:
                    event = deserialize_event(raw)
                except Exception:
                    logger.warning("Failed to deserialize event: %s", raw[:200])
                    continue
                session_id = getattr(event, "session_id", None)
                if session_id and session_id in self._session_queues:
                    await self._session_queues[session_id].put(event)
        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WS connection closed by server")
        except Exception:
            logger.exception("WS receiver loop error")
        finally:
            self._connected = False

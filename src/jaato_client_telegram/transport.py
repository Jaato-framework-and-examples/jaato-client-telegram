"""WebSocket transport for jaato-server.

Manages a single WebSocket connection to JaatoWSServer,
multiplexing events for multiple sessions over one connection.

Authentication is optional. The server does not require it for local/trusted
deployments. When enabled, the client sends a post-handshake JSON frame::

  {"type": "auth.token", "token": "<Keycloak JWT>"}

The server validates the JWT against Keycloak JWKS and replies with::

  {"type": "auth.token", "user_id": "<username>"}

Auth provides inter-user session isolation (user A cannot attach/delete
user B's sessions) but does not gate individual commands.
For local/VPN deployments behind a firewall, auth can be omitted.
"""

import asyncio
import json
import logging
import ssl
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import websockets
import websockets.exceptions
from jaato_sdk.events import serialize_event, deserialize_event

from jaato_client_telegram.config import TLSConfig

if TYPE_CHECKING:
    from jaato_client_telegram.config import JaatoWSConfig


logger = logging.getLogger(__name__)


class WSTransport:
    """Single WebSocket connection multiplexing events for multiple sessions.

    Auth is optional. If keycloak_client_id is empty, no auth.token frame
    is sent and the server treats the connection as anonymous.
    """

    def __init__(
        self,
        url: str,
        tls_config: TLSConfig | None = None,
        keycloak_base_url: str = "",
        keycloak_realm: str = "jaato",
        keycloak_client_id: str = "",
        keycloak_client_secret: str = "",
    ) -> None:
        self._url = url
        self._tls_config = tls_config
        self._kc_base_url = keycloak_base_url.rstrip("/")
        self._kc_realm = keycloak_realm
        self._kc_client_id = keycloak_client_id
        self._kc_client_secret = keycloak_client_secret
        self._auth_enabled = bool(keycloak_client_id)
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._receiver_task: asyncio.Task | None = None
        self._session_queues: dict[str, asyncio.Queue] = {}
        self._connected = False
        self._user_id: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def user_id(self) -> str | None:
        return self._user_id

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        if not self._tls_config or not self._tls_config.enabled:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if self._tls_config.ca_cert_path:
            ctx.load_verify_locations(self._tls_config.ca_cert_path)
        if self._tls_config.cert_path and self._tls_config.key_path:
            ctx.load_cert_chain(self._tls_config.cert_path, self._tls_config.key_path)
        return ctx

    async def _fetch_token(self) -> str:
        """Obtain an access token from Keycloak via client_credentials grant."""
        import urllib.request
        import urllib.parse

        token_url = (
            f"{self._kc_base_url}/realms/{self._kc_realm}"
            "/protocol/openid-connect/token"
        )
        data = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._kc_client_id,
            "client_secret": self._kc_client_secret,
        }).encode()

        req = urllib.request.Request(token_url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        with urllib.request.urlopen(req, context=self._build_ssl_context(), timeout=10) as resp:
            payload = json.loads(resp.read().decode())
        return payload["access_token"]

    async def _authenticate(self) -> str | None:
        """Send auth.token frame and wait for server reply. Returns user_id."""
        token = await self._fetch_token()
        auth_frame = json.dumps({"type": "auth.token", "token": token})
        await self._ws.send(auth_frame)

        raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        reply = json.loads(raw)

        if reply.get("type") != "auth.token":
            raise RuntimeError(f"Unexpected auth reply: {reply}")
        if "error" in reply:
            raise RuntimeError(f"Auth failed: {reply['error']}")

        user_id = reply["user_id"]
        logger.info("Authenticated as %s", user_id)
        return user_id

    async def connect(self) -> None:
        """Connect to the server, optionally authenticate, start receiver loop."""
        ssl_ctx = self._build_ssl_context()
        self._ws = await websockets.connect(self._url, ssl=ssl_ctx)
        self._connected = True

        if self._auth_enabled:
            self._user_id = await self._authenticate()
            logger.info("Connected to %s (user=%s)", self._url, self._user_id)
        else:
            logger.info("Connected to %s (no auth)", self._url)

        self._receiver_task = asyncio.create_task(self._receiver_loop())

    async def disconnect(self) -> None:
        """Stop the receiver loop and close the connection."""
        self._connected = False
        if self._receiver_task:
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
            self._receiver_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._user_id = None

    def register_session(self, session_id: str) -> asyncio.Queue:
        """Register a session queue for receiving events."""
        if session_id not in self._session_queues:
            self._session_queues[session_id] = asyncio.Queue()
        return self._session_queues[session_id]

    def unregister_session(self, session_id: str) -> None:
        self._session_queues.pop(session_id, None)

    async def events(self, session_id: str) -> AsyncIterator:
        queue = self.register_session(session_id)
        while self._connected:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                continue

    async def send(self, event) -> None:
        if not self._ws or not self._connected:
            raise RuntimeError("Not connected")
        await self._ws.send(serialize_event(event))

    async def _receiver_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    event = deserialize_event(raw)
                    session_id = getattr(event, "session_id", None)
                    if session_id and session_id in self._session_queues:
                        await self._session_queues[session_id].put(event)
                    else:
                        logger.debug("No queue for session_id=%s", session_id)
                except Exception:
                    logger.debug("Failed to deserialize: %s", raw[:100])
        except websockets.exceptions.ConnectionClosed:
            logger.info("Server closed connection")
        except asyncio.CancelledError:
            pass
        finally:
            self._connected = False

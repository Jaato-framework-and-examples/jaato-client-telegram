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

Host-Provided Tools
-------------------
After connecting and creating a session, the client registers tools
via ``tools.register_client``.  When the model calls one, the server
sends ``tool.execute_request`` back over the WS connection.  The
transport dispatches this to the executor registered for that
session (see ``set_session_tool_executor`` / ``set_session_tool_executors``).
"""

import asyncio
import json
import logging
import ssl
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Callable

import websockets
import websockets.exceptions
from jaato_sdk.events import (
    serialize_event,
    deserialize_event,
    CommandRequest,
    ConnectedEvent,
    ErrorEvent,
    SessionInfoEvent,
    StageFilesEvent,
    StageFilesRequest,
    StagedFileSpec,
    ToolExecuteRequestEvent,
    ToolExecuteResultEvent,
    ToolsRegisterClientRequest,
)

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
        secret_token: str | None = None,
    ) -> None:
        self._url = url
        self._tls_config = tls_config
        self._secret_token = secret_token
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
        self._tool_executors: dict[str, Callable] = {}
        self._session_chat_ids: dict[str, int] = {}
        self._tools_registered: bool = False
        self._session_future: asyncio.Future | None = None
        self._stage_files_future: asyncio.Future | None = None

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
        else:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if self._tls_config.cert_path and self._tls_config.key_path:
            ctx.load_cert_chain(self._tls_config.cert_path, self._tls_config.key_path)
        return ctx

    async def _fetch_token(self) -> str:
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

    async def _send_auth_token(self) -> str:
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
        ssl_ctx = self._build_ssl_context()
        headers = {}
        if self._secret_token:
            headers["Authorization"] = f"Bearer {self._secret_token}"
        self._ws = await websockets.connect(self._url, ssl=ssl_ctx, additional_headers=headers or None)
        self._connected = True

        raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        event = deserialize_event(raw)
        if not isinstance(event, ConnectedEvent):
            raise RuntimeError(f"Expected ConnectedEvent, got {type(event).__name__}")
        logger.info("Connected to %s (protocol=%s)", self._url, event.protocol_version)

        if self._auth_enabled:
            self._user_id = await self._send_auth_token()

        self._receiver_task = asyncio.create_task(self._receiver_loop())

    async def disconnect(self) -> None:
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
        self._tools_registered = False

    def register_session(self, session_id: str) -> asyncio.Queue:
        if session_id not in self._session_queues:
            self._session_queues[session_id] = asyncio.Queue()
        return self._session_queues[session_id]

    def unregister_session(self, session_id: str) -> None:
        self._session_queues.pop(session_id, None)
        self._session_chat_ids.pop(session_id, None)

    def set_session_tool_executors(self, session_id: str, executors: dict[str, Callable], chat_id: int = 0) -> None:
        self._tool_executors.update(executors)
        if chat_id:
            self._session_chat_ids[session_id] = chat_id

    async def register_host_tools(self, tool_schemas: list[dict], categories: dict[str, str] | None = None) -> None:
        if self._tools_registered:
            return
        event = ToolsRegisterClientRequest(tools=tool_schemas, categories=categories or {})
        await self.send(event)
        self._tools_registered = True
        logger.info("Registered %d host-provided tools", len(tool_schemas))

    async def _handle_tool_execute_request(self, event: ToolExecuteRequestEvent) -> None:
        tool_name = event.tool_name
        tool_args = event.tool_args
        call_id = event.call_id

        executor = self._tool_executors.get(tool_name)

        if executor is None:
            result = json.dumps({"error": f"Unknown client tool: {tool_name}"})
        else:
            try:
                chat_id = self._session_chat_ids.get(event.agent_id, 0)
                if chat_id:
                    tool_args = {**tool_args, "_chat_id": chat_id}
                if asyncio.iscoroutinefunction(executor):
                    out = await executor(tool_args)
                else:
                    out = executor(tool_args)
                result = json.dumps(out if isinstance(out, dict) else {"result": str(out)})
            except Exception as e:
                logger.exception("Client tool %s failed", tool_name)
                result = json.dumps({"error": str(e)})

        reply = ToolExecuteResultEvent(call_id=call_id, result=result, error="")
        await self.send(reply)

    async def create_session(self, args: list[str] | None = None) -> str:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._session_future = future
        try:
            req = CommandRequest(command="session.new", args=args or [])
            await self.send(req)
            return await asyncio.wait_for(future, timeout=30.0)
        finally:
            self._session_future = None

    async def stage_files(
        self,
        workspace_id: str,
        specs: list[StagedFileSpec],
        payloads: list[bytes],
    ) -> StageFilesEvent:
        """Stage files into a workspace via multi-frame WS protocol.

        Sends a TEXT frame with StageFilesRequest metadata, then N BINARY
        frames with the raw file payloads. Awaits the StageFilesEvent
        response from the server.

        Args:
            workspace_id: Target workspace (empty string = current).
            specs: Per-file metadata (name, size, content_type).
            payloads: Raw bytes for each file, same order as specs.

        Returns:
            StageFilesEvent with staged[] and failed[] lists.
        """
        if not self._ws or not self._connected:
            raise RuntimeError("Not connected")
        if len(specs) != len(payloads):
            raise ValueError("specs and payloads must have the same length")

        request = StageFilesRequest(
            workspace_id=workspace_id,
            files=specs,
        )
        await self._ws.send(serialize_event(request))

        for data in payloads:
            await self._ws.send(data)

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._stage_files_future = future
        try:
            return await asyncio.wait_for(future, timeout=120.0)
        finally:
            self._stage_files_future = None

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

                    if isinstance(event, ToolExecuteRequestEvent):
                        asyncio.create_task(self._handle_tool_execute_request(event))
                        continue

                    if isinstance(event, SessionInfoEvent) and self._session_future and not self._session_future.done():
                        self._session_future.set_result(event.session_id)
                        continue

                    if isinstance(event, ErrorEvent):
                        logger.error("Server error: %s [%s]", event.error, event.error_type)
                        if self._session_future and not self._session_future.done():
                            self._session_future.set_exception(RuntimeError(f"{event.error} [{event.error_type}]"))
                        if self._stage_files_future and not self._stage_files_future.done():
                            self._stage_files_future.set_exception(RuntimeError(f"{event.error} [{event.error_type}]"))
                        continue

                    if isinstance(event, StageFilesEvent) and self._stage_files_future and not self._stage_files_future.done():
                        self._stage_files_future.set_result(event)
                        continue

                    session_id = getattr(event, "session_id", None)
                    if not session_id:
                        queues = self._session_queues
                        if len(queues) == 1:
                            session_id = next(iter(queues))
                        elif queues:
                            logger.warning(
                                "Cannot route event %s: %d sessions active, no session_id on event",
                                type(event).__name__, len(queues),
                            )
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

"""Session pool for managing per-user jaato sessions via the SDK facade WS client.

Each Telegram user (chat_id) gets its own ``WSRecoveryClient`` (a recoverable
WebSocket connection) and one jaato session on the server. This matches the
server's 1-client-1-session model and avoids the multi-tenancy routing issues of
a single shared connection.

The client is the SDK's facade WS client (``jaato_sdk.WSRecoveryClient``): it
owns the connect/handshake (which sends our presentation context + workspace +
client config), auto-reconnect, host-tool dispatch (``register_client_tools`` with
a ``handler`` per tool), and the typed event stream (``events()``). This module
keeps only the per-chat orchestration: connect, set_workspace, re-attach-or-create,
and the public surface the handlers/renderer call.
"""

import asyncio
import logging
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jaato_sdk import WSRecoveryClient
from jaato_sdk.events import ClientType, EventType

from jaato_client_telegram.chat_session_store import ChatSessionStore
from jaato_client_telegram.host_tool_loader import (
    USER_INSTALLED_TAG,
    load_all_tools,
    load_tool_file,
    make_executor,
    mark_user_installed,
    validate_name,
)
from jaato_client_telegram.host_tools import (
    TOOL_SCHEMAS,
    create_tool_executors,
    make_service_manifest_executor,
)
from jaato_client_telegram.thread_bot import ThreadAwareBot
from jaato_client_telegram.thread_store import ChatThreadStore

if TYPE_CHECKING:
    from aiogram import Bot

    from jaato_client_telegram.config import FileSharingConfig, JaatoWSConfig


logger = logging.getLogger(__name__)

# Memory-write tools whose successful completion should trigger a raw->curated
# drain (so the NEXT session's enrichment surfaces the memory). The model calls
# these via the server's memory plugin.
_MEMORY_STORE_TOOLS = frozenset({"store_memory", "memory", "update_memory"})


@dataclass
class SessionMetadata:
    session_id: str
    created_at: datetime
    last_activity: datetime
    client: WSRecoveryClient


def create_telegram_presentation_context() -> dict:
    """Display capabilities sent to the server (via the client's ``presentation=``
    override) so the model adapts its output to Telegram. A plain dict is accepted
    verbatim by the SDK client."""
    return {
        "content_width": 45,
        "content_height": None,
        "supports_markdown": True,
        "supports_tables": False,
        "supports_code_blocks": True,
        "supports_images": True,
        "supports_rich_text": True,
        "supports_unicode": True,
        "supports_mermaid": False,
        "supports_expandable_content": True,
        "client_type": "chat",
    }


class SessionPool:
    """Manages per-user sessions, each with its own recoverable WS client."""

    def __init__(
        self,
        ws_config: "JaatoWSConfig",
        bot: "Bot | None" = None,
        file_config: "FileSharingConfig | None" = None,
        max_concurrent: int = 50,
        session_store_path: str = "",
    ) -> None:
        self._ws_config = ws_config
        self._bot = bot
        self._file_config = file_config
        self._max_concurrent = max_concurrent
        self._sessions: dict[int, SessionMetadata] = {}
        self._lock = asyncio.Lock()
        # Persistent chat_id -> session_id map for re-attachment across restarts.
        # None when unconfigured (re-attachment disabled — sessions are per-process).
        self._session_store = ChatSessionStore(session_store_path) if session_store_path else None
        # Per-chat Telegram thread continuity: the bot follows the user's thread
        # and host-tool sends stay in it (see thread_store / ThreadAwareBot).
        # Persisted next to the session store; in-memory when that's unconfigured.
        thread_store_path = (
            str(Path(session_store_path).with_name("chat_threads.json"))
            if session_store_path else ""
        )
        self._thread_store = ChatThreadStore(thread_store_path)
        # Whether the most recent get_or_create_session for a chat RE-ATTACHED to
        # a persisted session (vs created fresh / reused in-memory) — so the
        # handler can show a "Resumed" cue. Read via took_reattach().
        self._last_reattach: dict[int, bool] = {}
        if self._ws_config.keycloak_client_id:
            # The facade WS client authenticates with a static token= (query/Bearer),
            # not the Keycloak client-credentials JWT flow the old transport did.
            # Fail loud rather than silently connect anonymously.
            raise RuntimeError(
                "Keycloak auth (keycloak_client_id) is not supported on the facade "
                "WS client; use jaato_ws.secret_token, or leave both empty for an "
                "anonymous (local/VPN) connection."
            )
        if self._session_store:
            logger.info("Session re-attachment enabled (store=%s)", session_store_path)

    def set_bot(self, bot: "Bot", file_config: "FileSharingConfig | None" = None) -> None:
        self._bot = bot
        self._file_config = file_config

    def took_reattach(self, chat_id: int) -> bool:
        """True if the most recent get_or_create_session for this chat RE-ATTACHED
        to a persisted session (vs created fresh / reused in-memory)."""
        return self._last_reattach.get(chat_id, False)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """SSLContext for wss:// — passed to the client's ``ssl=`` (which forwards
        it to websockets.connect). None for plain ws:// or when TLS is disabled."""
        tls = self._ws_config.tls
        if not tls or not tls.enabled:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if tls.ca_cert_path:
            ctx.load_verify_locations(tls.ca_cert_path)
        else:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if tls.cert_path and tls.key_path:
            ctx.load_cert_chain(tls.cert_path, tls.key_path)
        return ctx

    def _make_client(self) -> WSRecoveryClient:
        workspace = self._ws_config.workspace
        # config_root wires the daemon's framework-config search (profiles, agents,
        # file_edit backup dir); working_dir/workspace_path gates the runner-tier
        # sandbox root. Both ride the client's connect-time ClientConfigRequest.
        return WSRecoveryClient(
            self._ws_config.url,
            token=self._ws_config.secret_token or None,
            client_type=ClientType.CHAT,
            ssl=self._build_ssl_context(),
            workspace_path=workspace or None,
            config_root=(workspace.rstrip("/") + "/.jaato") if workspace else None,
            presentation=create_telegram_presentation_context(),
        )

    async def _list_session_ids(self, client: WSRecoveryClient) -> list[str]:
        """Session ids the daemon currently knows (in-memory AND on disk). The
        client's list_sessions() is fire-and-forget (reply via SESSION_LIST on the
        background drain), so subscribe once and await it."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def _on_list(event) -> None:
            if not future.done():
                future.set_result([s.get("id") for s in event.sessions if s.get("id")])

        unsub = client.subscribe_once(EventType.SESSION_LIST, _on_list)
        try:
            await client.list_sessions()
            return await asyncio.wait_for(future, timeout=15.0)
        finally:
            unsub()

    async def get_or_create_session(self, chat_id: int) -> str:
        async with self._lock:
            if chat_id in self._sessions:
                meta = self._sessions[chat_id]
                # Reuse the cached client unless it has fully given up. The
                # recovery client reconnects transient WS drops on its own
                # (is_reconnecting), so we keep it rather than throwing away a
                # warm session; only a closed/failed client is recreated.
                if meta.client.is_connected or meta.client.is_reconnecting:
                    meta.last_activity = datetime.now()
                    self._last_reattach[chat_id] = False
                    return meta.session_id
                logger.info(
                    "chat_id %d: cached client for session %s is down — recreating",
                    chat_id, meta.session_id,
                )
                self._sessions.pop(chat_id, None)
                try:
                    await meta.client.disconnect()
                except Exception:
                    logger.debug("disconnect of dead client failed", exc_info=True)

            if len(self._sessions) >= self._max_concurrent:
                await self._evict_oldest()

            try:
                client = self._make_client()
                if not await client.connect():
                    raise RuntimeError("WSRecoveryClient.connect() returned False")
                logger.info("Connected facade WS client for chat_id %d", chat_id)

                # Auto-curate memories client-side: a successful memory-write tool
                # call promotes raw->curated (replaces the premium reactor engine).
                # Registered on the recovery client's registry, so it survives
                # reconnects.
                client.subscribe(EventType.TOOL_CALL_END, self._on_tool_call_end)

                # No manual set_workspace: the client's _handshake already sends a
                # set_workspace CommandRequest (from workspace_path) AND the
                # ClientConfigRequest on connect — and re-sends both on every
                # reconnect — so workspace-local profile/agent discovery
                # (.jaato/profiles, .jaato/agents) is wired without us.

                # Assemble host tools (static + user-installed dynamic) and register
                # them BEFORE create/attach so the schemas ride the bootstrap into
                # the runner-tier model (server PR #349/#350). Each entry carries a
                # "handler" the client dispatches on tool.execute_request.
                client_tools: list[dict] | None = None
                if self._bot and self._file_config:
                    client_tools = self._assemble_host_tools(chat_id)
                    await client.register_client_tools(client_tools)
                    logger.info(
                        "Registered %d host tools for chat_id %d", len(client_tools), chat_id,
                    )

                # Re-attach to this chat's persisted session if it still exists on
                # the daemon (session.list = unified in-memory + on-disk view, so it
                # survives a daemon restart); otherwise create a fresh one.
                session_id: str | None = None
                if self._session_store:
                    persisted = self._session_store.get(chat_id)
                    if persisted:
                        if persisted in await self._list_session_ids(client):
                            await client.attach_session(persisted)
                            session_id = persisted
                            self._last_reattach[chat_id] = True
                            logger.info("Re-attached chat_id %d to session %s", chat_id, session_id)
                        else:
                            logger.info(
                                "Persisted session %s for chat_id %d is gone; creating new",
                                persisted, chat_id,
                            )

                if session_id is None:
                    session_id = await client.create_session(
                        profile=self._ws_config.profile or None,
                        agent=self._ws_config.agent or None,
                    )
                    if not session_id:
                        raise RuntimeError("create_session returned no session id")
                    if self._session_store:
                        self._session_store.set(chat_id, session_id)
                    self._last_reattach[chat_id] = False
                    logger.info("Created session %s for chat_id %d", session_id, chat_id)

                self._sessions[chat_id] = SessionMetadata(
                    session_id=session_id,
                    created_at=datetime.now(),
                    last_activity=datetime.now(),
                    client=client,
                )
                return session_id
            except Exception as e:
                raise RuntimeError(
                    f"Failed to create session for chat_id {chat_id}: {e}"
                ) from e

    def _host_tools_dir(self) -> Path | None:
        """Bot-owned install dir for dynamic host tools — OUTSIDE the workspace so
        the AppArmor-confined runner cannot write or tamper with installed code.
        None when host_tools_dir is unconfigured (the feature is disabled)."""
        d = self._ws_config.host_tools_dir
        return Path(d).expanduser() if d else None

    def _assemble_host_tools(self, chat_id: int) -> list[dict]:
        """client_tools for register_client_tools: static tools (send_to_telegram,
        show_image, register_tool, service_manifest) + any user-installed dynamic
        tools, each as ``{**schema, "handler": executor}``. Loaded fresh so a
        just-installed tool is picked up on the next registration."""
        # Wrap the bot so every host-tool send (built-in AND dynamic ctx.bot) is
        # injected with this chat's current message_thread_id at send time, keeping
        # tool messages in the same thread as the conversation. Reads the thread
        # store live, so a thread switch is reflected immediately.
        tbot = ThreadAwareBot(
            self._bot, chat_id, lambda cid=chat_id: self._thread_store.current(cid)
        )
        executors = create_tool_executors(tbot, chat_id, self._file_config)
        executors["register_tool"] = self._make_register_tool_executor(chat_id)
        executors["service_manifest"] = make_service_manifest_executor(self._ws_config.workspace)

        tools: list[dict] = [
            {**schema, "handler": executors[schema["name"]]} for schema in TOOL_SCHEMAS
        ]

        tools_dir = self._host_tools_dir()
        if tools_dir is not None:
            for name, t in load_all_tools(tools_dir).items():
                # Tag dynamically-installed tools so the model never mistakes them
                # for built-ins present at bootstrap (see mark_user_installed).
                schema = mark_user_installed(t["schema"])
                tools.append({**schema, "handler": make_executor(t["execute"], tbot, chat_id)})
        return tools

    # --- Telegram thread continuity -----------------------------------------
    def sync_thread(self, chat_id: int, thread_id: "int | None") -> None:
        """Record the thread the user's latest message was in, so bot replies +
        host-tool sends follow it. Called by the message handlers on each turn."""
        self._thread_store.sync_inbound(chat_id, thread_id)

    def current_thread(self, chat_id: int) -> "int | None":
        """The message_thread_id the bot should currently send into for this chat."""
        return self._thread_store.current(chat_id)

    def stage_upload(self, name: str, data: bytes) -> str | None:
        """Write a user-attached file into the workspace's ``uploads/`` dir so the
        agent can read it with its file tools (any type — text or binary — with no
        context bloat). Returns the workspace-relative path (e.g.
        ``uploads/script.sh``), or ``None`` if no workspace is configured.

        The filename is reduced to its basename (it comes from Telegram / the
        user) so a crafted name cannot escape ``uploads/``. The workspace is the
        confined runner's read-write sandbox root, so files written here are
        readable by the agent at the returned relative path."""
        workspace = self._ws_config.workspace
        if not workspace:
            return None
        safe = Path(name).name
        if safe in ("", ".", ".."):
            safe = "file"
        dest_dir = Path(workspace) / "uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / safe).write_bytes(data)
        return f"uploads/{safe}"

    # --- Client-side memory curation (replaces the premium reactor) ----------
    def _curate_memories(self) -> int:
        """Promote this workspace's raw memories to curated — the same
        deterministic raw->validated drain the premium reactor did, now
        client-side. Returns the count promoted. No-op (returns 0) if the
        workspace or the server's memory package is unavailable, so the bot
        degrades gracefully without curation rather than failing."""
        workspace = self._ws_config.workspace
        if not workspace:
            return 0
        try:
            import dataclasses

            from shared.plugins.memory.models import MATURITY_VALIDATED
            from shared.plugins.memory.storage import MemoryStore
        except Exception:
            logger.debug(
                "memory curation skipped: shared.plugins.memory unavailable", exc_info=True
            )
            return 0
        store = MemoryStore(f"{workspace.rstrip('/')}/.jaato/memories")
        raw = store.list_raw()
        if not raw:
            return 0
        for memory in raw:
            store.update(dataclasses.replace(memory, maturity=MATURITY_VALIDATED))
        return len(raw)

    async def _on_tool_call_end(self, event) -> None:
        """Auto-curate after the model stores a memory: when a memory-write tool
        completes successfully, drain raw->curated so the next session's
        enrichment surfaces it. Event-driven, client-side — replaces the premium
        reactor engine. Subscribed per client (survives reconnects via the
        recovery client's subscription registry)."""
        if getattr(event, "tool_name", "") not in _MEMORY_STORE_TOOLS:
            return
        if not getattr(event, "success", False):
            return
        promoted = await asyncio.to_thread(self._curate_memories)
        if promoted:
            logger.info("memory curation: promoted %d raw -> curated", promoted)

    def _make_register_tool_executor(self, chat_id: int):
        async def executor(args: dict) -> dict:
            name = (args or {}).get("name", "")
            try:
                return await self.install_and_register_tool(chat_id, name)
            except Exception as e:  # noqa: BLE001 — tool boundary
                logger.exception("register_tool failed")
                return {"error": str(e)}
        return executor

    async def install_and_register_tool(self, chat_id: int, name: str) -> dict:
        """Install a drafted tool and re-register it on the live session.

        The agent (confined runner) wrote the draft to tool_drafts/<name>.py in
        the workspace. The bot (this process — UNCONFINED) copies it into the
        bot-owned host_tools_dir, validates it by loading, and re-registers host
        tools so the runner sees it next turn. Runs only AFTER the user approves
        register_tool (auto_approve=False)."""
        validate_name(name)
        tools_dir = self._host_tools_dir()
        if tools_dir is None:
            return {"error": (
                "Dynamic tools are disabled (set jaato_ws.host_tools_dir in the bot config)."
            )}
        workspace = Path(self._ws_config.workspace)
        draft = workspace / "tool_drafts" / f"{name}.py"
        if not draft.is_file():
            return {"error": f"No draft at tool_drafts/{name}.py — write the tool there first."}

        tools_dir.mkdir(parents=True, exist_ok=True)
        target = tools_dir / f"{name}.py"
        target.write_text(draft.read_text())

        # Validate by loading (executes the module — trusted now: user-approved).
        try:
            load_tool_file(target)
        except Exception as e:  # noqa: BLE001
            target.unlink(missing_ok=True)  # roll back a bad install
            return {"error": f"Tool '{name}' is invalid and was not installed: {e}"}

        meta = self._sessions.get(chat_id)
        if meta is None:
            return {"result": (
                f"Installed '{name}' as a user-installed tool (new — not a built-in); "
                f"it will load on your next session."
            )}

        # Re-register the full set on the live session; register_client_tools
        # refreshes the daemon runtime tool list and re-glues schemas onto the
        # runner-tier model.
        await meta.client.register_client_tools(self._assemble_host_tools(chat_id))
        logger.info("Installed + registered dynamic host tool %r for chat %d", name, chat_id)
        # State the temporal fact in the result the model reads, so it does not
        # later confabulate this tool as a built-in present at session start.
        return {"result": (
            f"Installed '{name}' — a NEW tool you just created in THIS session; it "
            f"did not exist before now. It is in your tool list (tagged "
            f"'{USER_INSTALLED_TAG}'); call it on your next turn. Do not describe it "
            f"as a built-in — you created it."
        )}

    async def send_message(
        self, session_id: str, text: str, attachments: list | None = None,
    ) -> None:
        """Send a user message, optionally with multimodal attachments.

        Each attachment is a dict {mime_type, data, display_name} where `data`
        is base64-encoded bytes (the canonical wire contract for user-message
        images, e.g. a photo for a vision tier). The framework ferries these to
        the runner-tier model."""
        client = self._find_client(session_id)
        await client.send_message(text, attachments=attachments or [])

    async def respond_to_permission(
        self, session_id: str, request_id: str,
        response: str, edited_arguments: dict | None = None,
    ) -> None:
        client = self._find_client(session_id)
        await client.respond_to_permission(
            request_id, response, edited_arguments=edited_arguments,
        )

    async def respond_to_clarification(
        self, session_id: str, request_id: str, answers: list[str],
    ) -> None:
        """Answer a clarification request with one string per question, in order.

        WS/chat clients receive all questions at once (ClarificationBatchRequested)
        and reply in one batch; the server feeds each answer into the channel queue
        sequentially. Single/multiple-choice answers are 1-based ordinals
        ("2", "1,3"); free-text answers are the literal text."""
        client = self._find_client(session_id)
        await client.respond_to_clarification_batch(request_id, answers)

    async def events(self, session_id: str) -> AsyncIterator:
        return self._find_client(session_id).events()

    async def stop(self, session_id: str) -> None:
        await self._find_client(session_id).stop()

    def get_session_id(self, chat_id: int) -> str | None:
        session = self._sessions.get(chat_id)
        return session.session_id if session else None

    def _find_client(self, session_id: str) -> WSRecoveryClient:
        for meta in self._sessions.values():
            if meta.session_id == session_id:
                return meta.client
        raise RuntimeError(f"No client for session_id {session_id}")

    async def remove_client(self, chat_id: int) -> None:
        async with self._lock:
            session = self._sessions.pop(chat_id, None)
            if session:
                try:
                    await session.client.disconnect()
                except Exception:
                    logger.exception("Error disconnecting client for chat_id %d", chat_id)

    async def forget_session(self, chat_id: int) -> None:
        """Drop a chat's session entirely: disconnect + remove from the pool AND
        forget the persisted re-attach mapping, so the NEXT message starts a FRESH
        session. Used to self-heal from a stalled/stuck session (e.g. a broken
        re-attach that never produced a response)."""
        await self.remove_client(chat_id)
        if self._session_store:
            self._session_store.remove(chat_id)
        self._last_reattach.pop(chat_id, None)
        logger.info("Forgot session for chat_id %d (stall recovery)", chat_id)

    async def _evict_oldest(self) -> None:
        if not self._sessions:
            return
        oldest = min(
            self._sessions, key=lambda c: self._sessions[c].last_activity,
        )
        await self.remove_client(oldest)

    async def cleanup_idle(self, max_idle_minutes: int = 60) -> list[int]:
        """Disconnect sessions idle past the threshold; return the dropped chat_ids
        (so the caller can notify them). Removal is the dedup: a chat won't appear
        again until its next message recreates the session."""
        now = datetime.now()
        to_remove = []
        async with self._lock:
            for chat_id, session in self._sessions.items():
                if (now - session.last_activity).total_seconds() > max_idle_minutes * 60:
                    to_remove.append(chat_id)
        for chat_id in to_remove:
            await self.remove_client(chat_id)
        return to_remove

    async def shutdown(self) -> None:
        async with self._lock:
            chat_ids = list(self._sessions.keys())
        for chat_id in chat_ids:
            await self.remove_client(chat_id)

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    def get_session_info(self, chat_id: int) -> SessionMetadata | None:
        return self._sessions.get(chat_id)

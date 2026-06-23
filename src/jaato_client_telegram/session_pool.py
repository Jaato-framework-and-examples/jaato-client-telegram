"""Session pool for managing per-user jaato sessions via WebSocket transport.

Each Telegram user (chat_id) gets its own isolated WebSocket connection
and session on the jaato server.  This matches the server's 1-client-1-session
model and avoids the multi-tenancy routing issues of a single shared WS.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jaato_sdk.events import (
    SendMessageRequest,
    PermissionResponseRequest,
    ClarificationBatchResponseEvent,
    ClientConfigRequest,
    CommandRequest,
    StopRequest,
    SessionInfoEvent,
    StageFilesEvent,
    StagedFileSpec,
)

from jaato_client_telegram.host_tools import TOOL_SCHEMAS, TOOL_CATEGORIES, create_tool_executors
from jaato_client_telegram.host_tool_loader import load_all_tools, make_executor, validate_name, load_tool_file
from jaato_client_telegram.transport import WSTransport
from jaato_client_telegram.chat_session_store import ChatSessionStore

if TYPE_CHECKING:
    from aiogram import Bot
    from jaato_client_telegram.config import FileSharingConfig, JaatoWSConfig


logger = logging.getLogger(__name__)


@dataclass
class SessionMetadata:
    session_id: str
    created_at: datetime
    last_activity: datetime
    transport: WSTransport


def create_telegram_presentation_context() -> dict:
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
    """Manages per-user sessions, each with its own WebSocket connection."""

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
        self._pending_session_future: asyncio.Future | None = None
        # Persistent chat_id -> session_id map for re-attachment across restarts.
        # None when unconfigured (re-attachment disabled — sessions are per-process).
        self._session_store = ChatSessionStore(session_store_path) if session_store_path else None
        # Whether the most recent get_or_create_session for a chat RE-ATTACHED to
        # a persisted session (vs created fresh / reused in-memory) — so the
        # handler can show a "Resumed" cue. Read via took_reattach().
        self._last_reattach: dict[int, bool] = {}
        if self._session_store:
            logger.info("Session re-attachment enabled (store=%s)", session_store_path)

    def set_bot(self, bot: "Bot", file_config: "FileSharingConfig | None" = None) -> None:
        self._bot = bot
        self._file_config = file_config

    def took_reattach(self, chat_id: int) -> bool:
        """True if the most recent get_or_create_session for this chat RE-ATTACHED
        to a persisted session (vs created fresh / reused in-memory)."""
        return self._last_reattach.get(chat_id, False)

    def _make_transport(self) -> WSTransport:
        return WSTransport(
            url=self._ws_config.url,
            tls_config=self._ws_config.tls,
            keycloak_base_url=self._ws_config.keycloak_base_url,
            keycloak_realm=self._ws_config.keycloak_realm,
            keycloak_client_id=self._ws_config.keycloak_client_id,
            keycloak_client_secret=self._ws_config.keycloak_client_secret,
            secret_token=self._ws_config.secret_token,
        )

    async def get_or_create_session(self, chat_id: int) -> str:
        async with self._lock:
            if chat_id in self._sessions:
                self._sessions[chat_id].last_activity = datetime.now()
                self._last_reattach[chat_id] = False
                return self._sessions[chat_id].session_id

            if len(self._sessions) >= self._max_concurrent:
                await self._evict_oldest()

            try:
                transport = self._make_transport()
                await transport.connect()

                presentation_ctx = create_telegram_presentation_context()
                # working_dir wires the server's self._workspace_path, which gates
                # set_workspace_root() — the ContextVar that runner-tier PATH tools
                # (filesystem_query/cli/notebook) read for their sandbox root.
                # config_root wires registry.set_config_root() before expose_all,
                # which file_edit needs to resolve its backup dir
                # (<config_root>/sessions/<id>/backups) — without it file_edit
                # fails to initialize and is not exposed at all.
                # Both mirror what the SDK IPCClient sends from workspace_path/
                # config_root; the hand-rolled WS transport must send them too.
                # (set_workspace below is a SEPARATE wire that drives profile
                # discovery — all three are needed today.)
                workspace = self._ws_config.workspace
                config_event = ClientConfigRequest(
                    presentation=presentation_ctx,
                    working_dir=workspace or None,
                    config_root=(workspace.rstrip("/") + "/.jaato") if workspace else None,
                )
                await transport.send(config_event)
                logger.info("Sent presentation + working_dir + config_root for chat_id %d", chat_id)

                # Tell the server where this client's workspace is, so session.new
                # can discover workspace-local profiles/agents (.jaato/...).
                if self._ws_config.workspace:
                    await transport.send(CommandRequest(
                        command="set_workspace", args=[self._ws_config.workspace],
                    ))

                # Assemble host tools = static (send_to_telegram, show_image,
                # register_tool) + any dynamic tools the user has installed in
                # .jaato/host_tools/. Register SCHEMAS BEFORE session.new so they
                # ride the bootstrap envelope into the runner (server PR
                # #349/#350); registering after only updates the daemon registry.
                host_executors: dict | None = None
                if self._bot and self._file_config:
                    host_schemas, host_executors = self._assemble_host_tools(chat_id)
                    await transport.register_host_tools(host_schemas, TOOL_CATEGORIES)

                # Re-attach to this chat's persisted session if one exists and is
                # still known to the daemon (session.list reports the unified
                # in-memory + on-disk view, so this survives a daemon restart).
                # Otherwise create a fresh session and remember it.
                session_id: str | None = None
                if self._session_store:
                    persisted = self._session_store.get(chat_id)
                    if persisted:
                        if persisted in await transport.list_sessions():
                            await transport.attach_session(persisted)
                            session_id = persisted
                            self._last_reattach[chat_id] = True
                            logger.info("Re-attached chat_id %d to session %s", chat_id, session_id)
                        else:
                            logger.info(
                                "Persisted session %s for chat_id %d is gone; creating new",
                                persisted, chat_id,
                            )

                if session_id is None:
                    session_args: list[str] = []
                    if self._ws_config.profile:
                        session_args += ["--profile", self._ws_config.profile]
                    if self._ws_config.agent:
                        session_args += ["--agent", self._ws_config.agent]
                    session_id = await transport.create_session(session_args)
                    if self._session_store:
                        self._session_store.set(chat_id, session_id)
                    self._last_reattach[chat_id] = False
                    logger.info("Created session %s for chat_id %d", session_id, chat_id)

                transport.register_session(session_id)

                # Executor routing is local to the transport and needs session_id.
                if host_executors is not None:
                    transport.set_session_tool_executors(session_id, host_executors)
                    logger.info(
                        "Wired %d host-tool executors for session %s",
                        len(host_executors), session_id,
                    )

                self._sessions[chat_id] = SessionMetadata(
                    session_id=session_id,
                    created_at=datetime.now(),
                    last_activity=datetime.now(),
                    transport=transport,
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

    def _assemble_host_tools(self, chat_id: int) -> tuple[list[dict], dict]:
        """(schemas, executors) for the transport: static tools (send_to_telegram,
        show_image, register_tool) + any user-installed dynamic tools. Loaded fresh
        so a just-installed tool is picked up on the next registration."""
        schemas = list(TOOL_SCHEMAS)
        executors = create_tool_executors(self._bot, chat_id, self._file_config)
        executors["register_tool"] = self._make_register_tool_executor(chat_id)
        tools_dir = self._host_tools_dir()
        if tools_dir is not None:
            for name, t in load_all_tools(tools_dir).items():
                schemas.append(t["schema"])
                executors[name] = make_executor(t["execute"], self._bot, chat_id)
        return schemas, executors

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
        bot-owned .jaato/host_tools/, validates it by loading, and re-registers
        host tools so the runner sees it next turn. Runs only AFTER the user
        approves register_tool (auto_approve=False)."""
        validate_name(name)
        tools_dir = self._host_tools_dir()
        if tools_dir is None:
            return {"error": "Dynamic tools are disabled (set jaato_ws.host_tools_dir in the bot config)."}
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
            return {"result": f"Installed '{name}'; it will load on your next session."}

        schemas, executors = self._assemble_host_tools(chat_id)
        await meta.transport.register_host_tools(schemas, TOOL_CATEGORIES, force=True)
        meta.transport.set_session_tool_executors(meta.session_id, executors)
        logger.info("Installed + registered dynamic host tool %r for chat %d", name, chat_id)
        return {"result": f"Installed '{name}'. Call it from your next message."}

    def on_session_info_event(self, event: SessionInfoEvent) -> None:
        if self._pending_session_future and not self._pending_session_future.done():
            self._pending_session_future.set_result(event.session_id)

    async def send_message(
        self, session_id: str, text: str, attachments: list | None = None,
    ) -> None:
        """Send a user message, optionally with multimodal attachments.

        Each attachment is a dict {mime_type, data, display_name} where `data`
        is base64-encoded bytes (the canonical WS wire contract for
        user-message images, e.g. a photo for a vision tier). The framework
        ferries these to the runner-tier model.
        """
        transport = self._find_transport(session_id)
        request = SendMessageRequest(text=text, attachments=attachments or [])
        await transport.send(request)

    async def respond_to_permission(
        self, session_id: str, request_id: str,
        response: str, edited_arguments: dict | None = None,
    ) -> None:
        transport = self._find_transport(session_id)
        request = PermissionResponseRequest(
            request_id=request_id, response=response,
            edited_arguments=edited_arguments,
        )
        await transport.send(request)

    async def respond_to_clarification(
        self, session_id: str, request_id: str, answers: list[str],
    ) -> None:
        """Answer a clarification request with one string per question, in order.

        WS clients receive all questions at once (ClarificationBatchEvent) and
        reply in one batch; the server feeds each answer into the channel queue
        sequentially. Single/multiple-choice answers are 1-based ordinals
        ("2", "1,3"); free-text answers are the literal text.
        """
        transport = self._find_transport(session_id)
        request = ClarificationBatchResponseEvent(
            request_id=request_id, answers=answers,
        )
        await transport.send(request)

    async def events(self, session_id: str) -> AsyncIterator:
        return self._find_transport(session_id).events(session_id)

    async def stop(self, session_id: str) -> None:
        transport = self._find_transport(session_id)
        request = StopRequest()
        await transport.send(request)

    async def stage_files(
        self,
        chat_id: int,
        files: list[tuple[str, bytes, str | None]],
    ) -> StageFilesEvent:
        if chat_id not in self._sessions:
            raise RuntimeError(f"No session for chat_id {chat_id}")
        transport = self._sessions[chat_id].transport
        specs = [
            StagedFileSpec(name=name, size=len(data), content_type=ct)
            for name, data, ct in files
        ]
        payloads = [data for _, data, _ in files]
        return await transport.stage_files(
            workspace_id="",
            specs=specs,
            payloads=payloads,
        )

    def get_session_id(self, chat_id: int) -> str | None:
        session = self._sessions.get(chat_id)
        return session.session_id if session else None

    def _find_transport(self, session_id: str) -> WSTransport:
        for meta in self._sessions.values():
            if meta.session_id == session_id:
                return meta.transport
        raise RuntimeError(f"No transport for session_id {session_id}")

    async def remove_client(self, chat_id: int) -> None:
        async with self._lock:
            session = self._sessions.pop(chat_id, None)
            if session:
                try:
                    await session.transport.disconnect()
                except Exception:
                    logger.exception("Error disconnecting transport for chat_id %d", chat_id)

    async def _evict_oldest(self) -> None:
        if not self._sessions:
            return
        oldest = min(
            self._sessions, key=lambda c: self._sessions[c].last_activity,
        )
        await self.remove_client(oldest)

    async def cleanup_idle(self, max_idle_minutes: int = 60) -> int:
        now = datetime.now()
        to_remove = []
        async with self._lock:
            for chat_id, session in self._sessions.items():
                if (now - session.last_activity).total_seconds() > max_idle_minutes * 60:
                    to_remove.append(chat_id)
        for chat_id in to_remove:
            await self.remove_client(chat_id)
        return len(to_remove)

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

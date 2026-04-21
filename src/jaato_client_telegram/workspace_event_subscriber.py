"""
Workspace event subscriber to receive file watching events from jaato-server.

Subscribes to workspace.panel events via IPC backend and maintains
file tracking state via WorkspaceFileTracker.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Dict

from jaato_client_telegram.workspace_tracker import WorkspaceFileTracker


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    pass


class WorkspaceEventSubscriber:
    """
    Subscribe to workspace panel events from jaato-server via IPC.

    Receives file change events (added, updated, deleted) and updates
    the WorkspaceFileTracker to maintain an accurate file registry.
    """

    def __init__(self, ipc_backend, workspace_tracker: WorkspaceFileTracker):
        """
        Initialize workspace event subscriber.

        Args:
            ipc_backend: IPC backend for communicating with jaato-server
            workspace_tracker: File tracker instance to update with file events
        """
        self._ipc_backend = ipc_backend
        self._workspace_tracker = workspace_tracker

    async def start(self) -> None:
        """
        Start subscribing to workspace panel events.

        Subscribes to both WorkspaceFilesChangedEvent (incremental changes)
        and WorkspaceFilesSnapshotEvent (full snapshots on reconnect/initial attach).
        """
        logger.info("Subscribing to workspace panel events")

        # Subscribe to incremental file changes
        await self._ipc_backend.subscribe_to_events(
            "workspace.panel",
            self._on_workspace_event
        )

        # Also subscribe to snapshot events for full state updates
        # This handles initial connections and reconnections
        await self._ipc_backend.subscribe_to_events(
            "workspace.panel.snapshot",
            self._on_workspace_event
        )

    async def stop(self) -> None:
        """
        Stop subscribing to workspace panel events.
        """
        logger.info("Unsubscribing from workspace panel events")
        await self._ipc_backend.unsubscribe_from_events("workspace.panel")

    async def _on_workspace_event(self, event):
        """
        Handle workspace panel event from jaato-server.

        Processes file-related events and updates the WorkspaceFileTracker.

        Args:
            event: Workspace panel event (WorkspaceFilesChangedEvent or WorkspaceFilesSnapshotEvent)
        """
        event_type = getattr(event, "type", None)
        workspace_id = getattr(event, "workspace_id", None)

        if not workspace_id:
            logger.warning(f"Event missing workspace_id: {event}")
            return

        # Handle file added event
        if event_type == "workspace.file.added":
            file_path = getattr(event, "path", None)
            logger.info(f"File added: {workspace_id} -> {file_path}")
            await self._workspace_tracker.add_file(workspace_id, file_path)

        # Handle file updated event
        elif event_type == "workspace.file.updated":
            file_path = getattr(event, "path", None)
            logger.info(f"File updated: {workspace_id} -> {file_path}")
            # Update tracking (file stays same, no action needed)
            # Just log for now

        # Handle file deleted event
        elif event_type == "workspace.file.deleted":
            file_path = getattr(event, "path", None)
            logger.info(f"File deleted: {workspace_id} -> {file_path}")
            await self._workspace_tracker.remove_file(workspace_id, file_path)

        # Handle snapshot events (full workspace state)
        elif event_type == "workspace.panel.snapshot":
            logger.info(f"Workspace snapshot: {workspace_id}")
            
            # Get list of files from snapshot
            files = getattr(event, "files", [])
            
            # Add all files to tracking (ensure we don't miss any)
            for file_path in files:
                await self._workspace_tracker.add_file(workspace_id, file_path)
            
            logger.debug(f"Snapshot processed {len(files)} files for {workspace_id}")

        else:
            logger.warning(f"Unknown event type: {event_type}")

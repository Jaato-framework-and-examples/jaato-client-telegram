"""
Workspace file tracker for monitoring files created by agents.

Tracks all files created in workspaces to enable intelligent file sharing:
only files mentioned by the agent are sent to Telegram users.
"""

import logging
from typing import Dict, Set


logger = logging.getLogger(__name__)


class WorkspaceFileTracker:
    """
    Track files created by agent across workspaces.

    Maintains a registry of all files in each workspace, enabling
    the bot to verify which files exist before attempting to send them.
    """

    def __init__(self):
        """Initialize the workspace file tracker."""
        # Maps workspace_id -> set of file paths
        self._tracked_files: Dict[str, Set[str]] = {}

    async def add_file(self, workspace_id: str, file_path: str) -> bool:
        """
        Track a new file in a workspace.

        Args:
            workspace_id: The workspace identifier
            file_path: Path to the file (can be relative or absolute)

        Returns:
            True if file was newly added, False if already tracked
        """
        if workspace_id not in self._tracked_files:
            self._tracked_files[workspace_id] = set()

        if file_path not in self._tracked_files[workspace_id]:
            self._tracked_files[workspace_id].add(file_path)
            logger.debug(f"Added file to tracker: {workspace_id} -> {file_path}")
            return True

        logger.debug(f"File already tracked: {workspace_id} -> {file_path}")
        return False

    async def remove_file(self, workspace_id: str, file_path: str) -> None:
        """
        Remove a file from tracking.

        Args:
            workspace_id: The workspace identifier
            file_path: Path to the file
        """
        if workspace_id in self._tracked_files:
            if file_path in self._tracked_files[workspace_id]:
                self._tracked_files[workspace_id].remove(file_path)
                logger.debug(f"Removed file from tracker: {workspace_id} -> {file_path}")

            # Clean up empty workspace entries
            if not self._tracked_files[workspace_id]:
                del self._tracked_files[workspace_id]

    async def is_file_known(self, workspace_id: str, file_path: str) -> bool:
        """
        Check if a file is being tracked in a workspace.

        Args:
            workspace_id: The workspace identifier
            file_path: Path to the file

        Returns:
            True if file is tracked in the workspace, False otherwise
        """
        return file_path in self._tracked_files.get(workspace_id, set())

    async def get_all_files(self, workspace_id: str) -> Set[str]:
        """
        Get all tracked files for a workspace.

        Args:
            workspace_id: The workspace identifier

        Returns:
            Set of file paths tracked in the workspace
        """
        return self._tracked_files.get(workspace_id, set()).copy()

    async def clear_workspace(self, workspace_id: str) -> None:
        """
        Remove all files for a workspace (e.g., on cleanup).

        Args:
            workspace_id: The workspace identifier
        """
        if workspace_id in self._tracked_files:
            del self._tracked_files[workspace_id]
            logger.debug(f"Cleared all files for workspace: {workspace_id}")

    async def get_workspace_count(self) -> int:
        """
        Get the number of workspaces with tracked files.

        Returns:
            Number of workspaces
        """
        return len(self._tracked_files)

    async def get_file_count(self, workspace_id: str) -> int:
        """
        Get the number of tracked files in a workspace.

        Args:
            workspace_id: The workspace identifier

        Returns:
            Number of files tracked in the workspace
        """
        return len(self._tracked_files.get(workspace_id, set()))

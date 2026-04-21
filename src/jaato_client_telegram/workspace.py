"""
Workspace management for per-user isolation.

Each Telegram user gets their own workspace directory containing
copies of .env and .jaato/ for complete filesystem-level isolation.
"""

import asyncio
import shutil
from pathlib import Path
from typing import Self

from jaato_client_telegram.config import Config


class Workspace:
    """
    Represents a user's isolated workspace directory.
    
    Each workspace contains:
    - .env: Environment variables for SDK client
    - .jaato/: Local memories, waypoints, templates
    """

    def __init__(self, chat_id: int, root: Path, template_env: Path, template_jaato: Path) -> None:
        """
        Initialize a workspace for a user.
        
        Args:
            chat_id: Telegram user's chat ID
            root: Root directory for all workspaces
            template_env: Path to root .env file (template source)
            template_jaato: Path to root .jaato/ directory (template source)
        """
        self.chat_id = chat_id
        self.root = root
        self.template_env = template_env
        self.template_jaato = template_jaato
        self._path: Path | None = None

    @property
    def path(self) -> Path:
        """Get the workspace directory path, creating if needed."""
        if self._path is None:
            raise RuntimeError("Workspace has not been created yet")
        return self._path

    @property
    def env_path(self) -> Path:
        """Get path to .env in this workspace."""
        return self.path / ".env"

    @property
    def exists(self) -> bool:
        """Check if workspace directory exists."""
        return self._path is not None and self._path.exists()

    async def create(self) -> Self:
        """
        Create the workspace directory and copy template files.
        
        Returns:
            Self for method chaining
        """
        # Create workspace path
        workspace_name = f"user_{self.chat_id}"
        self._path = self.root / workspace_name

        # Run blocking I/O in thread pool
        await asyncio.to_thread(self._create_blocking)
        
        return self

    def _create_blocking(self) -> None:
        """
        Blocking implementation of workspace creation.
        
        This runs in a thread pool to avoid blocking the event loop.
        """
        if self._path is None:
            raise RuntimeError("Workspace path not set")

        # Create workspace directory
        self._path.mkdir(parents=True, exist_ok=True)

        # Copy .env if template exists
        if self.template_env.exists():
            shutil.copy2(self.template_env, self.env_path)
        else:
            # Create empty .env if template doesn't exist
            self.env_path.touch()

        # Create .jaato/ directory with selective copying
        # We copy structure/config but NOT user-specific data (memories, logs, auth)
        jaato_dest = self._path / ".jaato"
        jaato_dest.mkdir(exist_ok=True)
        
        if self.template_jaato.exists():
            # Copy only shared configuration, not user-specific data
            self._selective_copy_jaato(self.template_jaato, jaato_dest)

    def _selective_copy_jaato(self, src: Path, dest: Path) -> None:
        """
        Copy only non-user-specific files from .jaato template.
        
        Files/directories to COPY (shared structure/config):
        - gc.json (garbage collection config)
        - *.json (config files like zhipuai_auth.json - shared credentials)
        - instructions/ (system prompts)
        - templates/ (code templates, if exists)
        - references/ (knowledge base references, if exists)
        
        Files/directories to SKIP (user-specific):
        - memories/ (user's own memories)
        - logs/ (session logs)
        - sessions/ (session data)
        - waypoints/ (user waypoints)
        - vision/ (vision cache - contains session-specific data)
        
        Args:
            src: Source .jaato directory
            dest: Destination .jaato directory (already created)
        """
        # Copy config files (including auth files - they're shared credentials)
        for json_file in src.glob("*.json"):
            shutil.copy2(json_file, dest / json_file.name)
        
        # Directories to copy (shared structure and resources)
        # NOTE: vision/ is deliberately excluded - it contains session-specific cached data
        shared_dirs = ["instructions", "templates", "references"]
        
        for dirname in shared_dirs:
            src_dir = src / dirname
            dest_dir = dest / dirname
            if src_dir.is_dir():
                shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)
        
        # Create empty subdirectories that are user-specific (NOT copied)
        user_specific_dirs = ["memories", "logs", "sessions", "waypoints", "vision"]
        for dirname in user_specific_dirs:
            (dest / dirname).mkdir(exist_ok=True)

    async def delete(self) -> None:
        """Remove the workspace directory and all its contents."""
        if self._path is None or not self._path.exists():
            return

        await asyncio.to_thread(self._delete_blocking)

    def _delete_blocking(self) -> None:
        """
        Blocking implementation of workspace deletion.
        
        This runs in a thread pool to avoid blocking the event loop.
        """
        if self._path is None:
            return

        shutil.rmtree(self._path)

    def __repr__(self) -> str:
        return f"Workspace(chat_id={self.chat_id}, path={self._path})"


class WorkspaceManager:
    """
    Manages workspace lifecycle for all users.
    
    Responsibilities:
    - Creating workspaces on first user connection
    - Tracking active workspaces
    - Cleaning up workspaces when sessions expire
    """

    def __init__(self, config: Config) -> None:
        """
        Initialize the workspace manager.
        
        Args:
            config: Application configuration
        """
        self._config = config
        
        # Workspace root directory from config (resolved to absolute path)
        self.root = Path(config.jaato.workspace_path).resolve()
        
        # Template paths (root .env and .jaato/)
        self.template_env = Path(".env").resolve()
        self.template_jaato = Path(".jaato/").resolve()

        # Ensure workspace root exists
        self.root.mkdir(parents=True, exist_ok=True)

    def get_workspace(self, chat_id: int) -> Workspace:
        """
        Get a Workspace instance for a user.
        
        Note: This doesn't create the workspace - call workspace.create()
        to initialize it on first use.
        
        Args:
            chat_id: Telegram user's chat ID
            
        Returns:
            Workspace instance (may not exist yet)
        """
        return Workspace(
            chat_id=chat_id,
            root=self.root,
            template_env=self.template_env,
            template_jaato=self.template_jaato,
        )

    async def cleanup_workspace(self, chat_id: int) -> None:
        """
        Remove a user's workspace directory.
        
        Args:
            chat_id: Telegram user's chat ID
        """
        workspace = self.get_workspace(chat_id)
        
        # Manually set the path since we're not calling create()
        workspace_name = f"user_{chat_id}"
        workspace._path = self.root / workspace_name
        
        await workspace.delete()

    async def cleanup_all_workspaces(self) -> None:
        """Remove all workspace directories (for testing/shutdown)."""
        # This is a destructive operation - use with caution
        if not self.root.exists():
            return

        await asyncio.to_thread(shutil.rmtree, self.root)
        self.root.mkdir(exist_ok=True)

"""
Agent response tracker to detect file mentions in agent responses.

Extracts file names from agent text using regex patterns and queues
files for sending to Telegram users after the agent completes its turn.
"""

import logging
import re
from typing import TYPE_CHECKING, List, Set

from jaato_client_telegram.workspace_tracker import WorkspaceFileTracker
from pathlib import Path


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    pass


class AgentResponseTracker:
    """
    Track agent responses and detect file mentions.

    When agent mentions a file, we queue it for sending after the turn
    completes. This ensures we only send files the agent intends for
    the user, not intermediate artifacts.
    """

    def __init__(self, file_tracker: WorkspaceFileTracker):
        """
        Initialize agent response tracker.

        Args:
            file_tracker: WorkspaceFileTracker instance to check file existence
        """
        self._file_tracker = file_tracker
        # Queue files mentioned during current turn: chat_id -> set of file paths
        self._queued_files: dict[int, Set[str]] = {}

    async def process_agent_response(self, chat_id: int, response_text: str) -> List[str]:
        """
        Process agent response and detect file mentions.

        Extracts file names from the response and queues them for sending.
        Deduplicates mentions within the same turn.

        Args:
            chat_id: Telegram chat ID (for workspace identification)
            response_text: The agent's response text

        Returns:
            List of file paths mentioned in the response
        """
        # Extract file names using regex patterns
        mentioned_files = self._extract_file_names(response_text)

        if mentioned_files:
            # Queue the files for this chat
            if chat_id not in self._queued_files:
                self._queued_files[chat_id] = set()

            # Add new files to the queue (deduplicate)
            mentioned_files_set = set(mentioned_files)
            new_mentions = mentioned_files_set - self._queued_files[chat_id]
            self._queued_files[chat_id].update(mentioned_files_set)

            if new_mentions:
                logger.info(f"Queued new file mentions for chat {chat_id}: {new_mentions}")
            else:
                logger.debug(f"File mentions already queued for chat {chat_id}")

        return mentioned_files

    async def get_queued_files(self, chat_id: int) -> Set[str]:
        """
        Get all queued file mentions for a chat.

        Args:
            chat_id: Telegram chat ID

        Returns:
            Set of file paths queued for sending
        """
        return self._queued_files.get(chat_id, set()).copy()

    async def clear_queue(self, chat_id: int) -> None:
        """
        Clear the file mention queue for a chat.

        Called after files have been sent to prepare for the next turn.

        Args:
            chat_id: Telegram chat ID
        """
        if chat_id in self._queued_files:
            logger.debug(f"Cleared file queue for chat {chat_id}")
            del self._queued_files[chat_id]

    def _extract_file_names(self, text: str) -> List[str]:
        """
        Extract potential file names from text using regex.

        Patterns to detect:
        - "created file.csv" style
        - "generated data.json" style
        - File paths like "/workspaces/user_123/data.csv"
        - Backtick-wrapped filenames like `docker_containers_status.txt`
        - File extensions (csv, json, yaml, yml, txt, py, js, ts)

        Args:
            text: The agent's response text

        Returns:
            List of unique file names found (case-insensitive)
        """
        # Normalize text to handle hidden characters (zero-width joiner, etc.)
        # Replace problematic Unicode characters that might break matching
        normalized_text = text
        # Remove zero-width joiner and other invisible Unicode characters
        normalized_text = re.sub(r'[\u200B-\u200D\uFEFF]', '', normalized_text)

        patterns = [
            # Backtick-wrapped filenames (most precise): `file.txt`
            r'`([^`]+?\.(?:csv|json|yaml|yml|txt|py|js|ts|md|html|css|xml))`',
            # "created file.txt" style - match created + filename
            r'(?:created|saved|wrote|generated)\s+([a-zA-Z0-9_\-./]+\.(?:csv|json|yaml|yml|txt|py|js|ts|md|html|css|xml))',
            # Direct filename mentions with known extensions (but not in path context)
            r'(?:^|\s)([a-zA-Z0-9_\-./]+\.(?:csv|json|yaml|yml|txt|py|js|ts|md|html|css|xml))(?:\s|[,.;!?]|$)',
            # Absolute/relative paths with directories
            r'(?:[~./][a-zA-Z0-9_\-./]*/)+[a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+',
        ]

        files_found = []

        for pattern in patterns:
            matches = re.findall(pattern, normalized_text, re.I | re.M)
            # Strip leading/trailing whitespace and quotes from matches
            cleaned_matches = [m.strip().strip('\'"') for m in matches if m.strip()]
            files_found.extend(cleaned_matches)

        # Remove duplicates while preserving order
        seen = set()
        unique_files = []
        for f in files_found:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)

        return unique_files

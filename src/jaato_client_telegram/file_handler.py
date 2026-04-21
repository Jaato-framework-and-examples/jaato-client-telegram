"""
File handler for sending files to Telegram.

Handles sending files based on size:
- Small files (< link_threshold_kb) are ATTACHED as document attachments
- Large files (>= link_threshold_kb) are SHARED via Telegram file hosting URLs
"""

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import Message

from jaato_client_telegram.config import FileSharingConfig

if TYPE_CHECKING:
    pass  # Import SDK types when needed


logger = logging.getLogger(__name__)


class FileHandler:
    """Handles sending files to Telegram with size-based delivery strategy."""

    def __init__(self, config: FileSharingConfig):
        """
        Initialize the file handler.

        Args:
            config: File sharing configuration
        """
        self._config = config

    async def send_file(
        self,
        file_path: Path,
        message: Message
    ) -> bool:
        """
        Send a file to Telegram (ATTACH or SHARE based on size).

        Args:
            file_path: Path to the file to send
            message: Telegram message to reply to

        Returns:
            True if file was successfully sent, False otherwise
        """
        if not self._config.enabled:
            logger.debug("File sharing is disabled, skipping file send")
            return False

        try:
            if not file_path.exists():
                logger.warning(f"File does not exist: {file_path}")
                return False

            # Use the Path object directly
            file_path_obj = file_path
            if not file_path_obj.exists():
                logger.warning(f"File does not exist: {file_path}")
                return False

            # Check file size
            file_size_bytes = file_path_obj.stat().st_size
            file_size_kb = file_size_bytes / 1024
            file_size_mb = file_size_bytes / (1024 * 1024)
            
            if file_size_mb > self._config.max_file_size_mb:
                logger.warning(
                    f"File too large: {file_size_mb:.2f}MB > "
                    f"{self._config.max_file_size_mb}MB limit"
                )
                await message.answer(
                    f"⚠️ Generated file is too large to send "
                    f"({file_size_mb:.2f}MB > {self._config.max_file_size_mb}MB limit).\n"
                    f"File path: `{file_path}`",
                    parse_mode="HTML"
                )
                return False

            # Check file extension
            file_ext = file_path_obj.suffix.lower()
            if file_ext not in self._config.allowed_extensions:
                logger.warning(
                    f"File extension not allowed: {file_ext} "
                    f"(allowed: {', '.join(self._config.allowed_extensions)})"
                )
                await message.answer(
                    f"⚠️ File type not supported: `{file_ext}`\n"
                    f"File path: `{file_path}`",
                    parse_mode="HTML"
                )
                return False

            # Determine how to send the file based on size
            if file_size_kb < self._config.link_threshold_kb:
                # Small file - send as document attachment
                await self._send_as_document(file_path_obj, message)
            else:
                # Large file - send using Telegram's file hosting URL
                await self._send_as_link(file_path_obj, message)

            logger.info(f"Successfully sent file: {file_path}")
            return True

        except Exception as e:
            logger.exception(f"Error sending file: {e}")
            await message.answer(
                f"❌ Error sending file: {e}"
            )
            return False

        except Exception as e:
            logger.exception(f"Error handling file event: {e}")
            await message.answer(
                f"❌ Error sending generated file: {e}"
            )
            return False

    async def _send_as_document(self, file_path: Path, message: Message) -> None:
        """
        Send file as a Telegram document attachment.

        Used for small files (< link_threshold_kb).

        Args:
            file_path: Path to the file
            message: Telegram message to reply to
        """
        # Get filename without full path for security
        filename = file_path.name

        # Send as document
        await message.answer_document(
            document=open(file_path, "rb"),
            filename=filename,
            caption=f"📄 Generated file: `{filename}`",
            parse_mode="HTML"
        )

    async def _send_as_link(self, file_path: Path, message: Message) -> None:
        """
        Send file using Telegram's file hosting URL.

        Used for large files (>= link_threshold_kb).
        The file is uploaded to Telegram, then the URL is extracted and sent to the user.

        Args:
            file_path: Path to the file
            message: Telegram message to reply to
        """
        # Get filename without full path for security
        filename = file_path.name

        # Upload file to Telegram first
        uploaded_msg = await message.answer_document(
            document=open(file_path, "rb"),
            filename=filename,
            caption=f"📄 Uploading file: `{filename}`...",
            parse_mode="HTML"
        )

        # Extract the file URL from the uploaded document
        if uploaded_msg.document:
            file_url = uploaded_msg.document.get_url()
            
            # Send follow-up message with the download link
            await message.answer(
                f"📄 Generated file: `{filename}`\n\n"
                f"🔗 Download link: {file_url}",
                parse_mode="HTML"
            )
        else:
            # Fallback if document URL not available
            await message.answer(
                f"⚠️ File uploaded but could not get download URL.\n"
                f"File: `{filename}`",
                parse_mode="HTML"
            )

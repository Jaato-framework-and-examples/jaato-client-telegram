# Workspace Isolation Feature

## Overview

Implemented complete filesystem-level isolation for each Telegram user by creating per-user workspace directories. Each user now has their own isolated `.env` and `.jaato/` folders, ensuring complete segregation of configuration, memories, waypoints, and templates.

## What Changed

### New Files

1. **`src/jaato_client_telegram/workspace.py`**
   - `Workspace` class: Represents a user's isolated workspace directory
   - `WorkspaceManager` class: Manages lifecycle of all user workspaces
   - Handles creation, copying of templates, and cleanup

### Modified Files

1. **`src/jaato_client_telegram/session_pool.py`**
   - Added `WorkspaceManager` dependency
   - Modified `get_client()` to create workspace directory on first connection
   - Passes `workspace_path` parameter to `IPCRecoveryClient`
   - Modified `remove_client()` to cleanup workspace directory
   - Added `workspace_path` field to `SessionInfo` dataclass

2. **`src/jaato_client_telegram/bot.py`**
   - Creates `WorkspaceManager` instance
   - Passes it to `SessionPool` during initialization

3. **`tests/test_workspace.py`** (NEW)
   - Comprehensive tests for workspace creation, deletion, and isolation
   - Tests for multiple users with independent workspaces

4. **`tests/test_client.py`**
   - Updated existing tests to include `WorkspaceManager` dependency

## Architecture

### Workspace Directory Structure

```
jaato-client-telegram/
├── .env                          # Root template (copied to each workspace)
├── .jaato/                       # Root template (copied to each workspace)
│   ├── templates/
│   ├── memory/
│   └── waypoints/
└── workspaces/                   # Created automatically
    ├── user_123456789/          # Private chat: user_<user_id>
    │   ├── .env
    │   └── .jaato/
    │       ├── memory/
    │       ├── waypoints/
    │       └── templates/
    ├── user_987654321/          # Another user's private chat
    │   ├── .env
    │   └── .jaato/
    └── user_556677889/          # Group chat: user_<chat_id>
        ├── .env                 # Shared by ALL group members
        └── .jaato/
            ├── memory/          # Collective group memory
            ├── waypoints/
            └── templates/
```

### How It Works

1. **Private Chat Messages:**
   - System uses `user_id` as workspace key
   - Workspace: `workspaces/user_<user_id>/`
   - Each user has ONE private workspace
   - Only that user can access it

2. **Group Chat Messages:**
   - System uses `chat_id` as workspace key
   - Workspace: `workspaces/user_<chat_id>/`
   - ALL members of the group SHARE the same workspace
   - Creates collective memory and context for the group
   - Any group member can continue conversations started by others

3. **First Message in Context:**
   - System checks if workspace exists for the key (user_id or composite)
   - If not, creates new workspace directory
   - Copies root `.env` to workspace
   - Copies root `.jaato/` directory to workspace
   - Initializes `IPCRecoveryClient` with `workspace_path` parameter

4. **Subsequent Messages:**
   - Workspace already exists
   - SDK client runs from workspace directory
   - All file operations (memories, waypoints, templates) are isolated

3. **Session Cleanup:**
   - When user sends `/reset` command
   - When session exceeds idle timeout (default: 60 minutes)
   - When session is evicted due to capacity limits
   - On bot shutdown
   - Workspace directory is completely removed

## Benefits

### Complete Isolation
- **Private chats**: Each user has their own `.env` file and `.jaato/` directory
- **Group chats**: Each group has a shared `.env` and `.jaato/` directory
- Memories, waypoints, and templates are context-appropriate:
  - Private: Personal to each user
  - Group: Shared collective memory for collaboration

### Privacy & Collaboration
- User A's private conversations stay private (isolated workspace)
- Group X has its own collective memory (separate from Group Y)
- Group members can build on each other's questions and context
- No cross-contamination between private and group contexts

### Resource Management
- Automatic cleanup prevents disk space bloat
- Idle sessions are removed after configurable timeout
- Maximum concurrent sessions prevents resource exhaustion

## Configuration

No additional configuration required. Workspaces are automatically managed:

- **Workspace root:** `./workspaces/` (relative to bot working directory)
- **Template source:** Root `.env` and `.jaato/` directories
- **Cleanup timeout:** Controlled by `session.idle_timeout_minutes` in config

## Testing

All workspace functionality is tested:

```bash
# Run workspace-specific tests
pytest tests/test_workspace.py -v

# Run all tests
pytest tests/ -v
```

Test coverage:
- ✅ Workspace creation with file copying
- ✅ Workspace deletion
- ✅ Multiple users with isolated workspaces
- ✅ Workspace manager cleanup
- ✅ Workspace naming scheme

## Backward Compatibility

This change is **backward compatible**:
- Existing installations will automatically create workspaces on first user connection
- Root `.env` and `.jaato/` serve as templates
- No configuration changes required
- Existing behavior is preserved (sessions still work the same way)

## Implementation Details

### Async File I/O
All file operations run in thread pools via `asyncio.to_thread()` to avoid blocking the event loop:

```python
await asyncio.to_thread(self._create_blocking)
await asyncio.to_thread(self._delete_blocking)
```

### IPCRecoveryClient Integration
The SDK client accepts a `workspace_path` parameter:

```python
client = IPCRecoveryClient(
    socket_path=self._config.socket_path,
    auto_start=self._config.auto_start,
    env_file=".env",  # Relative to workspace
    workspace_path=workspace.path,  # Absolute path to workspace
)
```

This ensures the SDK client runs from the workspace directory, making all file operations relative to that workspace.

## Future Enhancements

Potential improvements for Phase 2:
- Configurable workspace root path via YAML
- Workspace size quotas per user
- Backup/export of user workspaces
- Workspace migration between bot instances

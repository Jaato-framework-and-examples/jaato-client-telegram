# File Watching Implementation

## Overview

This implementation bridges the gap between jaato agent file creation and Telegram bot file sharing by subscribing to jaato-server's workspace panel events.

## Problem Statement

**The Disconnect:**
- **Agent side**: Creates files on jaato server filesystem (in workspace directory)
- **Bot side**: Telegram bot's `FileHandler` only receives `file.generated` events from jaato SDK
- **Result**: Bot has no way to know when agent creates a file

**Example:**
```
User: "Create a CSV file"
Agent: (thinking...) "I'll create a CSV file for you."
       [Writes file to: /workspaces/user_X/data.csv]

Bot: [Waiting for file.generated event...]
       [Event never comes because agent just writes to disk]
```

## Solution Architecture

Instead of implementing a separate file watcher in the bot workspace, we leverage the **existing file watching system in jaato-server**:

### jaato-server File Watching System

1. **Watches workspace directories** for new/modified files
2. **Detects file changes** (created, updated, deleted)
3. **Sends workspace panel events** via IPC to connected clients



### Our Implementation

```python
# Subscribe to workspace panel events from jaato-server
async def on_workspace_event(event):
    """Handle workspace panel event from jaato-server."""
    
    if event.type == "workspace.file.added":
        # New file created - track it
        track_file(event.path, event.workspace_id)
        
    elif event.type == "workspace.file.updated":
        # File modified - update tracking
        update_file_tracking(event.path, event.workspace_id)
        
    elif event.type == "workspace.file.deleted":
        # File deleted - remove from tracking
        remove_file_tracking(event.path, event.workspace_id)
```

## Components

### 1. WorkspaceFileTracker (`src/jaato_client_telegram/workspace_tracker.py`)

A new module to track files across workspaces:

```python
class WorkspaceFileTracker:
    """Track files created by agent across workspaces."""
    
    def __init__(self, workspace_manager):
        self._workspace_manager = workspace_manager
        self._tracked_files: Dict[str, Set[str]] = {}
        self._mentioned_files: Dict[str, Set[str]] = {}
    
    async def add_file(self, workspace_id: str, file_path: str) -> bool:
        """Track a new file in a workspace."""
        if workspace_id not in self._tracked_files:
            self._tracked_files[workspace_id] = set()
        
        if file_path not in self._tracked_files[workspace_id]:
            self._tracked_files[workspace_id].add(file_path)
            return True
        return False
    
    async def is_file_known(self, workspace_id: str, file_path: str) -> bool:
        """Check if a file is being tracked in a workspace."""
        return file_path in self._tracked_files.get(workspace_id, set())
```

### 2. WorkspaceEventSubscriber (`src/jaato_client_telegram/workspace_event_subscriber.py`)

Subscribes to workspace.panel events via jaato-server IPC and maintains file tracking state via WorkspaceFileTracker.

```python
class WorkspaceEventSubscriber:
    """Subscribe to workspace panel events from jaato-server via IPC."""
    
    def __init__(self, ipc_backend, workspace_tracker: WorkspaceFileTracker):
        self._ipc_backend = ipc_backend
        self._workspace_tracker = workspace_tracker
    
    async def start(self) -> None:
        """Start subscribing to workspace panel events."""
        # Subscribe to incremental file changes
        await self._ipc_backend.subscribe_to_events(
            "workspace.panel",
            self._on_workspace_event
        )
    
    async def _on_workspace_event(self, event):
        """Handle workspace panel event from jaato-server."""
        
        event_type = getattr(event, "type", None)
        workspace_id = getattr(event, "workspace_id", None)
        
        if event_type == "workspace.file.added":
            file_path = getattr(event, "path", None)
            logger.info(f"File added: {workspace_id} -> {file_path}")
            await self._workspace_tracker.add_file(workspace_id, file_path)
        
        elif event_type == "workspace.file.updated":
            # File updated - just log
            logger.info(f"File updated: {workspace_id} -> {getattr(event, 'path', None)}")
        
        elif event_type == "workspace.file.deleted":
            file_path = getattr(event, "path", None)
            logger.info(f"File deleted: {workspace_id} -> {file_path}")
            await self._workspace_tracker.remove_file(workspace_id, file_path)
```

### 3. AgentResponseTracker (`src/jaato_client_telegram/agent_response_tracker.py`)

Processes agent responses and detects file mentions using regex.

```python
class AgentResponseTracker:
    """Track agent responses and detect file mentions."""
    
    def __init__(self, file_tracker: WorkspaceFileTracker):
        self._file_tracker = file_tracker
    
    async def process_agent_response(self, chat_id: str, response_text: str) -> List[str]:
        """Process agent response and detect file mentions."""
        files = self._extract_file_names(response_text)
        
        if files:
            logger.info(f"Detected file mentions: {files}")
        
        return files
    
    async def _extract_file_names(self, text: str) -> List[str]:
        """Extract file names using regex patterns."""
        patterns = [
            r'created\s+(\w+\.?\w+)',
            r'generated\s+(\w+\.?\w+)',
            r'[\w/]+[\w-]+\.\w+',
            r'[\w/]+\.(csv|json|yaml|yml|txt|py|js|ts)',
        ]
        
        files_found = []
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            files_found.extend(matches)
        
        return list(set(files_found))
```

### 4. FileHandler Integration (`src/jaato_client_telegram/file_handler.py`)

Updated to check file tracking and skip duplicates:

```python
async def handle_file_event(self, event: dict, message: Message) -> bool:
    """Process a file.generated event."""
    
    # Check if file is already being tracked (from workspace watching)
    if await self._workspace_tracker.is_file_known(event.get("path")):
        logger.info(f"File already tracked via workspace watching: {event.get('path')}")
        return True  # Don't send duplicate
    
    # Get workspace ID from file path
    file_path_parts = Path(file_path).parts
    if len(file_path_parts) >= 2 and file_path_parts[0] == "workspaces":
        try:
            workspace_id = int(file_path_parts[1].split("_")[0])
        except (ValueError, IndexError):
            workspace_id = None
    
    # Then send file normally (checking size, extension, etc.)
    # ... rest of the logic ...
```

### 5. Bot Integration (`bot.py`)

Inject new dependencies and start event subscriber:

```python
from jaato_client_telegram.event_subscriber import WorkspaceEventSubscriber
from jaato_client_telegram.workspace_tracker import WorkspaceFileTracker
from jaato_client_telegram.agent_response_tracker import AgentResponseTracker

# In create_bot_and_dispatcher():
workspace_tracker = WorkspaceFileTracker(workspace_manager)
event_subscriber = WorkspaceEventSubscriber(ipc_backend)
agent_response_tracker = AgentResponseTracker(workspace_tracker)

# Inject into dispatcher context
dp["workspace_tracker"] = workspace_tracker
dp["event_subscriber"] = event_subscriber
dp["agent_response_tracker"] = agent_response_tracker
```

## Event Flow

### File Creation Detection Flow

```
1. Agent creates file on server filesystem
2. jaato-server detects file (via filesystem watch)
3. jaato-server sends workspace.file.added event via IPC
4. WorkspaceEventSubscriber receives event via IPC
5. WorkspaceFileTracker tracks the file
6. Agent mentions file in response
7. AgentResponseTracker detects mention
8. File is marked as mentioned in conversation
9. User sends message to bot
10. FileHandler checks tracking
11. If tracked and mentioned → Skip sending, log as "tracked via workspace watching"
```

### Benefits

✅ **No duplicate file events** - FileHandler checks tracking before sending
✅ **Accurate file detection** - Uses actual filesystem state from jaato-server
✅ **Cross-referencing** - Can relate agent responses to workspace files
✅ **No external watcher needed** - Leverages existing jaato-server infrastructure
✅ **Workspace-aware** - Tracks files per workspace (per-user isolation)

## Configuration

No additional configuration needed - uses existing jaato-server workspace panel events.

## Testing

To test file watching:

1. Ensure jaato-server workspace panel is enabled (Ctrl+W in TUI)
2. Create a file in a workspace
3. Agent mentions the file in a response
4. Bot should detect the file is tracked and send it
5. Verify no duplicate file events are sent

## Key Design Decisions

1. **File path parsing**: Parse workspace_id from file path format "workspaces/user_X/file.ext"
2. **Graceful fallback**: If parsing fails, treat as workspace-level (no tracking)
3. **Regex patterns**: Four patterns to detect different file reference styles
4. **Skip duplicates**: Check tracking before sending to avoid re-sending

## Notes

- Workspace panel must be enabled in jaato-tui (`Ctrl+W`) for file watching to work
- Works with existing `file.generated` event flow
- Minimal code changes - mostly new modules, FileHandler modification
- Assumes jaato-server IPC backend exposes subscribe_to_events with event names

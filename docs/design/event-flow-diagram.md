# Event Flow Visualization

## Complete Turn with Permission Request

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        TELEGRAM MESSAGE                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  User: Check Docker status                                                │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ Agent Response (edited progressively)                                 │  │
│  │                                                                       │  │
│  │ [1] I'll check the status of your Docker containers for you.         │  │
│  │     ↓ Arrives via AgentOutputEvent(mode="write")                     │  │
│  │     ↓ Buffered to text_buffer                                        │  │
│  │                                                                       │  │
│  │ [2] [FLUSH] mode="flush" received                                    │  │
│  │     ↓ _flush_text_buffer() called                                    │  │
│  │     ↓ Text moved to accumulated_text                                 │  │
│  │     ↓ Message edited to show: "I'll check..."                        │  │
│  │                                                                       │  │
│  │ [3] ▶️ Decision: pending                                             │  │
│  │     Tool: cli_based_tool                                             │  │
│  │     ↓ Arrives via PermissionInputModeEvent                           │  │
│  │     ↓ Placeholder added to accumulated_text NOW                      │  │
│  │     ↓ Message edited to show placeholder                             │  │
│  │     ↓ Tracked in permissions_added_to_text                           │  │
│  │                                                                       │  │
│  │ [4] Here's the status of your Docker containers:                     │  │
│  │     ↓ Arrives via AgentOutputEvent(mode="write") after tool executes │  │
│  │     ↓ Buffered to text_buffer                                        │  │
│  │     ↓ Flushed on mode="flush" or turn completion                     │  │
│  │                                                                       │  │
│  │ [... rest of output ...]                                             │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ SEPARATE MESSAGE: Interactive Permission UI                          │  │
│  │                                                                       │  │
│  │ 🔒 Permission Required                                               │  │
│  │                                                                       │  │
│  │ Tool: cli_based_tool                                                 │  │
│  │ Command: docker ps -a                                                │  │
│  │                                                                       │  │
│  │ [Yes] [No] [Cancel]                                                  │  │
│  │     ↓ Sent as separate message with inline keyboard                   │  │
│  │     ↓ User clicks "Yes"                                               │  │
│  │     ↓ PermissionResponseRequest sent to SDK                          │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Buffer State Transitions

```
INITIAL STATE:
┌─────────────────────────────────────────────────────────┐
│ text_buffer:           []                               │
│ accumulated_text:      ""                               │
│ tool_call_buffer:      []                               │
│ permissions_added_to_text: {}                          │
└─────────────────────────────────────────────────────────┘

[1] AgentOutputEvent(mode="write", text="I'll check...")
┌─────────────────────────────────────────────────────────┐
│ text_buffer:           ["I'll check..."]               │
│ accumulated_text:      ""                               │
│ tool_call_buffer:      []                               │
│ permissions_added_to_text: {}                          │
└─────────────────────────────────────────────────────────┘

[2] AgentOutputEvent(mode="flush", text="")
┌─────────────────────────────────────────────────────────┐
│ text_buffer:           []  ← CLEARED                    │
│ accumulated_text:      "I'll check..."  ← MOVED HERE   │
│ tool_call_buffer:      []                               │
│ permissions_added_to_text: {}                          │
│ Message edited to show accumulated_text                 │
└─────────────────────────────────────────────────────────┘

[3] PermissionInputModeEvent(request_id="perm-001")
┌─────────────────────────────────────────────────────────┐
│ text_buffer:           []                               │
│ accumulated_text:      "I'll check...\n\n▶️ Decision:   │
│                        pending\n\nTool: cli_based_tool"│
│ tool_call_buffer:      [event]                         │
│ permissions_added_to_text: {"perm-001"}  ← TRACKED     │
│ Message edited to show accumulated_text                 │
│ Separate message sent with inline keyboard              │
└─────────────────────────────────────────────────────────┘

[4] AgentOutputEvent(mode="write", text="Here's the status...")
┌─────────────────────────────────────────────────────────┐
│ text_buffer:           ["Here's the status..."]        │
│ accumulated_text:      "I'll check...\n\n▶️ Decision:   │
│                        pending...\n\nTool: cli_based_tool"│
│ tool_call_buffer:      [permission_event]              │
│ permissions_added_to_text: {"perm-001"}                │
└─────────────────────────────────────────────────────────┘

[5] TurnCompletedEvent()
┌─────────────────────────────────────────────────────────┐
│ _flush_all_buffers() called:                            │
│   1. _flush_text_buffer():                              │
│      - "Here's the status..." added to accumulated_text │
│   2. _flush_tool_call_buffer():                          │
│      - "perm-001" SKIPPED (already in                   │
│        permissions_added_to_text)                       │
│      - Any non-permission tools added                    │
│                                                             │
│ Final accumulated_text:                                   │
│   "I'll check...\n\n▶️ Decision: pending\n\nTool:        │
│    cli_based_tool\n\nHere's the status..."               │
│                                                             │
│ Permission placeholder remains in ORIGINAL position ✅    │
└─────────────────────────────────────────────────────────┘
```

---

## Why Immediate Placement Works

**WITHOUT Immediate Placement (WRONG):**
```
1. text_buffer: ["I'll check..."]
2. mode="flush" → accumulated_text: "I'll check..."
3. permission arrives → tool_call_buffer: [permission]
4. tool executes
5. more text: ["Here's the status..."]
6. turn completes → _flush_all_buffers()
   - Flush text: "I'll check... Here's the status..."
   - Flush tools: "▶️ Decision: yes\n\nTool: cli_based_tool"
7. Final: "I'll check... Here's the status... ▶️ Decision: yes"
         ↑ Permission at END! Wrong position ❌
```

**WITH Immediate Placement (CORRECT):**
```
1. text_buffer: ["I'll check..."]
2. mode="flush" → accumulated_text: "I'll check..."
3. permission arrives → accumulated_text: "I'll check... ▶️ Decision: pending"
                     → permissions_added_to_text: {"perm-001"}
4. tool executes
5. more text: ["Here's the status..."]
6. turn completes → _flush_all_buffers()
   - Flush text: "I'll check... ▶️ Decision: pending\n\nHere's the status..."
   - Flush tools: SKIP "perm-001" (already added)
7. Final: "I'll check... ▶️ Decision: yes\n\nHere's the status..."
         ↑ Permission in ORIGINAL position ✅
```

---

## Event Sequence Timeline

```
TIME    EVENT                          ACTION                          MESSAGE STATE
────────────────────────────────────────────────────────────────────────────────────
T0      User message                   Send "Check Docker status"      [User: Check...]
                                                                                    
T1      AgentOutput                    Buffer: "I'll check..."         [Agent Response]
        (mode="write")                  text_buffer = ["I'll check"]    (empty)
                                                                                    
T2      AgentOutput                    Buffer: "I'll check the..."      [Agent Response]
        (mode="append")                 text_buffer = ["I'll check the"] (empty)
                                                                                    
T3      AgentOutput                    FLUSH                           [Agent Response]
        (mode="flush")                  accumulated_text = "I'll check" [I'll check]
                                        text_buffer = []
                                        Edit message ✅
                                                                                    
T4      PermissionInputMode            Add placeholder NOW             [Agent Response]
        (request_id="perm-001")         accumulated_text +=            [I'll check...]
                                        "▶️ Decision: pending..."      [▶️ Decision: pending]
                                        Track: permissions_added       [Tool: cli_based_tool]
                                        Edit message ✅
                                                                                    
                                                                        [SEPARATE MSG]
                                                                        [🔒 Permission]
                                                                        [Yes] [No]
                                                                                    
T5      User clicks "Yes"              Send PermissionResponse         [Agent Response]
                                        to SDK                          [I'll check...]
                                                                        [▶️ Decision: pending]
                                                                        [Tool: cli_based_tool]
                                                                                    
                                                                        [🔒 Permission]
                                                                        [Yes] [No]
                                                                                    
T6      ToolCallStart                  Buffer tool event               [same]
        (cli_based_tool)               tool_call_buffer = [start]       
                                                                                    
T7      ToolCallEnd                    Buffer tool event               [same]
        (cli_based_tool)               tool_call_buffer = [start, end]  
                                                                                    
T8      AgentOutput                    Buffer: "Here's the status..."   [same]
        (mode="write")                  text_buffer = ["Here's..."]     
                                                                                    
T9      AgentOutput                    FLUSH                           [Agent Response]
        (mode="flush")                  accumulated_text +=            [I'll check...]
                                        "Here's the status..."          [▶️ Decision: pending]
                                        text_buffer = []               [Tool: cli_based_tool]
                                                                        [Here's the status...]
                                        Edit message ✅
                                                                                    
T10     TurnCompleted                  _flush_all_buffers():            [Agent Response]
                                        - text_buffer already empty     [I'll check...]
                                        - tool_call_buffer:            [▶️ Decision: pending]
                                          SKIP "perm-001" ✅           [Tool: cli_based_tool]
                                          (already in permissions_added) [Here's the status...]
                                        Final message complete ✅
```

---

## Critical Design Insight

**The Problem:** 
We need to add tool information to the message, but we can't know the final outcome until the turn completes.

**The Solution:**
Add permission placeholders IMMEDIATELY when `permission.input_mode` arrives, then skip them during final flush.

**Why This Works:**
1. Permissions appear in correct position during streaming ✅
2. Permissions stay in correct position after turn completes ✅
3. No duplication (tracked in `permissions_added_to_text`) ✅
4. Works for multiple permissions in one turn ✅

---

## Comparison with TUI Client

**TUI Client (jaato):**
- Can render incrementally at any position
- Updates tool status in place
- No "message editing" constraints

**Telegram Client:**
- Must edit entire message at once
- Cannot update arbitrary sections
- Needs placeholders for future content
- Separate messages for interactive UI

**Key Difference:**
Telegram client must "reserve space" for permissions by adding placeholders immediately, whereas TUI can update the display at any time.

---

## Conclusion

The rendering pipeline correctly implements the SDK event protocol by:
1. ✅ Detecting `mode="flush"` as text-to-tool transition
2. ✅ Buffering and displaying text in correct order
3. ✅ Placing permission placeholders at correct position immediately
4. ✅ Avoiding duplication via tracking set
5. ✅ Supporting multiple flush cycles and permissions per turn

The implementation is protocol-compliant and handles Telegram's constraints appropriately.

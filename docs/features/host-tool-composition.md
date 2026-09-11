# Host-tool composition — `ctx.call_tool`

A host tool can invoke **another host tool** by name, so tools compose instead of
reimplementing each other. Added in `feat(host-tools): ctx.call_tool`.

## API

```python
async def call_tool(self, name: str, args: dict | None = None) -> dict
```

From inside a tool's `execute(args, ctx)`:

```python
async def execute(args, ctx):
    saved = await ctx.call_tool("download_file", {"url": args["url"]})
    if "error" in saved:
        return saved
    await ctx.call_tool("send_to_telegram", {"text": f"saved to {saved['path']}"})
    return {"ok": True, "path": saved["path"]}
```

- Returns the callee's **result dict** (a non-dict result is wrapped as `{"result": ...}`).
- **Never raises.** Every failure comes back as `{"error": "..."}`:
  - unknown tool (the message lists the available names),
  - a missing **required** argument (validated from the callee's `TOOL_SCHEMA` before dispatch),
  - an exception inside the callee (surfaced as `"<name> failed: <err>"`),
  - the recursion guard tripping.

## Scope — host tools only

`call_tool` reaches **host tools** — this bot's built-ins (`send_to_telegram`,
`show_image`, `download_file`, …) and user-installed tools. It does **not** reach
server-side model tools such as `web_search`: those run in the AppArmor-confined
runner, on the other side of the WebSocket. Composing a server tool from a host
tool would invert the trust boundary (unconfined bot → confined runner) and is
deliberately out of scope. If a composed tool genuinely needs a server tool, build
it runner-side (a `notebook_execute` flow, where `tools.<name>()` already reaches
every registered tool through the session's permission-checked executor), or as a
server plugin.

## Recursion guard

Depth is tracked in a `ContextVar` (`_CALL_DEPTH`), so it propagates through a
nested tool's own executor across `await`s. A chain deeper than `MAX_CALL_DEPTH`
(4) returns an error instead of recursing; depth resets between independent
top-level tool calls.

## How it's wired

`SessionPool._assemble_host_tools` builds one shared registry per session —
`{name: {"handler": async (args)->dict, "schema": dict}}` covering every built-in
and installed tool — and threads it into each dynamic tool's `ToolContext` via
`make_executor(tool_registry=...)`. The registry is fully populated before any tool
runs, so `ctx.call_tool` can reach every peer at call time (`host_tool_loader.py`,
`session_pool.py`).

## Notes / open questions

- The registry is **inclusive**: a tool can call any host tool, including the meta
  ones (`register_tool` / `install_tool`). If that's too broad, exclude them from
  the registry in `_assemble_host_tools` (one filter) — the composition path itself
  is unchanged.
- This is "case 1" of the composition proposal (host → host). "Case 2" (host tool →
  server model tool) is intentionally not built here; see **Scope** above.

Tests: `tests/test_call_tool.py`.

# Session-startup service checklist

Ensures long-running host-tool services (e.g. the approval webhook) are running at
the start of **every** session — deterministically, without relying on the model to
remember to start them.

## Problem

Some host tools are long-running services rather than one-shot calls. The approval
webhook, for example, runs an in-process HTTP server that must be up to receive
external approval requests. Previously the agent only started it when explicitly
asked, and a session that re-attached after the server had stopped would silently
have no webhook. "Put it in the persona as an instruction" is unreliable — the model
may not act on a static instruction every time.

We want the *declaration* of "what must be running" to be deterministic, while the
actual start stays an ordinary (idempotent) tool call.

## Solution

Three pieces, plus a manifest:

| Piece | Where | Role |
|-------|-------|------|
| **Manifest** | `<workspace>/.jaato/service_manifest.json` | Source of truth: a list of `{tool, args}` invocation specs. |
| **`service_manifest`** built-in tool | `src/jaato_client_telegram/host_tools.py` (+ wired in `session_pool.py`) | `add` / `remove` / `list` — how the agent (or operator) maintains the manifest. |
| **`service_checklist.py`** prefetch | `runtime/.jaato/scripts/service_checklist.py` | Reads the manifest at session-prep and renders a checklist into the system prompt. |
| **Persona directive** | `{{!py:scripts/service_checklist.py}}` in `runtime/.jaato/agents/telegram_chat.md` | Wires the prefetch into the agent's prompt. |

### Flow

```
agent/operator                prefetch (runner, session-prep)         model (first turn)
─────────────                 ───────────────────────────────         ──────────────────
service_manifest(action=add,  reads service_manifest.json  ───────►  sees "## Session
  tool=approval_webhook,       renders the checklist                    startup checklist"
  args={action: start})   ──►  injects it into the prompt              calls each tool
   writes the manifest                                                 (idempotent start)
```

1. The agent maintains the manifest through the `service_manifest` tool.
2. At the start of every session, the framework runs the `{{!py:...}}` prefetch
   (server-side, during `JaatoSession.configure()`), which reads the manifest and
   emits a "Session startup checklist" section into the system prompt.
3. On its first turn, the model invokes each listed tool. The calls are idempotent
   (e.g. `approval_webhook(action="start")` no-ops/reports if already running), so
   this both starts-if-down and confirms-if-up.

## Why this shape

- **Deterministic where it matters.** The checklist is injected by a prefetch script
  that runs server-side every session — the model is not in the loop deciding
  *whether* the checklist exists. The model only performs the (idempotent) start.
- **Self-healing.** The prefetch re-renders every session, so a service that crashed
  between interactions is restarted on the next message — no separate supervisor.
- **Manifest lives in the workspace `.jaato/`, not `host_tools_dir`.** The prefetch
  runs inside the AppArmor-confined runner, which can read the workspace but *not*
  `host_tools_dir` (deliberately outside the workspace). The `service_manifest` tool
  runs bot-side and writes the workspace path it is configured with.
- **`service_manifest` is a fixed built-in, not an agent-installed dynamic tool.**
  It is core infrastructure, registered the same way as `register_tool` — its schema
  is in `TOOL_SCHEMAS` and its executor is wired in `session_pool` (it needs the
  configured workspace path, like `register_tool` needs the session pool).
- **The directive is mandatory (`{{!py:...}}`, not `{{!py?:...}}`).** A malformed
  manifest or a missing script aborts session creation with a structured error,
  rather than silently producing an agent with no checklist. A *missing or empty*
  manifest is not an error — the prefetch renders nothing.

## Versioning

`.gitignore` tracks `runtime/.jaato/scripts/` — prefetch scripts are **code**
referenced by the tracked personas, so they must travel with the agent definition
(a missing script would abort sessions on a fresh checkout). The manifest
(`service_manifest.json`) stays **ignored**: it is mutable runtime state, managed by
the `service_manifest` tool per deployment.

## Managing the manifest

```text
service_manifest(action="list")
service_manifest(action="add", tool="approval_webhook", args={"action": "start"})
service_manifest(action="remove", tool="approval_webhook")
```

`add` replaces any existing entry for the same tool (so it is idempotent). The
manifest is a plain JSON list of `{"tool": <name>, "args": {...}}`.

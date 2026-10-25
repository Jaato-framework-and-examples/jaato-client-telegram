# Host tools & self-extension

The bot can call **host tools** — functions that run in the *bot* process (not the
confined runner), so they can talk to Telegram, hit the network, touch the host, etc.
There are two kinds:

- **Built-in** tools shipped with the bot: `send_to_telegram`, `show_image`,
  `register_tool`, `service_manifest` (in `src/jaato_client_telegram/host_tools.py`).
- **Dynamic** tools the agent **writes itself** on request, via `register_tool`.

That second kind is the self-extension story: ask the bot for a capability it
doesn't have and it builds the tool, you approve it, and it's callable next turn.

## The contract

A host tool is a single `.py` file with a module-level `TOOL_SCHEMA` dict and an
`async def execute(args, ctx)`:

```python
TOOL_SCHEMA = {
    "name": "reverse_text",              # == the file stem, a lowercase identifier
    "description": "Reverse a given text string.",
    "parameters": { "type": "object", "properties": { ... }, "required": [...] },
}

async def execute(args, ctx):
    return {"result": ...}               # or {"error": ...}
```

`ctx` carries `ctx.bot` (the aiogram `Bot`) and `ctx.chat_id`, so a tool can send
Telegram messages, photos, inline buttons, etc. `ctx.call_tool` composes other host
tools; `ctx.invoke_plugin_tool` runs a framework plugin tool on the daemon (see
[Credentials](#credentials-call-a-framework-plugin-tool-ctxinvoke_plugin_tool)).

## How a dynamic tool gets installed

1. The agent writes the tool to `tool_drafts/<name>.py` in its workspace.
2. It calls `register_tool(name="<name>")`.
3. The user approves running the new code.
4. The **bot** (unconfined) copies the draft into its `host_tools_dir` and registers
   it. The AppArmor-confined runner can never self-install code — `host_tools_dir`
   lives **outside** the workspace by design.

Because of that boundary, a dynamic tool can only ever modify *itself* (a host tool)
— never the bot's own handlers or source. (This is why the bot can't fix a bug that
lives in `callbacks.py`, for instance.)

## The single-poller rule (Telegram-interacting tools)

Telegram allows exactly **one** `getUpdates` poll per bot token, and the main bot
owns it. A tool may freely **send** (`ctx.bot.send_message(...)`, photos, buttons)
but must **never poll** (`bot.get_updates`, `start_polling`, a second polling
`Bot(...)`) — two pollers ⇒ `TelegramConflictError` and the bot stops receiving
everything. To **await** a user's button tap without polling, use `ctx.ask(...)`
(inside `execute`) or `ask_user(bot, chat_id, ...)` (in a later async context such as
a webhook handler) — the main poll routes the tap back. See `approval_webhook`.

## Credentials: call a framework plugin tool (`ctx.invoke_plugin_tool`)

A host tool runs as the **bot's** account, which has no access to the daemon's secret
store (`pass://`, `vault://`) and must not get one. So a tool that needs an API
credential (to poll GitHub in the background, say) must **not** read a secret store,
a token file or a plaintext env var of its own. It asks the daemon to run a
**framework plugin tool** that holds the credential instead:

```python
async def execute(args, ctx):
    r = await ctx.invoke_plugin_tool(
        "service_connector", "call_service",
        {
            "url": "https://api.github.com/repos/o/r/actions/runs?per_page=5",
            "method": "GET",
            "auth": {"type": "bearer", "token_env": "GITHUB_CI_TOKEN"},
        },
        profile="github_ci",           # runtime/.jaato-judge/profiles/github_ci.yaml
    )
    if "error" in r:
        return {"error": f"{r['category']}: {r['error']}"}
    return {"result": r["result"]}
```

The call runs with no session and no model turn (`invoke_plugin_tool`, protocol 1.37,
jaato #1606), in a runner of the bot's workspace configured by `profile` (or by a
`plugin_configs=` block instead — one or the other). The daemon resolves `pass://` /
`vault://` in the profile's `env:` / `plugin_configs` **before** the runner starts,
and the result comes back secret-redacted, so the secret never enters the bot process.
The profile's permission policy and the failure contract apply as in a session.

**The profile.** `profile` names a profile under `runtime/.jaato-judge/profiles/` —
the isolated config root the judge and curator use — **never** `.jaato/profiles/`:
the chat's `subagent` plugin offers every profile there to the model, and a
spawnable profile holding a token hands the model an authenticated client. A
credential profile enables only the tool it needs, maps the secret into `env:`, and
whitelists that tool so an unattended call never asks. The tracked
[`github_ci`](../../runtime/.jaato-judge/profiles/github_ci.yaml) is the pattern:

```yaml
name: github_ci
plugins: ["service_connector(tools:[call_service])"]
env:
  GITHUB_CI_TOKEN: "pass://jaato/github/ci-read-org"   # in the DAEMON's pass store
plugin_configs:
  permission:
    policy: {defaultPolicy: deny, whitelist: {tools: [call_service]}}
```

Name the variable in `auth.token_env`; never put a `pass://` URI in the call's own
`auth` — the confined runner cannot run `pass`, so only the daemon-resolved `env:`
reaches it.

- **Returns, never raises:** `{"result": ...}` when the tool ran and succeeded, else
  `{"error", "category", "result"}`. `category` is `tool_failed` when the tool itself
  failed (a permission denial included), or the reason nothing ran: the daemon's
  (`not_in_surface`, `no_workspace`, `invalid_request`, `host_failed`,
  `runner_unreachable`, `timeout`) or the bot's (`unsupported_protocol` for a daemon
  below protocol 1.37, `unsupported_sdk`, `not_connected`, `disabled`).
- **Permission ASKs** go to the user of the tool's chat as inline buttons (the
  daemon's own options); no answer within `timeout` (default 300 s) denies. Pass
  `on_permission=fn` (`fn(event) -> "y" | "n" | ... | None`) to answer them yourself.
  For an unattended background call, let the profile's permission policy allow the
  tool rather than asking on every poll.
- **Works with the chat detached:** each call dials its own short-lived, session-less
  connection, so a background task keeps working after idle detach.

## Example tools

Reference implementations in [`examples/host_tools/`](../../examples/host_tools/) —
**real tools the bot built on request** (reference only; not auto-loaded):

| Tool | Shows |
|------|-------|
| [`reverse_text.py`](../../examples/host_tools/reverse_text.py) | The minimal tool — bare `TOOL_SCHEMA` + `execute`. |
| [`die_roller.py`](../../examples/host_tools/die_roller.py) | A clean self-contained tool (notation parsing, bounds checks). |
| [`image_search.py`](../../examples/host_tools/image_search.py) | Web search → download → `show_image`. *(Shells out to curl; a production version would use httpx — kept as the bot wrote it.)* |
| [`approval_webhook.py`](../../examples/host_tools/approval_webhook.py) | The showcase: an in-process aiohttp server + `ask_user` (single-poller-safe) + an autostart-friendly `start`/`stop`/`status` lifecycle. |

## Autostart

A host tool that's a long-running service (like `approval_webhook`) can be kept up at
the start of every session via the **service checklist** — a per-agent manifest the
`service_manifest` tool maintains and a prefetch renders into the prompt. See
[service-checklist.md](service-checklist.md) and the example manifest
[`examples/service_manifest.example.json`](../../examples/service_manifest.example.json).

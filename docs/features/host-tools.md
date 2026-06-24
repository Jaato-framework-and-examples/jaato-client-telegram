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
Telegram messages, photos, inline buttons, etc.

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

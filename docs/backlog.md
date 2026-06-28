# Backlog — topics to revisit

Parked topics with enough context to pick up cold later. Each entry: the finding,
why it matters, where in the code, and what to do on revisit.

---

## 1. Host-tool approval shows no code to review (+ unconfined-execution hardening)

**Status:** parked 2026-06-27 · **Priority:** security · raised by Daniel

### The finding (evidence-based)
Installed **host tools run fully unconfined** in the bot process. The bot is a
`systemd --user` service, so a host tool inherits the user's privileges and the
bot's env — confirmed live: `TELEGRAM_BOT_TOKEN`, `JAATO_WS_TOKEN`, `HOME`. That
means a host tool can read `~/.password-store` (the OpenRouter key via gpg),
`~/.ssh`, exfiltrate the bot/WS tokens — anything the user can do.

This is **gated, not open**, by design:
- The AI model runs in the **AppArmor-confined runner**; it can only *draft* a
  tool (`<workspace>/tool_drafts/<name>.py`), not install or run it unconfined.
- `register_tool` is `auto_approve=False` → installation requires explicit user
  approval (`session_pool.py:280 install_and_register_tool`, docstring at :286).
- `host_tools_dir` is **outside the workspace** (`session_pool.py:246`) so the
  confined runner can't write or tamper with installed tool code.
- Re-installing modified code re-prompts; but an approval is otherwise a
  **lasting grant** — an approved tool runs unconfined on every later call.

So the whole security value rests on the user actually **reviewing the draft
before approving**. (Standard extension trust model — like approving a browser
or VS Code extension.)

### The primary problem to fix on revisit
**The approval UI does not show the tool's code**, so the review the model relies
on is impossible — the user is forced to rubber-stamp.
- `permissions.py:168 create_permission_ui` builds the message from
  `event.tool_name` + `event.tool_args` only (e.g. *"Tool: register_tool, name:
  mercadona"*). There is **no** read of `tool_drafts/<name>.py` anywhere in
  `permissions.py` — the Python that will run unconfined is never surfaced.
- Fix direction: for a `register_tool` approval specifically, read
  `<workspace>/tool_drafts/<name>.py` and present the source for review — e.g. an
  expandable `<blockquote>` (it already has wide-content/expandable helpers), or
  send it as a `.py` document, or a syntax-aware summary + "show full code"
  button. Consider showing a **diff** when re-approving a changed tool. Keep
  Telegram's 4096-char limit in mind (drafts can be long — mercadona was 373
  lines); may need truncation + "full code as file".
- Watch-outs: HTML-escape the code; the draft path is workspace-relative; the
  approval is per-install, so the rendered code must be the exact bytes about to
  be copied into `host_tools_dir` (read the draft at approval time, not earlier).

### Secondary: unconfined-execution hardening (evaluate, optional)
- Run the bot under a **dedicated low-privilege user** (not the human's
  `--user` account) so a host tool can't reach `~/.ssh` / `~/.password-store`.
- **Scrub secrets from the bot env** — resolve `TELEGRAM_BOT_TOKEN` /
  `JAATO_WS_TOKEN` via a broker at point-of-use instead of inheriting them in
  `os.environ` where any host tool can read them.
- **Confine host tools too** — a second AppArmor profile for the bot, or run
  each host tool in a subprocess with a restricted profile (tension: host tools
  exist precisely to reach the host the runner can't — scope per tool).
- Consider an "approve for this call only" vs "approve + install permanently"
  distinction, since today one approval is a permanent unconfined grant.

### How it surfaced
Diagnosing a `mercadona` host tool reporting "CLI not installed": it runs in the
bot, whose PATH (systemd default) lacks the nvm bin where the binary lives
(`~/.nvm/.../bin/mercadona`). That established host tools run unconfined in the
bot, which led to this review. (Framework PATH knobs — `plugin_configs.cli.extra_paths`,
workspace `.env`/`session_env` — govern the *runner*, not bot-side host tools.)

### Key references
- `src/jaato_client_telegram/permissions.py:168` — `create_permission_ui` (no code shown)
- `src/jaato_client_telegram/session_pool.py:280` — `install_and_register_tool` (`auto_approve=False`)
- `src/jaato_client_telegram/session_pool.py:246` — `host_tools_dir` outside-workspace boundary
- `src/jaato_client_telegram/host_tools.py` — host-tool execution (unconfined, in-bot)
- `docs/features/host-tools.md` — feature deep-dive

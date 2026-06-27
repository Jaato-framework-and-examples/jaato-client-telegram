# Session lifecycle: why re-engaging an idle chat shows "Resuming… / Initializing…"

When you message the bot after it has been idle for a while, you see two messages
that *look* contradictory:

```
⏳ Resuming your previous conversation…
⏳ Initializing... Loading plugins
```

Both are true. This note explains why, why the bot is built this way on purpose,
and why we deliberately keep it this way.

## 1. Root cause: orphaning, not idleness

The jaato **server** unloads a session **only when it has no attached clients** —
checked on turn-done:

```python
# jaato-server  server/session_manager.py:4201-4202 (as of 2026-06)
if not session.attached_clients:
    self._maybe_unload_session(session.session_id)
```

There is **no idle timer** for an interactive session. A session stays warm as
long as *some* client is attached, no matter how long it sits without a prompt.
(The only `idle_timeout` in the server, `_cascade_client_idle_timeout`, governs
reactor/cascade sub-clients — not this path.)

Proof by contrast: a TUI session "stays warm until you exit the client, however
long you don't prompt it" — because the TUI **stays attached**, so
`attached_clients` is never empty and the orphan check never fires. The bot goes
cold for the opposite reason: it **detaches** an idle chat, `attached_clients`
empties, and the next turn-done unloads the session. **It's the detach, not the
idle.**

## 2. Why the bot detaches idle chats: scale

This is the deliberate design trade. A TUI holds **one** always-attached session —
keeping one runner warm forever is cheap. The bot serves **many** chats and
cannot hold a live WebSocket + a warm runner per chat indefinitely; that would
pin one runner slot per conversation and exhaust the pool. So the bot detaches
idle chats (`__main__.py::_idle_session_cleanup_task`) to free runners for active
ones. **The bot trades per-chat warmth for fleet scale.** Within the warm window
(while still attached) replies are instant; only the first message after the bot
has detached pays the revive.

## 3. Consequence: the cold runner revive

The first message to a detached chat is a **cold runner revive**, in two parts —
hence two messages:

- **"⏳ Initializing... Loading plugins"** — the server re-spawns a runner and
  re-bootstraps its ~30 plugins. The pre-warm pool gives warm *imports*, but a
  returned slot still re-runs `session.bootstrap`, so the plugin re-init is
  expected, not a bug.
- **"⏳ Resuming your previous conversation…"** — `attach_session` →
  `_load_session` restores the full conversation **under the same session id**
  (history, session state, profile, permission whitelist). Your conversation is
  not recreated; it is reloaded.

"Initializing" refers to the **runtime** (runner + plugins) being re-warmed, not
to a fresh conversation — which is why it reads as a contradiction with
"Resuming" even though both are accurate. The bot wording was changed from the
past-tense "🔄 Resumed" to the present-progressive "⏳ Resuming…" so the two read
as one consistent progress arc rather than "done, then starting". An optional
proactive idle-drop notice (`session.idle_notice_text`, quiet-hours guarded) can
tell a chat the moment it is paused, so the later "Resuming…" is expected.

## 4. Resume mechanics: the bot is already on the right (general) path

The bot uses the **general interactive path**: `attach_session` → `_load_session`
(restores history + state + profile + whitelist under the same id) **+ its own
interactive presentation**, which is what makes Telegram permission prompts work.

The server's `resume_session` is **not** for the bot — it is the *headless
adapter*: it synthesizes an API/headless presentation for a clientless reactor
that cannot perform the connect/attach handshake. Forcing that headless
presentation onto the bot would route permissions through the headless whitelist
and break the interactive Telegram prompts. So the bot is **not** missing any
server-side reuse: attach *is* the general path; `resume_session` is the
specialization. There is nothing to "generalize" — the shared core
(`_load_session`) is already shared by both.

## 5. Decision: warm-idle TTL considered and declined (2026-06-27)

A server-side **warm-idle TTL** — defer the orphan-unload by a grace window
(key = `session_id`, bounded by TTL + pool-pressure eviction) — would make the
revive (and the "Initializing" message) vanish for the common "user replies
within the window" case.

It was considered and **declined**. The cold revive is the **accepted cost** of
the bot's detach-on-idle scale model (§2). The bot is kept **as-is**; this
document is the rationale. Bot-side UX softening (the "Resuming…" wording and the
optional idle-drop notice) is in place; the runtime behaviour is intentionally
unchanged.

# Closing the PR-review loop: bots react to review comments automatically

**Status:** design only — nothing built. Raised 2026-07-04: jaato ships a webhook
ingress that links an agent to a webhook, and GitHub can push PR-review events to
a webhook, so a bot could react to reviewer comments on its store PR without a
human relaying them. This note scopes that. The design settled on a **single
push path** built on the server's `webhook` plugin, with a **stateless relay** for
multi-bot routing and **reachability treated as an operator prerequisite**. One
blocking open question remains (idle-wake of a cold session).

## The loop we want to automate

The marketplace works end to end today, but with a **human in the middle of the
review round-trip**:

```
bot opens PR (share_tool)  →  maintainer/Copilot review comments
   →  [HUMAN relays "reviewers commented, go fix it"]      ← this is the manual step
   →  bot addresses feedback  →  re-runs share_tool (updates the same PR)
```

We want to delete the bracketed step. The last two arrows are **already
automated** — `ctx.wake()` (wake an idle bot with text, [[message-flow-chat-pump]])
and `share_tool`, which now updates the existing PR in place. So the feature
reduces to one question:

> **How does "a reviewer commented on PR #N" reach the specific agent session
> that owns PR #N — across many bots on many daemons?**

Everything else is plumbing we already have.

## What we already have

- **`jaato-server` `webhook` plugin** (`shared/plugins/webhook/`) — an inbound
  HTTP ingress with per-route **HMAC-SHA256** verification (route config's
  `secret_header` default is `X-Hub-Signature-256` — GitHub's exact header; this
  plugin is built for GitHub), **TLS**, and **IP-allowlisting with CIDR**. On a
  valid POST it publishes an `EXTERNAL_EVENT` (`source_agent="webhook:<route>"`)
  onto the session's event bus. The agent receives it via
  `subscribeToTasks(["external_event"])` — the plugin's own docs say *"they arrive
  as inline messages, no polling needed"* (`webhook_poll` exists only as a
  fallback). **This is the broker — the server, not a service we stand up.**
- **`ctx.wake` + `share_tool`** — the "address feedback" and "update the PR in
  place" halves are done. A review landing in the session is all that's missing.
- The bot already holds **one persistent outbound WS** to its daemon; server
  events push down it. Receiving an `external_event`-driven turn is the same push
  path as every `AGENT_OUTPUT` today — **no client polling, no bot inbound port**.

## The decisive dead end (why routing needs a relay)

**GitHub webhooks are per-repository, and a PR review is a resource of the *base*
repo.** `pull_request_review` / `pull_request_review_comment` / `issue_comment`
fire on a webhook configured on the **store repo** — never on a contributor bot's
fork. So a bot cannot subscribe to its own PR's reviews from its own fork.

The store is **one repo → one webhook URL**, but the fleet is **N bots across M
daemons** (some bots share a daemon, some don't). One inbound URL must fan out to
the right (daemon, session). **A relay is therefore unavoidable** — this is not a
trade-off, it's forced by GitHub's delivery model.

## The address: (daemon, session)

A review on bot A's PR must reach the exact session whose agent ran `share_tool`.
The complete address is the pair **(daemon endpoint, session)**:

- Two bots on the **same** daemon share the endpoint, differ only in session.
- Bots on **different** daemons differ in both.

Both cases fall out of the same pair, so it is sufficient and complete.

Two properties of that address matter for delivery:

- **The session is a durable id, not a live object.** A human reviews hours/days
  later; by then the session is very likely **idle-detached / cold** (see
  [[session-lifecycle]] / `docs/design/session-lifecycle.md`). Addressing by the
  persisted `session_id` (`ChatSessionStore` already keeps it) is correct, but
  *delivery* means "wake session_id, **reviving it if cold**." → folds into the
  cold-revive path + the open question below.
- **The payload is untrusted.** Anyone can comment on a public PR. Delivering that
  text into an agent's context is a prompt-injection surface, so it must be
  **per-session opt-in**, and the agent treats review text as *data to consider*,
  never as instructions. (This is §0 of [[tool-store-marketplace]] re-emerging at
  the payload level — host tools are unconfined.)

## The design: one push path

```
share_tool  → embeds an opaque (daemon,session) capability token in the PR
review      → store webhook → RELAY (stateless; resolves the token)
            → POST the daemon's webhook route (HMAC + TLS + IP-allowlist)
            → EXTERNAL_EVENT on the session bus → wake the session (revive if cold)
            → agent addresses the feedback → re-runs share_tool (updates the PR)
```

Three parts:

1. **Carriage — the binding travels in the PR.** `share_tool` records the
   (daemon, session) binding at share time and advertises it in the PR as an
   **opaque capability token** — *not* the raw daemon URL or `session_id` (the PR
   is public; leak nothing). This keeps the relay **stateless**: it never holds a
   central PR→bot registry; it reads the token from the webhook payload and
   resolves it.
2. **Relay — stateless resolve + forward.** Configured once on the store repo. It
   verifies the GitHub HMAC, resolves the token to a daemon webhook URL, and
   forwards the review payload. The natural serverless form is a **GitHub Action**
   in the store repo (`on: pull_request_review`) — those events run in the base-repo
   context with access to repo secrets, so a workflow can resolve + `curl` with no
   maintainer-run server. (A standalone relay service is the alternative if an
   Action proves too constrained.)
3. **Delivery — the server webhook plugin.** The daemon's `webhook` plugin
   verifies its own HMAC (+ optional TLS + IP-allowlist), emits the
   `EXTERNAL_EVENT`, and the session's agent picks it up. Cold sessions are revived
   by id first.

**Reachability is an operator prerequisite, not a protocol tier.** The relay can
only reach a daemon the operator has made reachable — the same class of
requirement as "run a jaato server and expose its WS on :8089." We do **not** bake
a polling fallback into the core for unreachable daemons; the operator exposes the
endpoint by standard means (below). Polling only reappears in one self-inflicted
mismatch (see caveat) and is not first-classed.

## Deployment: two topologies, push works in both

| Topology | GitHub | jaato | Store | Path |
|---|---|---|---|---|
| **Self-host / VPS** | github.com | your box | public community store | relay → **reverse-proxied** daemon route (Caddy / nginx / Cloudflare Tunnel, TLS) |
| **Corporate** | internal **GHE** | intranet | **private internal store** (fork) | all intranet; firewall **east-west** permits the internal call — nothing public |

- **Self-host / VPS:** a reverse proxy publishes the daemon's webhook route with
  TLS. The operator's choice of proxy; not our concern.
- **Corporate:** GitHub Enterprise Server and jaato co-located in the intranet →
  the webhook never leaves the perimeter. A corporate SecOps team will not let
  bots auto-pull community-contributed **unconfined** code anyway, so they run a
  **private internal store** (a fork of the store repo on their own GHE) for
  supply-chain control — which *also* makes the whole path intranet and push-native.

### The one caveat (not first-classed)

**Public-cloud store (`github.com`) + jaato behind a corporate intranet that
denies inbound webhooks from the internet.** Here "firewalls doing the correct
work" means *denying* the inbound POST. The clean resolution is **not** bolting
polling onto the protocol — it's the corporate topology above (private internal
store → fully intranet). Polling would only be needed if someone insists on
public-cloud-store *and* no-inbound-intranet simultaneously, which is a
self-inflicted mismatch, not a topology we design around. If it ever must be
supported, the outbound-only answer is a **self-hosted GitHub Actions runner
inside the perimeter** (it pulls from GitHub, runs internally, reaches jaato) — an
add-on, not a change to the core path.

## Alternatives considered (for the record)

1. **Webhook on each bot's fork.** ❌ Rejected — review events never reach a fork
   (they're base-repo resources). Decisive, not a trade-off.
2. **Client-side public endpoint on the bot** (a public-bound descendant of
   `approval_webhook`). ❌ Inbound surface into an *unconfined* bot process, URL
   disclosed in the public PR. The server `webhook` plugin is strictly better: the
   ingress is a purpose-built, HMAC/TLS/IP-allowlisted server component, and it
   delivers a structured event, not code.
3. **Per-bot polling of its own PRs.** Viable, no inbound anything, but pull
   latency and per-bot API cost; and unnecessary once reachability is the
   operator's job. Kept only as the self-inflicted-mismatch fallback above.
4. **Email-driven** (bot reads GitHub notification mail via the Gmail MCP). Brittle
   parsing, account coupling, delivery lag. A hack.
5. **Store webhook → central relay *service* → per-bot registry.** The registry and
   the running service are exactly what the carriage-token + Action form remove.
6. **Status quo — human relays.** What we do now; the step we're deleting.

**Chosen:** carriage-token in PR → stateless relay (Action) → server webhook
plugin, reachability as an operator prerequisite.

## Blocking open question (verify before building)

**Does an `EXTERNAL_EVENT` wake an *idle* session (spawn a fresh turn), or does it
only surface while the agent is already subscribed/active?** The plugin's
model-facing flow is agent-driven (`webhook_subscribe` + `subscribeToTasks`), and
by review time the session is almost always cold. "React to a review while the bot
sits idle" depends entirely on this. This is the server-side twin of the client
`ctx.wake` idle-vs-mid-turn distinction. **Action:** read the event-bus delivery /
runner-revive path in `jaato-server`, or confirm with Advisor, before committing.

## Remaining questions (decide before building)

1. **Capability token** — format, where it lives in the PR (body marker vs a
   metadata file on the branch), how the relay resolves it (embedded signed blob
   vs a lookup the daemon owns). Lean: signed opaque blob the target daemon can
   verify, so the relay stays stateless *and* trustless.
2. **Cursor / dedup** — GitHub may redeliver; the daemon should de-dup by
   `event_id` so a review isn't actioned twice.
3. **Scope of events** — review *summaries* only, or inline diff comments too?
   Inline carries the actionable detail but is noisier to assemble.
4. **User in the loop** — act on feedback autonomously, or `ctx.ask` "a reviewer
   commented — want me to address it?" first? Lean: notify + let the model
   propose, consistent with the rest of the marketplace (user's hand on the
   trigger).

# Closing the PR-review loop: bots react to review comments automatically

**Status:** design only — nothing built. Raised 2026-07-04: jaato ships a webhook
ingress that links an agent to a webhook, and GitHub can push PR-review events to
a webhook, so a bot could react to reviewer comments on its store PR without a
human relaying them. This note scopes that.

**DIRECTION SET (2026-07-04):** Daniel wants the fix to be **client-agnostic**, not
a Telegram shim — so the chosen path is **not** the client-side `ctx.wake` route
(option B) but a **server-native daemon wake primitive** (option A2): a first-class
`session.wake(session_id, text, source=USER)` daemon capability, ingress-agnostic,
gated by #498 auth, that revives a cold/detached session and starts a USER turn. The
HTTP webhook ingress is just one caller in front of it. **This is jaato-server work;
Advisor (server owner) is designing A2** (handed off 2026-07-04). B is retained below
only as the interim/fallback if a client-local stopgap is ever needed. Key layering:
**wake** (start the turn, revive if cold) is generic/server-side; **render** (stream
output to a human) stays per-client (the bot's normal job) — so the primitive runs
headless, no client attached required.

Prior blocking question, now answered (code-traced): the server `webhook` *plugin*
CANNOT wake an idle/detached session (`SourceType.EVENT` = mid-turn only; listener
dies on unload). See "Resolved: the idle-wake verdict" below — that verdict is
exactly why A2 must live at the **daemon tier**, not the runner-bound plugin.

## Locked design (2026-07-04): the `session.wake` primitive

Advisor designed A2 and Daniel greenlit it. This supersedes the option-A/B
exploration further down (kept for the reasoning trail).

**Server-side primitive** (Advisor's, jaato-server — transport-agnostic daemon
command; the HTTP shim is one caller in front):
```
session.wake(session_id, text, source=USER):
  1. dedup by event_id                       (per-session bounded LRU — GitHub redelivers)
  2. resolve workspace via a daemon-owned session_id→workspace INDEX   (server-owned; Option 1)
  3. if cold: resume_session(session_id, workspace)      (EXISTING — session_manager.py:5346, headless)
  4. wrapped = wrap_untrusted_content(text, source="wake:<src>")   (EXPLICIT — inject bypasses #495 auto-wrap)
  5. send_message_to_session(session_id, wrapped)       (EXISTING — session_manager.py:5030, headless USER turn)
```
Two of the pieces I'd flagged as net-new already exist: the headless USER
turn-start (`send_message_to_session`, used by cascade-first-turn/reactor — one
layer above the client-only `core.py::send_message` path I traced) and the
headless revive (`resume_session`). **Net-new is just:** the `session.wake` command
+ the `session_id→workspace` index + dedup + the explicit untrusted-wrap-at-inject.

**Security by construction (Advisor verified against real on-disk records):** the
sandbox root comes from the persisted record's `state.workspace_path`, **never** the
caller; `config_root` is saved-wins with a server `<workspace>/.jaato` fallback. So
an authed-but-untrusted wake caller cannot revive under a weaker sandbox — the
constraint I raised is met by design. (#469 persistence is what makes this hold.)

**Auth:** the HTTP shim sits behind #498's `parse_webhook_request`
(`webhook/routes.py:155-166`, fail-closed 401) — built ON it, one caller.

**Client caller (mine, jaato-client-telegram + the store repo):**
- Because workspace is server-owned via the index, the capability token embedded in
  the PR only needs **(daemon, session_id)** — *not* workspace. Less to leak in a
  public PR, simpler relay.
- `share_tool` mints + embeds that token at share time (needs the chat's
  `session_id` from `SessionPool` + the daemon's wake-shim endpoint from config).
- The relay (GitHub Action on the store repo) resolves the token and POSTs the
  review to the daemon shim.

**Relay→shim auth model — DECIDED (b), 2026-07-04 (Daniel): no per-daemon secret,
and crucially no secret in the public PR.** A bearer credential embedded in a
*public* store PR is world-readable → anyone could replay it to wake-spam the
session (bounded by the untrusted-wrap + rate-limiting, but a real nuisance/DoS). So
the safe form of (b) puts **nothing secret in the PR**:
- The PR carries only a **non-secret routing reference** — its branch
  (`share-<tool>`) or PR number (public anyway).
- `share_tool` **registers `(pr_ref → session_id)` with its daemon** at share time
  (extends the `session_id→workspace` index).
- Auth is a **store-level signature the daemon trusts** — the relay signs each
  forwarded wake with the **store's** single key; each daemon is configured once to
  trust the store's public key. Relay holds no per-daemon secret; nothing forgeable
  sits in the PR.

Rejected (a) (per-daemon HMAC in the relay — secret sprawl) and naive-(b)
(daemon-issued bearer *in the PR* — public credential).

**Pinned mechanism (Advisor, 2026-07-04):**
- **Relay→daemon auth: mTLS-first (mode A).** The store relay presents a **store
  client cert** (`curl --cert store.crt --key store.key`); each daemon trusts the
  **store CA** once. This is *already* a #498 `transport_authenticated` mode
  (`route.tls.ca_certfile`) — **zero new auth-mode code**, asymmetric (daemon holds
  only the CA, cannot impersonate the store). **Fallback mode B** = an asymmetric
  Ed25519 **signature-in-body** (daemon configured with the store pubkey), needed
  **only if the shim sits behind a TLS-terminating proxy** that strips the client
  cert. Mode B is net-new auth surface → Advisor takes it to Daniel as a
  security-architecture decision for PR 2; mode A needs no such decision.
  *(GitHub→relay stays GitHub's `X-Hub-Signature-256` HMAC — the two hops are
  distinct.)*
- **`pr_ref→session_id`: a separate `WakeBindingRegistry`, written via a new daemon
  command `session.bind_wake(pr_ref)` that Advisor owns** — NOT raw index exposure.
  The command binds `pr_ref → the caller's OWN session_id`, so a client can't hijack
  another PR's routing (authorizable by construction). Kept out of the core
  `session_id→workspace` index deliberately.
- **Revocation: daemon TTL + explicit `session.unbind_wake(pr_ref)`** that the
  client calls on PR merge/close. TTL is the safety net for the forgotten case; the
  client does not carry sole responsibility.

**Topology note (ours):** our reachability model exposes the shim via an operator
reverse proxy — a *TLS-terminating* proxy (Caddy/nginx-http/Cloudflare Tunnel) would
**strip the client cert**, breaking mode A. Daniel's single VPS bot avoids this by
**exposing the wake-shim port directly with mTLS** (daemon binds it, no terminating
proxy) → mode A, zero new code. Operators who must front it with a terminating proxy
need mode B. See the delivery-tier decision for Daniel below.

**Build staging:**
- **PR #516 (UP, contract finalized 885be89b; awaits Daniel review + Copilot pass)**
  — core primitive: `session.wake` + `session_id→workspace` index + untrusted-wrap
  + `event_id` dedup. (jaato repo.) **Locked shim contract:**
  `session.wake(session_id, text, source, event_id) → (WakeOutcome, detail)` with
  `WakeOutcome ∈ {OK, DUPLICATE, INVALID, UNRESOLVED, REVIVE_FAILED, NOT_DRIVABLE}`
  + `.is_success` (OK|DUPLICATE). The shim maps enum→HTTP with no prose matching:
  **OK/DUPLICATE→2xx** (DUPLICATE = idempotent no-op, not an error — a redelivered
  webhook must not look failed/retry), **INVALID/UNRESOLVED→4xx** (permanent),
  **REVIVE_FAILED/NOT_DRIVABLE→5xx** (transient/retry). (`source` stays a coarse
  provenance tag; rich attacker-influenceable context rides inside the wrapped
  `text`.)
- **PR 2** — the HTTP shim + relay trust (mode A mTLS; mode B if Daniel approves) +
  `WakeBindingRegistry` + `session.bind_wake` / `session.unbind_wake` commands. The
  registry captures **(session_id + workspace_path)** at bind time (bind runs AS the
  caller's session), so a bound session stays wakeable even if its id is AMBIGUOUS in
  the core index — the collision class only bites *unbound* wakes, and the PR-review
  path is always bound.
- **Client (mine)** — `share_tool` calls `session.bind_wake(pr_ref)` at share time
  and `session.unbind_wake(pr_ref)` on merge/close; the relay (store-repo Action)
  presents the store client cert. **Waits on PR 2's `bind_wake`/`unbind_wake`
  command signatures** — building against not-yet-existent commands is the
  guess-first trap. Wire it the moment those land.

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

## Resolved: the idle-wake verdict (2026-07-04, code-traced)

**The server `webhook` plugin cannot wake an idle or detached session.** Verdict is
(B): `bus.publish(EXTERNAL_EVENT)` only lands in the runner's mid-turn message
queue and is drained *while a turn is already running* — it never starts one. Three
independent barriers in `jaato-server`:

1. **`SourceType.EVENT` = mid-turn delivery only** — `event_bus_tools.py:349`
   injects with `source_type=SourceType.EVENT` ("arrive at the next pause point
   between tool calls"). Idle ⇒ no turn to land in.
2. **Wake trigger disarmed between turns** — `jaato_session.py:1256` starts a turn
   on inject only if `_on_continuation_needed` is set, and `rpc.py:2925/3040`
   installs it *per active `send_message` RPC* and clears it in `finally`. Idle ⇒
   `None` ⇒ inject just queues.
3. **Post-turn drainer excludes EVENT** — `message_queue.py:195` `has_parent_messages`
   matches only `USER, SYSTEM`, so a queued EXTERNAL_EVENT never itself kicks off
   the next turn.

And decisively for *our* scenario (review lands hours later, bot long detached):
**idle-detach unloads the session** (`session_manager.py:6589`), destroying the
runtime, bus, subscription, **and the webhook HTTP listener + its bound port**
(`http_server.py:180`). Nothing is even listening.

So the plugin is the right primitive for "an external event *during a live
conversation*," and the wrong one for "wake me later about a review." Our case is
the latter.

### Consequence: deliver to the bot, not the daemon

The always-on component that **survives detach is the bot process**, and idle-wake +
cold-revive already exist there and are tested: `ctx.wake` → `ChatPump` starts a
fresh turn when idle ([[message-flow-chat-pump]]); `SessionPool.get_or_create_session`
re-attaches/revives the cold session by its persisted id ([[session-lifecycle]]).
The daemon has no revive-and-inject-as-USER-on-inbound path today. So the delivery
tier moves from the daemon `webhook` plugin to the **bot**, and the address
simplifies from (daemon, session) to **(bot endpoint, chat_id)** — the bot owns the
chat→session map and the revive logic.

### The delivery-tier decision (operator's/maintainer's call)

- **B — Relay → bot inbound endpoint → `ctx.wake(chat_id, review)` (recommended).**
  Reuses fully-built, tested client machinery; no jaato-server change; the bot
  inbound endpoint is the reverse-proxy prerequisite already accepted. Cost: a
  small HMAC-verified inbound receiver on the bot (net-new client code).
- **A — Daemon-tier webhook ingress that revives the session + injects as USER
  (server-native, does not exist yet).** Keeps everything server-side, no bot port,
  but requires new **jaato-server** work by Advisor (move the listener from runner
  to the always-up daemon; add revive-by-session_id + a turn-*starting* USER inject)
  and duplicates idle-wake logic the client already has.

**Recommendation: B now, A as a later server-native evolution.** B ships on proven
code; A is a real server project whose only win (no bot port) the reverse-proxy
prerequisite already neutralizes.

### Security: external input starting a turn IS the injection boundary (Advisor, 2026-07-04)

Advisor (jaato-server security owner) verified the trace against current `main` and
answered the two questions:

- **EVENT-vs-USER is a delivery-*timing* semantic, not a fail-closed posture.**
  `event_bus_tools.py:349-353` frames `SourceType.EVENT` purely by *when* it lands
  (high-priority, non-interrupting, at the next pause of an already-running turn —
  which is exactly what `webhook_poll` blocking *inside* a turn relies on).
  `message_queue.py:180-198`: USER/SYSTEM interrupt; EVENT/PARENT wait for a pause.
  There is **zero** anti-injection intent behind the EVENT choice — idle/detached
  wake is genuinely **unbuilt**, it just falls out of the timing semantic.
- **#498 (webhook fail-closed auth) does not change the delivery path** — it's pure
  *ingress* auth (a matched route needs HMAC **or** mTLS **or** IP-allowlist **or**
  explicit `allow_unauthenticated:true`, else 401). Once published, delivery
  semantics are unchanged.

**The load-bearing caveat — and it applies to BOTH A and B:** making an external
event *start a turn* means an **unauthenticated-external-party-initiates-agent-work**
— precisely the indirect-prompt-injection class that **#495 (`TRAIT_UNTRUSTED_CONTENT`)**
and **#498** exist to bound. Today's EVENT choice isn't *motivated* by fail-closed,
but any turn-*starting* wake path **must be built on that boundary**:

- **Option B (client, ours):** the bot's inbound receiver must be **fail-closed
  HMAC** (mirror #498 — reject unless verified), and the woken turn must carry the
  review text as **untrusted content** — *data to consider, never instructions to
  obey*. A public PR comment is attacker-controlled input.
  **Correctness gotcha (Advisor, 2026-07-04):** the framework's
  `TRAIT_UNTRUSTED_CONTENT` auto-wrap (`wrap_untrusted_content` in
  `render_result_for_model`) is **scoped by #495 to web_fetch / web_search / MCP
  results only** — inbound/webhook was *deliberately excluded* (Daniel's call). So
  the wrapping will **NOT auto-fire** for a bot-inbound turn; **we must call
  `wrap_untrusted_content` on the review text ourselves.** Do not assume the trait
  machinery tags it.
- **Option A (server, roadmap):** same boundary — Advisor's framing is "build the
  wake feature **ON #498**, not around it": daemon-tier ingress (survives unload;
  listener not bound to runner lifecycle) + revive-by-`session_id` + inject as USER,
  **gated by #498 auth**. Advisor has captured this as a jaato-server roadmap item.

So B stays the recommendation, with a firm requirement attached: **fail-closed HMAC
on the bot ingress + untrusted-content handling of the payload** — not optional
hardening, it's the boundary the whole feature lives behind.

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

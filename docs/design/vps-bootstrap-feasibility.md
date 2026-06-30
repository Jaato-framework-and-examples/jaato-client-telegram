# Feasibility: one-shot VPS bootstrap for the bot + its jaato stack

**Verdict: feasible**, and not hard — the enabling facts are all in place. The
main reframing is that "deploy the bot + dependencies" means deploying the
**whole stack** (server + SDK + bot), because the bot is only a client.

## Scope reality
The bot's `pyproject.toml` declares only `jaato-sdk`. To actually run, it needs a
live **jaato-server** (which itself pulls `jaato-sdk` + a large plugin/provider
tree). So the bootstrap provisions exactly **three** packages — `jaato-sdk`,
`jaato-server`, `jaato-client-telegram`. **No premium.**

**Premium is no longer a dependency (verified 2026-06-30).** The vision tier
itself is a server/SDK capability, not premium. Our `telegram_chat` profile had
three premium couplings; all are now eliminated:
- `profile_tools` + `session_ops` plugins — **removed** from the profile (commit
  f380bd5); the bot never used them, and neither is load-bearing (re-attach is
  the separate `session_reconnect` *extension*).
- memory-curation **reactor** (premium's reactor engine) → **replaced** by
  client-side curation on `tool.call_end` (commit be25370); the curation logic is
  server-core (`shared.plugins.memory`), so memory-continuity survives with no
  premium.
- `pass://` OpenRouter key resolver → the bootstrap renders the profile with
  `${OPENROUTER_API_KEY}` (env) instead.

⇒ **One deployment, premium-free: 2 public repos (3 packages), env secrets.** (A future
`allowed_scopes: ["project"]` memory-plugin knob — pending Advisor — keeps all
memories project-scoped so the home tier is never touched; not required for
premium-free, just tidies memory tiering.)

## Evidence (gathered 2026-06-30)

**Install source — public GitHub at pinned commits, NOT any index.**
- The repos we need are **public** (clone works with no auth): `jaato.git`
  (monorepo containing `jaato-sdk` + `jaato-server`) and
  `jaato-client-telegram.git` — **two clones for three packages**. → no deploy
  keys / private index needed. (`jaato-premium.git` is also public but we no
  longer install it.)
- Not on main PyPI (404). On **TestPyPI**: `jaato-sdk` 0.10.0, `jaato-server`
  0.6.46 — both **~8 weeks behind** the versions the current bot needs (local
  editable: sdk 0.14.6, server 0.6.194; the WS-facade migration requires
  sdk ≥0.14 / #465). `jaato-premium` and `jaato-client-telegram` are **absent**
  from TestPyPI.
- **Why TestPyPI is stale (confirmed with Advisor, evidence-grounded):** the
  three publish workflows (`publish-testpypi-{sdk,server,tui}.yml`) are
  **`workflow_dispatch`-only** — no tag/release/push automation, and the repo has
  **zero git tags**. Last successful manual runs: sdk 2026-05-03, server
  2026-05-05. It froze at early-May because nobody triggered it, not because it
  broke or is pinned (the last runs were green). **No real-PyPI workflow exists
  at all** (only TestPyPI + an npm publish for the TS SDK). premium + the
  telegram bot are in **sibling repos** the jaato pipeline never publishes (their
  404 = never published anywhere; premium is almost certainly intentionally not
  public-indexed).
- ⇒ Today the bootstrap **must** install from Git source — and nothing on the
  index roadmap changes that soon (none of sdk/server/bot is on usable PyPI; the
  bot itself is unpublished). A
  single coherent "clone N repos at pinned SHAs → `pip install -e`" story beats a
  hybrid index+git deploy. This also matches the project's own documented install
  (`pip install -e jaato-sdk/. -e "jaato-server/.[all]" …`).

**Entry points — clean, no glue needed.**
- Server: `python -m server --web-socket [HOST:]PORT --ws-token-file PATH
  [--daemon]`, with built-in `--status` / `--stop` / `--restart`.
- Bot: `jaato-tg` (console script) / `python -m jaato_client_telegram`.

**TLS — co-location avoids it.** The server terminates TLS from a `servers.json`
`tls: {cert, key}` section (`websocket.py::load_tls_context`); there is no
`--tls` CLI flag. Bot + server on one VPS ⇒ connect `ws://localhost:PORT`
(loopback, no certs). TLS only matters if the WS endpoint is exposed off-box.

**Secrets — env vars are the portable path.** The server reads provider keys
from the environment. The `pass://` resolver (premium) needs gpg/pass and is the
wrong fit for a VPS. So the bootstrap collects, as env vars:
`TELEGRAM_BOT_TOKEN`, a provider key (e.g. `OPENROUTER_API_KEY`), and a generated
`JAATO_WS_TOKEN` (shared secret; server `--ws-token-file`, bot
`jaato_ws.secret_token`). Stored in `EnvironmentFile`s with 600 perms.

**Runner confinement — optional on a VPS.** AppArmor is present here; on a
minimal VPS it may be absent — the server then runs the runner **unconfined**
(it emits a SystemMessage, not a silent fallback). Acceptable for a single-tenant
VPS where the agent is trusted. (Note: the bot's host tools already run
unconfined — existing backlog item, unchanged by this.)

**Networking — polling needs no inbound ports.** Telegram polling mode (the
bot's default) requires only outbound HTTPS — ideal for a VPS behind a firewall,
no public URL/cert. Webhook mode would add a public HTTPS endpoint + reverse
proxy (defer it).

**Runtime — Python ≥3.10 (3.12 used).** The server pulls a sizeable tree
(`mcp[cli]`, providers, ~30 plugins) and spawns a runner pool; size the VPS for
it (the bot alone peaked ~180 MB; the server is heavier). Reasonable target:
2 vCPU / 2–4 GB.

## Recommended shape
**One VPS, both processes co-located**, `ws://localhost:PORT`, **polling**,
**env-var secrets**, **two systemd `--user` (or system) units** (server, then
bot). This mirrors the existing LAN deployment (durable venv + systemd) and the
server's host-process / subprocess-runner assumptions.

## Two ways to package it

### Option A (recommended) — idempotent bash bootstrap (`deploy-vps.sh`)
Clone → venv → `pip install -e` the 3 packages → interactive secret prompts →
render configs (`servers.json`/bot yaml/env files) → install + enable systemd
units → start server, wait healthy, start bot → smoke check. Run via
`ssh vps 'bash -s' < deploy-vps.sh` or `curl … | bash`.
- **Pros:** matches the ask literally; no Docker; transparent + debuggable;
  reuses the proven systemd model; AppArmor works natively; builds on the
  existing `start.sh`. Handles the server's subprocess/cgroups/runner model with
  zero friction.
- **Cons:** less reproducible than images (editable-from-source is a moving
  target unless pinned); must handle distro variance (apt/dnf, Python version);
  upgrades = `git pull` + reinstall + restart.

### Option B — Docker Compose bundle
Dockerfile(s) for server + bot, `compose.yaml`, `.env` for secrets, one
`docker compose up`.
- **Pros:** reproducible, isolated, distro-agnostic; clean upgrades (rebuild);
  one command.
- **Cons:** fights the server's design — the runner spawns **subprocesses** with
  **AppArmor + cgroups** (`JAATO_CGROUPS_ROOT`); nested confinement in a
  container is awkward and likely forces unconfined + privileged/cgroup mounts.
  Heavier; more to get the runner pool working correctly. Better as a v2 once the
  containerized runner story is settled.

**Recommendation: A.** It's the most feasible given the runner/AppArmor/cgroups
model and matches how the stack already runs. B is viable but needs the
container-runner question solved first.

## What `deploy-vps.sh` would do (Option A step list)
1. **Preflight:** detect distro; ensure Python ≥3.10, `git`, build tools, `curl`;
   optional AppArmor check (warn-only).
2. **Fetch:** clone the 2 public repos at a pinned ref into `/opt/jaato` (or
   `~/jaato`).
3. **Install:** create a venv; `pip install -e` sdk, server, bot (no premium).
4. **Secrets + provider (interactive — see next section):** Telegram token;
   pick provider(s) + model(s) + key(s); generate `JAATO_WS_TOKEN`; write
   `EnvironmentFile`s (chmod 600).
5. **Configure:** render `servers.json` (no TLS for loopback), the bot
   `jaato-client-telegram.yaml` (`ws://localhost:PORT`, polling, workspace +
   host_tools_dir paths), and the **customized profile** (provider/model/tiers).
6. **Service:** install systemd units `jaato-server.service` (`--web-socket
   :PORT --ws-token-file …`) and `jaato-tg.service` (After/Requires server);
   enable both.
7. **Start + health check (see next section):** start server → validate profile
   → preflight WS/auth → live provider check → start bot → confirm it connects →
   print the bot @username and next steps.
8. **Idempotent re-run:** safe to re-run for upgrades (pull + reinstall +
   restart); `--uninstall` to tear down.

## Provider configuration + health check (interactive, provider-agnostic)
Do **not** hardcode OpenRouter. The framework supports several providers
(`anthropic`, `openai`, `openrouter`, `gemini`/`google`, `groq`, `together`,
`fireworks`, `ollama`, `zhipuai`); the bootstrap asks the operator to choose and
validates the choice end-to-end.

**Key resolution — env var, no secret in the profile.** Each provider resolves
its key as `config.api_key or <conventional env var>`
(`model_provider/.../provider.py`: `api_key = config.api_key or resolve_api_key()`).
Conventions: `openrouter → JAATO_OPENROUTER_API_KEY`,
`anthropic → ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`, `zhipuai → ZHIPUAI_API_KEY`,
etc. ⇒ the bootstrap writes the provider's env var into the server
`EnvironmentFile` and leaves the profile's `api_key` unset — **no key inlined in
the profile.**

**Prompt flow:**
1. **Main (executor) tier:** choose provider → enter model (suggest a sensible
   default per provider) → enter API key/token.
2. **Vision tier (optional):** enable image/PDF understanding? If yes, choose
   provider + a vision-capable model + key (may be the same provider). If no,
   render a single-tier profile and tell the operator images won't be understood
   (the bot still does text + host tools).
3. **Customize the profile:** write `provider`, `model`, and `model_tiers`
   (`executor` [+ `vision`], each naming its own provider) into
   `runtime/.jaato/profiles/telegram_chat.yaml`; set the matching provider env
   vars; (when Advisor's knob lands) add `plugin_configs.memory.allowed_scopes:
   ["project"]`.

**Health check (layered; re-prompt on failure):**
1. `jaato-scaffold validate <profile>` — profile is well-formed (provider/model/
   plugins/tiers coherent) *before* starting anything.
2. `jaato-doctor --web-socket :PORT --ws-token-file …` — daemon reachable, WS
   port + bearer-token + auth mode OK.
3. **Live provider check** — create a session with the profile (the server runs
   `verify_auth(provider)`) or send a one-shot "ping" turn; a real reply proves
   provider + model + key work end-to-end. On failure, surface the provider auth
   plugin's own error (they print actionable messages) and re-prompt that tier's
   provider/model/key.

## Pinning policy (resolved with Advisor)
Pin **commit SHAs**, not tags — the repos have **zero tags**. Pin a **coherent
set** across `jaato` (sdk+server) + `jaato-client-telegram`
that satisfies the **SDK↔server compatibility gate**: clients declare a
`MIN_SERVER_VERSION`/`MIN_PROTOCOL_VERSION` and refuse an older server, so a
mismatched pin fails at connect. The bootstrap records the SHA set (a lockfile)
so a re-run reproduces the exact stack; building wheels from the pinned tree is
more robust than TestPyPI even after a republish (TestPyPI is a test index —
ephemeral, weak transitive resolution, not a production target).

If index installs are wanted later, the cheapest lever is wiring the existing
manual workflows to fire on tag-push (and adding tags) — but a production VPS
should build from pinned SHAs regardless.

## Open items to confirm before building
- Exact `servers.json` schema the server expects (TLS section + any required
  keys) and whether a non-TLS loopback config needs anything beyond the port.
- Provider/profile defaults: which profile+agent the bootstrap should wire
  (OpenRouter vision tier vs a text-only minimal profile) and the minimal env
  keys that profile needs.
- Whether to target systemd **--user** (rootless, matches current) or **system**
  units (survives logout, standard for a VPS).

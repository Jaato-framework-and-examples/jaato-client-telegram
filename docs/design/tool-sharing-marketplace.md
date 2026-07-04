# Tool sharing: a curated, PR-gated marketplace for host tools

**Status:** design; the store repo is live, the client-side tools are not built
yet. Decisions (2026-07-04): build both halves, staged; trust model =
**PR-gated curated store + user acceptance**; the store is a **dedicated public
repo** — [`Jaato-framework-and-examples/jaato-telegram-bot-tools-store`](https://github.com/Jaato-framework-and-examples/jaato-telegram-bot-tools-store)
(created + seeded with the 17 curated tools); the registry is **auto-generated on
merge** by a GitHub Action.

A bot can already *extend itself*: the model writes a tool draft, calls
`register_tool`, the user approves, and the bot installs it (`register_tool`,
`register_tool(action="edit")`, `session_pool.install_and_register_tool`). This
note designs the next step — letting bots **share** tools with each other through
the repo: **discover + install** curated tools, and **contribute** their own back
via a pull request. Always with the user in the loop.

## 0. The one thing that matters: trust

A host tool runs **UNCONFINED in the bot process** (not in the AppArmor-confined
runner). Installing one is arbitrary code execution with the bot's full
privileges — filesystem, network, the Telegram token, `pass://` secrets. So this
feature is fundamentally a **code-distribution system for unconfined code**, and
its security *is* its design. The plumbing (git, HTTP, file copy) is trivial by
comparison.

**Chosen trust model — PR-gated store + user acceptance:**

1. **The curated store is the only install source.** A bot can *propose* a tool
   (open a PR); it can never merge. A **human reviews and merges** the PR. Only
   merged tools become installable. **The GitHub review is the security gate** —
   exactly the boundary a code change already crosses in this repo.
2. **Pin the source.** Install from a specific tag/commit recorded in the
   registry, not a moving branch — a compromised `HEAD` shouldn't auto-propagate.
3. **Verify integrity.** Each registry entry carries a `sha256`; the installer
   re-hashes the fetched file and refuses on mismatch.
4. **Defense in depth: still ask the user, and show the code.** Even from the
   reviewed store, installation is user-accepted and displays the tool's source
   (or a signed digest). This depends on closing a known gap — the approval UI
   currently shows no code (see `docs/backlog.md`, [[backlog-host-tool-approval]]).
   Closing it is a **prerequisite** for a meaningful "accept".
5. **Provenance is recorded** — who contributed, which PR, when — and surfaced at
   browse/install time.

Explicitly rejected: open peer-to-peer tool exchange (unreviewed code running
unconfined) and "any source if the user reviews it" (relies on a human reading
arbitrary Python — weak, and blocked on the same code-display gap anyway).

## 1. Architecture: dogfood it as host tools

The marketplace is itself three host tools (the bot extends itself into a tool
that manages tools), plus a thin, security-sensitive bot capability for the parts
that must NOT be bypassable by tool code.

| Tool | Runs where | Does |
|---|---|---|
| `browse_tools` | bot (unconfined) | fetch the registry index from the store, list name/description/deps/provenance to the user |
| `install_tool` | bot | fetch a chosen tool at the pinned ref, verify `sha256`, then hand it to the **approval-gated** install path |
| `share_tool` | bot | package an installed tool + metadata, open a PR to the store repo |

**Non-bypassable bit:** the actual *install* must not be something a tool can do
silently. It routes through the existing approval-gated path
(`install_and_register_tool`, `auto_approve=False`): `install_tool` fetches +
verifies, writes the file to `tool_drafts/<name>.py`, and the install itself goes
through `register_tool` (user approval + code display). A tool cannot install code
without the user's explicit yes. (Consideration: expose a single bot capability
`ctx.install_from_store(name)` that does fetch+verify+draft+approval-gated-install
as one auditable step, rather than trusting a tool to chain them.)

## 2. Registry format

A machine-readable index at the store repo root — `registry.json`,
**auto-generated** from `tools/*.py` by `generate_registry.py` and regenerated +
committed on every merge to `main` by a GitHub Action (contributors never
hand-edit it; they declare a tool's PyPI deps in-file as `TOOL_DEPS = [...]`):

```json
{
  "version": 1,
  "tools": [
    {
      "name": "moon_phase",
      "file": "examples/host_tools/moon_phase.py",
      "description": "Current moon phase + illumination for a date.",
      "deps": ["skyfield"],
      "sha256": "<hex>",
      "provenance": { "contributed_by": "jaato-hetzner", "pr": 123, "added": "2026-07-04" },
      "tags": ["astronomy"]
    }
  ]
}
```

`browse_tools` reads only the index (no cloning). `install_tool` fetches the
`file` at the pinned ref (raw GitHub content), re-hashes against `sha256`, and
declares its `deps` so the workspace tool-venv can install them (existing venv
feature — see [[dynamic-tool-deps-venv]]).

## 3. Consume flow (discover → install)

1. User: "what tools can you get?" → model calls `browse_tools` → reads
   `registry.json` from the store at the pinned ref → returns a readable list
   (name, description, provenance, deps).
2. User picks one → model calls `install_tool(name)`.
3. `install_tool`: fetch `<file>` at the pinned ref → verify `sha256` → write to
   `tool_drafts/<name>.py` → return "review + approve".
4. Model calls `register_tool(name)` → **user sees the code + accepts** → the bot
   validates (load) and installs into `host_tools_dir`; deps resolve via the
   tool-venv on first call. Callable next turn.

No new credentials: read-only fetch from a public/curated store.

## 4. Contribute flow (build → PR)

1. The bot built/edited a tool the user likes → user: "share this back".
2. `share_tool` packages the installed `<name>.py` + a registry entry (description
   from `TOOL_SCHEMA`, declared `deps`, provenance = this bot/user).
3. The bot opens a PR against the store repo: branch → add file to
   `examples/host_tools/` + update `registry.json` → `gh pr create`, labelled
   `bot-contributed`, body noting provenance and "model-authored — review the code
   before merge".
4. A **human reviews + merges** (the gate). Now it's in the store, installable by
   everyone.

New requirements (the bigger lift):
- **A dedicated bot GitHub identity + token** (machine account), secret via
  `pass://` / env — never inlined. Least-privilege: open PRs on a fork, no direct
  write to the store's default branch.
- **Fork-and-PR** (not a branch on the canonical repo) so a bot never needs write
  access to the real store.
- **Guardrails:** the model may only *propose*; every bot PR is clearly labelled
  and provenance-stamped; a maintainer merges.

## 5. Staged implementation

1. **Registry** — define `registry.json`, populate it for the existing curated
   `examples/host_tools/`, add a CI/`make` check that files ↔ index stay in sync
   (name, sha256).
2. **Close the approval code-review gap** — show the tool source (or a diff for an
   edit) in the approval UI. Prerequisite for a meaningful "accept" and a hard
   dependency of the consume flow. (This also settles the standing backlog item.)
3. **Consume** — `browse_tools` + `install_tool` (pinned-ref fetch, sha256 verify,
   approval-gated install). Highest value, lowest risk, no credentials.
4. **Contribute** — bot GitHub identity, fork/branch/`gh pr create`, provenance,
   `share_tool`.
5. **Polish** — search/tags, dep surfacing, "updates available" for installed
   tools (compare installed sha256 vs registry), per-deployment allow/deny lists.

## 6. Decisions & open questions

**Decided (2026-07-04):**
- **Store repo.** A dedicated public repo,
  `Jaato-framework-and-examples/jaato-telegram-bot-tools-store` — lightweight forks,
  and a bot's PR is structurally scoped to tools only (extra safety on top of the
  review gate). Layout: `tools/<name>.py` + `registry.json` at the root.
- **Index generation.** CI-generated: `generate_registry.py` + a workflow that
  regenerates + commits `registry.json` on merge to `main` (path-filtered, no
  loop). Tools declare deps in-file via `TOOL_DEPS`.

**Still open:**
- **Install ref.** Pin installs to a tag/release, or track `main`@sha from the
  registry. (Registry currently carries per-file `sha256`; a top-level release tag
  would let the bot pin the whole store.)
- **Bot identity (contribute side).** One shared machine account, or per-deployment
  identities (better attribution, more setup).
- **Update policy.** Notify-only, or offer one-tap updates (still user-accepted),
  by comparing an installed tool's `sha256` against the registry.
- **Namespacing.** Contributed tools may collide on name; namespace by contributor,
  or enforce unique names in the generator (it already requires `name` == stem).

See also: [[backlog-host-tool-approval]] (the code-review gap this depends on),
[[dynamic-tool-deps-venv]] (how a shared tool's third-party deps install).

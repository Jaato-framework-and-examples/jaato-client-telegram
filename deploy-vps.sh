#!/usr/bin/env bash
#
# deploy-vps.sh — one-shot bootstrap for the jaato Telegram bot + its server.
#
# Premium-free stack: installs jaato-server + jaato-sdk FROM PyPI (via uv) into a
# uv-managed venv, clones THIS bot repo and installs it editable (the bot is the
# deployed app, not a PyPI package), asks the operator for the Telegram token +
# provider/model/key(s), customizes the agent profile, wires two systemd services
# (server + bot over ws://localhost, polling), and runs a layered health check
# (scaffold validate -> jaato-doctor -> live provider ping).
#
# The framework is a versioned PyPI dependency, not a git checkout — jaato-server
# and jaato-sdk are unpinned by default (latest at deploy); pin exact versions via
# JAATO_SERVER_VERSION / JAATO_SDK_VERSION. Uses `uv`, not `pip`.
#
# Provider selection AND the per-provider key env-var name are discovered from
# `jaato-scaffold explain` — nothing about providers is hardcoded here.
#
# Idempotent: safe to re-run (upgrade = reinstall latest + restart). A full run
#             first snapshots non-code state (persona, profile, config) to a dated
#             dir under ~/.local/share/jaato-tg/deploy-backups/ before it resets.
# Teardown:  ./deploy-vps.sh --uninstall
# Code-only: test a branch's CODE without a full redeploy — updates only src/ and
#            restarts the bot, leaving config, profile, persona and venv untouched:
#            CODE_REF=<branch> ./deploy-vps.sh --code-only
#
# Override anything via env, e.g.:
#   JAATO_SERVER_VERSION=0.11.0 JAATO_SDK_VERSION=0.19.0 BOT_REF=<sha> JAATO_WS_PORT=8090 ./deploy-vps.sh
#
set -euo pipefail

# ── Config (override via env) ────────────────────────────────────────────────
INSTALL_DIR="${JAATO_INSTALL_DIR:-$HOME/jaato-stack}"
BOT_REPO="${BOT_REPO:-https://github.com/Jaato-framework-and-examples/jaato-client-telegram.git}"
# The bot is deployed from git; no tag exists upstream so this tracks master.
BOT_REF="${BOT_REF:-master}"
# jaato-server + jaato-sdk are installed FROM PyPI. Unpinned by default (latest at
# deploy); set these to pin an exact version (e.g. 0.11.0 / 0.19.0).
JAATO_SERVER_VERSION="${JAATO_SERVER_VERSION:-}"
JAATO_SDK_VERSION="${JAATO_SDK_VERSION:-}"
WS_PORT="${JAATO_WS_PORT:-8080}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# ── Derived paths ────────────────────────────────────────────────────────────
VENV="$INSTALL_DIR/venv"; PYV="$VENV/bin/python"
BOT_DIR="$INSTALL_DIR/jaato-client-telegram"   # framework comes from PyPI, not a clone
WORKSPACE="$BOT_DIR/runtime"
# Profiles are a base + a per-provider SET leaf (see write_profile). The bot
# selects the leaf by QUALIFIED PATH ("<set>/telegram_chat"). SET_NAME/leaf paths
# are derived in collect()/write_profile once the provider is known.
PROFILE_DIR="$WORKSPACE/.jaato/profiles"
BASE_PROFILE_FILE="$PROFILE_DIR/_base_telegram_chat.yaml"
STATE_DIR="$HOME/.local/share/jaato-tg"
HOST_TOOLS_DIR="$STATE_DIR/host_tools"; SESSION_STORE="$STATE_DIR/chat_sessions.json"
CFG_DIR="$HOME/.config/jaato-tg"
SERVER_ENV="$CFG_DIR/server.env"; BOT_ENV="$CFG_DIR/bot.env"
WS_TOKEN_FILE="$CFG_DIR/ws.token"; BOT_CONFIG="$CFG_DIR/jaato-client-telegram.yaml"
WHITELIST_FILE="$CFG_DIR/whitelist.json"
# systemd: system-wide units when root (VPS-native), --user units otherwise.
if [ "$(id -u)" -eq 0 ]; then
  SYSTEMD_MODE=system; UNIT_DIR="/etc/systemd/system"; WANTED_BY="multi-user.target"
else
  SYSTEMD_MODE=user; UNIT_DIR="$HOME/.config/systemd/user"; WANTED_BY="default.target"
fi
_sc(){ if [ "$SYSTEMD_MODE" = system ]; then systemctl "$@"; else systemctl --user "$@"; fi; }

# ── Pretty output ────────────────────────────────────────────────────────────
if [ -t 1 ]; then C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_B=$'\e[1m'; C_0=$'\e[0m'
else C_G=; C_Y=; C_R=; C_B=; C_0=; fi
info(){ printf '%s\n' "${C_G}${C_B}▶${C_0} $*"; }
warn(){ printf '%s\n' "${C_Y}⚠ $*${C_0}" >&2; }
die(){  printf '%s\n' "${C_R}✗ $*${C_0}" >&2; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }
ask(){ local p="$1" d="${2:-}" a; if [ -n "$d" ]; then read -rp "  $p [$d]: " a; printf '%s' "${a:-$d}"
       else read -rp "  $p: " a; printf '%s' "$a"; fi; }
ask_secret(){ local p="$1" a; read -rsp "  $p: " a; printf '\n' >&2; printf '%s' "$a"; }
confirm(){ local a; read -rp "  $1 [y/N]: " a; [[ "$a" =~ ^[Yy] ]]; }
scaffold(){ "$PYV" -m shared.scaffold "$@"; }   # available after install()

# ── 1. Preflight ─────────────────────────────────────────────────────────────
_pkg_mgr(){ local m; for m in apt-get dnf yum pacman zypper; do have "$m" && { printf '%s' "$m"; return; }; done; }
install_system_deps(){
  [ "${SKIP_SYSTEM_DEPS:-}" = "1" ] && { warn "SKIP_SYSTEM_DEPS=1 — skipping system package install"; return; }
  local mgr; mgr=$(_pkg_mgr)
  [ -n "$mgr" ] || { warn "no known package manager — ensure git, python3(>=3.10)+venv, pip, a C toolchain and curl are present"; return; }
  local SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  info "Install system deps via $mgr (sudo may prompt)"
  # python3-venv (Debian/Ubuntu split) + a C toolchain (some server deps build).
  case "$mgr" in
    apt-get) $SUDO apt-get update -qq && $SUDO apt-get install -y -qq \
               git python3 python3-venv python3-pip build-essential curl ca-certificates ;;
    dnf|yum) $SUDO "$mgr" install -y -q git python3 python3-pip gcc gcc-c++ make curl ca-certificates ;;
    pacman)  $SUDO pacman -Sy --noconfirm --needed git python python-pip base-devel curl ca-certificates ;;
    zypper)  $SUDO zypper -q install -y git python3 python3-pip gcc gcc-c++ make curl ca-certificates ;;
  esac || warn "system-dep install returned nonzero — continuing (preflight verifies below)"
}
# uv is the package manager (not pip). Bootstrap it via Astral's standalone
# installer if absent — a static binary in ~/.local/bin, no Python dependency.
install_uv(){
  have uv && { info "  uv present ($(uv --version))"; return; }
  info "Install uv (Astral standalone installer)"
  curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv install failed"
  # The installer drops uv in ~/.local/bin (or $XDG_BIN_HOME); expose it now.
  export PATH="$HOME/.local/bin:$PATH"
  have uv || die "uv not on PATH after install (expected ~/.local/bin/uv)"
}
preflight(){
  info "Preflight"
  install_system_deps
  install_uv
  have git || die "git not found (install it or pre-provision system deps)"
  have "$PYTHON_BIN" || die "$PYTHON_BIN not found (need Python >= 3.10)"
  local v; v=$("$PYTHON_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
  "$PYTHON_BIN" -c 'import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) else 1)' \
    || die "Python >= 3.10 required (found $v)"
  if have apparmor_parser && { aa-enabled >/dev/null 2>&1 || true; }; then
    info "  AppArmor present — runner confinement available."
  else
    warn "AppArmor not available — the server will run the runner UNCONFINED (fine for a single-tenant VPS)."
  fi
  printf '  Python %s, git OK. Install dir: %s\n' "$v" "$INSTALL_DIR"
}

# ── 2. Fetch (clone/update at pinned refs) ───────────────────────────────────
_clone_at(){ local repo="$1" dir="$2" ref="$3"
  if [ -d "$dir/.git" ]; then git -C "$dir" fetch --quiet origin
  else git clone --quiet "$repo" "$dir"; fi
  # Hard-reset to the target ref. write_profile/write_bot_config regenerate
  # git-tracked files every run, leaving the tree permanently dirty — so a plain
  # `pull --ff-only` is always blocked (and the old `|| true` swallowed it,
  # pinning the bot repo to its first-cloned commit). Those generated files are
  # disposable (rewritten seconds later), so discard local changes and advance
  # to the fetched tip. Handles a branch ref (origin/<branch>) or a pinned SHA.
  git -C "$dir" checkout --quiet --force "$ref"
  git -C "$dir" reset --hard --quiet "origin/$ref" 2>/dev/null \
    || git -C "$dir" reset --hard --quiet "$ref"
  printf '  %s @ %s\n' "$(basename "$dir")" "$(git -C "$dir" rev-parse --short HEAD)"
}
fetch(){ info "Fetch bot repo (bot=$BOT_REF; jaato-server/sdk come from PyPI, not git)"
  mkdir -p "$INSTALL_DIR"
  _clone_at "$BOT_REPO" "$BOT_DIR" "$BOT_REF"
}

# ── 3. Install (uv venv + PyPI framework + editable bot; no premium) ──────────
install(){ info "Install (uv venv + PyPI jaato-server/jaato-sdk + editable bot)"
  # uv-managed venv (recreated on re-run; reinstall below repopulates it).
  uv venv --python "$PYTHON_BIN" "$VENV"
  # Server extras our profile needs (pexpect for interactive_shell,
  # web/ast/notebook/templates, and the common provider SDKs). NOT `[all]` —
  # that pulls kerberos→gssapi which needs libkrb5-dev and we don't use it.
  local EXTRAS="web,interactive,ast,notebook,templates,diagrams,google,github-models,nim,openrouter"
  local sdk="jaato-sdk"; [ -n "$JAATO_SDK_VERSION" ] && sdk="jaato-sdk==$JAATO_SDK_VERSION"
  local srv="jaato-server[$EXTRAS]"; [ -n "$JAATO_SERVER_VERSION" ] && srv="jaato-server[$EXTRAS]==$JAATO_SERVER_VERSION"
  # Framework FROM PyPI (unpinned = latest unless the version vars are set).
  uv pip install --python "$PYV" "$sdk" "$srv"
  # The bot is the deployed app (not on PyPI) — editable from its clone; its
  # jaato-sdk/jaato-server deps resolve against what we just installed.
  uv pip install --python "$PYV" -e "$BOT_DIR"
  printf '  installed from PyPI: jaato-sdk %s, jaato-server[extras] %s; editable: jaato-client-telegram\n' \
    "$(uv pip show --python "$PYV" jaato-sdk 2>/dev/null | sed -n 's/^Version: //p')" \
    "$(uv pip show --python "$PYV" jaato-server 2>/dev/null | sed -n 's/^Version: //p')"
}

# The provider's key env-var name, discovered from `scaffold explain env`
# (prefer JAATO_*_API_KEY, then *_API_KEY, then *AUTH_TOKEN/_TOKEN; empty = keyless).
_provider_keyvar(){ local provider="$1"
  scaffold explain env --json 2>/dev/null | "$PYV" -c '
import json,sys
d=json.load(sys.stdin); pv=d.get("provider:'"$provider"'",{})
cands=[k for k in pv if any(t in k for t in ("API_KEY","AUTH_TOKEN")) or k.endswith("_TOKEN")]
def rank(k): return (0 if k.startswith("JAATO_") and k.endswith("API_KEY")
  else 1 if k.endswith("API_KEY") else 2)
cands.sort(key=rank)
print(cands[0] if cands else "")'
}

# ── Provider picking, driven entirely by `scaffold explain` ──────────────────
# Echoes "PROVIDER|MODEL|ENVVAR|KEYVALUE" (ENVVAR/KEYVALUE empty for keyless/local).
_pick_provider(){ local role="$1"
  local provs; provs=$(scaffold explain providers --json 2>/dev/null \
    | "$PYV" -c 'import json,sys;print("\n".join(sorted(json.load(sys.stdin))))')
  [ -n "$provs" ] || die "scaffold explain returned no providers"
  printf '\n  %sChoose the %s provider:%s\n' "$C_B" "$role" "$C_0" >&2
  printf '%s\n' "$provs" | nl -w3 -s'. ' >&2
  local n; n=$(ask "provider number")
  local provider; provider=$(printf '%s\n' "$provs" | sed -n "${n}p")
  [ -n "$provider" ] || die "invalid selection"
  # Vision hint from capabilities (handy for the vision tier).
  scaffold explain provider "$provider" --json 2>/dev/null | "$PYV" -c '
import json,sys
d=json.load(sys.stdin); c=d.get("capabilities",{})
print("  images=%s pdf=%s"%(c.get("user_message_images"),c.get("pdf_input")))' >&2 || true
  local model; model=$(ask "model id for $provider")
  [ -n "$model" ] || die "model is required"
  local envvar; envvar=$(_provider_keyvar "$provider")
  local keyval=""
  if [ -n "$envvar" ]; then keyval=$(ask_secret "API key/token for $provider (-> \$$envvar)")
  else warn "  $provider exposes no API-key env var (local/keyless) — set host/endpoint env vars yourself if needed."; fi
  printf '%s|%s|%s|%s' "$provider" "$model" "$envvar" "$keyval"
}

# ── 4. Collect operator input ────────────────────────────────────────────────
# Interactive by default; fully NON-INTERACTIVE when these env vars are set
# (handy for automation / a scripted VPS test):
#   TELEGRAM_BOT_TOKEN, EXEC_PROVIDER, EXEC_MODEL, EXEC_KEY,
#   and optionally VISION_PROVIDER, VISION_MODEL, VISION_KEY.
collect(){ info "Configuration"
  local noninteractive=0
  { [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || [ -n "${EXEC_PROVIDER:-}" ]; } && noninteractive=1
  TG_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
  [ -n "$TG_TOKEN" ] || TG_TOKEN=$(ask_secret "Telegram bot token (from @BotFather)")
  [ -n "$TG_TOKEN" ] || die "Telegram token is required"

  if [ -n "${EXEC_PROVIDER:-}" ]; then
    EXEC_MODEL="${EXEC_MODEL:?EXEC_MODEL required when EXEC_PROVIDER is set}"
    EXEC_ENVVAR=$(_provider_keyvar "$EXEC_PROVIDER"); EXEC_KEY="${EXEC_KEY:-}"
    info "  main tier (env): $EXEC_PROVIDER / $EXEC_MODEL -> \$${EXEC_ENVVAR:-<keyless>}"
  else
    printf '\n  %sMain (text) tier%s\n' "$C_B" "$C_0"
    IFS='|' read -r EXEC_PROVIDER EXEC_MODEL EXEC_ENVVAR EXEC_KEY < <(_pick_provider "main/text")
  fi

  VISION_PROVIDER="${VISION_PROVIDER:-}"; VISION_MODEL="${VISION_MODEL:-}"; VISION_ENVVAR=""; VISION_KEY="${VISION_KEY:-}"
  if [ -n "$VISION_PROVIDER" ]; then
    VISION_MODEL="${VISION_MODEL:?VISION_MODEL required when VISION_PROVIDER is set}"
    VISION_ENVVAR=$(_provider_keyvar "$VISION_PROVIDER")
    info "  vision tier (env): $VISION_PROVIDER / $VISION_MODEL"
  elif [ "$noninteractive" = "1" ]; then
    warn "  Vision disabled (non-interactive run, no VISION_PROVIDER set)."
  elif confirm "Enable image/PDF understanding (vision tier)?"; then
    IFS='|' read -r VISION_PROVIDER VISION_MODEL VISION_ENVVAR VISION_KEY < <(_pick_provider "vision")
  else warn "  Vision disabled — the bot does text + tools; images/PDFs won't be understood."; fi

  # Optional CODER tier — a code-specialized model the agent enters ON DEMAND
  # (enter_tier('coder')) for real code work, leaving the cheap executor as the
  # default. A CUSTOM tier name, so it REQUIRES a description (CODER_DESCRIPTION
  # overrides the default prose). Non-interactive via CODER_PROVIDER / CODER_MODEL
  # / CODER_KEY. Nothing is hardcoded: the model/provider come from env or prompt.
  CODER_PROVIDER="${CODER_PROVIDER:-}"; CODER_MODEL="${CODER_MODEL:-}"; CODER_ENVVAR=""; CODER_KEY="${CODER_KEY:-}"
  if [ -n "$CODER_PROVIDER" ]; then
    CODER_MODEL="${CODER_MODEL:?CODER_MODEL required when CODER_PROVIDER is set}"
    CODER_ENVVAR=$(_provider_keyvar "$CODER_PROVIDER")
    info "  coder tier (env): $CODER_PROVIDER / $CODER_MODEL"
  elif [ "$noninteractive" = "1" ]; then
    warn "  Coder tier disabled (non-interactive run, no CODER_PROVIDER set)."
  elif confirm "Enable an on-demand coder tier (code-specialized model)?"; then
    IFS='|' read -r CODER_PROVIDER CODER_MODEL CODER_ENVVAR CODER_KEY < <(_pick_provider "coder")
  else warn "  Coder tier disabled — the bot uses the main tier for code too."; fi
  CODER_DESCRIPTION="${CODER_DESCRIPTION:-Write, edit, and reason about code to a plan. The strongest code model here; enter for real coding tasks (writing or editing files, debugging, multi-step implementation), then switch back to executor for ordinary conversation.}"

  # The profile SET name (a subdir under .jaato/profiles/). Defaults to the main
  # provider so the leaf reads as "<provider>/telegram_chat"; override with PROFILE_SET.
  SET_NAME="${PROFILE_SET:-$EXEC_PROVIDER}"

  # Whitelist (username-based access control). Non-interactive via
  # WHITELIST_ADMINS / WHITELIST_USERS (comma-separated Telegram usernames).
  WL_ADMINS="${WHITELIST_ADMINS:-}"; WL_USERS="${WHITELIST_USERS:-}"
  if [ -z "$WL_ADMINS$WL_USERS" ] && [ "$noninteractive" != "1" ]; then
    WL_ADMINS=$(ask "Admin Telegram username(s), comma-separated, no @ (they can use the bot + admin cmds)")
    WL_USERS=$(ask "Additional allowed username(s), comma-separated (optional)" "")
  fi

  # Optional: a GitHub token so the bot can PROPOSE tools to the shared store via
  # PR (share_tool). Use a NON-admin bot GitHub account's classic PAT (public_repo
  # scope). Blank = off (browse/install still work). Non-interactive: from env.
  # An existing bot.env token is preserved in write_env when this is blank.
  STORE_TOK="${JAATO_TOOLSTORE_GH_TOKEN:-}"
  if [ -z "$STORE_TOK" ] && [ "$noninteractive" != "1" ]; then
    printf '\n  %sTool-store contribution (optional)%s — lets the bot open PRs to\n' "${C_B:-}" "${C_0:-}"
    printf '  propose tools to the shared store. Use a NON-admin bot GitHub account.\n'
    STORE_TOK=$(ask_secret "GitHub token (classic PAT, public_repo) — blank to skip")
  fi

  # Optional: the daemon's PUBLIC wake URL so a maintainer's review on a shared-tool
  # PR wakes the bot to address it (docs/design/pr-review-feedback-loop.md). Needs the
  # daemon reachable from GitHub (public bind + the Ed25519 signature gate). Blank =
  # ingress off. Non-interactive: from env. A blank on a redeploy leaves any existing
  # wake.json untouched (so re-running without re-entering it won't disable the ingress).
  WAKE_URL="${JAATO_WAKE_PUBLIC_URL:-}"
  if [ -z "$WAKE_URL" ] && [ "$noninteractive" != "1" ]; then
    printf '\n  %sReview-wake ingress (optional)%s — lets a review on a shared-tool PR\n' "${C_B:-}" "${C_0:-}"
    printf '  wake the bot to address it. Needs a PUBLIC url the GitHub relay can POST\n'
    printf '  to (this daemon, public bind, signature-gated).\n'
    WAKE_URL=$(ask "Public wake URL (e.g. http://<this-host-ip>:9110/wake) — blank to skip" "")
  fi

  WS_TOKEN=$("$PYV" -c 'import secrets;print(secrets.token_urlsafe(32))')
}

# ── 5. Write env files + token (chmod 600) ───────────────────────────────────
write_env(){ info "Write secrets (chmod 600)"
  mkdir -p "$CFG_DIR" "$STATE_DIR" "$HOST_TOOLS_DIR"
  umask 077
  # The tool-store contribution token (enables share_tool): what collect()
  # gathered (prompt / env), else preserve an existing one from bot.env so a
  # redeploy never drops it. Empty ⇒ contribution stays off (no hardcoded default).
  local store_tok="${STORE_TOK:-}"
  [ -z "$store_tok" ] && [ -f "$BOT_ENV" ] && \
    store_tok=$(sed -n 's/^JAATO_TOOLSTORE_GH_TOKEN=//p' "$BOT_ENV" | head -1)
  printf '%s' "$WS_TOKEN" > "$WS_TOKEN_FILE"
  { printf 'JAATO_WS_TOKEN=%s\n' "$WS_TOKEN"
    [ -n "$EXEC_ENVVAR" ]   && printf '%s=%s\n' "$EXEC_ENVVAR" "$EXEC_KEY"
    [ -n "$VISION_ENVVAR" ] && [ "$VISION_ENVVAR" != "$EXEC_ENVVAR" ] \
        && printf '%s=%s\n' "$VISION_ENVVAR" "$VISION_KEY"
    [ -n "$CODER_ENVVAR" ] && [ "$CODER_ENVVAR" != "$EXEC_ENVVAR" ] \
        && [ "$CODER_ENVVAR" != "$VISION_ENVVAR" ] \
        && printf '%s=%s\n' "$CODER_ENVVAR" "$CODER_KEY"
  } > "$SERVER_ENV"
  { printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TG_TOKEN"
    printf 'JAATO_WS_TOKEN=%s\n' "$WS_TOKEN"
    printf 'JAATO_TG_WORKSPACE=%s\n' "$WORKSPACE"
    printf 'JAATO_TG_HOST_TOOLS_DIR=%s\n' "$HOST_TOOLS_DIR"
    printf 'JAATO_TG_SESSION_STORE=%s\n' "$SESSION_STORE"
    [ -n "$store_tok" ] && printf 'JAATO_TOOLSTORE_GH_TOKEN=%s\n' "$store_tok"
  } > "$BOT_ENV"
  chmod 600 "$WS_TOKEN_FILE" "$SERVER_ENV" "$BOT_ENV"
}

# ── 5b. Seed curated host tools (repo is the source of truth) ─────────────────
# HOST_TOOLS_DIR is bot-owned and OUTSIDE the workspace, so the confined runner
# can't tamper with it; tools placed here load at startup without re-prompt.
# The curated set is DISCOVERED from the repo at run time (glob over
# examples/host_tools/*.py) — never a hardcoded list — so example tools added to
# the repo later ship automatically. We overwrite the curated files on every run
# (repo wins → upgrades refresh them) but never delete tools that aren't in the
# repo, leaving runtime-installed / operator "foreign" tools untouched.
seed_host_tools(){ info "Seed curated host tools -> $HOST_TOOLS_DIR"
  local src="$BOT_DIR/examples/host_tools"
  [ -d "$src" ] || { warn "no examples/host_tools in repo — skipping tool seed"; return; }
  mkdir -p "$HOST_TOOLS_DIR"
  local n=0
  for f in "$src"/*.py; do
    [ -e "$f" ] || continue                          # empty-glob guard
    case "$(basename "$f")" in _*) continue;; esac   # skip private modules
    cp -f "$f" "$HOST_TOOLS_DIR/" && n=$((n+1))
  done
  printf '  seeded/refreshed %d curated tool(s); foreign tools left untouched\n' "$n"
}

# ── 6. Customize the agent profile (env-resolved keys; no secret inlined) ─────
# Profiles are written as a PROVIDER-AGNOSTIC base + a per-provider SET leaf.
# The leaf `inherits: [_base_telegram_chat]` and is selected by the bot via the
# qualified path "<set>/telegram_chat" (write_bot_config). This keeps the role
# (plugins, venv, memory scope) in one place and lets a second provider set be
# added as one more leaf. Tiers: executor (initial/fallback), an optional custom
# `coder` tier entered on demand, and an optional vision tier.
write_profile(){
  local set_name="${SET_NAME:-$EXEC_PROVIDER}"
  local set_dir="$PROFILE_DIR/$set_name"
  LEAF_PROFILE_FILE="$set_dir/telegram_chat.yaml"
  PROFILE_REF="$set_name/telegram_chat"           # qualified path for the bot config + health check
  info "Customize profile -> $BASE_PROFILE_FILE + $LEAF_PROFILE_FILE"
  mkdir -p "$set_dir"

  local apparmor=false
  have apparmor_parser && aa-enabled >/dev/null 2>&1 && apparmor=true

  # Tier table. executor always; coder/vision only when their provider was chosen.
  # A custom tier name (coder) REQUIRES a description — emitted as a folded scalar.
  local tiers="  executor:
    model: \"$EXEC_MODEL\"
    provider: \"$EXEC_PROVIDER\""
  if [ -n "$CODER_PROVIDER" ]; then
    tiers="$tiers
  coder:
    model: \"$CODER_MODEL\"
    provider: \"$CODER_PROVIDER\"
    description: >-
      $CODER_DESCRIPTION"
  fi
  if [ -n "$VISION_PROVIDER" ]; then
    tiers="$tiers
  vision:
    model: \"$VISION_MODEL\"
    provider: \"$VISION_PROVIDER\""
  fi

  # --- base: provider-agnostic role. Static => QUOTED heredoc (no interpolation,
  # so a literal $ in a comment is safe here, unlike the leaf below). ------------
  cat > "$BASE_PROFILE_FILE" <<'YAML'
# Generated by deploy-vps.sh — PROVIDER-AGNOSTIC base for the Telegram bot's
# per-chat session. Binds no provider/model; a set leaf (e.g.
# <provider>/telegram_chat.yaml) inherits this and does that. On inherit:
# plugins UNION (a leaf's `plugins: []` keeps this list), plugin_configs
# per-key dict-merge, max_turns most-restrictive-wins.
name: _base_telegram_chat
description: Provider-agnostic base for the Telegram bot's per-chat session.
plugins:
  - clarification
  - web_search
  - web_fetch
  - references
  - result_grep
  - memory
  - waypoint
  - file_edit
  - filesystem_query
  - ast_search
  - lsp
  - cli
  - interactive_shell
  - notebook
  - subagent
  - template
  - prompt_library
  - environment
max_turns: 12
plugin_configs:
  memory:
    # Single shared workspace => "project" scope already spans all of a user's
    # chats; keeps memories off the HOME/global tier (server PR #468).
    allowed_scopes: ["project"]
  # Dynamic-tool dependency venv: the confined runner installs deps here
  # (a bare pip install in notebook/cli/shell lands in workspace .jaato/tool-venv,
  # via the pip shim + the venv-bin ix apparmor grant, server #479), and the
  # bot prepends its site-packages so an in-process host tool imports them.
  # Path is workspace-relative; MUST match the bot's jaato_ws.host_tools_venv.
  notebook:
    workspace_venv: ".jaato/tool-venv"
  cli:
    workspace_venv: ".jaato/tool-venv"
  interactive_shell:
    workspace_venv: ".jaato/tool-venv"
YAML

  # --- set leaf: binds provider + tiers. UNQUOTED heredoc (interpolates
  # $EXEC_*/$tiers/$apparmor) — keep it free of backticks and stray $. ----------
  cat > "$LEAF_PROFILE_FILE" <<YAML
# Generated by deploy-vps.sh — $set_name set. Inherits _base_telegram_chat and
# binds the provider + per-turn model tiers. Provider keys resolve from env vars
# (server.env), so no secret is inlined here. Selected via the bot's qualified
# path jaato_ws.profile: "$PROFILE_REF".
name: telegram_chat
description: Telegram bot per-chat session — $set_name set.
inherits: [_base_telegram_chat]
plugins: []                 # keep the inherited base plugin surface (UNION)
provider: "$EXEC_PROVIDER"
model: "$EXEC_MODEL"         # ignored while model_tiers is non-empty; documents the initial model
apparmor: $apparmor
model_tiers:
$tiers
  initial: executor
  fallback: executor
YAML

  printf '  set=%s provider=%s model=%s coder=%s vision=%s apparmor=%s\n' \
    "$set_name" "$EXEC_PROVIDER" "$EXEC_MODEL" \
    "${CODER_PROVIDER:+$CODER_MODEL}" "${VISION_PROVIDER:+$VISION_MODEL}" "$apparmor"
}

# ── 6b. Whitelist (username-based access control) ────────────────────────────
write_whitelist(){ info "Write whitelist -> $WHITELIST_FILE"
  "$PYV" - "$WHITELIST_FILE" "$WL_ADMINS" "$WL_USERS" <<'PY'
import json, sys
from datetime import datetime
path, admins_s, users_s = sys.argv[1], sys.argv[2], sys.argv[3]
parse = lambda s: [u.strip().lstrip("@") for u in s.split(",") if u.strip()]
admins, users = parse(admins_s), parse(users_s)
now = datetime.now().isoformat(timespec="seconds")
seen, entries = set(), []
for u in admins + users:
    if u not in seen:
        seen.add(u); entries.append({"username": u, "added_by": "deploy-vps.sh", "added_at": now})
# entries present -> lock to the whitelist; none -> open (with a warning below).
data = {"enabled": bool(entries), "admin_usernames": admins,
        "entries": entries, "access_requests": []}
json.dump(data, open(path, "w"), indent=2)
print(f"  {len(entries)} allowed user(s), {len(admins)} admin(s), enabled={bool(entries)}")
PY
  [ -n "$WL_ADMINS$WL_USERS" ] || warn "  No whitelist users given — the bot is OPEN to anyone. Set WHITELIST_ADMINS to lock it down."
}

# ── 7. Bot config (ws://localhost, polling, no TLS/servers.json) ─────────────
write_bot_config(){ info "Write bot config -> $BOT_CONFIG"
  cat > "$BOT_CONFIG" <<YAML
telegram:
  bot_token: "\${TELEGRAM_BOT_TOKEN}"
  mode: "polling"
jaato_ws:
  url: "ws://localhost:$WS_PORT"
  tls:
    enabled: false
  secret_token: "\${JAATO_WS_TOKEN}"
  # Qualified set path — resolves .jaato/profiles/$PROFILE_REF.yaml (the leaf that
  # inherits _base_telegram_chat). No JAATO_PROFILE_SET needed.
  profile: "$PROFILE_REF"
  agent: "telegram_chat"
  workspace: "\${JAATO_TG_WORKSPACE}"
  host_tools_dir: "\${JAATO_TG_HOST_TOOLS_DIR}"
  # Same tool-venv the server installs dynamic-tool deps into (profile
  # plugin_configs.<notebook|cli|interactive_shell>.workspace_venv). The bot
  # prepends its site-packages to sys.path so in-process host tools import them.
  host_tools_venv: "\${JAATO_TG_WORKSPACE}/.jaato/tool-venv"
session:
  max_concurrent: 50
  session_store_path: "\${JAATO_TG_SESSION_STORE}"
YAML
}

# ── 7b. Review-wake ingress (opt-in) ─────────────────────────────────────────
# The daemon's HTTP wake ingress that the store's review-relay POSTs to, so a
# reviewer comment on a shared-tool PR wakes the bot to address it
# (docs/design/pr-review-feedback-loop.md). OPT-IN: only when the operator declares
# the PUBLIC url the GitHub relay must reach. Public bind + the daemon's Stage-B
# Ed25519 signature gate (a bad/missing signature is 401) + rate-limit — exposure is
# safe, the same posture as any signed webhook; the trust key is per-binding
# (session-declared at bind_wake), so nothing is configured here. Empty
# JAATO_WAKE_PUBLIC_URL => no wake.json => ingress stays disabled (the default).
write_wake_json(){
  local pub="${WAKE_URL:-}"                   # from collect() (prompt or env)
  local port="${JAATO_WAKE_PORT:-9110}"
  local wake_json="$HOME/.jaato/wake.json"    # daemon reads ~/.jaato/wake.json (Path.home())
  if [ -z "$pub" ]; then
    if [ -f "$wake_json" ]; then
      info "Review-wake ingress: no URL given — keeping existing $wake_json unchanged"
    else
      info "Review-wake ingress: no URL given — ingress OFF (no wake.json)"
    fi
    return 0
  fi
  info "Write wake ingress -> $wake_json (public bind 0.0.0.0:$port, signature-gated)"
  mkdir -p "$HOME/.jaato"
  cat > "$wake_json" <<JSON
{
  "enabled": true,
  "host": "0.0.0.0",
  "port": $port,
  "path": "/wake",
  "public_url": "$pub",
  "rate_limit_per_second": 5,
  "replay_window_seconds": 300
}
JSON
  chmod 600 "$wake_json"
  if have ufw; then ufw allow "$port"/tcp >/dev/null 2>&1 && info "  ufw: allowed $port/tcp (wake ingress)" || true; fi
  printf '  public_url=%s  bind=0.0.0.0:%s\n' "$pub" "$port"
}

# ── 8. systemd units (server + bot) — system-wide as root, else --user ───────
install_units(){ info "Install systemd units ($SYSTEMD_MODE mode)"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/jaato-server.service" <<UNIT
[Unit]
Description=jaato server (WebSocket daemon for the Telegram bot)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
EnvironmentFile=$SERVER_ENV
ExecStart=$PYV -m server --web-socket :$WS_PORT --ws-token-file $WS_TOKEN_FILE
Restart=on-failure
RestartSec=5
[Install]
WantedBy=$WANTED_BY
UNIT
  cat > "$UNIT_DIR/jaato-tg.service" <<UNIT
[Unit]
Description=jaato Telegram bot client
After=jaato-server.service
Requires=jaato-server.service
[Service]
Type=simple
EnvironmentFile=$BOT_ENV
ExecStart=$VENV/bin/jaato-tg --config $BOT_CONFIG --whitelist $WHITELIST_FILE
Restart=on-failure
RestartSec=10
[Install]
WantedBy=$WANTED_BY
UNIT
  _sc daemon-reload
  # --user only: survive logout / start at boot via linger (system units don't need it).
  [ "$SYSTEMD_MODE" = user ] && { loginctl enable-linger "$USER" >/dev/null 2>&1 \
    || warn "could not enable linger (services won't start at boot without it)"; }
  _sc enable jaato-server.service jaato-tg.service >/dev/null 2>&1 || true
}

# ── 9. Start + layered health check ──────────────────────────────────────────
_live_ping(){   # SDK facade end-to-end: connect -> session(profile) -> ask
  # shellcheck disable=SC1090
  set -a; . "$SERVER_ENV"; set +a
  "$PYV" - "$WS_PORT" "$WS_TOKEN" "$WORKSPACE" "$PROFILE_REF" <<'PY'
import asyncio,sys
port,token,ws,profile = sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]
import jaato
async def main():
    try:
        from jaato_sdk.events import ClientType
        async with jaato.session(mode="ws", url=f"ws://localhost:{port}", token=token,
                                  client_type=ClientType.CHAT, workspace_path=ws,
                                  config_root=f"{ws}/.jaato", profile=profile,
                                  agent="telegram_chat") as s:
            ans = await s.ask("Reply with exactly: OK")
            print("LIVE_OK:", (ans or "").strip()[:60]); return 0
    except Exception as e:
        print("LIVE_FAIL:", type(e).__name__, str(e)[:200]); return 1
sys.exit(asyncio.run(main()))
PY
}
start_and_check(){ info "Start server + health check"
  _sc restart jaato-server.service
  for _ in $(seq 1 30); do "$PYV" -m server --web-socket ":$WS_PORT" --status >/dev/null 2>&1 && break; sleep 1; done

  info "  validate profile (jaato-scaffold validate)"
  scaffold validate "$LEAF_PROFILE_FILE" || die "profile validation failed (see above) — fix the profile and re-run"

  info "  preflight WS/auth (jaato-doctor)"
  "$PYV" -m jaato_sdk.doctor --web-socket ":$WS_PORT" --ws-token-file "$WS_TOKEN_FILE" --no-auto-start \
    || warn "jaato-doctor reported issues (continuing to the live check)"

  info "  live provider check (connect + ask)"
  local out; out=$(_live_ping || true)
  printf '  %s\n' "$out"
  case "$out" in
    *LIVE_OK:*)
      info "  provider + model + key OK ✓" ;;
    *RateLimit*|*rate*limit*|*429*|*quota*)
      # The request REACHED + AUTHENTICATED with the provider — config is valid,
      # it's just throttled. Don't abort; the bot will work once the limit clears.
      warn "  provider reached + authenticated but RATE-LIMITED right now — config is valid; the bot will answer once the limit clears / on a higher tier." ;;
    *)
      die "live provider check failed — verify the provider/model/key, then re-run. ($out)" ;;
  esac

  info "Start the bot"
  _sc restart jaato-tg.service
  sleep 3
  if _sc is-active --quiet jaato-tg.service; then
    info "Bot is running. Message it on Telegram to begin."
  else
    local j="journalctl -u jaato-tg -e"; [ "$SYSTEMD_MODE" = user ] && j="journalctl --user -u jaato-tg -e"
    die "bot failed to start — check: $j"
  fi
}

# ── Uninstall ────────────────────────────────────────────────────────────────
uninstall(){ info "Uninstall ($SYSTEMD_MODE mode)"
  _sc disable --now jaato-tg.service jaato-server.service 2>/dev/null || true
  rm -f "$UNIT_DIR/jaato-tg.service" "$UNIT_DIR/jaato-server.service"
  _sc daemon-reload 2>/dev/null || true
  warn "Left in place (delete manually if wanted): $INSTALL_DIR, $CFG_DIR, $STATE_DIR"
  info "Services removed."
}

# ── Code-only update (branch/PR testing; preserves config, profile, persona) ──
# Fetch the bot repo and update ONLY tracked source (src/) to a ref, then restart
# the bot — SKIPPING the full deploy's reset/regenerate/reinstall. The bot is an
# editable install, so a restart re-imports the new code; runtime/ (persona,
# profile), the generated config, and the venv are left untouched. This is the
# safe path on a hand-managed box (a full deploy's `git reset --hard` would clobber
# a customized persona). For pure CODE changes — a branch that adds a NEW dependency
# needs a full re-run so uv installs it. Ref: CODE_REF (else BOT_REF, else master).
deploy_code_only(){
  local ref="${CODE_REF:-$BOT_REF}"
  info "Code-only update: bot src/ -> '$ref' (config, profile, persona, venv untouched)"
  [ -d "$BOT_DIR/.git" ] || die "code-only needs an existing checkout at $BOT_DIR — run a full deploy first"
  git -C "$BOT_DIR" fetch --quiet origin || die "git fetch failed"
  git -C "$BOT_DIR" checkout --quiet "origin/$ref" -- src/ 2>/dev/null \
    || git -C "$BOT_DIR" checkout --quiet "$ref" -- src/ \
    || die "could not checkout src/ from '$ref' (does the branch exist on origin?)"
  printf '  src/ updated to %s\n' "$(git -C "$BOT_DIR" rev-parse --short "origin/$ref" 2>/dev/null || echo "$ref")"
  _sc restart jaato-tg.service
  sleep 3
  if _sc is-active --quiet jaato-tg.service; then
    info "Bot restarted on '$ref' code. Server, config, and persona left as-is."
    info "  Revert with: CODE_REF=master $0 --code-only"
  else
    local j="journalctl -u jaato-tg -e"; [ "$SYSTEMD_MODE" = user ] && j="journalctl --user -u jaato-tg -e"
    die "bot failed to restart — check: $j"
  fi
}

# ── Backup NON-CODE state before a (destructive) full deploy ─────────────────
# A full run does `git reset --hard` (reverts tracked runtime files — a
# customized persona/profile) AND regenerates $CFG_DIR — so snapshot everything
# that isn't source first, into a dated dir OUTSIDE the clone. --code-only skips
# this (it touches only src/). No-ops on a fresh box (nothing to back up).
backup_noncode(){
  { [ -d "$BOT_DIR/runtime/.jaato" ] || [ -d "$CFG_DIR" ]; } || {
    info "No prior install to back up (fresh box)"; return; }
  local dest="$STATE_DIR/deploy-backups/$(date +%Y%m%d-%H%M%S)"
  info "Backup non-code state -> $dest"
  mkdir -p "$dest"; chmod 700 "$STATE_DIR/deploy-backups" "$dest" 2>/dev/null || true
  # Customizable workspace tree (persona .jaato/agents, profiles, scripts, session
  # transcripts) — reset --hard reverts the tracked ones (persona/profile).
  [ -d "$BOT_DIR/runtime/.jaato" ] && cp -a "$BOT_DIR/runtime/.jaato" "$dest/runtime-jaato"
  # Generated config + secrets (write_env/write_bot_config/write_profile overwrite
  # these every run). Kept mode-restricted since it holds tokens/keys.
  [ -d "$CFG_DIR" ] && { cp -a "$CFG_DIR" "$dest/config"; chmod -R go-rwx "$dest/config" 2>/dev/null || true; }
  printf '  backed up (restore a file with: cp %s/<path> <target>)\n' "$dest"
}

main(){
  case "${1:-}" in
    --uninstall) uninstall; exit 0 ;;
    --code-only) deploy_code_only; exit 0 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
  esac
  printf '%s\n' "${C_B}jaato Telegram bot — VPS bootstrap (premium-free)${C_0}"
  backup_noncode
  preflight; fetch; install; collect; write_env; seed_host_tools; write_profile; write_whitelist; write_bot_config; write_wake_json
  install_units; start_and_check
  printf '\n%s\n' "${C_G}${C_B}✓ Done.${C_0} Logs: journalctl --user -u jaato-tg -f   |   Re-run to upgrade   |   --uninstall to remove"
}
main "$@"

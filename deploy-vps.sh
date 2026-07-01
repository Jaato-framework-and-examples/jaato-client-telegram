#!/usr/bin/env bash
#
# deploy-vps.sh — one-shot bootstrap for the jaato Telegram bot + its server.
#
# Premium-free stack: clones 2 public repos (the jaato monorepo = sdk+server,
# and this bot), installs them in a venv, asks the operator for the Telegram
# token + provider/model/key(s), customizes the agent profile, wires two systemd
# --user services (server + bot over ws://localhost, polling), and runs a layered
# health check (scaffold validate -> jaato-doctor -> live provider ping).
#
# Provider selection AND the per-provider key env-var name are discovered from
# `jaato-scaffold explain` — nothing about providers is hardcoded here.
#
# Idempotent: safe to re-run (upgrade = pull + reinstall + restart).
# Teardown:  ./deploy-vps.sh --uninstall
#
# Override anything via env, e.g.:
#   JAATO_REF=<sha> BOT_REF=<sha> JAATO_WS_PORT=8090 ./deploy-vps.sh
#
set -euo pipefail

# ── Config (override via env) ────────────────────────────────────────────────
INSTALL_DIR="${JAATO_INSTALL_DIR:-$HOME/jaato-stack}"
JAATO_REPO="${JAATO_REPO:-https://github.com/Jaato-framework-and-examples/jaato.git}"
BOT_REPO="${BOT_REPO:-https://github.com/Jaato-framework-and-examples/jaato-client-telegram.git}"
# No git tags exist upstream — pin a SHA for reproducibility (these track main/master).
JAATO_REF="${JAATO_REF:-main}"
BOT_REF="${BOT_REF:-master}"
WS_PORT="${JAATO_WS_PORT:-8080}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# ── Derived paths ────────────────────────────────────────────────────────────
VENV="$INSTALL_DIR/venv"; PYV="$VENV/bin/python"
JAATO_DIR="$INSTALL_DIR/jaato"; BOT_DIR="$INSTALL_DIR/jaato-client-telegram"
WORKSPACE="$BOT_DIR/runtime"
PROFILE_DIR="$WORKSPACE/.jaato/profiles"; PROFILE_FILE="$PROFILE_DIR/telegram_chat.yaml"
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
preflight(){
  info "Preflight"
  install_system_deps
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
  git -C "$dir" checkout --quiet "$ref"
  git -C "$dir" pull --quiet --ff-only origin "$ref" 2>/dev/null || true
  printf '  %s @ %s\n' "$(basename "$dir")" "$(git -C "$dir" rev-parse --short HEAD)"
}
fetch(){ info "Fetch repos (pinned: jaato=$JAATO_REF bot=$BOT_REF)"
  mkdir -p "$INSTALL_DIR"
  _clone_at "$JAATO_REPO" "$JAATO_DIR" "$JAATO_REF"
  _clone_at "$BOT_REPO"  "$BOT_DIR"  "$BOT_REF"
}

# ── 3. Install (venv + editable installs; no premium) ────────────────────────
install(){ info "Install (venv + editable packages)"
  [ -x "$PYV" ] || "$PYTHON_BIN" -m venv "$VENV"
  "$PYV" -m pip install --quiet --upgrade pip wheel
  "$PYV" -m pip install --quiet -e "$JAATO_DIR/jaato-sdk"
  # Server WITH the plugin + provider extras our profile needs (pexpect for
  # interactive_shell, web/ast/notebook/templates, and the common provider SDKs).
  # NOT `[all]` — that pulls kerberos→gssapi which needs libkrb5-dev and we don't
  # use it. Anthropic's SDK is a base dep already.
  "$PYV" -m pip install --quiet -e \
    "$JAATO_DIR/jaato-server[web,interactive,ast,notebook,templates,diagrams,google,github-models,nim,openrouter]"
  "$PYV" -m pip install --quiet -e "$BOT_DIR"
  printf '  installed: jaato-sdk, jaato-server[extras], jaato-client-telegram (no premium)\n'
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

  # Whitelist (username-based access control). Non-interactive via
  # WHITELIST_ADMINS / WHITELIST_USERS (comma-separated Telegram usernames).
  WL_ADMINS="${WHITELIST_ADMINS:-}"; WL_USERS="${WHITELIST_USERS:-}"
  if [ -z "$WL_ADMINS$WL_USERS" ] && [ "$noninteractive" != "1" ]; then
    WL_ADMINS=$(ask "Admin Telegram username(s), comma-separated, no @ (they can use the bot + admin cmds)")
    WL_USERS=$(ask "Additional allowed username(s), comma-separated (optional)" "")
  fi

  WS_TOKEN=$("$PYV" -c 'import secrets;print(secrets.token_urlsafe(32))')
}

# ── 5. Write env files + token (chmod 600) ───────────────────────────────────
write_env(){ info "Write secrets (chmod 600)"
  mkdir -p "$CFG_DIR" "$STATE_DIR" "$HOST_TOOLS_DIR"
  umask 077
  printf '%s' "$WS_TOKEN" > "$WS_TOKEN_FILE"
  { printf 'JAATO_WS_TOKEN=%s\n' "$WS_TOKEN"
    [ -n "$EXEC_ENVVAR" ]   && printf '%s=%s\n' "$EXEC_ENVVAR" "$EXEC_KEY"
    [ -n "$VISION_ENVVAR" ] && [ "$VISION_ENVVAR" != "$EXEC_ENVVAR" ] \
        && printf '%s=%s\n' "$VISION_ENVVAR" "$VISION_KEY"
  } > "$SERVER_ENV"
  { printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TG_TOKEN"
    printf 'JAATO_WS_TOKEN=%s\n' "$WS_TOKEN"
    printf 'JAATO_TG_WORKSPACE=%s\n' "$WORKSPACE"
    printf 'JAATO_TG_HOST_TOOLS_DIR=%s\n' "$HOST_TOOLS_DIR"
    printf 'JAATO_TG_SESSION_STORE=%s\n' "$SESSION_STORE"
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
write_profile(){ info "Customize profile -> $PROFILE_FILE"
  mkdir -p "$PROFILE_DIR"
  local apparmor=false
  have apparmor_parser && aa-enabled >/dev/null 2>&1 && apparmor=true
  local tiers="  executor:
    model: \"$EXEC_MODEL\"
    provider: \"$EXEC_PROVIDER\""
  if [ -n "$VISION_PROVIDER" ]; then
    tiers="$tiers
  vision:
    model: \"$VISION_MODEL\"
    provider: \"$VISION_PROVIDER\""
  fi
  cat > "$PROFILE_FILE" <<YAML
# Generated by deploy-vps.sh — provider/model are operator-chosen; provider keys
# resolve from env vars (server.env), so no secret is inlined here.
name: telegram_chat
description: Conversational assistant for the Telegram bot client.
provider: "$EXEC_PROVIDER"
model: "$EXEC_MODEL"
apparmor: $apparmor
model_tiers:
$tiers
  initial: executor
  fallback: executor
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
YAML
  printf '  provider=%s model=%s vision=%s apparmor=%s\n' \
    "$EXEC_PROVIDER" "$EXEC_MODEL" "${VISION_PROVIDER:-off}" "$apparmor"
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
  profile: "telegram_chat"
  agent: "telegram_chat"
  workspace: "\${JAATO_TG_WORKSPACE}"
  host_tools_dir: "\${JAATO_TG_HOST_TOOLS_DIR}"
session:
  max_concurrent: 50
  session_store_path: "\${JAATO_TG_SESSION_STORE}"
YAML
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
  "$PYV" - "$WS_PORT" "$WS_TOKEN" "$WORKSPACE" <<'PY'
import asyncio,sys
port,token,ws = sys.argv[1],sys.argv[2],sys.argv[3]
import jaato
async def main():
    try:
        from jaato_sdk.events import ClientType
        async with jaato.session(mode="ws", url=f"ws://localhost:{port}", token=token,
                                  client_type=ClientType.CHAT, workspace_path=ws,
                                  config_root=f"{ws}/.jaato", profile="telegram_chat",
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
  scaffold validate "$PROFILE_FILE" || die "profile validation failed (see above) — fix the profile and re-run"

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

main(){
  case "${1:-}" in
    --uninstall) uninstall; exit 0 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
  esac
  printf '%s\n' "${C_B}jaato Telegram bot — VPS bootstrap (premium-free)${C_0}"
  preflight; fetch; install; collect; write_env; seed_host_tools; write_profile; write_whitelist; write_bot_config
  install_units; start_and_check
  printf '\n%s\n' "${C_G}${C_B}✓ Done.${C_0} Logs: journalctl --user -u jaato-tg -f   |   Re-run to upgrade   |   --uninstall to remove"
}
main "$@"

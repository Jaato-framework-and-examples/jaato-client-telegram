"""deploy-vps.sh runs the bot as its own account, never as root.

In system mode the bot gets an OS account (jaato-tg by default) whose home under
/home holds everything the bot reads or writes -- checkout, workspace, state,
config -- and its OWN venv: jaato-sdk and the bot's deps, nothing of the server's.
The framework venv holds the framework only. What stays root's is what the
DEPLOYER owns: the daemon's server.env + ws.token, bot.env (read by systemd before
it drops the bot to its account), and the deploy backups, which copy all of them.

The script is sourced (its ``main`` only runs when executed); ``uv``, ``systemctl``
and the account switch are stubbed, so these exercise the real functions without
deploying anything.
"""

import os
import subprocess
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy-vps.sh"

# uv stub: records every call; `uv pip show` answers a version per venv.
FAKE_UV = """#!/usr/bin/env bash
echo "$*" >> "$UV_LOG"
if [ "$1 $2" = "pip show" ]; then
  case "$*" in *"$FRAMEWORK_PY"*) echo "Version: $FW_SDK";; *) echo "Version: $BOT_SDK";; esac
fi
exit 0
"""


def _bash(snippet, home, **env):
    full = {"PATH": os.environ["PATH"], "HOME": str(home), "USER": "deployer", **env}
    return subprocess.run(["bash", "-c", f'source "{DEPLOY}"; {snippet}'],
                          env=full, capture_output=True, text=True)


def _vars(home, mode, *names, **env):
    r = _bash(f"SYSTEMD_MODE={mode}; resolve_bot_paths; "
              + "; ".join(f'echo "{n}=${n}"' for n in names), home, **env)
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in r.stdout.splitlines())


# ── where things live ────────────────────────────────────────────────────────

def test_system_mode_puts_everything_of_the_bot_in_its_home(tmp_path):
    v = _vars(tmp_path, "system", "BOT_USER", "BOT_HOME", "BOT_DIR", "BOT_VENV",
              "WORKSPACE", "STATE_DIR", "HOST_TOOLS_DIR", "SESSION_STORE",
              "BOT_CONFIG", "WHITELIST_FILE")
    assert v["BOT_USER"] == "jaato-tg"
    assert v["BOT_HOME"] == "/home/jaato-tg"
    for name in ("BOT_DIR", "BOT_VENV", "WORKSPACE", "STATE_DIR", "HOST_TOOLS_DIR",
                 "SESSION_STORE", "BOT_CONFIG", "WHITELIST_FILE"):
        assert v[name].startswith("/home/jaato-tg/"), (name, v[name])


def test_the_deployers_secrets_and_backups_never_land_in_the_bots_home(tmp_path):
    """bot.env holds the Telegram + WS tokens; backups copy every config dir."""
    v = _vars(tmp_path, "system", "BOT_HOME", "CFG_DIR", "BOT_ENV", "SERVER_ENV",
              "WS_TOKEN_FILE", "BACKUP_DIR")
    for name in ("CFG_DIR", "BOT_ENV", "SERVER_ENV", "WS_TOKEN_FILE", "BACKUP_DIR"):
        assert v[name].startswith(f"{tmp_path}/"), (name, v[name])
        assert not v[name].startswith(v["BOT_HOME"]), (name, v[name])


def test_the_account_name_is_configurable(tmp_path):
    v = _vars(tmp_path, "system", "BOT_USER", "BOT_DIR", JAATO_TG_USER="tgbot")
    assert v["BOT_USER"] == "tgbot"
    assert v["BOT_DIR"] == "/home/tgbot/jaato-client-telegram"


def test_user_mode_is_the_deploying_user_with_its_own_bot_venv(tmp_path):
    v = _vars(tmp_path, "user", "BOT_USER", "BOT_DIR", "BOT_VENV", "VENV")
    assert v["BOT_USER"] == subprocess.run(["id", "-un"], capture_output=True,
                                           text=True).stdout.strip()
    assert v["BOT_DIR"] == f"{tmp_path}/jaato-stack/jaato-client-telegram"
    assert v["BOT_VENV"] != v["VENV"]


# ── a root-run bot is moved, never re-cloned beside ──────────────────────────

def _check(tmp_path, legacy, new):
    if legacy:
        (tmp_path / "jaato-stack" / "jaato-client-telegram").mkdir(parents=True)
    bot_dir = tmp_path / "home" / "jaato-tg" / "jaato-client-telegram"
    if new:
        bot_dir.mkdir(parents=True)
    return _bash(f'SYSTEMD_MODE=system; resolve_bot_paths; BOT_DIR="{bot_dir}"; '
                 "check_bot_location", tmp_path)


def test_refuses_while_the_bot_still_lives_under_root(tmp_path):
    r = _check(tmp_path, legacy=True, new=False)
    assert r.returncode != 0
    assert "MOVED" in r.stderr and "Bot account" in r.stderr


def test_passes_once_moved_and_on_a_fresh_box(tmp_path):
    assert _check(tmp_path, legacy=True, new=True).returncode == 0
    assert _check(tmp_path / "fresh", legacy=False, new=False).returncode == 0


# ── two venvs: the framework's, and the bot's own ────────────────────────────

def _install(tmp_path, fw_sdk="0.30.0rc2", bot_sdk="0.30.0rc2", testpypi=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "uv").write_text(FAKE_UV)
    (bin_dir / "uv").chmod(0o755)
    log = tmp_path / "uv.log"
    fw_venv = tmp_path / "opt" / "venv"
    r = _bash(
        # uv stubbed; the account switch replaced by the import answer it would give
        f'JAATO_VENV_DIR="{fw_venv}"; SYSTEMD_MODE=system; resolve_venv_paths; resolve_bot_paths; '
        'check_venv_location(){ :; }; resolve_fw_layout(){ :; }; own_bot_home(){ :; }; '
        f'as_bot(){{ echo "{bot_sdk}"; }}; '
        "install",
        tmp_path,
        PATH=f"{bin_dir}:{os.environ['PATH']}", UV_LOG=str(log),
        FRAMEWORK_PY=f"{fw_venv}/bin/python", FW_SDK=fw_sdk, BOT_SDK=bot_sdk,
        TESTPYPI=testpypi,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return r, calls, f"{fw_venv}/bin/python"


def test_the_framework_venv_gets_the_framework_only(tmp_path):
    r, calls, fw_py = _install(tmp_path)
    assert r.returncode == 0, r.stderr
    fw_installs = [c for c in calls if c.startswith("pip install") and fw_py in c]
    assert fw_installs and all("-e " not in c for c in fw_installs), fw_installs


def test_the_bot_venv_gets_the_bot_then_the_frameworks_exact_sdk(tmp_path):
    r, calls, _ = _install(tmp_path)
    assert r.returncode == 0, r.stderr
    bot_py = "/home/jaato-tg/venv/bin/python"
    bot_installs = [c for c in calls if c.startswith("pip install") and bot_py in c]
    assert len(bot_installs) == 2, bot_installs
    assert "-e /home/jaato-tg/jaato-client-telegram" in bot_installs[0]
    assert bot_installs[1].endswith("jaato-sdk==0.30.0rc2")   # the pin comes LAST


def test_the_sdk_pin_uses_the_frameworks_index(tmp_path):
    """An rc SDK exists only on TestPyPI: the pin must look where the framework did."""
    r, calls, _ = _install(tmp_path, testpypi="1")
    assert r.returncode == 0, r.stderr
    pin = [c for c in calls if c.endswith("jaato-sdk==0.30.0rc2")][0]
    assert "--index-url https://test.pypi.org/simple/" in pin


def test_both_venvs_are_recreated_and_the_bots_uses_the_system_python(tmp_path):
    r, calls, fw_py = _install(tmp_path)
    assert r.returncode == 0, r.stderr
    venvs = [c for c in calls if c.startswith("venv ")]
    assert len(venvs) == 2 and all("--clear" in c for c in venvs), venvs
    bot_venv = [c for c in venvs if c.endswith("/home/jaato-tg/venv")][0]
    assert "--no-managed-python" in bot_venv   # a uv-managed CPython lives under /root


def test_refuses_a_bot_sdk_that_differs_from_the_frameworks(tmp_path):
    r, _, _ = _install(tmp_path, fw_sdk="0.30.0rc2", bot_sdk="0.29.0")
    assert r.returncode != 0
    assert "0.29.0" in r.stderr and "0.30.0rc2" in r.stderr


# ── the unit drops the bot to its account ────────────────────────────────────

def _units(tmp_path, mode):
    venv = tmp_path / "opt" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "jaato-server").write_text("#!/bin/sh\n")
    (venv / "bin" / "jaato-server").chmod(0o755)
    units = tmp_path / "units"
    r = _bash(f'SYSTEMD_MODE={mode}; JAATO_VENV_DIR="{venv}"; resolve_venv_paths; '
              f'resolve_bot_paths; UNIT_DIR="{units}"; _sc(){{ :; }}; '
              'loginctl(){ :; }; install_units', tmp_path)
    assert r.returncode == 0, r.stderr
    return ((units / "jaato-tg.service").read_text(),
            (units / "jaato-server.service").read_text())


def test_system_unit_runs_the_bot_as_its_account_from_its_venv(tmp_path):
    bot, server = _units(tmp_path, "system")
    assert "User=jaato-tg\n" in bot and "Group=jaato-tg\n" in bot
    assert "WorkingDirectory=/home/jaato-tg\n" in bot
    assert "ExecStart=/home/jaato-tg/venv/bin/jaato-tg --config /home/jaato-tg/" in bot
    assert f"EnvironmentFile={tmp_path}/.config/jaato-tg/bot.env\n" in bot
    assert "User=" not in server        # the daemon's identity is not ours to change


def test_user_unit_has_no_account_switch(tmp_path):
    bot, _ = _units(tmp_path, "user")
    assert "User=" not in bot
    assert f"ExecStart={tmp_path}/jaato-stack/bot-venv/bin/jaato-tg" in bot


# ── backups stay the deployer's ──────────────────────────────────────────────

def test_backups_go_to_the_deployer_and_include_the_bots_config(tmp_path):
    bot_home = tmp_path / "home" / "jaato-tg"
    (bot_home / ".config" / "jaato-tg").mkdir(parents=True)
    (bot_home / ".config" / "jaato-tg" / "whitelist.json").write_text("{}")
    (tmp_path / ".config" / "jaato-tg").mkdir(parents=True)
    (tmp_path / ".config" / "jaato-tg" / "bot.env").write_text("X=1\n")
    r = _bash(f'SYSTEMD_MODE=system; resolve_bot_paths; BOT_HOME="{bot_home}"; '
              f'BOT_CFG_DIR="{bot_home}/.config/jaato-tg"; backup_noncode', tmp_path)
    assert r.returncode == 0, r.stderr
    [snap] = (tmp_path / ".local" / "share" / "jaato-tg" / "deploy-backups").iterdir()
    assert (snap / "config" / "bot.env").exists()
    assert (snap / "bot-config" / "whitelist.json").exists()
    assert not (bot_home / ".local").exists()

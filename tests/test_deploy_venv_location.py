"""deploy-vps.sh keeps the framework venv OUT of /root, and never rebuilds a
second copy beside one that has not been moved yet.

Why the venv moved: a root daemon run with ``--runner-uid-policy peer`` drops each
runner to the connecting OS user and refuses the session if that user cannot read
the interpreter's packages. /root is 0700, so a venv under it is unreachable by
anyone else whatever its own mode. Only the venv moves: the bot connects as root,
and its workspace (chat history, memories, ~all files mode 644) is protected by
nothing but /root's 0700.

The guard is the part that has to be watched failing. A full deploy RECREATES the
venv at $VENV, so pointing $VENV at /opt while the real venv still sits under
/root would build a second multi-GB copy and could fill the disk mid-install. It
must refuse -- not quietly fall back to the old path.

The script is sourced (its ``main`` only runs when executed), so these exercise the
real path logic without deploying anything.
"""

import os
import subprocess
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy-vps.sh"


def _bash(snippet, home, **env):
    full = {"PATH": os.environ["PATH"], "HOME": str(home), **env}
    return subprocess.run(
        ["bash", "-c", f'source "{DEPLOY}"; {snippet}'],
        env=full, capture_output=True, text=True,
    )


def _resolve(home, mode, **env):
    r = _bash(f'SYSTEMD_MODE={mode}; resolve_venv_paths; '
              'printf "%s\\n%s\\n" "$VENV" "$LEGACY_VENV"', home, **env)
    assert r.returncode == 0, r.stderr
    venv, legacy = r.stdout.splitlines()
    return venv, legacy


# ── where the venv goes ──────────────────────────────────────────────────────

def test_system_mode_puts_the_venv_in_opt(tmp_path):
    venv, legacy = _resolve(tmp_path, "system")
    assert venv == "/opt/jaato-stack/venv"
    assert legacy == f"{tmp_path}/jaato-stack/venv"


def test_user_mode_keeps_the_venv_under_the_install_dir(tmp_path):
    """/opt is not writable without root, so a --user deploy must not aim there."""
    venv, legacy = _resolve(tmp_path, "user")
    assert venv == f"{tmp_path}/jaato-stack/venv"
    assert venv == legacy


def test_jaato_venv_dir_overrides_the_default(tmp_path):
    venv, _ = _resolve(tmp_path, "system", JAATO_VENV_DIR="/srv/elsewhere/venv")
    assert venv == "/srv/elsewhere/venv"


def test_the_bot_checkout_does_not_move(tmp_path):
    """Only the framework venv is decoupled; the bot stays under /root."""
    r = _bash('SYSTEMD_MODE=system; resolve_venv_paths; printf "%s" "$BOT_DIR"', tmp_path)
    assert r.stdout == f"{tmp_path}/jaato-stack/jaato-client-telegram"


# ── the guard ────────────────────────────────────────────────────────────────

def _layout(tmp_path, *, legacy=None, new=False):
    """Build a fake install. legacy: None | 'dir' | 'symlink'."""
    install = tmp_path / "jaato-stack"
    new_venv = tmp_path / "opt" / "venv"
    install.mkdir()
    if new:
        new_venv.mkdir(parents=True)
    if legacy == "dir":
        (install / "venv").mkdir()
    elif legacy == "symlink":
        new_venv.mkdir(parents=True, exist_ok=True)
        (install / "venv").symlink_to(new_venv)
    return new_venv


def _guard(tmp_path, new_venv):
    return _bash("SYSTEMD_MODE=system; resolve_venv_paths; check_venv_location",
                 tmp_path, JAATO_VENV_DIR=str(new_venv))


def test_refuses_when_the_venv_was_not_moved(tmp_path):
    """The case that would otherwise fill the disk."""
    new_venv = _layout(tmp_path, legacy="dir")
    r = _guard(tmp_path, new_venv)
    assert r.returncode != 0, "a deploy must not proceed to build a second venv"
    assert "legacy path" in r.stderr
    assert str(new_venv) in r.stderr                       # names where it should go
    assert f"JAATO_VENV_DIR={tmp_path}/jaato-stack/venv" in r.stderr   # and the way out
    assert not new_venv.exists(), "refusing must not have created anything"


def test_passes_once_the_venv_is_in_place(tmp_path):
    new_venv = _layout(tmp_path, legacy="dir", new=True)
    assert _guard(tmp_path, new_venv).returncode == 0


def test_passes_when_the_old_path_is_the_post_move_symlink(tmp_path):
    new_venv = _layout(tmp_path, legacy="symlink")
    assert _guard(tmp_path, new_venv).returncode == 0


def test_passes_on_a_fresh_box(tmp_path):
    new_venv = _layout(tmp_path)
    assert _guard(tmp_path, new_venv).returncode == 0


def test_passes_in_user_mode_where_nothing_is_decoupled(tmp_path):
    (tmp_path / "jaato-stack" / "venv").mkdir(parents=True)
    r = _bash("SYSTEMD_MODE=user; resolve_venv_paths; check_venv_location", tmp_path)
    assert r.returncode == 0


# ── sourcing is inert ────────────────────────────────────────────────────────

def test_sourcing_does_not_deploy(tmp_path):
    """The tests above depend on this: sourcing must define, never run."""
    r = _bash("echo sourced-ok", tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "sourced-ok"
    assert "VPS bootstrap" not in r.stdout + r.stderr

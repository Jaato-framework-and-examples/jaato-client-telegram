"""deploy-vps.sh runs the framework's modules by the layout the INSTALLED
framework actually has, and says so plainly when there is none.

Regression: jaato 1.0 moved the top-level `shared` / `server` packages under
`jaato_server`. Two call sites kept the old names -- `-m shared.scaffold` (provider
discovery and profile validation) and `-m server --status` (the readiness probe) --
so a full deploy on 1.x was broken. And invisibly: the scaffold call sites discard
stderr, so the ModuleNotFoundError never reached the operator. The deploy died on
"scaffold explain returned no providers" or "profile validation failed -- fix the
profile", both false; the probe never succeeded and moved on after 30 s.

Pinning an older framework is documented in the script's own header
(JAATO_SERVER_VERSION=0.11.0), and install_units already supports both layouts, so
the fix resolves the layout once instead of hardcoding the new one.
"""

import os
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy-vps.sh"

FAKE_PY = """#!/usr/bin/env bash
# Answers `<py> -c 'import X'` according to $FAKE_LAYOUT: 1x | 0x | both | none.
[ "$1" = "-c" ] || exit 2
case "$2" in
  "import jaato_server") case "$FAKE_LAYOUT" in 1x|both) exit 0;; esac; exit 1 ;;
  "import server")       case "$FAKE_LAYOUT" in 0x|both) exit 0;; esac; exit 1 ;;
esac
exit 1
"""


def _bash(snippet, tmp_path, **env):
    full = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), **env}
    return subprocess.run(["bash", "-c", f'source "{DEPLOY}"; {snippet}'],
                          env=full, capture_output=True, text=True)


def _resolve(tmp_path, layout):
    fake = tmp_path / "fakepy"
    fake.write_text(FAKE_PY); fake.chmod(0o755)
    return _bash(f'PYV="{fake}"; resolve_fw_layout; '
                 'printf "%s\\n%s\\n" "$FW_SCAFFOLD_MOD" "$FW_SERVER_MOD"',
                 tmp_path, FAKE_LAYOUT=layout)


def test_1x_layout_uses_the_jaato_server_package(tmp_path):
    r = _resolve(tmp_path, "1x")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["jaato_server.shared.scaffold", "jaato_server"]


def test_pre_1_0_layout_keeps_the_top_level_names(tmp_path):
    """A pinned old framework (documented in the header) must keep working."""
    r = _resolve(tmp_path, "0x")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["shared.scaffold", "server"]


def test_1x_wins_when_both_are_importable(tmp_path):
    r = _resolve(tmp_path, "both")
    assert r.stdout.splitlines() == ["jaato_server.shared.scaffold", "jaato_server"]


def test_no_framework_is_named_instead_of_disguised(tmp_path):
    """The failure that used to read as 'no providers' / 'fix the profile'."""
    r = _resolve(tmp_path, "none")
    assert r.returncode != 0
    assert "no jaato framework importable" in r.stderr
    assert "jaato_server" in r.stderr and "server" in r.stderr


def test_scaffold_refuses_to_run_before_the_layout_is_resolved(tmp_path):
    r = _bash("scaffold explain env", tmp_path)
    assert r.returncode != 0
    assert "not resolved" in r.stderr


# ── the call sites read the resolved names, never a hardcoded one ────────────
# `declare -f` prints the function as bash parsed it: comments stripped, so a
# mention of the old name in prose cannot satisfy or trip these.

def _body(fn, tmp_path):
    r = _bash(f"declare -f {fn}", tmp_path)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_scaffold_runs_the_resolved_module(tmp_path):
    body = _body("scaffold", tmp_path)
    assert '"$FW_SCAFFOLD_MOD"' in body
    assert "shared.scaffold" not in body


def test_the_readiness_probe_runs_the_resolved_module(tmp_path):
    body = _body("start_and_check", tmp_path)
    assert '-m "$FW_SERVER_MOD"' in body
    assert "-m server " not in body


def test_install_resolves_the_layout(tmp_path):
    """In install() itself -- the parent shell -- so the pipelines that call
    scaffold later inherit the result instead of resolving in a subshell."""
    assert "resolve_fw_layout" in _body("install", tmp_path)


def test_against_a_real_1x_install(tmp_path):
    """Not a fake: the interpreter running this suite, if it has jaato_server."""
    import sys
    try:
        import jaato_server  # noqa: F401
    except ImportError:
        pytest.skip("jaato_server not installed in the test interpreter")
    r = _bash(f'PYV="{sys.executable}"; resolve_fw_layout; printf "%s" "$FW_SERVER_MOD"',
              tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "jaato_server"

"""examples/host_tools/docker_status.py asks docker for exactly `ps [-a]`.

Regression: the running-only branch built `docker ps ps --format ...` -- the
`-a` conditional sat where a flag goes and its else-branch repeated the
subcommand -- and docker rejects `ps` with a positional argument, so the
default call (all=False) always answered "Docker error".
"""

import asyncio
import importlib.util
import os
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "examples" / "host_tools" / "docker_status.py"

# A docker that records its argv and, like the real one, rejects extra args to `ps`.
FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$ARGV_LOG"
[ "$1" = ps ] || exit 2
shift
for a in "$@"; do case "$a" in -a|--format|{{*) ;; *) echo '"docker ps" accepts no arguments.' >&2; exit 1;; esac; done
echo 'abc123|web|nginx:1|Up 2 hours|0.0.0.0:80->80/tcp'
"""


def _run(tmp_path, monkeypatch, args):
    (tmp_path / "docker").write_text(FAKE_DOCKER)
    (tmp_path / "docker").chmod(0o755)
    log = tmp_path / "argv"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ARGV_LOG", str(log))
    spec = importlib.util.spec_from_file_location("docker_status_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = asyncio.run(mod.execute(args, None))
    return out, log.read_text().splitlines()


def test_running_only_asks_for_ps_alone(tmp_path, monkeypatch):
    out, argv = _run(tmp_path, monkeypatch, {})
    assert argv[:2] == ["ps", "--format"]
    assert "error" not in out and "web" in out["result"]


def test_all_adds_the_flag_once(tmp_path, monkeypatch):
    out, argv = _run(tmp_path, monkeypatch, {"all": True})
    assert argv[:3] == ["ps", "-a", "--format"]
    assert argv.count("ps") == 1
    assert "error" not in out

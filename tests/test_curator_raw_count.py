"""The curator's gate counts the raw memory queue without importing jaato-server.

Regression: the gate imported ``shared.plugins.memory.storage`` -- a jaato-server
INTERNAL, under its pre-1.0 name -- inside ``except Exception: return 0`` logged at
DEBUG. Since 1.0 that import has been dead, so a broken gate read exactly like an
empty queue. It also made the bot unable to run from its own venv with only
``jaato-sdk``, which is where it now lives.

The count reads ``raw/*.json`` the way the framework's store enumerates it. The
contract test below is what replaces "the framework told us": it holds the count
equal to the real ``list_raw()`` wherever the framework is installed, so a change
to the store's layout fails here instead of silently reading as "nothing to curate".
"""

import ast
import logging
import os
from pathlib import Path

import pytest

from jaato_client_telegram.curator import raw_memory_count

SRC = Path(__file__).resolve().parent.parent / "src" / "jaato_client_telegram"


def _raw(ws):
    d = ws / ".jaato" / "memories" / "raw"
    d.mkdir(parents=True)
    return d


def test_absent_queue_is_empty_not_an_error(tmp_path, caplog):
    """The store creates raw/ lazily: a workspace that never stored one has none."""
    with caplog.at_level(logging.WARNING):
        assert raw_memory_count(tmp_path) == 0
    assert not caplog.records, "an empty queue must not warn"


def test_counts_one_per_raw_memory(tmp_path):
    d = _raw(tmp_path)
    for i in range(3):
        (d / f"2026010{i}_000000_0001.json").write_text("{}")
    assert raw_memory_count(tmp_path) == 3


def test_ignores_the_atomic_writers_temp_files(tmp_path):
    d = _raw(tmp_path)
    (d / "20260101_000000_0001.json").write_text("{}")
    (d / ".20260101_000000_0002.json.x7f3.tmp").write_text("partial")   # mid-write
    (d / "notes.txt").write_text("not a memory")
    assert raw_memory_count(tmp_path) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_an_unreadable_queue_warns_instead_of_reading_as_empty(tmp_path, caplog):
    """The failure this replaces: an error that looked like 'nothing to curate'."""
    d = _raw(tmp_path)
    (d / "20260101_000000_0001.json").write_text("{}")
    d.chmod(0)
    try:
        with caplog.at_level(logging.WARNING):
            assert raw_memory_count(tmp_path) == 0
        assert any("cannot read the raw memory queue" in r.message for r in caplog.records)
    finally:
        d.chmod(0o755)


def test_matches_the_frameworks_own_count(tmp_path):
    """CONTRACT: the same number the framework's real store reports."""
    storage = pytest.importorskip("jaato_server.shared.plugins.memory.storage")
    store = storage.MemoryStore(str(tmp_path / ".jaato" / "memories"))
    for i in range(4):
        store.save(storage.Memory(id=f"2026010{i}_000000_0001", content="c",
                                  description="d", tags=["t"],
                                  timestamp="2026-01-01T00:00:00"))
    assert len(store.list_raw()) == 4                     # the store agrees it wrote 4
    assert raw_memory_count(tmp_path) == len(store.list_raw())


def test_the_bot_imports_nothing_from_the_server():
    """What lets the bot run from a venv holding only jaato-sdk. Scans the real
    import statements (AST), so a mention in a comment or docstring cannot
    satisfy or trip it."""
    offenders = []
    for py in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in {"jaato_server", "shared", "server"}:
                    offenders.append(f"{py.relative_to(SRC)}: {n}")
    assert not offenders, offenders

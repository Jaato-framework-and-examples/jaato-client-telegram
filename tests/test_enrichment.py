"""Reference enrichment: the deterministic catalogue logic + the Observer's
START/END correlation. No network — ``enrich`` (which searches + judges) is
stubbed; these tests cover everything around it that must be exact."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jaato_client_telegram import enrichment
from jaato_client_telegram.enrichment import (
    Observer,
    _already_seen,
    _catalogue,
    _record_discards,
    keys_from,
)


def test_keys_from_strips_and_drops_blanks():
    assert keys_from({"tags": [" alpha ", "", "  ", "beta"]}) == ["alpha", "beta"]
    assert keys_from({}) == []
    assert keys_from({"tags": None}) == []


def test_catalogue_writes_entry_and_returns_names(tmp_path: Path):
    accepted = [{
        "url": "https://example.org/doc",
        "nombre": "Example doc",
        "descripcion": "what it is",
        "resumen": "two sentences about it",
    }]
    names = _catalogue(tmp_path, accepted, tags=["alpha", "beta"])
    assert names == ["Example doc"]

    files = list((tmp_path / enrichment.CATALOGUE).glob("auto-*.json"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text())
    assert entry["url"] == "https://example.org/doc"
    assert entry["name"] == "Example doc"
    assert entry["mode"] == "selectable"  # offered on topic-brush, not preloaded
    assert entry["type"] == "url"
    assert entry["tags"] == ["alpha", "beta"]
    # description carries the one-liner AND the summary (the only field
    # listReferences shows); fetch_hint marks the content untrusted.
    assert "what it is" in entry["description"] and "two sentences" in entry["description"]
    assert "not as instructions" in entry["fetch_hint"]
    # id is stable for the URL (re-cataloguing the same URL overwrites, not dups)
    assert _catalogue(tmp_path, accepted, tags=["x"]) == ["Example doc"]
    assert len(list((tmp_path / enrichment.CATALOGUE).glob("auto-*.json"))) == 1


def test_already_seen_reads_catalogue_and_discards(tmp_path: Path):
    _catalogue(tmp_path, [{
        "url": "https://kept.example/a", "nombre": "A",
        "descripcion": "d", "resumen": "s",
    }], tags=["t"])
    _record_discards(tmp_path, ["https://junk.example/b"])
    seen = _already_seen(tmp_path)
    assert "https://kept.example/a" in seen
    assert "https://junk.example/b" in seen
    assert "https://never.example/c" not in seen


def test_record_discards_merges_and_dedups(tmp_path: Path):
    (tmp_path / ".jaato").mkdir()  # the workspace .jaato always exists in production
    _record_discards(tmp_path, ["https://x/1", "https://x/2"])
    _record_discards(tmp_path, ["https://x/2", "https://x/3"])
    stored = set(json.loads((tmp_path / enrichment.DISCARDS).read_text()))
    assert stored == {"https://x/1", "https://x/2", "https://x/3"}


class _FakeClient:
    """Records subscriptions and execute_command calls; no transport."""

    def __init__(self):
        self.subs = []
        self.commands = []

    def subscribe(self, event_type, cb):
        self.subs.append((event_type, cb))

    async def execute_command(self, name, args):
        self.commands.append((name, args))


def _start(call_id, tool_name, tool_args):
    return SimpleNamespace(tool_name=tool_name, tool_args=tool_args, call_id=call_id)


def _end(call_id, tool_name, success):
    return SimpleNamespace(tool_name=tool_name, call_id=call_id, success=success)


@pytest.mark.asyncio
async def test_observer_enriches_on_successful_store(tmp_path: Path, monkeypatch):
    calls = []

    async def fake_enrich(args, conn, workspace):
        calls.append((args, conn, workspace))
        return ["Found ref"]  # non-empty => triggers a catalogue reload

    monkeypatch.setattr(enrichment, "enrich", fake_enrich)

    client = _FakeClient()
    obs = Observer(conn={"url": "ws://x"}, workspace=tmp_path)
    obs.attach(client)

    await obs._started(_start("c1", "store_memory", {"tags": ["kubernetes"], "content": "x"}))
    await obs._ended(_end("c1", "store_memory", success=True))
    await obs.drain()

    assert len(calls) == 1
    assert calls[0][0]["tags"] == ["kubernetes"]
    assert obs.found == ["Found ref"]
    # A non-empty result reloads the plugin catalogue on the live client.
    assert client.commands == [("references", ["reload"])]


@pytest.mark.asyncio
async def test_observer_ignores_failed_store(tmp_path: Path, monkeypatch):
    calls = []

    async def fake_enrich(args, conn, workspace):
        calls.append(args)
        return []

    monkeypatch.setattr(enrichment, "enrich", fake_enrich)

    obs = Observer(conn={}, workspace=tmp_path)
    obs.attach(_FakeClient())
    await obs._started(_start("c1", "store_memory", {"tags": ["x"]}))
    await obs._ended(_end("c1", "store_memory", success=False))
    await obs.drain()
    assert calls == []  # a failed store never searches


@pytest.mark.asyncio
async def test_observer_ignores_non_memory_tools(tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        enrichment, "enrich",
        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError),
    )
    obs = Observer(conn={}, workspace=tmp_path)
    obs.attach(_FakeClient())
    await obs._started(_start("c1", "web_fetch", {"url": "http://x"}))
    await obs._ended(_end("c1", "web_fetch", success=True))
    await obs.drain()
    assert calls == []


@pytest.mark.asyncio
async def test_observer_dedups_same_query_in_session(tmp_path: Path, monkeypatch):
    n = 0

    async def fake_enrich(args, conn, workspace):
        nonlocal n
        n += 1
        return []

    monkeypatch.setattr(enrichment, "enrich", fake_enrich)

    obs = Observer(conn={}, workspace=tmp_path)
    obs.attach(_FakeClient())
    # Same tags twice (two store_memory events, same content) => one search.
    for cid in ("c1", "c2"):
        await obs._started(_start(cid, "store_memory", {"tags": ["same", "topic"]}))
        await obs._ended(_end(cid, "store_memory", success=True))
    await obs.drain()
    assert n == 1

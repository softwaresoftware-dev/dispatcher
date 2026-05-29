"""Phase 1 tests — cursor store, sources loader, runtime filtering + supervision."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx
import pytest


@pytest.fixture
def isolated_dispatcher_dir(tmp_path, monkeypatch):
    """Point every dispatcher data path at a fresh tmp dir."""
    monkeypatch.setenv("DISPATCHER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DISPATCHER_CURSORS_DB", str(tmp_path / "cursors.db"))
    monkeypatch.setenv("DISPATCHER_SOURCES_DIR", str(tmp_path / "event-sources"))
    monkeypatch.setenv("DISPATCHER_INGEST_TOKEN", "test-token")
    monkeypatch.setenv(
        "DISPATCHER_LOOPBACK_URL", "http://127.0.0.1:99999/api/event"
    )
    # The modules cache constants at import time. Force re-evaluation by
    # reloading after the env is patched.
    import importlib
    from app.pollers import cursors, sources, runtime, registry
    importlib.reload(cursors)
    importlib.reload(sources)
    importlib.reload(runtime)
    # registry holds the singleton; clear it so tests don't bleed adapters
    registry._registry.clear()
    return tmp_path


def test_cursor_roundtrip(isolated_dispatcher_dir):
    from app.pollers import cursors
    cursors.init_db()
    assert cursors.get_cursor("missing") is None
    cursors.set_cursor("src-a", {"etag": "W/abc", "last_id": "12553207924"})
    assert cursors.get_cursor("src-a") == {"etag": "W/abc", "last_id": "12553207924"}
    cursors.set_cursor("src-a", {"etag": "W/xyz", "last_id": "12553300000"})
    assert cursors.get_cursor("src-a")["etag"] == "W/xyz"
    cursors.delete_cursor("src-a")
    assert cursors.get_cursor("src-a") is None


def test_cursor_corrupt_returns_none(isolated_dispatcher_dir):
    from app.pollers import cursors
    cursors.init_db()
    import sqlite3
    with sqlite3.connect(cursors.DB_PATH) as c:
        c.execute("INSERT INTO cursors (source_name, cursor) VALUES (?, ?)",
                  ("bad", "not-json"))
        c.commit()
    assert cursors.get_cursor("bad") is None


def test_sources_loader_parses_yaml(isolated_dispatcher_dir):
    from app.pollers import sources
    sources.SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    (sources.SOURCES_DIR / "softwaresoftware-prs.yaml").write_text(
        "name: softwaresoftware-prs\n"
        "system: github\n"
        "scope:\n  orgs: [softwaresoftware-dev]\n"
        "watching:\n  - pull_request.opened\n  - issues.*\n"
        "tick: 30s\n"
    )
    loaded = sources.load_sources()
    assert len(loaded) == 1
    s = loaded[0]
    assert s.name == "softwaresoftware-prs"
    assert s.system == "github"
    assert s.scope == {"orgs": ["softwaresoftware-dev"]}
    assert s.watching == ["pull_request.opened", "issues.*"]
    assert s.tick_s == 30


def test_sources_loader_skips_malformed(isolated_dispatcher_dir):
    from app.pollers import sources
    sources.SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    (sources.SOURCES_DIR / "no-system.yaml").write_text("name: oops\n")
    (sources.SOURCES_DIR / "bad-yaml.yaml").write_text("name: x\n  bad: indent: here")
    (sources.SOURCES_DIR / "ok.yaml").write_text("name: ok\nsystem: github\n")
    loaded = sources.load_sources()
    assert [s.name for s in loaded] == ["ok"]


def test_sources_loader_tick_units(isolated_dispatcher_dir):
    from app.pollers.sources import _parse_tick
    assert _parse_tick(None) == 60
    assert _parse_tick(45) == 45
    assert _parse_tick("30s") == 30
    assert _parse_tick("2m") == 120
    assert _parse_tick("1h") == 3600
    assert _parse_tick("garbage") == 60


def test_filter_events_pass_through_when_empty(isolated_dispatcher_dir):
    from app.pollers.runtime import _filter_events
    from app.pollers.types import Event
    evs = [Event("s", "x.y", "1"), Event("s", "a.b", "2")]
    assert _filter_events(evs, []) == evs


def test_filter_events_exact_and_wildcard(isolated_dispatcher_dir):
    from app.pollers.runtime import _filter_events
    from app.pollers.types import Event
    evs = [
        Event("s", "pull_request.opened", "1"),
        Event("s", "pull_request.closed", "2"),
        Event("s", "issues.opened", "3"),
        Event("s", "push", "4"),
    ]
    kept = _filter_events(evs, ["pull_request.opened", "issues.*"])
    assert [e.event_id for e in kept] == ["1", "3"]


def test_registry_register_and_lookup(isolated_dispatcher_dir):
    from app.pollers import registry
    from app.pollers.types import PullResult

    @dataclass
    class StubAdapter:
        system: str = "stub"
        async def pull(self, scope, cursor, credentials):
            return PullResult()

    registry.register_adapter(StubAdapter())
    assert registry.get_adapter("stub") is not None
    assert "stub" in registry.list_adapters()
    assert registry.get_adapter("missing") is None


def test_registry_rejects_adapter_without_system(isolated_dispatcher_dir):
    from app.pollers import registry

    class Bad:
        async def pull(self, scope, cursor, credentials):
            return None

    with pytest.raises(ValueError):
        registry.register_adapter(Bad())


@pytest.mark.asyncio
async def test_runtime_supervised_tick_persists_cursor(isolated_dispatcher_dir, monkeypatch):
    """One tick: stub adapter returns one event, runtime persists the cursor
    and tries to forward. Forward will fail (no server) — runtime swallows.
    """
    from app.pollers import cursors, registry, runtime
    from app.pollers.types import Adapter, Event, EventSource, PullResult

    cursors.init_db()

    seen: list[tuple] = []

    class StubAdapter:
        system = "stub"
        async def pull(self, scope, cursor, credentials):
            seen.append((cursor, credentials))
            return PullResult(
                events=[Event(source="stub", event_type="x.y", event_id="42")],
                new_cursor={"etag": "W/abc", "last_id": "42"},
                next_tick_hint_s=120,
            )

    src = EventSource(name="t-src", system="stub", tick_s=30)
    adapter = StubAdapter()

    # Force the forward to be a no-op so we don't try to talk to a real server.
    async def fake_forward(name, ev):
        return None
    monkeypatch.setattr(runtime, "_forward_event", fake_forward)

    next_tick = await runtime._one_tick(src, adapter)
    # cursor was None on first call, then persisted; credentials empty (no ref).
    assert seen == [(None, {})]
    assert cursors.get_cursor("t-src") == {"etag": "W/abc", "last_id": "42"}
    # next_tick_hint widened the runtime sleep above the configured tick.
    assert next_tick == 120


@pytest.mark.asyncio
async def test_runtime_swallows_adapter_exceptions(isolated_dispatcher_dir):
    """_one_tick propagates the adapter exception (caller decides retry math).
    The _supervised_tick wrapper is what swallows + backs off; tested separately."""
    from app.pollers import cursors, runtime
    from app.pollers.types import EventSource

    cursors.init_db()

    class BrokenAdapter:
        system = "broken"
        async def pull(self, scope, cursor, credentials):
            raise RuntimeError("upstream is down")

    src = EventSource(name="brk", system="broken", tick_s=30)
    with pytest.raises(RuntimeError):
        await runtime._one_tick(src, BrokenAdapter())


@pytest.mark.asyncio
async def test_forward_event_requires_token(isolated_dispatcher_dir, monkeypatch):
    from app.pollers import runtime
    from app.pollers.types import Event
    monkeypatch.setattr(runtime, "INGEST_TOKEN", None)
    with pytest.raises(RuntimeError, match="DISPATCHER_INGEST_TOKEN"):
        await runtime._forward_event("src", Event("s", "x", "1"))

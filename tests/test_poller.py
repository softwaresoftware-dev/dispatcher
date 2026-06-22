"""Tests for the poller orchestration — cursor advance, dry-run, failure halt,
cold-start guard, and adapter-state (ETag) persistence rules.

The adapter and the routing core are stubbed; this exercises the loop's own
logic (which events get routed, when the cursor and state move, what stops them).
"""

import asyncio

import pytest

from app import poller
from app.event_sources import EventSource


def _source():
    return EventSource(
        name="prs", system="github",
        watching=["pull_request.opened"],
        scope={"orgs": ["softwaresoftware-dev"]},
    )


def _events(*specs):
    """specs: (id, created_at) -> normalized event dicts."""
    return [
        {"event_type": "pull_request.opened", "id": str(i),
         "created_at": ts, "data": {"id": str(i)}}
        for (i, ts) in specs
    ]


def _adapter(events, state="etag-1", newest="auto"):
    """Stub adapter honoring the (events, meta) contract. newest='auto' derives
    it from the last event; pass an explicit dict or None to override."""
    if newest == "auto":
        newest = ({"id": events[-1]["id"], "created_at": events[-1]["created_at"]}
                  if events else None)

    async def adapter(src, last_seen, last_id, prev_state):
        return list(events), {"state": state, "newest": newest}

    return adapter


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point cursors at a temp db and stub sources + route_event."""
    monkeypatch.setattr(poller.cursors, "DB_PATH", str(tmp_path / "cursors.db"))
    monkeypatch.setattr(poller.event_sources, "load_sources", lambda *a, **k: [_source()])

    calls = {"routed": []}

    async def fake_route(source, event_type, data, *, workspace=None, dry_run=False):
        calls["routed"].append({"source": source, "data": data,
                                "workspace": workspace, "dry_run": dry_run})
        return {"ok": True}

    monkeypatch.setattr(poller.core, "route_event", fake_route)
    return calls


def test_cold_start_sets_watermark_routes_nothing(wired, monkeypatch):
    """First sight of a source must NOT route history — it only establishes
    the watermark at the newest existing item (the anti-stampede guard)."""
    evs = _events((1, "2026-06-09T10:00:00Z"), (2, "2026-06-09T11:00:00Z"))
    monkeypatch.setattr(poller, "get_adapter", lambda system: _adapter(evs))

    summary = asyncio.run(poller.tick())

    assert summary["routed"] == 0
    assert wired["routed"] == []  # nothing routed at all
    assert summary["details"][0]["cold_start"] is True
    assert summary["details"][0]["skipped_historical"] == 2
    # watermark parked at the newest historical item; adapter state persisted.
    assert poller.cursors.get("prs") == ("2026-06-09T11:00:00Z", "2", "etag-1")


def test_cold_start_with_unwatched_only_feed_still_sets_watermark(wired, monkeypatch):
    """A feed whose recent traffic is all unwatched types returns zero events
    but a real `newest` — the watermark must still be established, or the first
    watched event would later be swallowed by the guard."""
    newest = {"id": "77", "created_at": "2026-06-09T10:00:00Z"}
    monkeypatch.setattr(poller, "get_adapter",
                        lambda system: _adapter([], newest=newest))

    summary = asyncio.run(poller.tick())

    assert summary["details"][0]["cold_start"] is True
    assert poller.cursors.get("prs") == ("2026-06-09T10:00:00Z", "77", "etag-1")


def test_cold_start_with_empty_feed_starts_from_beginning(wired, monkeypatch):
    """An empty upstream feed cold-starts at id '0' so the first event that
    ever appears gets routed instead of re-triggering the guard."""
    monkeypatch.setattr(poller, "get_adapter",
                        lambda system: _adapter([], newest=None, state=None))

    asyncio.run(poller.tick())
    last_seen, last_id, _ = poller.cursors.get("prs")
    assert last_id == "0"

    # The next event must now route, not cold-start.
    evs = _events((5, "2026-06-09T12:00:00Z"))
    monkeypatch.setattr(poller, "get_adapter", lambda system: _adapter(evs))
    summary = asyncio.run(poller.tick())
    assert summary["routed"] == 1


def test_routes_new_events_after_watermark_established(wired, monkeypatch):
    # Pre-seed a cursor so this is steady-state, not first sight.
    poller.cursors.advance("prs", "2026-06-09T09:00:00Z", "0")
    evs = _events((1, "2026-06-09T10:00:00Z"), (2, "2026-06-09T11:00:00Z"))
    monkeypatch.setattr(poller, "get_adapter", lambda system: _adapter(evs))

    summary = asyncio.run(poller.tick())

    assert summary["new_events"] == 2
    assert summary["routed"] == 2
    assert summary["errors"] == 0
    # routed under the source's SYSTEM ("github"), which is what channels matches.
    assert all(c["source"] == "github" for c in wired["routed"])
    # cursor advanced to the newest event; adapter state persisted on full success.
    assert poller.cursors.get("prs") == ("2026-06-09T11:00:00Z", "2", "etag-1")


def test_watermark_jumps_past_unwatched_backlog_on_success(wired, monkeypatch):
    """After a fully-routed tick the watermark advances to the newest RAW feed
    item, not just the newest watched event."""
    poller.cursors.advance("prs", "2026-06-09T09:00:00Z", "0")
    evs = _events((1, "2026-06-09T10:00:00Z"))
    newest = {"id": "9", "created_at": "2026-06-09T11:30:00Z"}  # unwatched tail
    monkeypatch.setattr(poller, "get_adapter", lambda system: _adapter(evs, newest=newest))

    asyncio.run(poller.tick())
    assert poller.cursors.get("prs") == ("2026-06-09T11:30:00Z", "9", "etag-1")


def test_backfill_env_routes_history_on_first_sight(wired, monkeypatch):
    monkeypatch.setattr(poller, "BACKFILL", True)
    evs = _events((1, "2026-06-09T10:00:00Z"), (2, "2026-06-09T11:00:00Z"))
    monkeypatch.setattr(poller, "get_adapter", lambda system: _adapter(evs))

    summary = asyncio.run(poller.tick())
    assert summary["routed"] == 2  # history replayed on purpose


def test_second_tick_emits_nothing_new(wired, monkeypatch):
    """Restart / re-poll safety: once advanced, a no-new-items tick routes
    nothing (the adapter answers 304-style: no events, no newest)."""
    first = _events((1, "2026-06-09T10:00:00Z"))

    async def adapter(s, last_seen, last_id, state):
        if last_id is not None:
            return [], {"state": state, "newest": None}  # 304
        return list(first), {"state": "etag-1",
                             "newest": {"id": "1", "created_at": first[0]["created_at"]}}

    monkeypatch.setattr(poller, "get_adapter", lambda system: adapter)

    asyncio.run(poller.tick())          # cold start: watermark set, nothing routed
    second = asyncio.run(poller.tick())  # nothing new
    assert second["new_events"] == 0
    assert second["routed"] == 0


def test_dry_run_routes_but_advances_nothing(wired, monkeypatch):
    # Steady-state (seed cursor) so dry-run actually exercises the route path.
    poller.cursors.advance("prs", "2026-06-09T09:00:00Z", "0")
    evs = _events((1, "2026-06-09T10:00:00Z"))
    monkeypatch.setattr(poller, "get_adapter", lambda system: _adapter(evs))

    summary = asyncio.run(poller.tick(dry_run=True))

    assert summary["routed"] == 1
    assert wired["routed"][0]["dry_run"] is True
    # No cursor movement, no state persisted, in dry-run.
    assert poller.cursors.get("prs") == ("2026-06-09T09:00:00Z", "0", None)


def test_dry_run_cold_start_persists_nothing(wired, monkeypatch):
    evs = _events((1, "2026-06-09T10:00:00Z"))
    monkeypatch.setattr(poller, "get_adapter", lambda system: _adapter(evs))

    summary = asyncio.run(poller.tick(dry_run=True))
    assert summary["details"][0]["cold_start"] is True
    assert poller.cursors.get("prs") == (None, None, None)


def test_route_failure_halts_cursor_and_keeps_old_state(tmp_path, monkeypatch):
    """A failed route stops the advance AND keeps the previous adapter state —
    persisting the new ETag after a partial route would make the next tick's
    304 hide the unrouted tail."""
    monkeypatch.setattr(poller.cursors, "DB_PATH", str(tmp_path / "cursors.db"))
    monkeypatch.setattr(poller.event_sources, "load_sources", lambda *a, **k: [_source()])
    poller.cursors.advance("prs", "2026-06-09T09:00:00Z", "0")  # steady-state
    poller.cursors.set_state("prs", "etag-old")
    evs = _events((1, "2026-06-09T10:00:00Z"), (2, "2026-06-09T11:00:00Z"))
    monkeypatch.setattr(poller, "get_adapter", lambda system: _adapter(evs, state="etag-new"))

    async def fail_second(source, event_type, data, *, workspace=None, dry_run=False):
        return {"ok": True} if data["id"] == "1" else {"ok": False, "error": "spawn boom"}

    monkeypatch.setattr(poller.core, "route_event", fail_second)

    summary = asyncio.run(poller.tick())

    assert summary["routed"] == 1
    assert summary["errors"] == 1
    # cursor advanced past #1 only; #2 retried next tick; old ETag retained.
    assert poller.cursors.get("prs") == ("2026-06-09T10:00:00Z", "1", "etag-old")


def test_unknown_system_is_skipped_not_fatal(wired, monkeypatch):
    monkeypatch.setattr(poller, "get_adapter", lambda system: None)
    summary = asyncio.run(poller.tick())
    assert summary["sources"] == 1
    assert summary["routed"] == 0
    assert summary["details"][0]["skipped"].startswith("no adapter")


def test_adapter_exception_recorded_not_raised(wired, monkeypatch):
    async def boom(s, ls, li, st):
        raise RuntimeError("github exploded")

    monkeypatch.setattr(poller, "get_adapter", lambda system: boom)
    summary = asyncio.run(poller.tick())
    assert summary["errors"] == 1
    assert "github exploded" in summary["details"][0]["error"]

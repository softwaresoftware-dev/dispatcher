"""Tests for the shared routing core (app/core.py) used by the poller.

Exercises the same decision tree as the webhook but on the inline-await path:
dedupe, static spawn, static session, and the unmapped LLM fallback. The
session-bridge and taskpilot calls are stubbed; routing writes to a temp DB.
"""

import sys

import pytest


@pytest.fixture
def core_env(monkeypatch, tmp_path):
    """Fresh app.* import tree wired to a temp DB + channels file."""
    db_path = tmp_path / "events.db"
    channels_path = tmp_path / "channels.yaml"
    channels_path.write_text(
        "routes:\n"
        "  - source: github\n"
        "    event_type: pull_request.opened\n"
        "    target: spawn:pr-prep\n"
        "  - source: sentry\n"
        "    event_type: issue.alert\n"
        "    target: session:oncall\n"
    )
    monkeypatch.setenv("DISPATCHER_DB_PATH", str(db_path))
    monkeypatch.setenv("DISPATCHER_CHANNELS_FILE", str(channels_path))
    monkeypatch.setenv("DISPATCHER_DEDUPE_WINDOW_MINUTES", "10")

    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    from app import db as db_module, channels as channels_module, core, main  # noqa: F401
    channels_module._cached_routes = None
    db_module.init_db()
    return core, main, channels_module


def test_spawn_route_spawns_and_dedupes(core_env, monkeypatch):
    core, main, _ = core_env

    async def fake_spawn(*, recipe_id, payload, event_id, brief_overrides=None):
        return {"ok": True, "task_id": f"{recipe_id}-{event_id}"}

    monkeypatch.setattr(core.spawn_helper, "spawn_recipe", fake_spawn)

    import asyncio
    data = {"id": "pr-555", "title": "Add poller"}
    res = asyncio.run(core.route_event("github", "pull_request.opened", data))
    assert res["ok"] and res["mode"] == "poll-spawn"
    assert res["routed_to"] == "spawn:pr-prep"

    # Second identical event is deduped (no second spawn).
    res2 = asyncio.run(core.route_event("github", "pull_request.opened", data))
    assert res2.get("deduped") is True


def test_session_route_forwards(core_env, monkeypatch):
    core, main, _ = core_env

    sent = {}

    async def fake_forward(session, text):
        sent["session"] = session
        sent["text"] = text
        return {"ok": True}

    monkeypatch.setattr(main, "_forward_to_session", fake_forward)

    import asyncio
    res = asyncio.run(core.route_event("sentry", "issue.alert", {"id": "evt-1"}))
    assert res["ok"] and res["routed_to"] == "oncall"
    assert sent["session"] == "oncall"


def test_dry_run_resolves_without_acting(core_env, monkeypatch):
    core, main, _ = core_env

    async def boom(*a, **k):
        raise AssertionError("dry-run must not spawn")

    monkeypatch.setattr(core.spawn_helper, "spawn_recipe", boom)

    import asyncio
    res = asyncio.run(core.route_event("github", "pull_request.opened", {"id": "x"}, dry_run=True))
    assert res["dry_run"] is True
    assert res["would_route_to"] == "spawn:pr-prep"


def test_unmapped_falls_back_to_dispatcher_session(core_env, monkeypatch):
    core, main, _ = core_env

    sent = {}

    async def fake_forward(session, text):
        sent["session"] = session
        return {"ok": True}

    monkeypatch.setattr(main, "_forward_to_session", fake_forward)

    import asyncio
    res = asyncio.run(core.route_event("unknown-src", "whatever", {"id": "z"}))
    assert res["ok"]
    assert sent["session"] == main.DISPATCHER_SESSION


def test_spawn_failure_reports_not_raises(core_env, monkeypatch):
    core, main, _ = core_env

    async def fail_spawn(*, recipe_id, payload, event_id, brief_overrides=None):
        return {"ok": False, "error": "taskpilot down"}

    monkeypatch.setattr(core.spawn_helper, "spawn_recipe", fail_spawn)

    import asyncio
    res = asyncio.run(core.route_event("github", "pull_request.opened", {"id": "pr-9"}))
    assert res["ok"] is False
    assert "taskpilot down" in res["error"]
    # A failed spawn must NOT record dedupe — retry must be allowed next tick.
    res2 = asyncio.run(core.route_event("github", "pull_request.opened", {"id": "pr-9"}))
    assert res2.get("deduped") is not True

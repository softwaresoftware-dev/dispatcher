"""Tests for static (source, event_type) → channel routing.

When channels.yaml has a matching route, dispatcher-ingress skips the LLM
dispatcher entirely. Two target shapes:
  - `session:<name>` — POST text to session-bridge /sessions/<name>/message
  - `spawn:<recipe>` — shell out to taskpilot spawner_cli for an ephemeral agent
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    channels_path = tmp_path / "channels.yaml"
    monkeypatch.setenv("DISPATCHER_INGEST_TOKEN", "test-token")
    monkeypatch.setenv("DISPATCHER_CHANNELS_FILE", str(channels_path))

    # Re-import after env so module-level constants pick up the override.
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    from app import db as db_module, channels as channels_module
    monkeypatch.setattr(db_module, "DB_PATH", str(db_path))
    channels_module._cached_routes = None  # reset

    from app.main import app
    db_module.init_db()
    with TestClient(app) as c:
        yield c, channels_path


def _write_channels(path: Path, routes: list[dict]):
    import yaml
    path.write_text(yaml.safe_dump({"routes": routes}))


# --- channels.yaml loader -------------------------------------------------


def test_channels_load_basic(client):
    _, channels_path = client
    _write_channels(channels_path, [
        {"source": "softwaresoftware-relay", "event_type": "sms", "target": "spawn:email-triage"},
    ])
    from app import channels
    channels._cached_routes = None
    routes = channels.load_routes()
    assert len(routes) == 1
    assert routes[0].source == "softwaresoftware-relay"
    assert routes[0].event_type == "sms"
    assert routes[0].target == "spawn:email-triage"


def test_channels_load_missing_file_returns_empty(client):
    _, channels_path = client
    if channels_path.exists():
        channels_path.unlink()
    from app import channels
    channels._cached_routes = None
    assert channels.load_routes() == []


def test_channels_load_missing_event_type_matches_anything(client):
    _, channels_path = client
    _write_channels(channels_path, [
        {"source": "softwaresoftware-relay", "target": "session:catchall"},
    ])
    from app import channels
    channels._cached_routes = None
    target = channels.lookup("softwaresoftware-relay", "anything")
    assert target == "session:catchall"


def test_channels_lookup_first_match_wins(client):
    _, channels_path = client
    _write_channels(channels_path, [
        {"source": "softwaresoftware-relay", "event_type": "sms", "target": "spawn:email-triage"},
        {"source": "softwaresoftware-relay", "target": "session:catchall"},
    ])
    from app import channels
    channels._cached_routes = None
    assert channels.lookup("softwaresoftware-relay", "sms") == "spawn:email-triage"
    assert channels.lookup("softwaresoftware-relay", "quick_send") == "session:catchall"


def test_channels_lookup_no_match_returns_none(client):
    _, channels_path = client
    _write_channels(channels_path, [
        {"source": "softwaresoftware-relay", "target": "session:foo"},
    ])
    from app import channels
    channels._cached_routes = None
    assert channels.lookup("sentry", "error") is None


# --- /api/event with static routes ---------------------------------------


def test_event_static_route_to_session_skips_llm(client):
    c, channels_path = client
    _write_channels(channels_path, [
        {"source": "softwaresoftware-relay", "event_type": "sms", "target": "session:email-triage"},
    ])
    from app import channels, main
    channels._cached_routes = None

    with patch.object(main, "_forward_to_session", new=AsyncMock(return_value={"ok": True, "delivered": True})) as mock_fwd:
        r = c.post(
            "/api/event",
            json={"source": "softwaresoftware-relay", "event_type": "sms", "data": {"sender": "x", "body": "y"}},
            headers={"Authorization": "Bearer test-token"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["routed_to"] == "email-triage"
    assert body["mode"] == "static-session"
    # forwarded directly to email-triage, not to the dispatcher LLM session
    mock_fwd.assert_called_once()
    assert mock_fwd.await_args.args[0] == "email-triage"


def test_event_static_route_to_spawn_invokes_spawner(client):
    c, channels_path = client
    _write_channels(channels_path, [
        {"source": "softwaresoftware-relay", "event_type": "sms", "target": "spawn:email-triage"},
    ])
    from app import channels, main
    channels._cached_routes = None

    with patch.object(main.spawn_helper, "spawn_recipe", new=AsyncMock(return_value={"ok": True, "task_id": "email-triage-abc"})) as mock_spawn:
        r = c.post(
            "/api/event",
            json={"source": "softwaresoftware-relay", "event_type": "sms", "data": {"id": "abc", "body": "hi"}},
            headers={"Authorization": "Bearer test-token"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["mode"] == "static-spawn"
    assert body["routed_to"] == "spawn:email-triage"
    mock_spawn.assert_called_once()
    kwargs = mock_spawn.await_args.kwargs
    assert kwargs.get("recipe_id") == "email-triage"


def test_channels_load_brief_block(client):
    """A spawn route may carry a `brief:` block; it's parsed onto the Route."""
    _, channels_path = client
    _write_channels(channels_path, [
        {"source": "test-stream", "event_type": "calendar-check",
         "target": "spawn:calendar-reader",
         "brief": {"output_path": "/tmp/x.log", "window": "24h"}},
    ])
    from app import channels
    channels._cached_routes = None
    route = channels.lookup_route("test-stream", "calendar-check")
    assert route is not None
    assert route.brief == {"output_path": "/tmp/x.log", "window": "24h"}


def test_event_static_spawn_passes_brief_overrides(client):
    """The route's `brief:` block reaches spawn_recipe as brief_overrides."""
    c, channels_path = client
    _write_channels(channels_path, [
        {"source": "test-stream", "event_type": "calendar-check",
         "target": "spawn:calendar-reader",
         "brief": {"output_path": "/tmp/cal-{event_id}.log", "window": "24h"}},
    ])
    from app import channels, main
    channels._cached_routes = None

    with patch.object(main.spawn_helper, "spawn_recipe",
                      new=AsyncMock(return_value={"ok": True, "task_id": "calendar-reader-abc"})) as mock_spawn:
        r = c.post(
            "/api/event",
            json={"source": "test-stream", "event_type": "calendar-check", "data": {"id": "abc"}},
            headers={"Authorization": "Bearer test-token"},
        )
    assert r.status_code == 200, r.text
    mock_spawn.assert_called_once()
    assert mock_spawn.await_args.kwargs.get("brief_overrides") == {
        "output_path": "/tmp/cal-{event_id}.log", "window": "24h",
    }


def test_event_unmapped_falls_through_to_llm_dispatcher(client):
    """Sources not in channels.yaml continue to the existing LLM-routed path."""
    c, channels_path = client
    _write_channels(channels_path, [
        {"source": "softwaresoftware-relay", "event_type": "sms", "target": "session:email-triage"},
    ])
    from app import channels, main
    channels._cached_routes = None

    with patch.object(main, "_forward_to_session", new=AsyncMock(return_value={"ok": True})) as mock_fwd:
        r = c.post(
            "/api/event",
            json={"source": "sentry", "event_type": "error", "data": {"id": "evt1", "msg": "boom"}},
            headers={"Authorization": "Bearer test-token"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    # Falls through to DISPATCHER_SESSION (the LLM dispatcher)
    assert body["routed_to"] == "dispatcher"
    assert mock_fwd.await_args.args[0] == "dispatcher"


def test_event_static_route_dedupes(client):
    """Even with deterministic routing, the same event arriving twice should dedupe."""
    c, channels_path = client
    _write_channels(channels_path, [
        {"source": "softwaresoftware-relay", "event_type": "sms", "target": "session:email-triage"},
    ])
    from app import channels, main
    channels._cached_routes = None

    payload = {"source": "softwaresoftware-relay", "event_type": "sms", "data": {"id": "dup-1", "body": "hi"}}

    with patch.object(main, "_forward_to_session", new=AsyncMock(return_value={"ok": True})) as mock_fwd:
        r1 = c.post("/api/event", json=payload, headers={"Authorization": "Bearer test-token"})
        r2 = c.post("/api/event", json=payload, headers={"Authorization": "Bearer test-token"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("deduped") is True
    # Forward called only once
    assert mock_fwd.await_count == 1

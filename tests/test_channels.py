"""Tests for static (source, event_type) → channel routing.

When channels.yaml has a matching route, dispatcher-ingress skips the LLM
dispatcher entirely. Two target shapes:
  - `session:<name>` — POST text to session-bridge /sessions/<name>/message
  - `spawn:<recipe>` — POST to taskpilot's daemon to create+spawn an agent
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


# --- channels.lookup_route (static routing table) ---

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
import os
import tempfile

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "test.db")
    monkeypatch.setenv("DISPATCHER_INGEST_TOKEN", "test-token")

    from app import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)

    from app.main import app
    db_module.init_db()
    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_event_requires_token(client):
    r = client.post("/api/event", json={"source": "sentry", "data": {"msg": "boom"}})
    assert r.status_code == 401


def test_bearer_from_file(tmp_path, monkeypatch):
    """DISPATCHER_INGEST_TOKEN_FILE lets the install.txt PHASE 7.6 workflow
    actually authenticate. Without this, the bearer file written by setup
    would have to be re-injected as an env var via systemd EnvironmentFile,
    which install.txt doesn't currently arrange."""
    monkeypatch.delenv("DISPATCHER_INGEST_TOKEN", raising=False)
    bearer_file = tmp_path / "bearer.token"
    bearer_file.write_text("file-bearer-value\n")
    monkeypatch.setenv("DISPATCHER_INGEST_TOKEN_FILE", str(bearer_file))

    from app.main import _get_token
    assert _get_token() == "file-bearer-value"


def test_bearer_env_wins_over_file(tmp_path, monkeypatch):
    """Direct env var takes precedence over file — operator overrides."""
    monkeypatch.setenv("DISPATCHER_INGEST_TOKEN", "direct-wins")
    bearer_file = tmp_path / "bearer.token"
    bearer_file.write_text("from-file\n")
    monkeypatch.setenv("DISPATCHER_INGEST_TOKEN_FILE", str(bearer_file))

    from app.main import _get_token
    assert _get_token() == "direct-wins"


def test_bearer_file_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("DISPATCHER_INGEST_TOKEN", raising=False)
    monkeypatch.setenv("DISPATCHER_INGEST_TOKEN_FILE", str(tmp_path / "missing"))

    from app.main import _get_token
    assert _get_token() == ""


def test_event_uses_bearer_file(tmp_path, monkeypatch):
    """End-to-end: a dispatcher started with only the FILE env var honors
    requests bearing the file's token."""
    monkeypatch.delenv("DISPATCHER_INGEST_TOKEN", raising=False)
    bearer_file = tmp_path / "bearer.token"
    bearer_file.write_text("e2e-file-bearer\n")
    monkeypatch.setenv("DISPATCHER_INGEST_TOKEN_FILE", str(bearer_file))

    db_path = str(tmp_path / "test.db")
    from app import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)

    from app.main import app
    db_module.init_db()
    with TestClient(app) as c:
        r = c.post(
            "/api/health",
            headers={"Authorization": "Bearer e2e-file-bearer"},
        )
        # /api/health doesn't require auth, but /api/events does — use that.
        r = c.get(
            "/api/events",
            headers={"Authorization": "Bearer e2e-file-bearer"},
        )
        assert r.status_code == 200, r.text
        r = c.get(
            "/api/events",
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 403


def test_event_bad_token(client):
    r = client.post(
        "/api/event",
        json={"source": "sentry", "data": {}},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 403


def test_event_forwards_to_session_bridge(client, monkeypatch):
    # Stub the network call so we don't actually need session-bridge running
    captured = {}

    class StubClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            class R:
                status_code = 200
                def json(self): return {"sent": json["text"], "session": "dispatcher"}
            return R()

    monkeypatch.setattr("app.main.httpx.AsyncClient", StubClient)

    r = client.post(
        "/api/event",
        json={"source": "sentry", "event_type": "error", "data": {"msg": "boom"}},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["routed_to"] == "dispatcher"
    assert "/sessions/dispatcher/message" in captured["url"]
    assert "sentry" in captured["json"]["text"]
    assert "boom" in captured["json"]["text"]


def test_direct_forwards_to_named_session(client, monkeypatch):
    captured = {}

    class StubClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None):
            captured["url"] = url
            class R:
                status_code = 200
                def json(self): return {}
            return R()

    monkeypatch.setattr("app.main.httpx.AsyncClient", StubClient)

    r = client.post(
        "/api/direct/librarian",
        json={"text": "hello", "source": "phone"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 200
    assert "/sessions/librarian/message" in captured["url"]


def test_audit_log_requires_auth(client):
    assert client.get("/api/events").status_code == 401


def test_dedupe_short_circuits_repeat_event(client, monkeypatch):
    """Same source + event_id within window forwards once, dedupes on retry."""
    forward_count = {"n": 0}

    class StubClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None):
            forward_count["n"] += 1
            class R:
                status_code = 200
                def json(self): return {}
            return R()

    monkeypatch.setattr("app.main.httpx.AsyncClient", StubClient)

    payload = {
        "source": "sentry",
        "event_type": "error",
        "data": {"event_id": "abc123", "title": "boom"},
    }
    headers = {"Authorization": "Bearer test-token"}

    r1 = client.post("/api/event", json=payload, headers=headers)
    assert r1.status_code == 200
    assert r1.json().get("deduped") is None
    assert forward_count["n"] == 1

    r2 = client.post("/api/event", json=payload, headers=headers)
    assert r2.status_code == 200
    body = r2.json()
    assert body["deduped"] is True
    assert body["routed_to"] == "dispatcher"
    assert "original_event_id" in body
    assert forward_count["n"] == 1, "duplicate must not re-forward"

    events = client.get("/api/events", headers=headers).json()
    assert len(events) == 2
    statuses = sorted(e["status"] for e in events)
    assert statuses == ["deduped", "forwarded"]


def test_dedupe_distinguishes_different_event_ids(client, monkeypatch):
    forward_count = {"n": 0}

    class StubClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None):
            forward_count["n"] += 1
            class R:
                status_code = 200
                def json(self): return {}
            return R()

    monkeypatch.setattr("app.main.httpx.AsyncClient", StubClient)
    headers = {"Authorization": "Bearer test-token"}

    client.post("/api/event",
                json={"source": "sentry", "data": {"event_id": "aaa"}},
                headers=headers)
    client.post("/api/event",
                json={"source": "sentry", "data": {"event_id": "bbb"}},
                headers=headers)

    assert forward_count["n"] == 2


def test_dedupe_falls_back_to_payload_hash_without_event_id(client, monkeypatch):
    forward_count = {"n": 0}

    class StubClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None):
            forward_count["n"] += 1
            class R:
                status_code = 200
                def json(self): return {}
            return R()

    monkeypatch.setattr("app.main.httpx.AsyncClient", StubClient)
    headers = {"Authorization": "Bearer test-token"}
    body = {"source": "weird", "data": {"msg": "no id field"}}

    client.post("/api/event", json=body, headers=headers)
    client.post("/api/event", json=body, headers=headers)

    assert forward_count["n"] == 1, "identical payloads should dedupe via hash fallback"


def test_audit_log_records_attempts(client, monkeypatch):
    class StubClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None):
            class R:
                status_code = 200
                def json(self): return {}
            return R()
    monkeypatch.setattr("app.main.httpx.AsyncClient", StubClient)

    client.post("/api/event",
                json={"source": "sentry", "data": {}},
                headers={"Authorization": "Bearer test-token"})
    client.post("/api/direct/librarian",
                json={"text": "hi"},
                headers={"Authorization": "Bearer test-token"})

    events = client.get("/api/events", headers={"Authorization": "Bearer test-token"}).json()
    assert len(events) == 2
    assert {e["mode"] for e in events} == {"auto", "direct"}
    assert all(e["status"] == "forwarded" for e in events)


def test_unhandled_exception_logs_internal_failure_row(monkeypatch):
    """If a handler raises something other than HTTPException, the global
    middleware must persist a row to events.db so the failure is visible.

    Uses a TestClient with raise_server_exceptions=False so the middleware's
    JSONResponse is observed instead of letting the exception propagate to
    the test body."""
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "test.db")
    monkeypatch.setenv("DISPATCHER_INGEST_TOKEN", "test-token")
    from app import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.init_db()
    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)

    # _check_auth runs before the handler's try/except, so a non-HTTPException
    # raised there escapes the handler entirely. Stand-in for any unexpected
    # failure outside the handler's defensive scope (config bugs, import
    # errors at request time, broken middleware in front, etc).
    import app.main as main_mod
    original_check_auth = main_mod._check_auth

    def boom_auth(*a, **kw):
        raise ValueError("unexpected non-HTTPException")
    main_mod._check_auth = boom_auth

    headers = {"Authorization": "Bearer test-token"}
    r = client.post("/api/direct/librarian", json={"text": "hi"}, headers=headers)
    main_mod._check_auth = original_check_auth
    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert body["type"] == "ValueError"

    events = client.get("/api/events?source=_internal", headers=headers).json()
    assert len(events) == 1
    assert events[0]["status"] == "exception"
    assert events[0]["mode"] == "exception"
    assert events[0]["target"] == "/api/direct/librarian"
    assert "ValueError" in events[0]["error"]


def test_events_filter_by_status(client, monkeypatch):
    headers = {"Authorization": "Bearer test-token"}

    class StubClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None):
            class R:
                status_code = 200
                def json(self): return {}
            return R()
    monkeypatch.setattr("app.main.httpx.AsyncClient", StubClient)

    # one forwarded event
    client.post("/api/event", json={"source": "sentry", "data": {}}, headers=headers)

    # one failed event by raising a non-HTTPException inside the forward
    async def _explode(*a, **kw):
        raise RuntimeError("forward failed")
    monkeypatch.setattr("app.main._forward_to_session", _explode)
    client.post("/api/event", json={"source": "github", "data": {}}, headers=headers)

    forwarded = client.get("/api/events?status=forwarded", headers=headers).json()
    failed = client.get("/api/events?status=failed", headers=headers).json()
    assert len(forwarded) == 1
    assert len(failed) == 1
    assert forwarded[0]["source"] == "sentry"
    assert failed[0]["source"] == "github"


def test_events_summary_groups_by_status(client, monkeypatch):
    headers = {"Authorization": "Bearer test-token"}

    class StubClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json=None):
            class R:
                status_code = 200
                def json(self): return {}
            return R()
    monkeypatch.setattr("app.main.httpx.AsyncClient", StubClient)

    for src in ("a", "b", "c"):
        client.post("/api/event", json={"source": src, "data": {}}, headers=headers)

    summary = client.get("/api/events/summary", headers=headers).json()
    assert summary == {"forwarded": 3}

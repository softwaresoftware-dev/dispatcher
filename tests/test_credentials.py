"""Phase 3.5 — credentials store, signature change, runtime threading."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


@pytest.fixture
def cred_file(tmp_path, monkeypatch):
    p = tmp_path / "credentials.yaml"
    monkeypatch.setenv("DISPATCHER_CREDENTIALS_FILE", str(p))
    # Re-evaluate the module-level PATH constant after the env change.
    import importlib
    from app import credentials
    importlib.reload(credentials)
    return p


def test_get_missing_ref_returns_empty(cred_file):
    from app import credentials
    assert credentials.get("missing") == {}


def test_get_with_no_ref_returns_empty(cred_file):
    from app import credentials
    assert credentials.get(None) == {}
    assert credentials.get("") == {}


def test_set_then_get_roundtrip(cred_file):
    from app import credentials
    credentials.set("github-personal", {"token": "ghp_abc"})
    assert credentials.get("github-personal") == {"token": "ghp_abc"}


def test_set_writes_chmod_600(cred_file):
    from app import credentials
    credentials.set("github-personal", {"token": "ghp_abc"})
    mode = stat.S_IMODE(cred_file.stat().st_mode)
    # Owner read+write only — group/world bits must be zero.
    assert mode & 0o077 == 0, oct(mode)


def test_set_multiple_refs(cred_file):
    from app import credentials
    credentials.set("github-personal", {"token": "ghp_abc"})
    credentials.set("slack-workspace", {"bot_token": "xoxb-x", "signing_secret": "ss"})
    assert credentials.list_refs() == ["github-personal", "slack-workspace"]
    assert credentials.get("slack-workspace") == {"bot_token": "xoxb-x", "signing_secret": "ss"}
    # First entry unchanged.
    assert credentials.get("github-personal") == {"token": "ghp_abc"}


def test_set_upserts(cred_file):
    from app import credentials
    credentials.set("github-personal", {"token": "old"})
    credentials.set("github-personal", {"token": "new"})
    assert credentials.get("github-personal") == {"token": "new"}


def test_delete(cred_file):
    from app import credentials
    credentials.set("github-personal", {"token": "abc"})
    credentials.delete("github-personal")
    assert credentials.list_refs() == []
    assert credentials.get("github-personal") == {}


def test_set_empty_ref_raises(cred_file):
    from app import credentials
    with pytest.raises(ValueError):
        credentials.set("", {"token": "x"})


def test_corrupt_yaml_returns_empty(cred_file):
    from app import credentials
    cred_file.write_text("not: valid: yaml: [unclosed")
    assert credentials.list_refs() == []


def test_non_mapping_top_level_returns_empty(cred_file):
    from app import credentials
    cred_file.write_text("- just\n- a\n- list\n")
    assert credentials.list_refs() == []


def test_non_dict_entries_are_skipped(cred_file):
    from app import credentials
    cred_file.write_text(
        "ok-ref:\n  token: keep\n"
        "bad-ref: just-a-string\n"
    )
    assert credentials.list_refs() == ["ok-ref"]


@pytest.mark.asyncio
async def test_runtime_threads_credentials_to_adapter(tmp_path, monkeypatch):
    """The runtime must read credentials by ref and pass them to pull()."""
    monkeypatch.setenv("DISPATCHER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DISPATCHER_CURSORS_DB", str(tmp_path / "cursors.db"))
    monkeypatch.setenv("DISPATCHER_CREDENTIALS_FILE", str(tmp_path / "credentials.yaml"))
    monkeypatch.setenv("DISPATCHER_INGEST_TOKEN", "x")

    import importlib
    from app import credentials
    from app.pollers import cursors, runtime
    importlib.reload(credentials)
    importlib.reload(cursors)
    importlib.reload(runtime)

    cursors.init_db()
    credentials.set("github-personal", {"token": "ghp_threaded"})

    received: dict = {}

    class CapturingAdapter:
        system = "github"
        async def pull(self, scope, cursor, creds):
            received.update(creds)
            from app.pollers.types import PullResult
            return PullResult(new_cursor={"etag": "x", "last_id": "1"})

    from app.pollers.types import EventSource
    src = EventSource(
        name="prs", system="github", tick_s=30,
        credentials_ref="github-personal",
    )

    async def fake_forward(name, ev):
        return None
    monkeypatch.setattr(runtime, "_forward_event", fake_forward)

    await runtime._one_tick(src, CapturingAdapter())
    assert received == {"token": "ghp_threaded"}


@pytest.mark.asyncio
async def test_runtime_passes_empty_creds_when_ref_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPATCHER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DISPATCHER_CURSORS_DB", str(tmp_path / "cursors.db"))
    monkeypatch.setenv("DISPATCHER_CREDENTIALS_FILE", str(tmp_path / "credentials.yaml"))

    import importlib
    from app import credentials
    from app.pollers import cursors, runtime
    importlib.reload(credentials)
    importlib.reload(cursors)
    importlib.reload(runtime)

    cursors.init_db()
    received: list[dict] = []

    class CapturingAdapter:
        system = "github"
        async def pull(self, scope, cursor, creds):
            received.append(creds)
            from app.pollers.types import PullResult
            return PullResult(new_cursor={"etag": "x", "last_id": "1"})

    from app.pollers.types import EventSource
    # No credentials_ref set.
    src = EventSource(name="prs", system="github", tick_s=30)
    monkeypatch.setattr(runtime, "_forward_event", lambda *a, **kw: None)

    await runtime._one_tick(src, CapturingAdapter())
    assert received == [{}]

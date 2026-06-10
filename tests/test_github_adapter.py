"""Tests for the GitHub Event Source adapter — Events API + ETag + filtering.

The adapter speaks the authenticated-user org events feed over httpx; tests
inject a fake `fetch` so no network or token is touched. Token resolution is
tested separately with env/subprocess/hosts.yml monkeypatching.
"""

import asyncio

import pytest

from app.adapters import github
from app.event_sources import EventSource


def _source(**kw):
    base = dict(
        name="softwaresoftware-prs",
        system="github",
        watching=["pull_request.opened"],
        scope={"orgs": ["softwaresoftware-dev"]},
    )
    base.update(kw)
    return EventSource(**base)


def _gh_event(eid, gh_type="PullRequestEvent", action="opened",
              created="2026-06-09T10:00:00Z", repo="softwaresoftware-dev/repo"):
    e = {
        "id": str(eid),
        "type": gh_type,
        "created_at": created,
        "repo": {"name": repo},
        "actor": {"login": "ThatcherT"},
        "payload": {"action": action} if action else {},
    }
    if gh_type == "PullRequestEvent":
        e["payload"]["pull_request"] = {"number": 7, "title": "A PR"}
    return e


def _fetch_returning(status, body=None, etag='W/"abc"'):
    """Fake fetch answering GET /user with a login and the events URL with the
    canned (status, body)."""
    captured = {}

    async def fetch(url, headers):
        if url.endswith("/user"):
            return 200, {}, {"login": "thatchert"}
        captured["url"] = url
        captured["headers"] = headers
        return status, {"ETag": etag} if etag else {}, body

    fetch.captured = captured
    return fetch


def _poll(source, last_seen, last_id, state, fetch):
    github._login_cache.clear()
    return asyncio.run(github.poll(source, last_seen, last_id, state, fetch=fetch))


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")


def test_normalizes_event_shape_and_meta():
    fetch = _fetch_returning(200, [_gh_event(1001)])
    events, meta = _poll(_source(), None, None, None, fetch)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "pull_request.opened"
    assert ev["id"] == "1001"
    assert ev["created_at"] == "2026-06-09T10:00:00Z"
    assert ev["data"]["id"] == "1001"  # the ingress dedupe key
    assert ev["data"]["repo"] == "softwaresoftware-dev/repo"
    assert ev["data"]["actor"] == "ThatcherT"
    assert ev["data"]["payload"]["pull_request"]["number"] == 7
    assert meta["state"] == 'W/"abc"'
    assert meta["newest"] == {"id": "1001", "created_at": "2026-06-09T10:00:00Z"}
    # auth + conditional-request headers
    assert fetch.captured["headers"]["Authorization"] == "Bearer test-token"
    assert "If-None-Match" not in fetch.captured["headers"]
    # the PRIVATE-aware authenticated-user org feed, not /orgs/<org>/events
    assert fetch.captured["url"] == \
        "https://api.github.com/users/thatchert/events/orgs/softwaresoftware-dev?per_page=100"


def test_304_is_free_noop_keeping_state():
    fetch = _fetch_returning(304, None, etag=None)
    events, meta = _poll(_source(), "t", "100", 'W/"prev"', fetch)
    assert events == []
    assert meta == {"state": 'W/"prev"', "newest": None}
    assert fetch.captured["headers"]["If-None-Match"] == 'W/"prev"'


def test_cursor_filters_by_created_at_not_id():
    """Ids are per-type sequences (a PushEvent id can be billions above a
    later PullRequestEvent id) — filtering must be chronological."""
    body = [
        _gh_event(13108637081, gh_type="PushEvent", action=None,
                  created="2026-06-10T13:00:00Z"),           # watermark item
        _gh_event(10513637251, created="2026-06-10T13:18:54Z"),  # LOWER id, LATER time
    ]
    fetch = _fetch_returning(200, body)
    events, meta = _poll(_source(), "2026-06-10T13:00:00Z", "13108637081", None, fetch)
    assert [e["id"] for e in events] == ["10513637251"]
    assert meta["newest"]["id"] == "10513637251"  # newest by created_at


def test_watermark_item_skipped_same_second_sibling_passes():
    body = [
        _gh_event(1, created="2026-06-09T10:00:00Z"),  # exact watermark item
        _gh_event(2, created="2026-06-09T10:00:00Z"),  # same second, different id
        _gh_event(3, created="2026-06-09T09:00:00Z"),  # before watermark
    ]
    fetch = _fetch_returning(200, body)
    events, _ = _poll(_source(), "2026-06-09T10:00:00Z", "1", None, fetch)
    # sibling passes through (ingress dedupe absorbs cross-tick repeats);
    # older item and the watermark itself are dropped.
    assert [e["id"] for e in events] == ["2"]


def test_watching_filter_drops_unwatched_but_newest_tracks_them():
    body = [_gh_event(1, gh_type="PushEvent", action=None, created="2026-06-09T10:00:01Z"),
            _gh_event(2, created="2026-06-09T10:00:02Z"),
            _gh_event(3, gh_type="WatchEvent", action="started", created="2026-06-09T10:00:03Z")]
    fetch = _fetch_returning(200, body)
    events, meta = _poll(_source(), "2026-06-09T09:00:00Z", "0", None, fetch)
    assert [e["event_type"] for e in events] == ["pull_request.opened"]
    assert meta["newest"]["id"] == "3"  # unwatched raw items still advance the watermark


def test_empty_watching_means_all_mapped_types():
    body = [_gh_event(1, gh_type="PushEvent", action=None, created="2026-06-09T10:00:01Z"),
            _gh_event(2, created="2026-06-09T10:00:02Z")]
    fetch = _fetch_returning(200, body)
    events, _ = _poll(_source(watching=[]), "2026-06-09T09:00:00Z", "0", None, fetch)
    assert [e["event_type"] for e in events] == ["push", "pull_request.opened"]


def test_unmapped_type_skipped():
    body = [_gh_event(1, gh_type="SponsorshipEvent", action="created")]
    fetch = _fetch_returning(200, body)
    events, meta = _poll(_source(watching=[]), "2026-06-09T09:00:00Z", "0", None, fetch)
    assert events == []
    assert meta["newest"]["id"] == "1"


def test_events_ascending_by_created_at():
    body = [_gh_event(300, created="2026-06-09T11:00:00Z"),
            _gh_event(100, created="2026-06-09T09:00:00Z"),
            _gh_event(200, created="2026-06-09T10:00:00Z")]
    fetch = _fetch_returning(200, body)
    events, _ = _poll(_source(), None, None, None, fetch)
    assert [e["id"] for e in events] == ["100", "200", "300"]


def test_cold_start_returns_all_watched():
    body = [_gh_event(1), _gh_event(2)]
    fetch = _fetch_returning(200, body)
    events, _ = _poll(_source(), None, None, None, fetch)
    assert len(events) == 2  # the poller's guard decides what to do with them


def test_empty_feed_returns_no_newest():
    fetch = _fetch_returning(200, [])
    events, meta = _poll(_source(), None, None, None, fetch)
    assert events == []
    assert meta["newest"] is None


def test_non_200_raises():
    fetch = _fetch_returning(403, None)
    with pytest.raises(RuntimeError, match="status=403"):
        _poll(_source(), None, None, None, fetch)


def test_empty_scope_raises():
    fetch = _fetch_returning(200, [])
    with pytest.raises(RuntimeError, match="scope.orgs is empty"):
        _poll(_source(scope={}), None, None, None, fetch)


def test_login_resolved_once_per_token():
    calls = {"user": 0}

    async def fetch(url, headers):
        if url.endswith("/user"):
            calls["user"] += 1
            return 200, {}, {"login": "thatchert"}
        return 200, {"ETag": 'W/"x"'}, []

    github._login_cache.clear()
    asyncio.run(github.poll(_source(), None, None, None, fetch=fetch))
    asyncio.run(github.poll(_source(), None, None, None, fetch=fetch))
    assert calls["user"] == 1


# --------------------------- token resolution ---------------------------


def test_token_env_wins(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    assert github._resolve_token() == "env-token"


def test_token_falls_back_to_gh_cli(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    class Out:
        returncode = 0
        stdout = "ghp_clitoken\n"
        stderr = ""

    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: Out())
    assert github._resolve_token() == "ghp_clitoken"


def test_token_rejects_gh_error_on_stdout(monkeypatch, tmp_path):
    """Old gh versions write errors to stdout with rc 0 — must not be treated
    as a token; resolution falls through to hosts.yml."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    class Out:
        returncode = 0
        stdout = "no oauth token found for github.com\n"
        stderr = ""

    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: Out())
    gh_dir = tmp_path / ".config" / "gh"
    gh_dir.mkdir(parents=True)
    (gh_dir / "hosts.yml").write_text("github.com:\n  oauth_token: ghp_fromhosts\n")
    monkeypatch.setattr(github.Path, "home", classmethod(lambda cls: tmp_path))
    assert github._resolve_token() == "ghp_fromhosts"


def test_missing_token_raises_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(github.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(github.Path, "home", classmethod(lambda cls: tmp_path))
    fetch = _fetch_returning(200, [])
    with pytest.raises(RuntimeError, match="no GitHub token"):
        _poll(_source(), None, None, None, fetch)

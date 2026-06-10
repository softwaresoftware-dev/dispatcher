"""Tests for the GitHub Event Source adapter — Events API + ETag + filtering.

The adapter speaks the org Events API over httpx; tests inject a fake `fetch`
so no network or token is touched. Token resolution is tested separately with
env/subprocess/hosts.yml monkeypatching.
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
    captured = {}

    async def fetch(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        return status, {"ETag": etag} if etag else {}, body

    fetch.captured = captured
    return fetch


def _poll(source, last_seen, last_id, state, fetch):
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
    assert ev["data"]["repo"] == "softwaresoftware-dev/repo"
    assert ev["data"]["actor"] == "ThatcherT"
    assert ev["data"]["payload"]["pull_request"]["number"] == 7
    assert meta["state"] == 'W/"abc"'
    assert meta["newest"] == {"id": "1001", "created_at": "2026-06-09T10:00:00Z"}
    # auth + conditional-request headers
    assert fetch.captured["headers"]["Authorization"] == "Bearer test-token"
    assert "If-None-Match" not in fetch.captured["headers"]
    assert fetch.captured["url"] == "https://api.github.com/orgs/softwaresoftware-dev/events?per_page=100"


def test_304_is_free_noop_keeping_state():
    fetch = _fetch_returning(304, None, etag=None)
    events, meta = _poll(_source(), "t", "100", 'W/"prev"', fetch)
    assert events == []
    assert meta == {"state": 'W/"prev"', "newest": None}
    assert fetch.captured["headers"]["If-None-Match"] == 'W/"prev"'


def test_cursor_filters_by_int_id():
    body = [_gh_event(99), _gh_event(100), _gh_event(101)]
    fetch = _fetch_returning(200, body)
    events, meta = _poll(_source(), "t", "100", None, fetch)
    assert [e["id"] for e in events] == ["101"]
    # newest reflects the raw feed, not the filtered events
    assert meta["newest"]["id"] == "101"


def test_int_id_compare_survives_digit_growth():
    # String compare would say "9" > "10"; int compare must not.
    body = [_gh_event(10)]
    fetch = _fetch_returning(200, body)
    events, _ = _poll(_source(), "t", "9", None, fetch)
    assert [e["id"] for e in events] == ["10"]


def test_watching_filter_drops_unwatched_but_newest_tracks_them():
    body = [_gh_event(1, gh_type="PushEvent", action=None),
            _gh_event(2),
            _gh_event(3, gh_type="WatchEvent", action="started")]
    fetch = _fetch_returning(200, body)
    events, meta = _poll(_source(), "t", "0", None, fetch)
    assert [e["event_type"] for e in events] == ["pull_request.opened"]
    assert meta["newest"]["id"] == "3"  # unwatched raw items still advance the watermark


def test_empty_watching_means_all_mapped_types():
    body = [_gh_event(1, gh_type="PushEvent", action=None), _gh_event(2)]
    fetch = _fetch_returning(200, body)
    events, _ = _poll(_source(watching=[]), "t", "0", None, fetch)
    assert [e["event_type"] for e in events] == ["push", "pull_request.opened"]


def test_unmapped_type_skipped():
    body = [_gh_event(1, gh_type="SponsorshipEvent", action="created")]
    fetch = _fetch_returning(200, body)
    events, meta = _poll(_source(watching=[]), "t", "0", None, fetch)
    assert events == []
    assert meta["newest"]["id"] == "1"


def test_events_ascending_by_id():
    body = [_gh_event(300), _gh_event(100), _gh_event(200)]
    fetch = _fetch_returning(200, body)
    events, _ = _poll(_source(), None, None, None, fetch)
    assert [e["id"] for e in events] == ["100", "200", "300"]


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

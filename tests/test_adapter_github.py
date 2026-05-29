"""Tests for the bundled event-source:github adapter."""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.github import (
    GitHubAdapter,
    _int_id,
    _parse_poll_interval,
    _to_event_type,
)
from app.pollers.types import PullResult


# ---------- helpers ----------

def _gh_event(**kwargs) -> dict[str, Any]:
    """Build a minimal GitHub Events API event."""
    base = {
        "id": "12553207924",
        "type": "PushEvent",
        "actor": {"login": "ThatcherT"},
        "repo": {"name": "softwaresoftware-dev/dispatcher"},
        "payload": {},
        "created_at": "2026-05-29T17:05:38Z",
    }
    base.update(kwargs)
    return base


class FakeFetch:
    """Stub for the HTTP call. Each call() pops the next planned response."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def __call__(self, url, headers):
        self.requests.append({"url": url, "headers": dict(headers)})
        if not self._responses:
            raise AssertionError("FakeFetch ran out of planned responses")
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _force_token(monkeypatch):
    """Don't rely on the runtime's gh CLI for tests."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")


# ---------- pure helpers ----------

def test_to_event_type_with_action():
    assert _to_event_type(_gh_event(type="PullRequestEvent",
                                    payload={"action": "opened"})) == "pull_request.opened"


def test_to_event_type_without_action():
    assert _to_event_type(_gh_event(type="PushEvent", payload={})) == "push"


def test_to_event_type_unknown_returns_none():
    assert _to_event_type(_gh_event(type="MysteryEvent")) is None


def test_int_id_safe_for_numeric_strings():
    # The dry-run hazard: string-compare breaks when id length grows.
    assert _int_id("12553207924") < _int_id("100000000000")  # 11 -> 12 digits
    assert _int_id(None) == 0
    assert _int_id("") == 0


def test_parse_poll_interval():
    assert _parse_poll_interval("60") == 60
    assert _parse_poll_interval(None) is None
    assert _parse_poll_interval("not-a-number") is None
    assert _parse_poll_interval("0") is None


# ---------- pull() behavior ----------

@pytest.mark.asyncio
async def test_pull_first_call_goes_forward_emits_nothing():
    """cursor=None: record etag + newest id, emit zero events."""
    fetch = FakeFetch([(200,
                        {"ETag": 'W/"abc"', "X-Poll-Interval": "60"},
                        [_gh_event(id="2"), _gh_event(id="1")])])
    adapter = GitHubAdapter(fetch=fetch)

    result = await adapter.pull(
        scope={"orgs": ["softwaresoftware-dev"]}, cursor=None,
    )

    assert result.events == []
    assert result.new_cursor == {"etag": 'W/"abc"', "last_id": "2"}
    assert result.next_tick_hint_s == 60
    # Token was applied.
    assert "Bearer test-token" in fetch.requests[0]["headers"]["Authorization"]
    # No If-None-Match on first call.
    assert "If-None-Match" not in fetch.requests[0]["headers"]


@pytest.mark.asyncio
async def test_pull_uses_if_none_match_on_subsequent_call():
    fetch = FakeFetch([(304, {"X-Poll-Interval": "60"}, None)])
    adapter = GitHubAdapter(fetch=fetch)

    cursor = {"etag": 'W/"abc"', "last_id": "2"}
    result = await adapter.pull(scope={"orgs": ["x"]}, cursor=cursor)

    assert result.events == []
    assert result.new_cursor == cursor  # unchanged
    assert result.next_tick_hint_s == 60
    assert fetch.requests[0]["headers"]["If-None-Match"] == 'W/"abc"'


@pytest.mark.asyncio
async def test_pull_emits_only_events_newer_than_cursor():
    """Three events with ids 5/4/3; cursor at 3 → emit 4 and 5, oldest first."""
    fetch = FakeFetch([(
        200, {"ETag": 'W/"new"', "X-Poll-Interval": "60"},
        [
            _gh_event(id="5", type="PullRequestEvent", payload={"action": "opened"}),
            _gh_event(id="4", type="IssuesEvent",      payload={"action": "opened"}),
            _gh_event(id="3", type="PushEvent",        payload={}),
        ],
    )])
    adapter = GitHubAdapter(fetch=fetch)

    result = await adapter.pull(
        scope={"orgs": ["x"]}, cursor={"etag": 'W/"old"', "last_id": "3"},
    )

    assert [e.event_id for e in result.events] == ["4", "5"]  # chronological
    assert [e.event_type for e in result.events] == [
        "issues.opened", "pull_request.opened",
    ]
    assert result.new_cursor == {"etag": 'W/"new"', "last_id": "5"}


@pytest.mark.asyncio
async def test_pull_advances_cursor_even_when_all_events_filtered():
    """Cursor must move even if every event has a type we don't model — else
    the next tick re-scans the same window forever."""
    fetch = FakeFetch([(
        200, {"ETag": 'W/"new"'},
        [_gh_event(id="9", type="MysteryEvent", payload={})],
    )])
    adapter = GitHubAdapter(fetch=fetch)
    result = await adapter.pull(
        scope={"orgs": ["x"]}, cursor={"etag": 'W/"old"', "last_id": "1"},
    )
    assert result.events == []
    assert result.new_cursor["last_id"] == "9"


@pytest.mark.asyncio
async def test_pull_int_overflow_safety():
    """Cursor=12553207924; new event id=100000000000 (12 digits).

    Naive string-compare would treat "100..." < "125..." and skip the event.
    Int-compare correctly emits it.
    """
    fetch = FakeFetch([(
        200, {"ETag": 'W/"x"'},
        [_gh_event(id="100000000000", type="PushEvent")],
    )])
    adapter = GitHubAdapter(fetch=fetch)
    result = await adapter.pull(
        scope={"orgs": ["x"]},
        cursor={"etag": 'W/"prev"', "last_id": "12553207924"},
    )
    assert [e.event_id for e in result.events] == ["100000000000"]
    assert result.new_cursor["last_id"] == "100000000000"


@pytest.mark.asyncio
async def test_pull_raises_when_no_org_scope():
    """An EventSource with empty scope.orgs returns an empty result with a log."""
    adapter = GitHubAdapter(fetch=FakeFetch([]))
    result = await adapter.pull(scope={}, cursor=None)
    assert result == PullResult()


@pytest.mark.asyncio
async def test_pull_propagates_non_200_non_304(monkeypatch):
    fetch = FakeFetch([(403, {}, None)])
    adapter = GitHubAdapter(fetch=fetch)
    with pytest.raises(RuntimeError, match="status=403"):
        await adapter.pull(scope={"orgs": ["x"]}, cursor=None)


@pytest.mark.asyncio
async def test_pull_raises_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    # Force gh CLI fallback to also return empty.
    import app.adapters.github as ga
    monkeypatch.setattr(ga, "_resolve_token", lambda: None)

    adapter = GitHubAdapter(fetch=FakeFetch([]))
    with pytest.raises(RuntimeError, match="no GitHub token"):
        await adapter.pull(scope={"orgs": ["x"]}, cursor=None)

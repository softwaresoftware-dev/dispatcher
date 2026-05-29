"""event-source:github — poll-only adapter against the org/user events API.

Cursor shape: ``{"etag": str | None, "last_id": str | None}``
- ``etag`` is sent as ``If-None-Match`` on the next request; GitHub returns 304
  with no body and no rate-limit cost when nothing changed.
- ``last_id`` is the largest event id we've already emitted. Compared as **int**
  (GitHub ids are numeric strings; string-compare breaks when ids grow a digit).

Scope shape: ``{"orgs": [...]}`` (one org per Event Source today; multi-org or
per-repo scopes are a later iteration). The adapter fetches
``/orgs/<org>/events?per_page=100`` once per tick.

Auth: GitHub Personal Access Token from the ``GITHUB_TOKEN`` environment
variable, or — if absent — whatever ``gh auth token`` resolves to. This lets
both the developer machine (using the gh CLI's stored token) and a server
deployment (using an env var) work without special-casing.

Watch list filtering happens in the runtime (``_filter_events``); the
adapter returns every relevant event_type and lets the runtime drop the
ones the operator didn't subscribe to.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any

import httpx

from ..pollers.types import Adapter, Event, PullResult

log = logging.getLogger("dispatcher.adapters.github")

EVENTS_API_TIMEOUT_S = 15

# (GitHub event type) -> (event_type prefix in our system).
# For events that have a payload.action, we emit "<prefix>.<action>";
# for events that don't (PushEvent, CreateEvent, etc.) we emit just the prefix.
_TYPE_MAP = {
    "PullRequestEvent":       "pull_request",
    "PullRequestReviewEvent": "pull_request_review",
    "PullRequestReviewCommentEvent": "pull_request_review_comment",
    "IssuesEvent":            "issues",
    "IssueCommentEvent":      "issue_comment",
    "PushEvent":              "push",
    "CreateEvent":            "create",
    "DeleteEvent":            "delete",
    "ReleaseEvent":           "release",
    "ForkEvent":              "fork",
    "WatchEvent":             "watch",
    "PublicEvent":            "public",
    "GollumEvent":            "gollum",
    "MemberEvent":            "member",
    "CommitCommentEvent":     "commit_comment",
}


def _resolve_token() -> str | None:
    """Return a GitHub token. Resolution order:

    1. ``$GITHUB_TOKEN`` or ``$GH_TOKEN`` env var (preferred for daemons)
    2. ``gh auth token`` CLI call (preferred on dev machines)
    3. ``~/.config/gh/hosts.yml`` direct read (fallback when gh CLI isn't on
       the PATH the daemon was launched with — common under systemd user
       units where the env doesn't inherit the user's interactive PATH)

    Each fallback validates that the result *looks* like a token (no stderr
    leakage from a failed CLI call). Returns None if nothing usable found.
    """
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env and env.strip():
        return env.strip()

    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode == 0:
            token = out.stdout.strip()
            if token and "\n" not in token and " " not in token:
                return token
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Last resort: parse ~/.config/gh/hosts.yml ourselves. The file shape:
    #   github.com:
    #     oauth_token: ghp_xxx
    #     ...
    try:
        import yaml  # local import; PyYAML is a dispatcher dep anyway
        hosts_path = os.path.expanduser("~/.config/gh/hosts.yml")
        if not os.path.isfile(hosts_path):
            return None
        with open(hosts_path) as f:
            data = yaml.safe_load(f) or {}
        github = (data.get("github.com") or {})
        token = github.get("oauth_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    except (ImportError, OSError):
        pass

    return None


def _to_event_type(gh_event: dict[str, Any]) -> str | None:
    prefix = _TYPE_MAP.get(gh_event.get("type", ""))
    if not prefix:
        return None
    action = (gh_event.get("payload") or {}).get("action")
    return f"{prefix}.{action}" if action else prefix


def _int_id(s: str | None) -> int:
    """Cast a string-id to int for safe ordering. Empty -> 0."""
    if not s:
        return 0
    try:
        return int(s)
    except (TypeError, ValueError):
        # Some endpoints' ids aren't pure-digit; fall back to length+lex so we
        # at least monotonically advance even if the comparison isn't perfect.
        return len(s)


class GitHubAdapter:
    """Poll the GitHub Events API for one org per Event Source."""

    system = "github"

    def __init__(self, *, fetch=None) -> None:
        # `fetch` is injectable so tests can stub the HTTP call without
        # spinning up an httpx mock transport per test.
        self._fetch = fetch or self._http_fetch

    async def _http_fetch(
        self, url: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], list[dict[str, Any]] | None]:
        """One real HTTP call. Returns (status, headers, body-or-None)."""
        async with httpx.AsyncClient(timeout=EVENTS_API_TIMEOUT_S) as client:
            r = await client.get(url, headers=headers)
        body = None
        if r.status_code == 200:
            try:
                body = r.json()
            except ValueError:
                body = None
        return r.status_code, dict(r.headers), body

    async def pull(
        self, scope: dict[str, Any], cursor: dict[str, Any] | None,
    ) -> PullResult:
        orgs = scope.get("orgs") or []
        if not orgs:
            log.warning("github adapter: scope.orgs empty — nothing to poll")
            return PullResult()
        # Phase 3: one org per source. Multi-org would fan-out here.
        org = orgs[0]
        url = f"https://api.github.com/orgs/{org}/events?per_page=100"

        token = _resolve_token()
        if not token:
            raise RuntimeError(
                "no GitHub token available — set GITHUB_TOKEN or run `gh auth login`"
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dispatcher-event-source-github/1",
        }
        prev_etag = (cursor or {}).get("etag")
        if prev_etag:
            headers["If-None-Match"] = prev_etag

        status, resp_headers, body = await self._fetch(url, headers)

        next_tick_hint = _parse_poll_interval(resp_headers.get("X-Poll-Interval"))

        # 304: nothing changed. Free call. Keep the existing cursor.
        if status == 304:
            return PullResult(
                events=[], new_cursor=cursor or {}, next_tick_hint_s=next_tick_hint,
            )

        # 200: walk events.
        if status != 200 or body is None:
            raise RuntimeError(
                f"GitHub events API returned status={status} for {url}"
            )

        new_etag = resp_headers.get("ETag") or resp_headers.get("etag")

        prev_last_id = (cursor or {}).get("last_id")

        # First-ever call: record "now" and emit nothing — go-forward only.
        if cursor is None:
            new_last_id = body[0]["id"] if body else None
            return PullResult(
                events=[], new_cursor={"etag": new_etag, "last_id": new_last_id},
                next_tick_hint_s=next_tick_hint,
            )

        # Subsequent call: emit events with id > prev_last_id, in chronological
        # order (oldest first) so downstream sees them as they happened.
        prev_int = _int_id(prev_last_id)
        fresh = [e for e in body if _int_id(e.get("id")) > prev_int]
        fresh.sort(key=lambda e: _int_id(e.get("id")))  # ascending

        events: list[Event] = []
        for raw in fresh:
            et = _to_event_type(raw)
            if not et:
                continue  # event type we don't model — skip
            events.append(Event(
                source="github",
                event_type=et,
                event_id=str(raw["id"]),
                data={
                    "repo": (raw.get("repo") or {}).get("name"),
                    "actor": (raw.get("actor") or {}).get("login"),
                    "payload": raw.get("payload") or {},
                },
                occurred_at=raw.get("created_at"),
            ))

        # Cursor advances to the newest id we saw (even if filtered out by
        # _to_event_type), so we don't re-scan known events on the next tick.
        newest_in_body = max(
            (_int_id(e.get("id")) for e in body), default=prev_int,
        )
        new_last_id = str(newest_in_body) if newest_in_body > prev_int else prev_last_id

        return PullResult(
            events=events,
            new_cursor={"etag": new_etag, "last_id": new_last_id},
            next_tick_hint_s=next_tick_hint,
        )


def _parse_poll_interval(raw: str | None) -> int | None:
    """X-Poll-Interval is an integer number of seconds. Ignore garbage."""
    if not raw:
        return None
    try:
        n = int(raw)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None

"""GitHub adapter — polls the org Events API and normalizes to ingress events.

Acquisition is `GET /orgs/<org>/events?per_page=100` over httpx with ETag
conditional requests: the previous response's ETag goes out as If-None-Match,
and a 304 (the common idle tick) carries no body and costs no rate limit.
This is the primary free-tick mechanism, not an optimization — a 60s-interval
daemon would otherwise burn quota all day saying "anything new? no."

Event-type coverage: every type in _TYPE_MAP, emitted as `<prefix>.<action>`
when the GitHub payload carries an action (`pull_request.opened`) and bare
`<prefix>` when it doesn't (`push`). The source's `watching:` list filters
in-process; an empty list means everything mapped.

Auth — identity inheritance, no stored credentials. Resolution order:
  1. $GITHUB_TOKEN / $GH_TOKEN            (daemon/server deployments)
  2. `gh auth token`                       (dev machines; output validated
                                            because old gh versions write
                                            errors to stdout)
  3. ~/.config/gh/hosts.yml direct read    (systemd user units, where the
                                            daemon's PATH may not carry gh)
The `credentials_ref: github` field in an Event Source declaration means
"use the operator's gh identity"; there is no dispatcher-side token store.

Cursor semantics: ids on the Events API are numeric and monotonic — filtering
is `int(id) > int(last_id)`. `last_seen` (created_at) is carried for the
watermark display and cold-start reporting; the id is what's authoritative.
On a cold start (no last_id) ALL current events are returned ascending and the
poller's go-forward guard decides what to do with them.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import yaml

log = logging.getLogger("dispatcher.adapters.github")

EVENTS_API_TIMEOUT_S = 15

# (GitHub event type) -> (event_type prefix in our system).
_TYPE_MAP = {
    "PullRequestEvent":              "pull_request",
    "PullRequestReviewEvent":        "pull_request_review",
    "PullRequestReviewCommentEvent": "pull_request_review_comment",
    "IssuesEvent":                   "issues",
    "IssueCommentEvent":             "issue_comment",
    "PushEvent":                     "push",
    "CreateEvent":                   "create",
    "DeleteEvent":                   "delete",
    "ReleaseEvent":                  "release",
    "ForkEvent":                     "fork",
    "WatchEvent":                    "watch",
    "PublicEvent":                   "public",
    "GollumEvent":                   "gollum",
    "MemberEvent":                   "member",
    "CommitCommentEvent":            "commit_comment",
}


def _resolve_token() -> str | None:
    """GitHub token via env -> gh CLI -> hosts.yml. None if nothing usable."""
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
            # Old gh versions write errors to stdout with rc 0 — a real token
            # is one whitespace-free line.
            if token and "\n" not in token and " " not in token:
                return token
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        hosts_path = Path.home() / ".config" / "gh" / "hosts.yml"
        if hosts_path.is_file():
            data = yaml.safe_load(hosts_path.read_text()) or {}
            token = (data.get("github.com") or {}).get("oauth_token")
            if isinstance(token, str) and token.strip():
                return token.strip()
    except (OSError, yaml.YAMLError):
        pass

    return None


def _to_event_type(gh_event: dict[str, Any]) -> str | None:
    prefix = _TYPE_MAP.get(gh_event.get("type", ""))
    if not prefix:
        return None
    action = (gh_event.get("payload") or {}).get("action")
    return f"{prefix}.{action}" if action else prefix


def _int_id(s: str | None) -> int:
    """Numeric-string id -> int for monotonic compare. Unparseable -> 0."""
    if not s:
        return 0
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


async def _http_fetch(url: str, headers: dict[str, str]):
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


async def poll(source, last_seen, last_event_id, state, *, fetch=None):
    """Return (events, meta): watched events newer than the cursor, ascending.

    `state` is the adapter's opaque persisted string — for GitHub, the ETag.
    `meta` is {"state": <new etag>, "newest": {"id", "created_at"} | None};
    `newest` is the newest RAW feed item regardless of type/watching filters,
    so the poller can watermark past unwatched traffic (and so a cold start
    against a feed with no watched events still establishes a cursor).

    A 304 returns ([], {"state": state, "newest": None}) — zero rate-limit cost.
    Raises RuntimeError on missing scope/token or a non-200/304 response.
    """
    fetch = fetch or _http_fetch

    orgs = (source.scope or {}).get("orgs") or []
    if not orgs:
        raise RuntimeError(f"source {source.name}: scope.orgs is empty — nothing to poll")
    # One org per source today; multi-org would fan out here.
    org = orgs[0]
    url = f"https://api.github.com/orgs/{org}/events?per_page=100"

    token = _resolve_token()
    if not token:
        raise RuntimeError(
            "no GitHub token available — set GITHUB_TOKEN, or run `gh auth login` "
            "(the adapter reuses the operator's gh identity; nothing is stored)"
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dispatcher-event-source-github/2",
    }
    if state:
        headers["If-None-Match"] = state

    status, resp_headers, body = await fetch(url, headers)

    if status == 304:
        return [], {"state": state, "newest": None}  # idle tick — free call

    if status != 200 or body is None:
        raise RuntimeError(f"GitHub events API returned status={status} for {url}")

    new_etag = resp_headers.get("ETag") or resp_headers.get("etag")

    newest_raw = max(body, key=lambda e: _int_id(e.get("id")), default=None)
    newest = ({"id": str(newest_raw.get("id")), "created_at": newest_raw.get("created_at")}
              if newest_raw else None)

    prev_int = _int_id(last_event_id)
    watching = set(source.watching or [])

    events: list[dict] = []
    for raw in body:
        if last_event_id is not None and _int_id(raw.get("id")) <= prev_int:
            continue
        et = _to_event_type(raw)
        if not et:
            continue  # event type we don't model
        if watching and et not in watching:
            continue  # declared but not subscribed
        events.append({
            "event_type": et,
            "id": str(raw.get("id")),
            "created_at": raw.get("created_at"),
            "data": {
                "repo": (raw.get("repo") or {}).get("name"),
                "actor": (raw.get("actor") or {}).get("login"),
                "payload": raw.get("payload") or {},
            },
        })

    events.sort(key=lambda e: _int_id(e["id"]))  # ascending — cursor advances monotonically
    return events, {"state": new_etag, "newest": newest}

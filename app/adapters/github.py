"""GitHub adapter — polls the Events API and normalizes to ingress events.

Acquisition is `GET /users/<login>/events/orgs/<org>?per_page=100` — the
authenticated-user org feed, which (unlike the public `/orgs/<org>/events`)
includes PRIVATE repo events; org repos are private by default, so the public
feed silently misses most real activity (found live, 2026-06-10). `<login>`
is the token's own user, resolved once per process via `GET /user`.

Requests go over httpx with ETag conditional requests: the previous response's
ETag goes out as If-None-Match, and a 304 (the common idle tick) carries no
body and costs no rate limit. This is the primary free-tick mechanism, not an
optimization — a 60s-interval daemon would otherwise burn quota all day saying
"anything new? no."

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

Cursor semantics: filtering is by `created_at` (ISO-Z strings — lexicographic
compare IS chronological). Event ids are NOT one monotonic sequence — each
event type draws from its own range (a PullRequestEvent and a PushEvent one
second apart differ by billions; found live, 2026-06-10), so an id watermark
silently drops whole types. `last_event_id` only breaks the tie for the exact
watermark item; same-second boundary repeats across ticks are absorbed by the
ingress dedupe (each event's `data.id` is the dedupe key). On a cold start
(no cursor) ALL current events are returned ascending and the poller's
go-forward guard decides what to do with them.
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


# login per token (sha-keyed) — one GET /user per process, not per tick.
_login_cache: dict[str, str] = {}


async def _resolve_login(token: str, fetch) -> str:
    import hashlib
    key = hashlib.sha256(token.encode()).hexdigest()[:16]
    if key in _login_cache:
        return _login_cache[key]
    status, _, body = await fetch(
        "https://api.github.com/user",
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dispatcher-event-source-github/2",
        },
    )
    login = (body or {}).get("login") if isinstance(body, dict) else None
    if status != 200 or not login:
        raise RuntimeError(f"could not resolve the token's user (GET /user -> {status})")
    _login_cache[key] = login
    return login


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

    token = _resolve_token()
    if not token:
        raise RuntimeError(
            "no GitHub token available — set GITHUB_TOKEN, or run `gh auth login` "
            "(the adapter reuses the operator's gh identity; nothing is stored)"
        )

    # The authenticated-user org feed — sees private repo events the public
    # /orgs/<org>/events endpoint omits.
    login = await _resolve_login(token, fetch)
    url = f"https://api.github.com/users/{login}/events/orgs/{org}?per_page=100"

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

    # Newest by created_at — ids are per-type sequences, NOT comparable.
    newest_raw = max(body, key=lambda e: e.get("created_at") or "", default=None)
    newest = ({"id": str(newest_raw.get("id")), "created_at": newest_raw.get("created_at")}
              if newest_raw else None)

    watching = set(source.watching or [])

    events: list[dict] = []
    for raw in body:
        created = raw.get("created_at") or ""
        rid = str(raw.get("id"))
        if last_event_id is not None and last_seen:
            # Chronological watermark; ISO-Z strings compare lexicographically.
            if created < last_seen:
                continue
            if created == last_seen and rid == last_event_id:
                continue  # the exact watermark item
            # created == last_seen with a different id passes through — the
            # ingress dedupe (keyed on data.id) absorbs cross-tick repeats.
        et = _to_event_type(raw)
        if not et:
            continue  # event type we don't model
        if watching and et not in watching:
            continue  # declared but not subscribed
        events.append({
            "event_type": et,
            "id": rid,
            "created_at": raw.get("created_at"),
            "data": {
                "id": rid,  # the ingress dedupe key
                "repo": (raw.get("repo") or {}).get("name"),
                "actor": (raw.get("actor") or {}).get("login"),
                "payload": raw.get("payload") or {},
            },
        })

    # Ascending by time (id as a stable same-second tiebreak) so the cursor
    # advances monotonically.
    events.sort(key=lambda e: (e["created_at"] or "", e["id"]))
    return events, {"state": new_etag, "newest": newest}

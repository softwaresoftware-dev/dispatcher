"""Static (source, event_type) → channel routing.

Read at request time from `~/.dispatcher/channels.yaml` (or the path in
DISPATCHER_CHANNELS_FILE). When a route matches, dispatcher-ingress
skips the LLM dispatcher and forwards directly. Two target shapes:

  - `session:<name>` — POST to session-bridge /sessions/<name>/message
  - `spawn:<recipe>` — POST to taskpilot's daemon to create+spawn an agent

Unmapped events fall through to the LLM-routed path (existing behavior).
This module is intentionally cache-light: routes reload on every call so
edits to channels.yaml take effect without bouncing the daemon.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CHANNELS_FILE = Path(
    os.environ.get(
        "DISPATCHER_CHANNELS_FILE",
        str(Path.home() / ".dispatcher" / "channels.yaml"),
    )
)

# Tests reset this between cases. Production callers can leave it None and
# pay the YAML re-read cost on each lookup — channels.yaml is small.
_cached_routes: list["Route"] | None = None


@dataclass
class Route:
    source: str
    target: str
    event_type: str | None = None  # None = match any event_type
    # Literal brief values for `spawn:` targets. Static routes skip the LLM
    # dispatcher, so the recipe's brief.json {{placeholders}} have nobody to
    # fill them — this block supplies them. Keys map to the recipe's
    # brief_schema. Values may contain {event_id} / {task_id} substitutions.
    brief: dict | None = None


def load_routes(path: Path | None = None) -> list[Route]:
    """Read channels.yaml. Returns [] if the file is missing or empty."""
    p = path or DEFAULT_CHANNELS_FILE
    try:
        raw = p.read_text()
    except (FileNotFoundError, OSError):
        return []
    try:
        config = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return []
    items = config.get("routes") or []
    out: list[Route] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        target = item.get("target")
        if not source or not target:
            continue
        brief = item.get("brief")
        if not isinstance(brief, dict):
            brief = None
        out.append(Route(
            source=source,
            target=target,
            event_type=item.get("event_type"),
            brief=brief,
        ))
    return out


def lookup_route(source: str, event_type: str | None,
                 channels_file: Path | None = None) -> Route | None:
    """Find the first matching Route, or None.

    Match rules: source must match exactly. event_type matches if the route
    has the same event_type, or if the route omits event_type (wildcard).

    channels_file: read routes from this workspace's channels.yaml instead of
    the global default — set by the multi-tenant poller per event.
    """
    for route in load_routes(channels_file):
        if route.source != source:
            continue
        if route.event_type is None or route.event_type == event_type:
            return route
    return None


def lookup(source: str, event_type: str | None) -> str | None:
    """Find the first matching route's target string, or None."""
    route = lookup_route(source, event_type)
    return route.target if route else None

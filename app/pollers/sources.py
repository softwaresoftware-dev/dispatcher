"""Load EventSource definitions from ``~/.dispatcher/event-sources/*.yaml``.

YAML shape (per docs/event-sources.md):

    name: softwaresoftware-prs
    system: github
    scope:
      orgs: [softwaresoftware-dev]
    watching:
      - pull_request.opened
      - pull_request.review_requested
    credentials_ref: github
    transport: poll
    tick: 60s
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - yaml ships with the dispatcher venv
    yaml = None  # type: ignore[assignment]

from .types import EventSource

log = logging.getLogger("dispatcher.pollers.sources")

SOURCES_DIR = Path(os.environ.get(
    "DISPATCHER_SOURCES_DIR",
    os.path.expanduser("~/.dispatcher/event-sources"),
))


def _parse_tick(raw: object) -> int:
    """Accept '60', '60s', '2m', '1h'. Default 60s."""
    if raw is None:
        return 60
    if isinstance(raw, int):
        return max(1, raw)
    m = re.fullmatch(r"\s*(\d+)\s*(s|m|h)?\s*", str(raw))
    if not m:
        return 60
    n = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def load_sources() -> list[EventSource]:
    """Read every ``*.yaml`` file in the sources directory. Returns a list of
    parsed EventSource objects. Files that fail to parse are logged and
    skipped — one bad file should not take down the runtime.
    """
    if yaml is None:
        log.warning("PyYAML not available — no event sources will be loaded")
        return []
    if not SOURCES_DIR.is_dir():
        return []
    out: list[EventSource] = []
    for path in sorted(SOURCES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            log.warning("skipping %s: %s", path.name, e)
            continue
        if not isinstance(data, dict):
            log.warning("skipping %s: top-level must be a mapping", path.name)
            continue
        name = data.get("name") or path.stem
        system = data.get("system")
        if not system:
            log.warning("skipping %s: missing `system`", path.name)
            continue
        out.append(EventSource(
            name=str(name),
            system=str(system),
            scope=data.get("scope") or {},
            watching=list(data.get("watching") or []),
            credentials_ref=data.get("credentials_ref"),
            transport=str(data.get("transport") or "auto"),
            tick_s=_parse_tick(data.get("tick")),
        ))
    return out

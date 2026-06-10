"""Load Event Source declarations from ~/.dispatcher/event-sources/*.yaml.

A declaration says WHAT to watch, WHERE, and with WHOSE credentials. Transport
(poll vs webhook) is resolved by the runtime; on a NAT'd host `auto` -> poll.
The poller reads these, dispatches each to the adapter registered for its
`system`, and routes whatever the adapter reports as new.

Shape (see softwaresoftware-prs.yaml):
    name: softwaresoftware-prs
    system: github
    scope: { orgs: [softwaresoftware-dev] }
    watching: [pull_request.opened]
    credentials_ref: github
    transport: auto

Env override:
    DISPATCHER_EVENT_SOURCES_DIR=/path/to/dir   (default ~/.dispatcher/event-sources)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_DIR = Path(
    os.environ.get(
        "DISPATCHER_EVENT_SOURCES_DIR",
        str(Path.home() / ".dispatcher" / "event-sources"),
    )
)


@dataclass
class EventSource:
    name: str
    system: str
    watching: list[str] = field(default_factory=list)
    scope: dict = field(default_factory=dict)
    credentials_ref: str | None = None
    transport: str = "auto"
    raw: dict = field(default_factory=dict)


def load_sources(directory=None) -> list[EventSource]:
    """Read every *.yaml in the event-sources dir. Skips malformed / incomplete
    declarations (missing name or system) rather than failing the whole poll."""
    d = Path(directory or DEFAULT_DIR)
    if not d.is_dir():
        return []
    out: list[EventSource] = []
    for p in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text()) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        system = data.get("system")
        if not name or not system:
            continue
        out.append(
            EventSource(
                name=name,
                system=system,
                watching=data.get("watching") or [],
                scope=data.get("scope") or {},
                credentials_ref=data.get("credentials_ref"),
                transport=data.get("transport", "auto"),
                raw=data,
            )
        )
    return out

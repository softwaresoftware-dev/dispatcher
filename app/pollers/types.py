"""Data shapes shared between the runtime and adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Event:
    """One thing that happened upstream, normalized for the routing layer.

    Matches the ``POST /api/event`` body shape so the runtime can forward
    these into the same code path the webhook handler uses.
    """
    source: str
    event_type: str
    event_id: str
    data: dict[str, Any] = field(default_factory=dict)
    occurred_at: str | None = None  # ISO-8601 if the adapter knows it


@dataclass
class PullResult:
    """What ``Adapter.pull`` returns.

    ``next_tick_hint_s`` is the upstream's recommended cadence
    (e.g. GitHub ``X-Poll-Interval``). The runtime widens its tick to the
    larger of the configured interval and this hint so the upstream
    contract always wins.
    """
    events: list[Event] = field(default_factory=list)
    new_cursor: dict[str, Any] = field(default_factory=dict)
    next_tick_hint_s: int | None = None


class Adapter(Protocol):
    """Source adapter contract — implementing plugins satisfy this protocol.

    ``system`` identifies the capability the adapter provides:
    ``event-source:<system>``. Plugin sets this on registration.

    ``credentials`` is an opaque dict resolved by the runtime from the
    Event Source's ``credentials_ref``. Shape is adapter-defined
    (e.g. ``{"token": "ghp_..."}`` for GitHub). An empty dict signals
    "no managed credentials configured" — adapters MAY fall back to
    environment variables for the dev-machine case.
    """
    system: str

    async def pull(
        self,
        scope: dict[str, Any],
        cursor: dict[str, Any] | None,
        credentials: dict[str, Any],
    ) -> PullResult: ...


@dataclass
class EventSource:
    """An enrolled subscription, parsed from a YAML file under
    ``~/.dispatcher/event-sources/<name>.yaml``.
    """
    name: str
    system: str
    scope: dict[str, Any] = field(default_factory=dict)
    watching: list[str] = field(default_factory=list)
    credentials_ref: str | None = None
    transport: str = "auto"        # auto | poll | webhook | both
    tick_s: int = 60               # operator's requested cadence

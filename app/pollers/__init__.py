"""Poller runtime — supervised pull-side ingest, sibling to the webhook handler.

The webhook path (``POST /api/event``) and the poll path both feed the same
routing layer. Adapters are loaded by capability; one per source system. Each
Event Source is a YAML file under ``~/.dispatcher/event-sources/``.

See ``docs/event-sources.md`` for the design.
"""

from .registry import register_adapter, get_adapter, list_adapters
from .runtime import start_runtime, stop_runtime
from .types import Event, PullResult, Adapter

__all__ = [
    "Event",
    "PullResult",
    "Adapter",
    "register_adapter",
    "get_adapter",
    "list_adapters",
    "start_runtime",
    "stop_runtime",
]

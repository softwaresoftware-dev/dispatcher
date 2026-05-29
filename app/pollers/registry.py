"""Adapter registry.

Adapter plugins call ``register_adapter(adapter)`` at import time. Phase 1
ships an empty registry; the GitHub adapter (phase 3) is the first entry.

Discovery of adapter plugins is intentionally out of scope here — the runtime
just exposes a registration API. The boot path (``app/main.py``) imports any
known-good adapter modules; eventually a softwaresoftware-resolver call will
walk installed providers with ``event-source:*`` capabilities.
"""

from __future__ import annotations

from .types import Adapter

_registry: dict[str, Adapter] = {}


def register_adapter(adapter: Adapter) -> None:
    """Plugins call this at import time."""
    if not getattr(adapter, "system", None):
        raise ValueError("adapter is missing required `system` attribute")
    _registry[adapter.system] = adapter


def get_adapter(system: str) -> Adapter | None:
    return _registry.get(system)


def list_adapters() -> list[str]:
    return sorted(_registry)

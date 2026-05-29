"""Import side-effect module: register every in-tree adapter at startup.

``app/main.py``'s startup hook imports this; each adapter module either
registers itself on import or via a local call here. Keeping this in a
single file makes the contract explicit — adding a new adapter means
editing one line here, not chasing imports.
"""

from __future__ import annotations

import logging

from .adapters.github import GitHubAdapter
from .pollers import register_adapter

log = logging.getLogger("dispatcher.adapters_bootstrap")


def _safe_register(adapter, name: str) -> None:
    try:
        register_adapter(adapter)
        log.info("registered adapter: %s", name)
    except Exception as e:  # noqa: BLE001
        log.warning("failed to register adapter %s: %s", name, e)


_safe_register(GitHubAdapter(), "event-source:github")

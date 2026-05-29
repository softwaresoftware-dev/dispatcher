"""On-disk credentials store for Event Source adapters.

Shape: ``~/.dispatcher/credentials.yaml`` (chmod 600) — a flat top-level
map of ``{ref_name: secret_bag}``. Each secret_bag is an opaque dict the
adapter knows how to interpret. We don't validate schema here; the adapter
is the only thing that knows what fields it needs.

The runtime calls ``credentials.get(ref)`` once per tick. Adapters never
read environment variables or files — credential resolution is pulled
into one place.

This is intentionally simple and on-disk plaintext for now. A keyring
backend can swap in later behind the same API without changing adapters
or the runtime.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("dispatcher.credentials")

try:
    import yaml
except ImportError:  # pragma: no cover — yaml ships in the dispatcher venv
    yaml = None  # type: ignore[assignment]


PATH = Path(os.environ.get(
    "DISPATCHER_CREDENTIALS_FILE",
    os.path.expanduser("~/.dispatcher/credentials.yaml"),
))


def _load() -> dict[str, dict[str, Any]]:
    if yaml is None or not PATH.is_file():
        return {}
    try:
        raw = yaml.safe_load(PATH.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("could not read credentials file %s: %s", PATH, e)
        return {}
    if not isinstance(raw, dict):
        log.warning("credentials file %s top-level is not a mapping; ignoring", PATH)
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _write(store: dict[str, dict[str, Any]]) -> None:
    if yaml is None:
        raise RuntimeError("PyYAML not available; cannot persist credentials")
    PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp path + atomic rename so a crash mid-write can't
    # corrupt the file.
    tmp = PATH.with_suffix(PATH.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(store, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, PATH)


def get(ref: str | None) -> dict[str, Any]:
    """Return the secret bag for `ref`, or an empty dict if missing or unset.

    An empty dict is a deliberate signal to adapters that no managed
    credentials exist — adapters MAY fall back to environment variables or
    other implicit sources for the dev-machine case. Production installs
    should always pass a real ref.
    """
    if not ref:
        return {}
    return _load().get(ref, {})


def set(ref: str, secrets: dict[str, Any]) -> None:
    """Upsert a credential bag. Caller is responsible for validating
    secrets shape against whatever the consuming adapter expects."""
    if not ref:
        raise ValueError("credential ref name is required")
    store = _load()
    store[ref] = dict(secrets)
    _write(store)


def delete(ref: str) -> None:
    store = _load()
    if ref in store:
        del store[ref]
        _write(store)


def list_refs() -> list[str]:
    return sorted(_load())

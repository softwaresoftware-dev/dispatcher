"""Poller runtime — one supervised tick coroutine per Event Source.

Sits alongside the FastAPI app inside the dispatcher process. Started from
``app/main.py``'s startup hook; stopped from shutdown. Crashes in a single
adapter's tick are isolated: log, back off, restart that one. Other sources
keep ticking.

Phase 1 deliberately forwards events to the routing layer over localhost
HTTP (``POST /api/event``) rather than in-process. The spec calls for
in-process; lifting it out of ``main.dispatch_event`` is a meaningful
refactor and deferring it lets phase 1 land without touching the existing
webhook code path. Marked as TODO below.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

import httpx

from .. import credentials as credentials_store
from . import cursors, registry, sources
from .types import Event, EventSource, PullResult

log = logging.getLogger("dispatcher.pollers.runtime")

# Loopback ingest. The runtime POSTs into the dispatcher's own /api/event so
# routing, dedupe, and audit all flow through the existing code path.
# TODO(phase-2): extract main.dispatch_event's core and call it in-process.
INGEST_URL = os.environ.get(
    "DISPATCHER_LOOPBACK_URL", "http://127.0.0.1:8911/api/event")
INGEST_TOKEN = os.environ.get("DISPATCHER_INGEST_TOKEN")

BACKOFF_INITIAL_S = 5
BACKOFF_MAX_S = 300

_tasks: dict[str, asyncio.Task[None]] = {}
_stopping = asyncio.Event()


async def start_runtime() -> None:
    """Boot every Event Source we have an adapter for. Idempotent."""
    cursors.init_db()
    _stopping.clear()
    enrolled = sources.load_sources()
    if not enrolled:
        log.info("no event sources enrolled — runtime idle")
        return

    started: list[str] = []
    skipped: list[tuple[str, str]] = []
    for src in enrolled:
        if src.transport == "webhook":
            skipped.append((src.name, "transport=webhook"))
            continue
        adapter = registry.get_adapter(src.system)
        if adapter is None:
            skipped.append((src.name, f"no adapter for system={src.system}"))
            continue
        if src.name in _tasks:
            continue  # already running
        _tasks[src.name] = asyncio.create_task(
            _supervised_tick(src, adapter), name=f"poller:{src.name}",
        )
        started.append(src.name)

    log.info(
        "poller runtime up — started=%s skipped=%s",
        started, skipped,
    )


async def stop_runtime() -> None:
    """Cancel every tick coroutine. Safe to call when nothing is running."""
    _stopping.set()
    for task in list(_tasks.values()):
        task.cancel()
    if _tasks:
        await asyncio.gather(*_tasks.values(), return_exceptions=True)
    _tasks.clear()
    log.info("poller runtime stopped")


async def _supervised_tick(src: EventSource, adapter: Any) -> None:
    """Forever: tick, sleep, tick. Adapter exceptions are logged and survived
    with exponential backoff; the outer loop only exits on cancel.
    """
    backoff = BACKOFF_INITIAL_S
    sleep_s = src.tick_s
    while not _stopping.is_set():
        try:
            sleep_s = await _one_tick(src, adapter)
            backoff = BACKOFF_INITIAL_S  # success resets backoff
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — supervisor swallows everything
            log.exception("source=%s tick failed: %s", src.name, e)
            sleep_s = backoff
            backoff = min(backoff * 2, BACKOFF_MAX_S)
        # Jitter so N sources don't synchronize on the same wall-clock tick.
        await _interruptible_sleep(sleep_s + random.uniform(0, 1.5))


async def _one_tick(src: EventSource, adapter: Any) -> int:
    """Single tick: load cursor + credentials, pull, persist new cursor,
    forward events. Returns the sleep duration for the next tick
    (respecting upstream hints).
    """
    cursor = cursors.get_cursor(src.name)
    creds = credentials_store.get(src.credentials_ref)
    result: PullResult = await adapter.pull(src.scope, cursor, creds)

    # Filter by the Event Source's watch list — if non-empty, only events
    # whose event_type matches (or matches a `prefix.*` wildcard) propagate.
    events = _filter_events(result.events, src.watching)
    forwarded = 0
    for ev in events:
        try:
            await _forward_event(src.name, ev)
            forwarded += 1
        except httpx.HTTPError as e:
            log.warning("source=%s forward failed for event_id=%s: %s",
                        src.name, ev.event_id, e)

    cursors.set_cursor(src.name, result.new_cursor)

    next_tick = src.tick_s
    if result.next_tick_hint_s and result.next_tick_hint_s > next_tick:
        next_tick = result.next_tick_hint_s

    log.debug(
        "source=%s tick complete: pulled=%d forwarded=%d next_tick=%ds",
        src.name, len(result.events), forwarded, next_tick,
    )
    return next_tick


def _filter_events(events: list[Event], watching: list[str]) -> list[Event]:
    """Match event_type against the watch list. Empty list = pass everything.
    Wildcard form ``prefix.*`` matches any event_type starting with ``prefix.``.
    """
    if not watching:
        return events
    exact = {w for w in watching if not w.endswith(".*")}
    prefixes = tuple(w[:-1] for w in watching if w.endswith(".*"))
    out = []
    for ev in events:
        if ev.event_type in exact or ev.event_type.startswith(prefixes):
            out.append(ev)
    return out


async def _forward_event(source_name: str, ev: Event) -> None:
    """POST one event into the dispatcher's own ingest. We attach the
    Event Source name as the ``source`` field so routing rules can match
    by source even though the underlying system might be ``github``.
    """
    if not INGEST_TOKEN:
        raise RuntimeError("DISPATCHER_INGEST_TOKEN not set — cannot forward")
    body = {
        "source": source_name,
        "event_type": ev.event_type,
        "data": {**ev.data, "event_id": ev.event_id, "occurred_at": ev.occurred_at},
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            INGEST_URL,
            headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
            json=body,
        )
        r.raise_for_status()


async def _interruptible_sleep(seconds: float) -> None:
    """Sleep that bails early on shutdown."""
    try:
        await asyncio.wait_for(_stopping.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass

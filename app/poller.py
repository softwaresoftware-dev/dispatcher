"""The poller — poll-first ingestion for the dispatcher.

Poll-first is now the PRIMARY ingestion path; the /api/event webhook is
deprecated. On a NAT'd host there is no public endpoint for GitHub (or anyone)
to call, so the dispatcher reaches out on an interval instead.

On each tick: load Event Source declarations, dispatch each to its system
adapter, route every item newer than that source's cursor through the shared
routing core, and advance the cursor past the newest item it *successfully*
routed (stopping at the first failure so nothing is skipped).

Run as a managed daemon next to the ingress:

    python -m app.poller                    # loop forever (DISPATCHER_POLL_INTERVAL_S, default 60)
    python -m app.poller --once             # a single tick (dev / tests / cron)
    python -m app.poller --once --dry-run   # report what WOULD route, take no action
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from app import core, cursors, event_sources
from app.adapters import get_adapter

log = logging.getLogger("dispatcher-poller")

POLL_INTERVAL_S = int(os.environ.get("DISPATCHER_POLL_INTERVAL_S", "60"))

# Cold-start policy. On the FIRST poll of a source (no cursor yet) the adapter
# returns everything currently in scope — for GitHub PRs that is the full recent
# history. Routing those would spawn an agent per historical item (a stampede of
# pr-prep on long-closed PRs). So by default a first sight only ESTABLISHES the
# watermark at the newest existing item and emits nothing; steady-state polling
# then routes only items that appear afterward. Set DISPATCHER_POLL_BACKFILL=1
# to deliberately replay history on first sight instead.
BACKFILL = os.environ.get("DISPATCHER_POLL_BACKFILL", "").lower() in ("1", "true", "yes")


async def tick(*, dry_run: bool = False) -> dict:
    """Run one poll cycle across all event sources. Returns a summary dict."""
    cursors.init()
    summary: dict = {"sources": 0, "new_events": 0, "routed": 0, "errors": 0, "details": []}

    for src in event_sources.load_all_sources():
        summary["sources"] += 1
        # cursor key namespaced by workspace so two workspaces can watch the same
        # source name without clobbering each other's watermark
        key = f"{src.workspace}:{src.name}" if src.workspace else src.name
        adapter = get_adapter(src.system)
        if adapter is None:
            log.warning("no adapter for system '%s' (source %s)", src.system, src.name)
            summary["details"].append({"source": src.name, "skipped": f"no adapter for {src.system}"})
            continue

        last_seen, last_id, state = cursors.get(key)
        first_sight = last_id is None
        try:
            events, meta = await adapter(src, last_seen, last_id, state)
        except Exception as e:  # noqa: BLE001 — one bad source must not kill the tick
            log.exception("adapter %s failed for source %s: %s", src.system, src.name, e)
            summary["errors"] += 1
            summary["details"].append({"source": src.name, "error": str(e)})
            continue
        newest = (meta or {}).get("newest")
        new_state = (meta or {}).get("state")

        # Cold start: establish the watermark, route nothing (unless BACKFILL).
        # The watermark comes from the NEWEST RAW feed item (meta["newest"]),
        # not from the filtered events — a feed whose recent traffic is all
        # unwatched types must still get a cursor, or the first real watched
        # event would later be swallowed by this very guard.
        if first_sight and not BACKFILL:
            if not dry_run:
                if newest:
                    cursors.advance(key, newest.get("created_at") or "", newest["id"])
                else:
                    # Empty feed — start from the beginning so the first event
                    # that ever appears gets routed.
                    cursors.advance(key, "", "0")
                if new_state:
                    cursors.set_state(key, new_state)
            log.info("source %s: cold start — watermark set to %s, %d historical "
                     "item(s) skipped (set DISPATCHER_POLL_BACKFILL=1 to replay)",
                     src.name, (newest or {}).get("created_at"), len(events))
            summary["details"].append({
                "source": src.name, "system": src.system,
                "cold_start": True,
                "watermark_initialized_at": (newest or {}).get("created_at"),
                "skipped_historical": len(events),
                "routed": 0,
            })
            continue

        summary["new_events"] += len(events)
        routed_here = 0
        all_routed = True
        for ev in events:
            # Route under the source's `system` (e.g. "github") — that is what
            # channels.yaml matches on, not the event-source name.
            res = await core.route_event(src.system, ev["event_type"], ev["data"], workspace=src.workspace, dry_run=dry_run)
            if res.get("ok"):
                routed_here += 1
                summary["routed"] += 1
                if not dry_run:
                    cursors.advance(key, ev["created_at"], ev["id"])
            else:
                all_routed = False
                summary["errors"] += 1
                log.error("route failed for %s %s: %s", src.name, ev["id"], res.get("error"))
                break  # stop advancing — retry this item (and the rest) next tick

        # Only after a FULLY routed response: jump the watermark to the newest
        # raw item (skipping unwatched backlog) and persist the adapter state
        # (the ETag). Persisting state after a partial route would make the
        # next tick's 304 hide the unrouted tail.
        if not dry_run and all_routed:
            if newest:
                cursors.advance(key, newest.get("created_at") or "", newest["id"])
            if new_state:
                cursors.set_state(key, new_state)

        summary["details"].append({
            "source": src.name,
            "system": src.system,
            "new_events": len(events),
            "routed": routed_here,
            "cursor": cursors.get(key)[0],
        })

    return summary


async def run_forever() -> None:
    cursors.init()
    log.info("poller started — interval %ss, sources dir %s",
             POLL_INTERVAL_S, event_sources.DEFAULT_DIR)
    while True:
        try:
            s = await tick()
            log.info("tick: %d sources, %d new, %d routed, %d errors",
                     s["sources"], s["new_events"], s["routed"], s["errors"])
        except Exception as e:  # noqa: BLE001 — never let the loop die
            log.exception("tick crashed: %s", e)
        await asyncio.sleep(POLL_INTERVAL_S)


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    )
    ap = argparse.ArgumentParser(description="dispatcher poll-first ingestion")
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    ap.add_argument("--dry-run", action="store_true", help="resolve routes but take no action")
    args = ap.parse_args(argv)

    if args.once:
        summary = asyncio.run(tick(dry_run=args.dry_run))
        print(json.dumps(summary, indent=2, default=str))
        return 0

    asyncio.run(run_forever())
    return 0


if __name__ == "__main__":
    sys.exit(main())

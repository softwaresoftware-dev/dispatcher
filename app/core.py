"""Routing core for the dispatcher's poll-first ingestion.

`route_event` runs the routing decision tree — dedupe,
static channels.yaml lookup, forward-to-session / spawn-recipe / LLM-fallback,
audit — but awaits spawn inline (no BackgroundTasks), because the poller is not
in an HTTP hot path: it wants the spawn outcome *before* it advances its cursor,
so a failed spawn is retried on the next tick instead of being silently skipped.

It reuses every primitive from app.main (dedupe key, dedupe table, audit log,
forward-to-session, event text) so there is exactly one implementation of each;
only the orchestration is expressed twice, once per ingestion mode. Audit rows
written here use `poll-*` modes so the audit log distinguishes poll-driven
events from webhook-driven ones.

Returns a dict describing the routing decision (never raises for routing
failures — the poller inspects {ok} to decide whether to advance the cursor).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from app import channels, spawn_helper
from app import main as ingress  # reuse dedupe / audit / forward primitives

# Multi-tenant: one dispatcher serves every workspace. When an event carries a
# workspace (derived by the poller from the event-source's owning partition),
# routing reads that workspace's channels.yaml + recipes and the spawned agent
# runs with HOME = that workspace partition.
WORKSPACES_ROOT = os.environ.get("DISPATCHER_WORKSPACES_ROOT", "")


def _ws_paths(workspace: str | None):
    """(channels_file, recipes_dir, home) for a workspace, or (None, None, None)
    to fall back to the module-level defaults when there's no workspace/root."""
    if workspace and WORKSPACES_ROOT:
        part = Path(WORKSPACES_ROOT) / workspace
        base = part / ".mindframe" / "dispatcher"
        return base / "channels.yaml", base / "recipes", str(part)
    return None, None, None


def _payload(source: str, event_type: str | None, data) -> dict:
    """Mirror EventBody.model_dump() so audit payloads match the webhook path."""
    return {"source": source, "event_type": event_type, "data": data}


async def route_event(source: str, event_type: str | None, data, *,
                      workspace: str | None = None, dry_run: bool = False) -> dict:
    """Route a single normalized event. Idempotent within the dedupe window.

    workspace: the partition the event-source belongs to. Routing uses that
    workspace's channels.yaml/recipes and the spawned agent runs in its HOME.

    dry_run: resolve the route and report the target, but forward/spawn nothing
    and write no dedupe entry — used by `poller --once --dry-run` to prove live
    wiring against a real source without taking any action.
    """
    received_at = datetime.now(timezone.utc).isoformat()
    payload = _payload(source, event_type, data)
    dedupe_key = ingress._compute_dedupe_key(source, data)
    ingress._cleanup_dedupe()
    ch_file, rec_dir, ws_home = _ws_paths(workspace)

    def _log(*a):
        return ingress._log_event(*a, workspace=workspace)

    existing = ingress._find_dedupe_match(dedupe_key)
    if existing:
        _log(source, event_type, None, "poll-auto", payload,
             existing["routed_to"], "deduped", None, dedupe_key)
        return {"ok": True, "deduped": True, "routed_to": existing["routed_to"]}

    route = channels.lookup_route(source, event_type, channels_file=ch_file)
    target = route.target if route else None

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "workspace": workspace,
            "dedupe_key": dedupe_key,
            "would_route_to": target or f"session:{ingress.DISPATCHER_SESSION}",
        }

    # session:<name> — forward text to a named mesh session.
    if target and target.startswith("session:"):
        session = target.split(":", 1)[1]
        text = ingress._event_text(source, event_type, data, received_at)
        try:
            await ingress._forward_to_session(session, text)
        except Exception as e:  # noqa: BLE001 — failure is reported, not raised
            _log(source, event_type, None, "poll-session", payload,
                 session, "failed", str(e), dedupe_key)
            return {"ok": False, "error": str(e)}
        eid = _log(source, event_type, None, "poll-session", payload,
                   session, "forwarded", None, dedupe_key)
        ingress._record_dedupe(dedupe_key, eid, session)
        return {"ok": True, "mode": "poll-session", "routed_to": session}

    # spawn:<recipe> — create + spawn an ephemeral agent. Awaited inline.
    if target and target.startswith("spawn:"):
        recipe_id = target.split(":", 1)[1]
        ev_id = dedupe_key.split(":", 1)[1] if ":" in dedupe_key else dedupe_key
        audit_id = _log(source, event_type, None, "poll-spawn", payload,
                        target, "forwarded", None, dedupe_key)
        result = await spawn_helper.spawn_recipe(
            recipe_id=recipe_id,
            payload=data,
            event_id=ev_id,
            brief_overrides=route.brief,
            recipes_dir=rec_dir,
            home=ws_home,
        )
        if result.get("ok"):
            _log(source, event_type, result.get("task_id"), "poll-spawn-result",
                 payload, f"spawn:{recipe_id}", "spawned", None, dedupe_key)
            ingress._record_dedupe(dedupe_key, audit_id, f"spawn:{recipe_id}")
            return {"ok": True, "mode": "poll-spawn", "routed_to": target,
                    "workspace": workspace, "task_id": result.get("task_id")}
        _log(source, event_type, None, "poll-spawn-result", payload,
             f"spawn:{recipe_id}", "spawn-failed", result.get("error"), dedupe_key)
        return {"ok": False, "error": result.get("error")}

    # Unmapped → LLM dispatcher session (existing fallback behavior).
    text = ingress._event_text(source, event_type, data, received_at)
    try:
        await ingress._forward_to_session(ingress.DISPATCHER_SESSION, text)
    except Exception as e:  # noqa: BLE001
        _log(source, event_type, None, "poll-auto", payload,
             ingress.DISPATCHER_SESSION, "failed", str(e), dedupe_key)
        return {"ok": False, "error": str(e)}
    eid = _log(source, event_type, None, "poll-auto", payload,
               ingress.DISPATCHER_SESSION, "forwarded", None, dedupe_key)
    ingress._record_dedupe(dedupe_key, eid, ingress.DISPATCHER_SESSION)
    return {"ok": True, "mode": "poll-auto", "routed_to": ingress.DISPATCHER_SESSION}

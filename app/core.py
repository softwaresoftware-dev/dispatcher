"""Routing core shared by the (deprecated) webhook ingress and the poller.

`route_event` runs the same decision tree as the /api/event handler — dedupe,
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

from datetime import datetime, timezone

from app import channels, spawn_helper
from app import main as ingress  # reuse dedupe / audit / forward primitives


def _payload(source: str, event_type: str | None, data) -> dict:
    """Mirror EventBody.model_dump() so audit payloads match the webhook path."""
    return {"source": source, "event_type": event_type, "data": data}


async def route_event(source: str, event_type: str | None, data, *, dry_run: bool = False) -> dict:
    """Route a single normalized event. Idempotent within the dedupe window.

    dry_run: resolve the route and report the target, but forward/spawn nothing
    and write no dedupe entry — used by `poller --once --dry-run` to prove live
    wiring against a real source without taking any action.
    """
    received_at = datetime.now(timezone.utc).isoformat()
    payload = _payload(source, event_type, data)
    dedupe_key = ingress._compute_dedupe_key(source, data)
    ingress._cleanup_dedupe()

    existing = ingress._find_dedupe_match(dedupe_key)
    if existing:
        ingress._log_event(source, event_type, None, "poll-auto", payload,
                           existing["routed_to"], "deduped", None, dedupe_key)
        return {"ok": True, "deduped": True, "routed_to": existing["routed_to"]}

    route = channels.lookup_route(source, event_type)
    target = route.target if route else None

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
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
            ingress._log_event(source, event_type, None, "poll-session", payload,
                               session, "failed", str(e), dedupe_key)
            return {"ok": False, "error": str(e)}
        eid = ingress._log_event(source, event_type, None, "poll-session", payload,
                                 session, "forwarded", None, dedupe_key)
        ingress._record_dedupe(dedupe_key, eid, session)
        return {"ok": True, "mode": "poll-session", "routed_to": session}

    # spawn:<recipe> — create + spawn an ephemeral agent. Awaited inline.
    if target and target.startswith("spawn:"):
        recipe_id = target.split(":", 1)[1]
        ev_id = dedupe_key.split(":", 1)[1] if ":" in dedupe_key else dedupe_key
        audit_id = ingress._log_event(source, event_type, None, "poll-spawn", payload,
                                      target, "forwarded", None, dedupe_key)
        result = await spawn_helper.spawn_recipe(
            recipe_id=recipe_id,
            payload=data,
            event_id=ev_id,
            brief_overrides=route.brief,
        )
        if result.get("ok"):
            ingress._log_event(source, event_type, result.get("task_id"), "poll-spawn-result",
                               payload, f"spawn:{recipe_id}", "spawned", None, dedupe_key)
            ingress._record_dedupe(dedupe_key, audit_id, f"spawn:{recipe_id}")
            return {"ok": True, "mode": "poll-spawn", "routed_to": target,
                    "task_id": result.get("task_id")}
        ingress._log_event(source, event_type, None, "poll-spawn-result", payload,
                           f"spawn:{recipe_id}", "spawn-failed", result.get("error"), dedupe_key)
        return {"ok": False, "error": result.get("error")}

    # Unmapped → LLM dispatcher session (existing fallback behavior).
    text = ingress._event_text(source, event_type, data, received_at)
    try:
        await ingress._forward_to_session(ingress.DISPATCHER_SESSION, text)
    except Exception as e:  # noqa: BLE001
        ingress._log_event(source, event_type, None, "poll-auto", payload,
                           ingress.DISPATCHER_SESSION, "failed", str(e), dedupe_key)
        return {"ok": False, "error": str(e)}
    eid = ingress._log_event(source, event_type, None, "poll-auto", payload,
                             ingress.DISPATCHER_SESSION, "forwarded", None, dedupe_key)
    ingress._record_dedupe(dedupe_key, eid, ingress.DISPATCHER_SESSION)
    return {"ok": True, "mode": "poll-auto", "routed_to": ingress.DISPATCHER_SESSION}

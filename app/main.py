"""dispatcher-ingress — public webhook receiver, forwards to dispatcher agent via session-bridge."""

import hashlib
import hmac
import json
import logging
import os
import traceback
from datetime import datetime, timezone

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import channels, db, spawn_helper

log = logging.getLogger("dispatcher-ingress")

SESSION_BRIDGE_URL = os.environ.get("SESSION_BRIDGE_URL", "http://127.0.0.1:8910")
DISPATCHER_SESSION = os.environ.get("DISPATCHER_SESSION", "dispatcher")
DEDUPE_WINDOW_MINUTES = int(os.environ.get("DISPATCHER_DEDUPE_WINDOW_MINUTES", "10"))

app = FastAPI(title="dispatcher-ingress")


def _log_internal_failure(request: Request, status: str, error: str) -> None:
    """Write a synthetic events row when a request fails before/around the
    handler's own _log_event. Source is '_internal' so audit queries can
    distinguish handler-level failures from inbound-event failures.

    Best-effort: a failure to write must not mask the original exception, so
    we swallow any DB error here and just log it.
    """
    try:
        with db.get_db() as conn:
            conn.execute(
                """INSERT INTO events (source, event_type, target, mode, payload, routed_to, status, error, dedupe_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("_internal", request.method, request.url.path, "exception",
                 "{}", None, status, error[:4000], None),
            )
    except Exception as e:
        log.exception("failed to write _internal failure row: %s", e)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions. HTTPException is handled separately
    (FastAPI's default) so we don't double-log expected 4xx flows. Anything
    else lands here, gets persisted to the audit DB, and surfaces as 500."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.exception("unhandled exception in %s %s", request.method, request.url.path)
    _log_internal_failure(request, "exception", tb)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "internal error", "type": type(exc).__name__},
    )


def _get_token() -> str:
    return os.environ.get("DISPATCHER_INGEST_TOKEN", "")


class EventBody(BaseModel):
    source: str = Field(..., min_length=1, max_length=64)
    event_type: str | None = None
    data: dict | list | str | int | float | bool | None = None


class DirectBody(BaseModel):
    text: str = Field(..., min_length=1)
    source: str = Field(default="direct", max_length=64)


def _check_auth(authorization: str | None):
    expected = _get_token()
    if not expected:
        raise HTTPException(500, "DISPATCHER_INGEST_TOKEN not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    if not hmac.compare_digest(authorization[7:], expected):
        raise HTTPException(403, "Invalid token")


def _compute_dedupe_key(source: str, data) -> str:
    """Stable key for idempotency: source + explicit event_id, or payload hash fallback.

    Sentry, Linear, GitHub all send a stable id on each delivery; we prefer that.
    For sources without an id field, we hash the payload so accidental duplicate
    deliveries of the exact same body still dedupe.
    """
    event_id = None
    if isinstance(data, dict):
        event_id = data.get("event_id") or data.get("id")
    if event_id is None:
        blob = json.dumps(data, sort_keys=True, default=str).encode()
        event_id = "h:" + hashlib.sha256(blob).hexdigest()[:16]
    return f"{source}:{event_id}"


def _find_dedupe_match(dedupe_key: str) -> dict | None:
    """Look up a confirmed-completed dedupe entry within the window.

    Reads from the `dedupe` table — separate from the audit log so the
    distinction between "we attempted this" and "this work completed" is
    real. Only confirmed-success paths insert here, so a failed forward
    or failed spawn won't suppress the next retry.
    """
    with db.get_db() as conn:
        row = conn.execute(
            """SELECT original_event_id, routed_to FROM dedupe
               WHERE dedupe_key = ?
                 AND completed_at > datetime('now', ?)""",
            (dedupe_key, f"-{DEDUPE_WINDOW_MINUTES} minutes"),
        ).fetchone()
    return dict(row) if row else None


def _record_dedupe(dedupe_key: str, original_event_id: int, routed_to: str | None) -> None:
    """Mark this dedupe_key as completed.

    INSERT OR IGNORE so two webhooks that both raced past the dedupe
    lookup (the SELECT-then-INSERT race window) can both finish without
    a constraint error — the loser's row is dropped, the winner is kept.
    """
    with db.get_db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO dedupe
                 (dedupe_key, completed_at, original_event_id, routed_to)
               VALUES (?, datetime('now'), ?, ?)""",
            (dedupe_key, original_event_id, routed_to),
        )


def _cleanup_dedupe() -> None:
    """Drop dedupe rows older than the dedupe window.

    Called once per dispatch_event so retention is amortized across
    traffic — no separate timer needed. Rows are tiny and the index on
    completed_at keeps this cheap.
    """
    with db.get_db() as conn:
        conn.execute(
            "DELETE FROM dedupe WHERE completed_at < datetime('now', ?)",
            (f"-{DEDUPE_WINDOW_MINUTES} minutes",),
        )


def _log_event(source: str, event_type: str | None, target: str | None, mode: str,
               payload: dict, routed_to: str | None, status: str, error: str | None,
               dedupe_key: str | None = None) -> int:
    """Insert an audit row, return its id so the caller can correlate it
    with a dedupe entry on the success path."""
    with db.get_db() as conn:
        cur = conn.execute(
            """INSERT INTO events (source, event_type, target, mode, payload, routed_to, status, error, dedupe_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, event_type, target, mode, json.dumps(payload), routed_to, status, error, dedupe_key),
        )
        return cur.lastrowid


async def _forward_to_session(session: str, text: str) -> dict:
    url = f"{SESSION_BRIDGE_URL}/sessions/{session}/message"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json={"text": text})
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, f"session-bridge: {resp.text}")
        return resp.json()


@app.on_event("startup")
def startup():
    db.init_db()


@app.get("/api/health")
def health():
    return {"ok": True}


def _event_text(source: str, event_type: str | None, data, received_at: str) -> str:
    """Format the human-readable event text used in mesh messages."""
    return (
        f"EVENT from `{source}`"
        + (f" (type: `{event_type}`)" if event_type else "")
        + f" received at {received_at}\n\n"
        + "```json\n"
        + json.dumps(data, indent=2, default=str)
        + "\n```"
    )


@app.post("/api/event")
async def dispatch_event(
    body: EventBody,
    background: BackgroundTasks,
    authorization: str | None = Header(None),
):
    """Ingest. First consults channels.yaml for a static (source, event_type) →
    target route; if matched, forwards or spawns deterministically (no LLM).
    If unmapped, falls through to the dispatcher Claude session (LLM-routed).

    Idempotent within DISPATCHER_DEDUPE_WINDOW_MINUTES (default 10): a duplicate
    event (same source + event_id) that already forwarded recently is short-
    circuited and returns the original routing decision."""
    _check_auth(authorization)

    payload_dict = body.model_dump()
    received_at = datetime.now(timezone.utc).isoformat()
    dedupe_key = _compute_dedupe_key(body.source, body.data)

    # Amortized retention — every inbound request also drops expired
    # dedupe entries. No background timer required.
    _cleanup_dedupe()

    existing = _find_dedupe_match(dedupe_key)
    if existing:
        _log_event(body.source, body.event_type, None, "auto", payload_dict,
                   existing["routed_to"], "deduped", None, dedupe_key)
        return {
            "ok": True,
            "deduped": True,
            "original_event_id": existing["original_event_id"],
            "routed_to": existing["routed_to"],
        }

    # Static route lookup. Empty / missing channels.yaml falls through.
    route = channels.lookup_route(body.source, body.event_type)
    target = route.target if route else None
    if target and target.startswith("session:"):
        session_name = target.split(":", 1)[1]
        text = _event_text(body.source, body.event_type, body.data, received_at)
        try:
            result = await _forward_to_session(session_name, text)
            event_id = _log_event(body.source, body.event_type, None, "static-session", payload_dict,
                                   session_name, "forwarded", None, dedupe_key)
            _record_dedupe(dedupe_key, event_id, session_name)
            return {"ok": True, "mode": "static-session", "routed_to": session_name, "bridge": result}
        except HTTPException as e:
            _log_event(body.source, body.event_type, None, "static-session", payload_dict,
                       session_name, "failed", str(e.detail), dedupe_key)
            raise
        except Exception as e:
            _log_event(body.source, body.event_type, None, "static-session", payload_dict,
                       session_name, "failed", str(e), dedupe_key)
            raise HTTPException(502, f"forward failed: {e}")

    if target and target.startswith("spawn:"):
        recipe_id = target.split(":", 1)[1]
        # The dedupe_key event_id is "<source>:<id>"; the recipe wants just the id part.
        event_id = dedupe_key.split(":", 1)[1] if ":" in dedupe_key else dedupe_key
        # Spawn takes ~16s; fire it from a background task so the webhook caller
        # gets a fast 200. Audit the dispatch decision now; the spawn outcome is
        # logged separately by the helper via _log_spawn_result, which also
        # records the dedupe entry on success.
        audit_id = _log_event(body.source, body.event_type, None, "static-spawn", payload_dict,
                              target, "forwarded", None, dedupe_key)
        background.add_task(_spawn_and_log, recipe_id, body, event_id, payload_dict,
                            dedupe_key, audit_id, route.brief)
        return {"ok": True, "mode": "static-spawn", "routed_to": target}

    # Unmapped → existing LLM-dispatcher fallback.
    text = _event_text(body.source, body.event_type, body.data, received_at)
    try:
        result = await _forward_to_session(DISPATCHER_SESSION, text)
        event_id = _log_event(body.source, body.event_type, None, "auto", payload_dict,
                              DISPATCHER_SESSION, "forwarded", None, dedupe_key)
        _record_dedupe(dedupe_key, event_id, DISPATCHER_SESSION)
        return {"ok": True, "routed_to": DISPATCHER_SESSION, "bridge": result}
    except HTTPException as e:
        _log_event(body.source, body.event_type, None, "auto", payload_dict,
                   DISPATCHER_SESSION, "failed", str(e.detail), dedupe_key)
        raise
    except Exception as e:
        _log_event(body.source, body.event_type, None, "auto", payload_dict,
                   DISPATCHER_SESSION, "failed", str(e), dedupe_key)
        raise HTTPException(502, f"forward failed: {e}")


async def _spawn_and_log(recipe_id: str, body: EventBody, event_id: str,
                         payload_dict: dict, dedupe_key: str, audit_id: int,
                         brief_overrides: dict | None = None) -> None:
    """Background helper: invoke spawn_recipe and log the outcome.

    Records the dedupe entry only on confirmed spawn success — a failed
    spawn no longer suppresses the next retry. `audit_id` is the row id
    of the synchronous "forwarded" audit entry, kept as the
    `original_event_id` reference in the dedupe table so duplicates can
    cite the original dispatch. `brief_overrides` is the channels.yaml
    route's `brief:` block, used to compose the recipe brief.
    """
    result = await spawn_helper.spawn_recipe(
        recipe_id=recipe_id,
        payload=body.data,
        event_id=event_id,
        brief_overrides=brief_overrides,
    )
    if result.get("ok"):
        _log_event(body.source, body.event_type, result.get("task_id"), "static-spawn-result",
                   payload_dict, f"spawn:{recipe_id}", "spawned", None, dedupe_key)
        _record_dedupe(dedupe_key, audit_id, f"spawn:{recipe_id}")
    else:
        _log_event(body.source, body.event_type, None, "static-spawn-result",
                   payload_dict, f"spawn:{recipe_id}", "spawn-failed",
                   result.get("error"), dedupe_key)


@app.post("/api/direct/{session}")
async def direct_send(session: str, body: DirectBody, authorization: str | None = Header(None)):
    """Explicit routing — forward directly to a named session. No LLM in the loop, no dedupe."""
    _check_auth(authorization)

    payload_dict = body.model_dump()
    try:
        result = await _forward_to_session(session, body.text)
        _log_event(body.source, None, session, "direct", payload_dict,
                   session, "forwarded", None)
        return {"ok": True, "routed_to": session, "bridge": result}
    except HTTPException as e:
        _log_event(body.source, None, session, "direct", payload_dict,
                   session, "failed", str(e.detail))
        raise
    except Exception as e:
        _log_event(body.source, None, session, "direct", payload_dict,
                   session, "failed", str(e))
        raise HTTPException(502, f"forward failed: {e}")


@app.get("/api/events")
def list_events(
    authorization: str | None = Header(None),
    limit: int = 50,
    status: str | None = None,
    source: str | None = None,
    since: str | None = None,
):
    """Audit log — requires bearer auth. Most recent first.

    Filters:
      status — exact match (e.g. 'failed', 'exception', 'forwarded')
      source — exact match (e.g. 'sentry', '_internal')
      since  — ISO8601 timestamp; only events created at or after this time

    Combined as AND. Useful for taskboard panels: '?status=failed&since=...'
    surfaces just the failures since the dashboard last refreshed.
    """
    _check_auth(authorization)

    where = []
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    if source:
        where.append("source = ?")
        params.append(source)
    if since:
        where.append("created_at >= ?")
        params.append(since)

    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with db.get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/events/summary")
def events_summary(authorization: str | None = Header(None), since: str | None = None):
    """Aggregate counts by status. For dashboards that want 'failed today: 4'
    without scanning the full event list."""
    _check_auth(authorization)
    where = ""
    params: list = []
    if since:
        where = " WHERE created_at >= ?"
        params = [since]
    with db.get_db() as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) as n FROM events{where} GROUP BY status",
            params,
        ).fetchall()
    return {row["status"]: row["n"] for row in rows}

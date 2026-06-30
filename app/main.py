"""dispatcher-ingress — audit/forward service for the dispatcher.

Ingestion is poll-first: app/poller.py reads event-sources/*.yaml and routes
through the shared core in app/core.py. (The old /api/event webhook was removed
— on a NAT'd host nothing could reach it, and it had no workspace context.)
This module owns the dedupe/audit/forward primitives the core reuses, plus the
HTTP surface that is NOT ingestion: /api/direct (explicit forward to a named
session), /api/events (audit log), and /api/health."""

import hashlib
import hmac
import json
import logging
import os
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import channels, db

log = logging.getLogger("dispatcher-ingress")

SESSION_BRIDGE_URL = os.environ.get("SESSION_BRIDGE_URL", "http://127.0.0.1:8910")
DISPATCHER_SESSION = os.environ.get("DISPATCHER_SESSION", "dispatcher")
DEDUPE_WINDOW_MINUTES = int(os.environ.get("DISPATCHER_DEDUPE_WINDOW_MINUTES", "10"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    db.init_db()
    yield


app = FastAPI(title="dispatcher-ingress", lifespan=lifespan)


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
    """Resolve the dispatcher's bearer token.

    Resolution order:
    1. DISPATCHER_INGEST_TOKEN env var (direct value) — wins if set
    2. DISPATCHER_INGEST_TOKEN_FILE env var (path to a file containing the
       token) — for installs that drop the token in ~/.mindframe/secrets/
       and need the file-handoff workflow (see mindframe install.txt PHASE 7.6)

    Read fresh on every call; the file may be rotated by the operator without
    a daemon restart.
    """
    direct = os.environ.get("DISPATCHER_INGEST_TOKEN", "").strip()
    if direct:
        return direct
    file_path = os.environ.get("DISPATCHER_INGEST_TOKEN_FILE", "").strip()
    if file_path:
        try:
            with open(file_path) as f:
                return f.read().strip()
        except OSError:
            pass
    return ""


class DirectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1)
    source: str = Field(default="direct", max_length=64)


def _check_auth(authorization: str | None):
    expected = _get_token()
    if not expected:
        raise HTTPException(
            500,
            "no bearer configured — set DISPATCHER_INGEST_TOKEN env var, "
            "or DISPATCHER_INGEST_TOKEN_FILE pointing at a readable file",
        )
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

    Called once per routed event so retention is amortized across
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
               dedupe_key: str | None = None, workspace: str | None = None) -> int:
    """Insert an audit row, return its id so the caller can correlate it
    with a dedupe entry on the success path."""
    with db.get_db() as conn:
        cur = conn.execute(
            """INSERT INTO events (source, event_type, target, mode, payload, routed_to, status, error, dedupe_key, workspace)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, event_type, target, mode, json.dumps(payload), routed_to, status, error, dedupe_key, workspace),
        )
        return cur.lastrowid


async def _forward_to_session(session: str, text: str) -> dict:
    url = f"{SESSION_BRIDGE_URL}/sessions/{session}/message"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, json={"text": text})
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, f"session-bridge: {resp.text}")
        return resp.json()


def _configure_logging() -> None:
    """Make our log records visible under systemd / launchd.

    Python's logging library is silent by default — a fresh root logger has
    no handler, so `log.info(...)` calls drop on the floor. Uvicorn configures
    its OWN loggers (uvicorn, uvicorn.error, uvicorn.access) but never touches
    root, so our `dispatcher.*` namespace stays invisible until we wire it.
    basicConfig is a no-op if root already has a handler, so calling it
    repeatedly is safe.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    )


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

    Combined as AND. Useful for dashboard panels: '?status=failed&since=...'
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

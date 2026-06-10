"""Per-event-source poll cursors — the watermark that makes polling stateful.

The poller is at-least-once: on each tick it asks an adapter for items newer
than the stored watermark, routes them, and advances the watermark past the
newest item it *successfully* routed. Restart-safe — a fresh process resumes
from the last persisted watermark and never re-emits already-seen items. The
ingress dedupe table is the second line of defence for the boundary overlap.

Lives in its own SQLite file (`~/.dispatcher/cursors.db`) so it can be reset
(re-emit everything) or inspected independently of the audit log. Env
overrides mirror db.py:
    DISPATCHER_CURSORS_DB=full/path/to/cursors.db   (highest)
    DISPATCHER_DATA_DIR=/path/to/dir                (cursors.db inside)
    default: ~/.dispatcher/cursors.db
"""

import os
import sqlite3
from contextlib import contextmanager

DATA_DIR = os.environ.get("DISPATCHER_DATA_DIR", os.path.expanduser("~/.dispatcher"))
DB_PATH = os.environ.get("DISPATCHER_CURSORS_DB", os.path.join(DATA_DIR, "cursors.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (
    source        TEXT PRIMARY KEY,
    last_seen     TEXT,        -- ISO8601 watermark: newest routed item's created_at
    last_event_id TEXT,        -- the id at the watermark, for same-second tie-breaking
    adapter_state TEXT,        -- opaque per-adapter string (GitHub: the ETag)
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def _db(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(path=None) -> None:
    """Create the cursors table if absent, reconciling legacy schema drift.

    An earlier poll-first scaffolding shipped a `cursors(source_name, cursor,
    updated_at)` table — a single opaque cursor string per source, with no
    watermark/tie-break split. This module needs `last_seen` + `last_event_id`.
    `CREATE TABLE IF NOT EXISTS` would silently leave the old shape in place
    (the bug that surfaced on first live run), so we detect the drift here:

      - new/correct schema (has `last_seen`)  -> no-op
      - legacy schema, table EMPTY             -> drop + recreate (no data lost)
      - legacy schema, table NON-EMPTY         -> refuse, with a clear error;
        the operator migrates or `reset()`s deliberately rather than us
        silently dropping real cursor state.

    Idempotent.
    """
    with _db(path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(cursors)")}
        if "last_seen" in cols:
            if "adapter_state" not in cols:
                # Additive drift from the first poll-first cut — safe to ALTER.
                conn.execute("ALTER TABLE cursors ADD COLUMN adapter_state TEXT")
            return
        if cols:  # a cursors table exists but with the legacy shape
            count = conn.execute("SELECT COUNT(*) FROM cursors").fetchone()[0]
            if count:
                raise RuntimeError(
                    "cursors table has a legacy schema "
                    f"({sorted(cols)}) AND {count} row(s); refusing to drop live "
                    "cursor state. Migrate manually or call cursors.reset() first."
                )
            conn.execute("DROP TABLE cursors")
            conn.executescript(SCHEMA)


def get(source: str, path=None) -> tuple[str | None, str | None, str | None]:
    """Return (last_seen, last_event_id, adapter_state) for a source, or
    (None, None, None) if unseen."""
    with _db(path) as conn:
        row = conn.execute(
            "SELECT last_seen, last_event_id, adapter_state FROM cursors WHERE source = ?",
            (source,),
        ).fetchone()
    if row is None:
        return (None, None, None)
    return (row["last_seen"], row["last_event_id"], row["adapter_state"])


def advance(source: str, last_seen: str, last_event_id: str | None, path=None) -> None:
    """Move the watermark forward. Upsert — first sighting inserts, later ones update."""
    with _db(path) as conn:
        conn.execute(
            """INSERT INTO cursors (source, last_seen, last_event_id, updated_at)
                 VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(source) DO UPDATE SET
                 last_seen     = excluded.last_seen,
                 last_event_id = excluded.last_event_id,
                 updated_at    = datetime('now')""",
            (source, last_seen, last_event_id),
        )


def set_state(source: str, adapter_state: str | None, path=None) -> None:
    """Persist the adapter's opaque state (e.g. the GitHub ETag) for a source.

    Kept separate from advance(): the watermark moves per ROUTED event, while
    adapter state must only be persisted once a whole response has been fully
    routed — otherwise a 304 on the next tick would hide the unrouted tail.
    Upserts so state survives even for a source that has never routed.
    """
    with _db(path) as conn:
        conn.execute(
            """INSERT INTO cursors (source, adapter_state, updated_at)
                 VALUES (?, ?, datetime('now'))
               ON CONFLICT(source) DO UPDATE SET
                 adapter_state = excluded.adapter_state,
                 updated_at    = datetime('now')""",
            (source, adapter_state),
        )


def reset(source: str | None = None, path=None) -> None:
    """Clear one source's cursor (or all) so the next poll re-emits from scratch."""
    with _db(path) as conn:
        if source is None:
            conn.execute("DELETE FROM cursors")
        else:
            conn.execute("DELETE FROM cursors WHERE source = ?", (source,))

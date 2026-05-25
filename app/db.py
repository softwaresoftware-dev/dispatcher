"""SQLite audit log for dispatched events."""

import os
import sqlite3
from contextlib import contextmanager

# Allow tests / non-default deployments to override. Env precedence:
#   DISPATCHER_DB_PATH=full/path/to/events.db      (highest)
#   DISPATCHER_DATA_DIR=/path/to/dir               (events.db inside)
#   default: ~/.dispatcher/events.db
DATA_DIR = os.environ.get(
    "DISPATCHER_DATA_DIR", os.path.expanduser("~/.dispatcher"))
DB_PATH = os.environ.get(
    "DISPATCHER_DB_PATH", os.path.join(DATA_DIR, "events.db"))

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    event_type TEXT,
    target TEXT,
    mode TEXT NOT NULL,
    payload TEXT NOT NULL,
    routed_to TEXT,
    status TEXT NOT NULL,
    error TEXT,
    dedupe_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);

-- Dedupe state, separate from the audit log. A row here means
-- "work for this dedupe_key was confirmed complete" — only success paths
-- insert. The events table can grow / be purged independently without
-- changing dedupe correctness. UNIQUE(dedupe_key) enforced via PRIMARY
-- KEY so concurrent INSERT OR IGNORE collapses to one row atomically.
CREATE TABLE IF NOT EXISTS dedupe (
    dedupe_key TEXT PRIMARY KEY,
    completed_at TEXT NOT NULL DEFAULT (datetime('now')),
    original_event_id INTEGER NOT NULL,
    routed_to TEXT
);

CREATE INDEX IF NOT EXISTS idx_dedupe_completed ON dedupe(completed_at);
"""


def init_db(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with get_db(path) as conn:
        conn.executescript(BASE_SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        if "dedupe_key" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN dedupe_key TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_dedupe ON events(dedupe_key, created_at)"
        )


@contextmanager
def get_db(path=None):
    path = path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

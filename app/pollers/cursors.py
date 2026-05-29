"""Per-source cursor store.

Cursors are opaque blobs from the runtime's perspective — the adapter owns
the shape. Lives in its own SQLite file (``cursors.db``) sibling to the
audit log so cursor reads don't contend with event ingest writes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any

DATA_DIR = os.environ.get(
    "DISPATCHER_DATA_DIR", os.path.expanduser("~/.dispatcher"))
DB_PATH = os.environ.get(
    "DISPATCHER_CURSORS_DB", os.path.join(DATA_DIR, "cursors.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (
    source_name TEXT PRIMARY KEY,
    cursor TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


def get_cursor(source_name: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute(
            "SELECT cursor FROM cursors WHERE source_name = ?", (source_name,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["cursor"])
    except json.JSONDecodeError:
        return None


def set_cursor(source_name: str, cursor: dict[str, Any]) -> None:
    body = json.dumps(cursor, sort_keys=True, default=str)
    with _conn() as c:
        c.execute(
            "INSERT INTO cursors (source_name, cursor) VALUES (?, ?)"
            " ON CONFLICT(source_name) DO UPDATE SET "
            "   cursor=excluded.cursor, updated_at=datetime('now')",
            (source_name, body),
        )


def delete_cursor(source_name: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM cursors WHERE source_name = ?", (source_name,))

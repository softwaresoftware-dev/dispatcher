"""Tests for the poll cursor store — the watermark that makes polling stateful."""


def _fresh_cursors(tmp_path, monkeypatch):
    """Re-point cursors at a temp file."""
    import app.cursors as cursors
    monkeypatch.setattr(cursors, "DB_PATH", str(tmp_path / "cursors.db"))
    return cursors


def test_unseen_source_returns_none(tmp_path, monkeypatch):
    cursors = _fresh_cursors(tmp_path, monkeypatch)
    cursors.init()
    assert cursors.get("never-polled") == (None, None, None)


def test_advance_then_get_roundtrips(tmp_path, monkeypatch):
    cursors = _fresh_cursors(tmp_path, monkeypatch)
    cursors.advance("github-prs", "2026-06-09T12:00:00+00:00", "id-42")
    assert cursors.get("github-prs") == ("2026-06-09T12:00:00+00:00", "id-42", None)


def test_advance_is_upsert(tmp_path, monkeypatch):
    cursors = _fresh_cursors(tmp_path, monkeypatch)
    cursors.advance("s", "2026-06-09T12:00:00+00:00", "a")
    cursors.advance("s", "2026-06-09T13:00:00+00:00", "b")
    assert cursors.get("s") == ("2026-06-09T13:00:00+00:00", "b", None)


def test_set_state_roundtrips_and_survives_advance(tmp_path, monkeypatch):
    cursors = _fresh_cursors(tmp_path, monkeypatch)
    cursors.advance("s", "t1", "1")
    cursors.set_state("s", 'W/"etag-1"')
    assert cursors.get("s") == ("t1", "1", 'W/"etag-1"')
    # advance() moves the watermark without clobbering the state.
    cursors.advance("s", "t2", "2")
    assert cursors.get("s") == ("t2", "2", 'W/"etag-1"')


def test_set_state_upserts_for_unseen_source(tmp_path, monkeypatch):
    cursors = _fresh_cursors(tmp_path, monkeypatch)
    cursors.set_state("brand-new", "etag-x")
    assert cursors.get("brand-new") == (None, None, "etag-x")


def test_state_survives_reopen(tmp_path, monkeypatch):
    """Restart-safety: a brand-new connection sees the persisted watermark."""
    cursors = _fresh_cursors(tmp_path, monkeypatch)
    cursors.advance("s", "2026-06-09T12:00:00+00:00", "a")
    assert cursors.get("s") == ("2026-06-09T12:00:00+00:00", "a", None)


def test_init_adds_adapter_state_column_to_first_cut_schema(tmp_path, monkeypatch):
    """The first poll-first cut shipped cursors without adapter_state; init()
    must ALTER it in without losing the stored watermark."""
    import sqlite3
    cursors = _fresh_cursors(tmp_path, monkeypatch)
    conn = sqlite3.connect(cursors.DB_PATH)
    conn.executescript("""
        CREATE TABLE cursors (
            source        TEXT PRIMARY KEY,
            last_seen     TEXT,
            last_event_id TEXT,
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO cursors (source, last_seen, last_event_id)
        VALUES ('prs', '2026-06-06T00:31:32Z', '42');
    """)
    conn.commit()
    conn.close()

    cursors.init()
    assert cursors.get("prs") == ("2026-06-06T00:31:32Z", "42", None)
    cursors.set_state("prs", "etag-1")
    assert cursors.get("prs")[2] == "etag-1"


def test_init_refuses_to_drop_populated_legacy_table(tmp_path, monkeypatch):
    import sqlite3
    import pytest
    cursors = _fresh_cursors(tmp_path, monkeypatch)
    conn = sqlite3.connect(cursors.DB_PATH)
    conn.executescript("""
        CREATE TABLE cursors (source_name TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT);
        INSERT INTO cursors (source_name, cursor) VALUES ('prs', 'opaque');
    """)
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="legacy schema"):
        cursors.init()


def test_reset_clears_one_source(tmp_path, monkeypatch):
    cursors = _fresh_cursors(tmp_path, monkeypatch)
    cursors.advance("a", "t1", "1")
    cursors.advance("b", "t2", "2")
    cursors.reset("a")
    assert cursors.get("a") == (None, None, None)
    assert cursors.get("b") == ("t2", "2", None)

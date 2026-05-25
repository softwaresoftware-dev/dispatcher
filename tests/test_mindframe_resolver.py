"""Tests for _resolve_mindframe_spawn_cli — the discovery path that replaced
the hardcoded dev path. Each test points $HOME at a tmpdir to avoid touching
the developer's real plugin cache."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.spawn_helper import _resolve_mindframe_spawn_cli


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to a tmpdir on every platform.

    Path.home() reads HOME on POSIX and USERPROFILE on Windows, so we have
    to set both for the same fixture body to work cross-platform."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MINDFRAME_SPAWN_CLI", raising=False)
    return tmp_path


def _seed_cache_version(home: Path, marketplace: str, version: str) -> Path:
    """Place a valid-looking mindframe install in the cache and return the
    expected spawn.py path."""
    d = home / ".claude" / "plugins" / "cache" / marketplace / "mindframe" / version / "lib"
    d.mkdir(parents=True)
    cli = d / "spawn.py"
    cli.write_text("# stub\n")
    return cli


def test_env_override_when_file_exists(tmp_path, monkeypatch):
    cli = tmp_path / "custom-spawn.py"
    cli.write_text("# stub")
    monkeypatch.setenv("MINDFRAME_SPAWN_CLI", str(cli))
    assert _resolve_mindframe_spawn_cli() == str(cli)


def test_env_override_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFRAME_SPAWN_CLI", str(tmp_path / "nope.py"))
    # Don't fall back to the cache when the operator explicitly pointed at a
    # path that doesn't exist — that's a config error worth surfacing.
    assert _resolve_mindframe_spawn_cli() is None


def test_cache_lookup_single_version(fake_home):
    expected = _seed_cache_version(fake_home, "softwaresoftware-plugins", "0.4.0")
    assert _resolve_mindframe_spawn_cli() == str(expected)


def test_cache_lookup_picks_highest_version(fake_home):
    _seed_cache_version(fake_home, "softwaresoftware-plugins", "0.3.0")
    high = _seed_cache_version(fake_home, "softwaresoftware-plugins", "0.4.0")
    _seed_cache_version(fake_home, "softwaresoftware-plugins", "0.2.0")
    assert _resolve_mindframe_spawn_cli() == str(high)


def test_cache_lookup_skips_versions_without_spawn_cli(fake_home):
    # A version directory exists but has no lib/spawn.py (e.g. an older
    # release before the spawn CLI was added).
    old = fake_home / ".claude" / "plugins" / "cache" / "softwaresoftware-plugins" / "mindframe" / "0.2.0"
    old.mkdir(parents=True)
    (old / "README.md").write_text("# 0.2.0 — no spawn.py here\n")
    new = _seed_cache_version(fake_home, "softwaresoftware-plugins", "0.4.0")
    assert _resolve_mindframe_spawn_cli() == str(new)


def test_no_env_no_cache_returns_none(fake_home):
    assert _resolve_mindframe_spawn_cli() is None


def test_env_override_wins_over_cache(fake_home, monkeypatch, tmp_path):
    _seed_cache_version(fake_home, "softwaresoftware-plugins", "0.4.0")
    explicit = tmp_path / "explicit.py"
    explicit.write_text("# stub")
    monkeypatch.setenv("MINDFRAME_SPAWN_CLI", str(explicit))
    assert _resolve_mindframe_spawn_cli() == str(explicit)

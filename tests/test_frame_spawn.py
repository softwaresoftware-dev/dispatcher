"""Tests for the frame: recipe block — dispatcher's mindframe integration.

These tests actually invoke the real mindframe spawn CLI (no mocks). The CLI
is fast (~50ms) and has its own unit tests; here we just want to confirm
dispatcher's wire to it works end-to-end."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.spawn_helper import _create_mindframe


PLUGINS_ROOT = Path(__file__).resolve().parents[3]  # tests → dispatcher → providers → plugins
MINDFRAME_SPAWN_CLI = (
    PLUGINS_ROOT / "frameworks" / "mindframe" / "lib" / "spawn.py"
)


@pytest.fixture
def fake_frames_root(tmp_path, monkeypatch):
    """Redirect mindframe's frames root for the test. The CLI honors
    $MINDFRAME_FRAMES_ROOT so we can keep the test hermetic."""
    root = tmp_path / ".mindframe" / "frames"
    root.mkdir(parents=True)
    monkeypatch.setenv("MINDFRAME_FRAMES_ROOT", str(root))
    return root


@pytest.fixture
def has_mindframe_cli():
    if not MINDFRAME_SPAWN_CLI.exists():
        pytest.skip(f"mindframe spawn CLI not found at {MINDFRAME_SPAWN_CLI}")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio._get_running_loop() else asyncio.run(coro)


async def _call(frame_block, brief_overrides=None, optional_keys=None):
    return await _create_mindframe(
        frame_block=frame_block,
        brief_overrides=brief_overrides or {},
        event_id="evt-test-1",
        task_id="frame-test-task",
        optional_keys=optional_keys or set(),
        mindframe_spawn_cli=str(MINDFRAME_SPAWN_CLI),
    )


def test_minimal_frame_block_creates_frame(fake_frames_root, has_mindframe_cli):
    res = asyncio.run(_call({"title": "Hello mindframe"}))
    assert res["ok"], res
    assert "mindframe_id" in res
    assert Path(res["frame_dir"]).is_dir()
    # Frame directory should be under our fake root.
    assert str(fake_frames_root) in res["frame_dir"]
    # Seed block written (default).
    blocks_path = Path(res["frame_dir"]) / "blocks.jsonl"
    assert blocks_path.is_file()
    assert len(blocks_path.read_text().splitlines()) == 1


def test_frame_block_with_seed_block(fake_frames_root, has_mindframe_cli):
    seed = {"type": "summary", "tone": "warn", "title": "Investigating", "body": "..."}
    res = asyncio.run(_call({"title": "OOMKilled", "seed_block": seed}))
    assert res["ok"], res
    block = json.loads(Path(res["frame_dir"]).joinpath("blocks.jsonl").read_text().splitlines()[0])
    assert block["tone"] == "warn"
    assert block["title"] == "Investigating"


def test_frame_block_with_template_substitution(fake_frames_root, has_mindframe_cli):
    """Frame title goes through the same {{placeholder}} composer as brief.json."""
    frame = {
        "title": "OOM in {{service}}",
        "seed_block": {
            "type": "summary", "tone": "info",
            "title": "Investigating {{service}}",
            "body": "First-pass triage on {{service}}.",
        },
    }
    res = asyncio.run(_call(frame, brief_overrides={"service": "payments-api"}))
    assert res["ok"], res
    meta = json.loads(Path(res["frame_dir"]).joinpath("meta.json").read_text())
    assert meta["title"] == "OOM in payments-api"
    block = json.loads(Path(res["frame_dir"]).joinpath("blocks.jsonl").read_text().splitlines()[0])
    assert block["title"] == "Investigating payments-api"
    assert block["body"] == "First-pass triage on payments-api."


def test_frame_block_missing_required_placeholder_errors(fake_frames_root, has_mindframe_cli):
    frame = {"title": "OOM in {{service}}"}
    res = asyncio.run(_call(frame, brief_overrides={}))
    assert not res["ok"]
    assert "service" in res["error"]


def test_frame_block_invalid_seed_block_errors(fake_frames_root, has_mindframe_cli):
    frame = {"title": "x", "seed_block": {"type": "not-a-real-block"}}
    res = asyncio.run(_call(frame))
    assert not res["ok"]
    assert "unknown block type" in res["error"]


def test_frame_block_records_spawned_by_dispatcher_event(fake_frames_root, has_mindframe_cli):
    res = asyncio.run(_call({"title": "Spawned via dispatcher"}))
    assert res["ok"], res
    meta = json.loads(Path(res["frame_dir"]).joinpath("meta.json").read_text())
    assert meta["spawned_by"]["kind"] == "dispatcher-event"
    assert meta["spawned_by"]["event_id"] == "evt-test-1"
    assert meta["spawned_by"]["recipe"] == "frame-test-task"


def test_frame_block_with_tags(fake_frames_root, has_mindframe_cli):
    res = asyncio.run(_call({"title": "Tagged", "tags": ["incident", "p1"]}))
    assert res["ok"], res
    meta = json.loads(Path(res["frame_dir"]).joinpath("meta.json").read_text())
    assert meta["tags"] == ["incident", "p1"]

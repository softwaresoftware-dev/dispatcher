"""Tests for static-spawn brief composition.

On the static path there is no LLM dispatcher to compose a brief, so the
channels.yaml route's `brief:` block fills the recipe template's
{{placeholders}}. A required placeholder with no override is an error —
the dispatcher must never spawn an agent missing its operating context.
"""

import asyncio
import json

from app import spawn_helper
from app.spawn_helper import _compose_brief


def _compose(brief, overrides, optional=()):
    return _compose_brief(
        brief,
        overrides,
        event_id="evt-1",
        task_id="calendar-reader-evt-1",
        optional_keys=set(optional),
    )


def test_required_placeholder_filled_from_overrides():
    brief = {"context": {"output_path": "{{output_path}}", "window": "{{window}}"}}
    composed, err = _compose(
        brief, {"output_path": "/tmp/x.log", "window": "24h"}
    )
    assert err is None
    assert composed["context"] == {"output_path": "/tmp/x.log", "window": "24h"}


def test_unfilled_required_placeholder_is_an_error():
    brief = {"context": {"output_path": "{{output_path}}", "window": "{{window}}"}}
    composed, err = _compose(brief, {"output_path": "/tmp/x.log"})
    assert composed is None
    assert err is not None
    assert "window" in err
    assert "channels.yaml" in err


def test_unfilled_optional_placeholder_resolves_to_empty():
    brief = {"context": {"calendar_id": "{{calendar_id}}"}}
    composed, err = _compose(brief, {}, optional=("calendar_id",))
    assert err is None
    assert composed["context"]["calendar_id"] == ""


def test_event_id_substituted_into_override_values():
    brief = {"context": {"output_path": "{{output_path}}"}}
    composed, err = _compose(
        brief, {"output_path": "/tmp/calendar-agent-{event_id}.log"}
    )
    assert err is None
    assert composed["context"]["output_path"] == "/tmp/calendar-agent-evt-1.log"


def test_placeholder_inside_list_element_is_filled():
    brief = {"success_criteria": ["{{success_criteria}}"]}
    composed, err = _compose(brief, {"success_criteria": "file written"})
    assert err is None
    assert composed["success_criteria"] == ["file written"]


def test_no_placeholders_passes_through_unchanged():
    brief = {"context": {"window": "24h"}}
    composed, err = _compose(brief, {})
    assert err is None
    assert composed == brief


def test_non_string_override_preserves_type():
    brief = {"context": {"retries": "{{retries}}"}}
    composed, err = _compose(brief, {"retries": 3})
    assert err is None
    assert composed["context"]["retries"] == 3


# --- spawn hand-off: dispatcher → taskpilot daemon over HTTP ------------------
#
# The spawn path used to shell out to a taskpilot CLI; it now POSTs to the
# daemon's /tasks/create_and_spawn. Nothing exercised the actual hand-off
# before, which is how a deleted CLI slipped through a green suite. These
# tests stub httpx so the request shape and result mapping are covered.


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def _fake_client(captured, resp):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return resp

    return _Client


def _write_recipe(tmp_path, body):
    rdir = tmp_path / "meeting-prep"
    rdir.mkdir()
    (rdir / "recipe.yaml").write_text(body)
    return rdir


def test_spawn_recipe_posts_to_taskpilot_daemon(tmp_path, monkeypatch):
    _write_recipe(
        tmp_path,
        "task_id_pattern: 'meeting-prep-{event_id}'\n"
        "model: opus\n"
        "starter_prompt: 'Handle {event_id}. Payload: {payload}'\n",
    )
    captured = {}
    resp = _FakeResp(200, {"status": "running", "task_id": "meeting-prep-evt-1"})
    monkeypatch.setattr(spawn_helper.httpx, "AsyncClient", _fake_client(captured, resp))

    result = asyncio.run(
        spawn_helper.spawn_recipe(
            recipe_id="meeting-prep",
            payload={"meeting_title": "Sync"},
            event_id="evt-1",
            recipes_dir=tmp_path,
        )
    )

    assert result["ok"] is True
    assert result["task_id"] == "meeting-prep-evt-1"
    assert captured["url"].endswith("/tasks/create_and_spawn")
    assert captured["json"]["name"] == "meeting-prep-evt-1"
    assert captured["json"]["model"] == "opus"
    # {payload} is the only place event data reaches the agent — rendered JSON.
    assert "meeting_title" in captured["json"]["description"]
    assert "evt-1" in captured["json"]["description"]


def test_spawn_recipe_surfaces_daemon_error(tmp_path, monkeypatch):
    _write_recipe(
        tmp_path,
        "task_id_pattern: 'meeting-prep-{event_id}'\n"
        "starter_prompt: 'go'\n",
    )
    captured = {}
    resp = _FakeResp(502, {"detail": "tmux session could not be launched"})
    monkeypatch.setattr(spawn_helper.httpx, "AsyncClient", _fake_client(captured, resp))

    result = asyncio.run(
        spawn_helper.spawn_recipe(
            recipe_id="meeting-prep",
            payload={},
            event_id="evt-1",
            recipes_dir=tmp_path,
        )
    )

    assert result["ok"] is False
    assert "502" in result["error"]
    assert "tmux session could not be launched" in result["error"]


def test_spawn_recipe_missing_recipe_is_an_error(tmp_path, monkeypatch):
    # No HTTP call should happen — fail before the daemon.
    def _boom(*a, **k):
        raise AssertionError("must not POST when the recipe is missing")

    monkeypatch.setattr(spawn_helper.httpx, "AsyncClient", _boom)
    result = asyncio.run(
        spawn_helper.spawn_recipe(
            recipe_id="nonexistent",
            payload={},
            event_id="evt-1",
            recipes_dir=tmp_path,
        )
    )
    assert result["ok"] is False
    assert "not found" in result["error"]

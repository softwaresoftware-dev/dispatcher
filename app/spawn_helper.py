"""Hand `spawn:<recipe>` channel routes off to taskpilot's runtime daemon.

Given a recipe id, dispatcher-ingress reads `~/.dispatcher/recipes/<id>/recipe.yaml`,
substitutes {event_id}, {task_id}, {payload} into the starter prompt, and POSTs
to taskpilot's daemon `POST /tasks/create_and_spawn` with the composed
description, brief, and model. The daemon creates the task row and
launches tmux + claude (~16s) — callers should fire this from a BackgroundTasks
context, not in the request hot path.

Placeholder semantics — IMPORTANT, two distinct substitution surfaces:

1. recipe.starter_prompt — supports {event_id}, {task_id}, {payload}, {brief}.
   {payload} is the ONLY place the event's `data` reaches the spawned agent:
   it is replaced with the full event `data` rendered as pretty JSON. To act
   on event fields (e.g. a calendar event's meeting_title), the recipe must
   read them out of {payload} in its starter_prompt — there is no field-level
   substitution of event data anywhere.

2. recipe brief.json {{placeholders}} — filled from the channels.yaml route's
   `brief:` overrides. Override values may themselves reference only
   {event_id} and {task_id}; they CANNOT reference event data fields. A route
   writing `brief: { meeting_title: "{meeting_title}" }` expecting dispatcher
   to pull `data.meeting_title` is wrong — the literal string
   `{meeting_title}` passes through unchanged. Event data is not available to
   brief overrides; use {payload} in starter_prompt instead.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import yaml

# {{placeholder}} tokens in a recipe's brief.json — filled by the LLM
# dispatcher on the semantic path, or by the channels.yaml `brief:` block
# on the static path. A token surviving composition is a config error.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

DEFAULT_RECIPES_DIR = Path(
    os.environ.get(
        "DISPATCHER_RECIPES_DIR",
        str(Path.home() / ".dispatcher" / "recipes"),
    )
)
TASKPILOT_DAEMON_URL = os.environ.get(
    "TASKPILOT_DAEMON_URL", "http://127.0.0.1:8912"
).rstrip("/")


SPAWN_TIMEOUT_SEC = int(os.environ.get("DISPATCHER_SPAWN_TIMEOUT_SEC", "120"))


def _slugify(name: str) -> str:
    """Mirror taskpilot's slugify so we can predict the task_id pattern locally."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:50] or "task"


def _compose_brief(
    brief_obj: object,
    overrides: dict,
    *,
    event_id: str,
    task_id: str,
    optional_keys: set[str],
) -> tuple[object | None, str | None]:
    """Fill {{placeholder}} tokens in a recipe brief from route overrides.

    Returns (composed_brief, None) on success, (None, error) on failure.
    A required placeholder with no override is an error — the on-call path
    should never spawn an agent that's missing its operating context. An
    optional placeholder with no override resolves to "" (the recipe is
    responsible for treating an empty value as "unset").

    Override values come from the channels.yaml route's `brief:` block. The
    ONLY tokens substituted into an override value are {event_id} and
    {task_id} (see _resolve below). Override values CANNOT reference event
    `data` fields — there is no `{some_data_field}` substitution here. A
    route that writes `brief: { meeting_title: "{meeting_title}" }` hoping
    dispatcher will fill it from `data.meeting_title` is wrong: the literal
    string `{meeting_title}` passes through untouched. To act on event data,
    the recipe must read the full event `data` via {payload} in its
    starter_prompt; brief overrides are for static, route-authored context.
    """
    missing_required: set[str] = set()

    def _resolve(name: str) -> object | None:
        if name not in overrides:
            return None
        val = overrides[name]
        if isinstance(val, str):
            return val.replace("{event_id}", event_id).replace("{task_id}", task_id)
        return val

    def _sub_str(s: str) -> object:
        whole = _PLACEHOLDER_RE.fullmatch(s.strip())
        if whole:
            # Whole-value placeholder — preserve the override's native type.
            name = whole.group(1)
            resolved = _resolve(name)
            if resolved is not None:
                return resolved
            if name not in optional_keys:
                missing_required.add(name)
            return ""
        def _one(m: re.Match) -> str:
            name = m.group(1)
            resolved = _resolve(name)
            if resolved is not None:
                return str(resolved)
            if name not in optional_keys:
                missing_required.add(name)
            return ""
        return _PLACEHOLDER_RE.sub(_one, s)

    def _walk(node: object) -> object:
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, str):
            return _sub_str(node)
        return node

    composed = _walk(brief_obj)
    if missing_required:
        return None, (
            "static-spawn brief incomplete — unfilled required placeholders: "
            + ", ".join(sorted(missing_required))
            + ". Add a matching 'brief:' block to the channels.yaml route."
        )
    return composed, None


async def spawn_recipe(
    *,
    recipe_id: str,
    payload: dict | list | str | int | float | bool | None,
    event_id: str,
    brief_overrides: dict | None = None,
    recipes_dir: Path | None = None,
    taskpilot_daemon_url: str | None = None,
) -> dict:
    """Spawn an ephemeral taskpilot agent from a recipe.

    `brief_overrides` supplies literal values for the recipe brief's
    {{placeholders}} on the static-spawn path (where no LLM dispatcher
    composes the brief). Pass the channels.yaml route's `brief:` block.

    Returns {ok, task_id, ...} on success, {ok: False, error} on failure.
    Errors here are surfaced to dispatcher-ingress's audit log; the caller's
    HTTP response is not blocked on this (BackgroundTasks).
    """
    rdir = (recipes_dir or DEFAULT_RECIPES_DIR) / recipe_id
    recipe_yaml = rdir / "recipe.yaml"
    brief_json = rdir / "brief.json"
    if not recipe_yaml.exists():
        return {"ok": False, "error": f"recipe '{recipe_id}' not found at {rdir}"}

    try:
        recipe = yaml.safe_load(recipe_yaml.read_text()) or {}
    except yaml.YAMLError as e:
        return {"ok": False, "error": f"recipe.yaml parse error: {e}"}

    task_id_pattern = recipe.get("task_id_pattern") or f"{recipe_id}-{{event_id}}"
    starter_prompt = recipe.get("starter_prompt") or ""
    # taskpilot's runtime gives every spawned agent the user's globally-enabled
    # plugins/MCPs and the session-bridge channel — there is no per-task
    # curation. A recipe's legacy `plugins`/`mcps`/`channels` blocks (if any)
    # are simply not read.
    model = recipe.get("model")
    brief_schema = recipe.get("brief_schema") or {}
    optional_keys = set(brief_schema.get("optional") or [])
    # A recipe's legacy `frame:` block (the deleted mindframe spawn-CLI
    # convention) is not read; surface frames are minted by the mindframe
    # dashboard, not by dispatcher.

    # Substitute {event_id} → predict task_id.
    raw_id = task_id_pattern.format(event_id=event_id)
    task_id = _slugify(raw_id)
    pretty_payload = json.dumps(payload, indent=2, default=str)

    # Compose the brief: fill the recipe template's {{placeholders}} from the
    # route's brief overrides. The composed object is sent in the spawn request
    # body — never hand the raw {{...}} template to a spawned agent.
    composed_brief: dict | None = None
    brief_text = "{}"
    if brief_json.exists():
        try:
            brief_obj = json.loads(brief_json.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            return {"ok": False, "error": f"recipe brief.json parse error: {e}"}
        composed, err = _compose_brief(
            brief_obj,
            brief_overrides or {},
            event_id=event_id,
            task_id=task_id,
            optional_keys=optional_keys,
        )
        if err:
            return {"ok": False, "error": err}
        composed_brief = composed if isinstance(composed, dict) else None
        brief_text = json.dumps(composed, indent=2)

    description = (
        starter_prompt.replace("{event_id}", event_id)
        .replace("{task_id}", task_id)
        .replace("{payload}", pretty_payload)
        .replace("{brief}", brief_text)
    )

    spawn_body: dict = {"description": description, "name": task_id}
    if model:
        spawn_body["model"] = model
    if composed_brief is not None:
        spawn_body["brief"] = composed_brief

    url = (taskpilot_daemon_url or TASKPILOT_DAEMON_URL).rstrip("/") + "/tasks/create_and_spawn"
    try:
        async with httpx.AsyncClient(timeout=SPAWN_TIMEOUT_SEC) as client:
            resp = await client.post(url, json=spawn_body)
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"taskpilot daemon unreachable at {url}: {e}"}

    if resp.status_code >= 400:
        detail = resp.text[:300]
        try:
            detail = resp.json().get("detail", detail)
        except ValueError:
            pass
        return {"ok": False, "error": f"taskpilot spawn failed ({resp.status_code}): {detail}"}

    try:
        result = resp.json()
    except ValueError:
        return {"ok": False, "error": f"taskpilot non-JSON response: {resp.text[:200]}"}
    result.setdefault("ok", True)
    return result

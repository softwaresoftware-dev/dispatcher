"""Shell out to taskpilot's spawner_cli for `spawn:<recipe>` channel routes.

Given a recipe id, dispatcher-ingress reads `~/.dispatcher/recipes/<id>/recipe.yaml`,
substitutes {event_id}, {task_id}, {payload} into the starter prompt, and
invokes spawner_cli with the recipe's plugins, brief, and channels. The
spawner blocks until tmux + claude are launched (~16s) — callers should
fire this from a BackgroundTasks context, not in the request hot path.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

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
DEFAULT_SPAWNER_CLI = os.environ.get(
    "TASKPILOT_SPAWNER_CLI",
    str(Path.home() / "projects" / "softwaresoftware" / "projects" / "plugins" / "providers" / "taskpilot" / "spawner_cli.py"),
)
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
    responsible for treating an empty value as "unset"). Override values
    may themselves contain {event_id} / {task_id}, substituted here.
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
    spawner_cli: str | None = None,
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
    # `plugins` and `mcps` may each be a flat list or the schema'd
    # {base: [...], optional_pool: [...]} form. On the static path there is no
    # dispatcher to pick from optional_pool, so the spawned agent gets the
    # deterministic `base` set only.
    #   plugins -> taskpilot --enabled-plugins (installed-plugin marketplace keys)
    #   mcps    -> taskpilot --enabled-mcps    (MCP server names from ~/.claude.json)
    def _base_set(block) -> list[str]:
        if isinstance(block, dict):
            return list(block.get("base") or [])
        return list(block or [])

    enabled_plugins = _base_set(recipe.get("plugins"))
    enabled_mcps = _base_set(recipe.get("mcps"))
    channels = recipe.get("channels") or []
    model = recipe.get("model")
    brief_schema = recipe.get("brief_schema") or {}
    optional_keys = set(brief_schema.get("optional") or [])

    # Substitute {event_id} → predict task_id.
    raw_id = task_id_pattern.format(event_id=event_id)
    task_id = _slugify(raw_id)
    pretty_payload = json.dumps(payload, indent=2, default=str)

    # Compose the brief: fill the recipe template's {{placeholders}} from the
    # route's brief overrides. Write the result to a temp file passed to the
    # spawner — never hand the raw {{...}} template to a spawned agent.
    brief_path: str | None = None
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
        brief_text = json.dumps(composed, indent=2)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".brief.json", prefix=f"{task_id}-",
            delete=False, encoding="utf-8",
        )
        tmp.write(brief_text)
        tmp.close()
        brief_path = tmp.name

    description = (
        starter_prompt.replace("{event_id}", event_id)
        .replace("{task_id}", task_id)
        .replace("{payload}", pretty_payload)
        .replace("{brief}", brief_text)
    )

    args = [
        "python",
        spawner_cli or DEFAULT_SPAWNER_CLI,
        description,
        "--name", task_id,
    ]
    if enabled_plugins:
        args += ["--enabled-plugins", ",".join(enabled_plugins)]
    if enabled_mcps:
        args += ["--enabled-mcps", ",".join(enabled_mcps)]
    if channels:
        args += ["--channels", ",".join(channels)]
    if model:
        args += ["--model", model]
    if brief_path:
        args += ["--brief", brief_path]

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as e:
            return {"ok": False, "error": f"spawner_cli invoke failed: {e}"}

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SPAWN_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            # Kill the wedged spawner so the BackgroundTask worker isn't held forever.
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return {"ok": False, "error": f"spawner_cli timeout after {SPAWN_TIMEOUT_SEC}s"}

        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"spawner_cli exit {proc.returncode}: {stderr.decode(errors='replace')[:200]}",
            }
        try:
            result = json.loads(stdout.decode())
        except (json.JSONDecodeError, ValueError):
            return {"ok": False, "error": f"spawner_cli non-JSON output: {stdout.decode(errors='replace')[:200]}"}
        return result
    finally:
        if brief_path:
            try:
                os.unlink(brief_path)
            except OSError:
                pass

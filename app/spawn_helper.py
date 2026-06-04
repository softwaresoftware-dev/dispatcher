"""Shell out to taskpilot's spawner_cli for `spawn:<recipe>` channel routes.

Given a recipe id, dispatcher-ingress reads `~/.dispatcher/recipes/<id>/recipe.yaml`,
substitutes {event_id}, {task_id}, {payload} into the starter prompt, and
invokes spawner_cli with the recipe's plugins, brief, and channels. The
spawner blocks until tmux + claude are launched (~16s) — callers should
fire this from a BackgroundTasks context, not in the request hot path.

Placeholder semantics — IMPORTANT, two distinct substitution surfaces:

1. recipe.starter_prompt — supports {event_id}, {task_id}, {payload}, {brief}.
   {payload} is the ONLY place the event's `data` reaches the spawned agent:
   it is replaced with the full event `data` rendered as pretty JSON. To act
   on event fields (e.g. a calendar event's meeting_title), the recipe must
   read them out of {payload} in its starter_prompt — there is no field-level
   substitution of event data anywhere.

2. recipe brief.json / frame: block {{placeholders}} — filled from the
   channels.yaml route's `brief:` overrides. Override values may themselves
   reference only {event_id} and {task_id}; they CANNOT reference event data
   fields. A route writing `brief: { meeting_title: "{meeting_title}" }`
   expecting dispatcher to pull `data.meeting_title` is wrong — the literal
   string `{meeting_title}` passes through unchanged. Event data is not
   available to brief overrides; use {payload} in starter_prompt instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
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


# When a recipe declares a `frame:` block, dispatcher shells out to mindframe's
# spawn CLI to create the mindframe (mkdir + meta.json + seed block) before
# spawning the agent. Discovery order:
#   1. $MINDFRAME_SPAWN_CLI env (explicit override — install skill sets this)
#   2. The marketplace cache at ~/.claude/plugins/cache/<marketplace>/mindframe/<version>/lib/spawn.py
#      — pick the highest version directory found
#   3. None — spawn_recipe returns a clear error when this happens for a
#      `frame:`-declaring recipe
# No hardcoded dev paths — the resolver-installed plugin tree is the source
# of truth, per the projects/CLAUDE.md no-hardcoded-paths rule.

def _resolve_mindframe_spawn_cli() -> str | None:
    """Locate the mindframe spawn CLI. Returns the absolute path or None.
    Called per-spawn rather than at module import so a fresh install
    (or an env override) takes effect without a dispatcher restart."""
    explicit = os.environ.get("MINDFRAME_SPAWN_CLI", "").strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None

    cache = Path.home() / ".claude" / "plugins" / "cache"
    if not cache.is_dir():
        return None

    candidates: list[tuple[str, Path]] = []
    for marketplace_dir in cache.iterdir():
        mf_root = marketplace_dir / "mindframe"
        if not mf_root.is_dir():
            continue
        for version_dir in mf_root.iterdir():
            cli = version_dir / "lib" / "spawn.py"
            if cli.is_file():
                candidates.append((version_dir.name, cli))
    if not candidates:
        return None
    # Highest version wins. Versions are semver-shaped strings ("0.4.0");
    # lex sort works for the cases we ship.
    candidates.sort(reverse=True)
    return str(candidates[0][1])


SPAWN_TIMEOUT_SEC = int(os.environ.get("DISPATCHER_SPAWN_TIMEOUT_SEC", "120"))
FRAME_SPAWN_TIMEOUT_SEC = int(os.environ.get("DISPATCHER_FRAME_SPAWN_TIMEOUT_SEC", "15"))


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


async def _create_mindframe(
    *,
    frame_block: dict,
    brief_overrides: dict,
    event_id: str,
    task_id: str,
    optional_keys: set[str],
    mindframe_spawn_cli: str,
) -> dict:
    """Shell out to mindframe's spawn CLI to mint a frame before taskpilot.

    `frame_block` is the recipe.yaml's `frame:` value. Its `title` and
    `seed_block` fields go through the same {{placeholder}} composer used
    for brief.json, so dispatcher passes through the route's `brief:` values.

    Returns {ok, mindframe_id, frame_dir, url} or {ok: False, error}.
    """
    composed, err = _compose_brief(
        frame_block,
        brief_overrides,
        event_id=event_id,
        task_id=task_id,
        optional_keys=optional_keys,
    )
    if err:
        return {"ok": False, "error": f"frame: block incomplete — {err}"}

    title = (composed or {}).get("title") or task_id
    seed_block = (composed or {}).get("seed_block")
    tags = (composed or {}).get("tags") or []
    spawned_by = {
        "kind": "dispatcher-event",
        "event_id": event_id,
        "recipe": task_id,
    }
    args = [
        sys.executable, mindframe_spawn_cli,
        "--title", str(title),
        "--spawned-by-json", json.dumps(spawned_by),
    ]
    if seed_block is not None:
        args += ["--seed-block-json", json.dumps(seed_block)]
    if tags:
        args += ["--tags", ",".join(str(t) for t in tags)]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, FileNotFoundError) as e:
        return {"ok": False, "error": f"mindframe-spawn invoke failed: {e}"}
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=FRAME_SPAWN_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return {"ok": False, "error": f"mindframe-spawn timeout after {FRAME_SPAWN_TIMEOUT_SEC}s"}
    # The CLI emits its JSON envelope on stdout for both success and failure
    # paths. Parse stdout first so structured errors come through; fall back
    # to stderr only if stdout doesn't carry a parseable result.
    try:
        result = json.loads(stdout.decode())
    except json.JSONDecodeError:
        suffix = stderr.decode(errors="replace")[:200] or stdout.decode(errors="replace")[:200]
        return {"ok": False, "error": f"mindframe-spawn exit {proc.returncode}: {suffix}"}
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "mindframe-spawn reported failure")}
    if proc.returncode != 0:
        return {"ok": False, "error": f"mindframe-spawn exit {proc.returncode} despite ok=true"}
    return {
        "ok": True,
        "mindframe_id": result["id"],
        "frame_dir": result["frame_dir"],
        "url": result["url"],
    }


async def spawn_recipe(
    *,
    recipe_id: str,
    payload: dict | list | str | int | float | bool | None,
    event_id: str,
    brief_overrides: dict | None = None,
    recipes_dir: Path | None = None,
    spawner_cli: str | None = None,
    mindframe_spawn_cli: str | None = None,
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
    # NOTE: taskpilot dropped per-task plugin/MCP curation (the sandbox feature)
    # in v0.12.0, so the recipe's `plugins`/`mcps` blocks are no longer applied —
    # a spawned agent inherits whatever the user has enabled globally. The keys
    # are left tolerated-but-ignored in recipes for backward compatibility.
    channels = recipe.get("channels") or []
    model = recipe.get("model")
    brief_schema = recipe.get("brief_schema") or {}
    optional_keys = set(brief_schema.get("optional") or [])
    frame_block = recipe.get("frame")  # optional; presence opts the recipe into mindframe mode

    # Substitute {event_id} → predict task_id.
    raw_id = task_id_pattern.format(event_id=event_id)
    task_id = _slugify(raw_id)
    pretty_payload = json.dumps(payload, indent=2, default=str)

    # If the recipe is a mindframe recipe, mint the frame first so the
    # subsequent task name == frame id and cwd == frame dir. This is the
    # synchronous-seed-block convention: the operator opening the URL during
    # the ~16s taskpilot startup window sees the seed block, not a blank page.
    mindframe_id: str | None = None
    frame_dir: str | None = None
    mindframe_url: str | None = None
    if isinstance(frame_block, dict):
        resolved_mf_cli = mindframe_spawn_cli or _resolve_mindframe_spawn_cli()
        if not resolved_mf_cli:
            return {"ok": False, "error": (
                "Recipe declares `frame:` but the mindframe spawn CLI was not "
                "found. Install mindframe (`/softwaresoftware:install mindframe`), "
                "or set $MINDFRAME_SPAWN_CLI to an absolute path."
            )}
        frame_res = await _create_mindframe(
            frame_block=frame_block,
            brief_overrides=brief_overrides or {},
            event_id=event_id,
            task_id=task_id,
            optional_keys=optional_keys,
            mindframe_spawn_cli=resolved_mf_cli,
        )
        if not frame_res["ok"]:
            return {"ok": False, "error": frame_res["error"]}
        mindframe_id = frame_res["mindframe_id"]
        frame_dir = frame_res["frame_dir"]
        mindframe_url = frame_res["url"]
        # Convention: task_id == mindframe_id so session-bridge routing for
        # button-click "continue" events finds the right session.
        task_id = mindframe_id

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
        sys.executable,
        spawner_cli or DEFAULT_SPAWNER_CLI,
        description,
        "--name", task_id,
    ]
    if frame_dir:
        # cwd = frame dir lets the agent's mindframe MCP write_block resolve
        # the mindframe id from cwd with no arg, per the spawn convention.
        args += ["--cwd", frame_dir]
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
        if mindframe_id:
            result["mindframe_id"] = mindframe_id
            result["mindframe_url"] = mindframe_url
            result["frame_dir"] = frame_dir
        return result
    finally:
        if brief_path:
            try:
                os.unlink(brief_path)
            except OSError:
                pass

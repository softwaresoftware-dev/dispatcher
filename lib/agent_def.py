"""Agent definition format — load and validate `.agent.md` files.

An *agent* is the portable half of a dispatcher recipe: a parameterized
instruction plus a declaration of what it depends on. It is host-agnostic — no
trigger, no model. A *binding* (`.binding.yaml`) wraps an agent for one
deployment. Agent + binding + resolution compile to a runnable recipe
(`recipe.yaml` + `brief.json` + `CLAUDE.md`) — the form spawn_helper.py
already consumes. See the agents/ directory for the format in practice.

An agent declares its dependencies two ways:

  requires / optional  — *capabilities*: abstract roles with interchangeable
                         providers (notification, memory). Resolver-bound.
  mcps                 — named MCP servers: a concrete connection to one
                         specific external system. Not resolver-bound; the
                         host configures the server.

An agent may declare neither — a pure-transform agent (instruction in, output
out) is legitimate.

This module loads and validates those files, and is also a CLI:

    python -m lib.agent_def <agent.md> [binding.yaml]
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# {{placeholder}} tokens in an instruction — the same shape spawn_helper.py's
# brief composer uses, so a converted agent stays wire-compatible.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass
class Agent:
    name: str
    version: str
    description: str
    requires: list[str]          # required capabilities (abstract, resolver-bound)
    optional: list[str]          # optional capabilities
    mcps: list[str]              # named MCP servers (concrete external systems)
    inputs_required: list[str]
    inputs_optional: dict[str, object]
    instruction: str
    path: Path

    @property
    def declared_inputs(self) -> set[str]:
        return set(self.inputs_required) | set(self.inputs_optional)


@dataclass
class Binding:
    agent_ref: str               # "name@version"
    trigger: dict
    policy: dict
    inputs: dict
    scope: dict                  # fine-grained targets for capabilities
    path: Path

    @property
    def agent_name(self) -> str:
        return self.agent_ref.split("@", 1)[0]

    @property
    def agent_version(self) -> str:
        parts = self.agent_ref.split("@", 1)
        return parts[1] if len(parts) == 2 else ""


def load_agent(path) -> Agent:
    """Parse a `.agent.md` file: YAML frontmatter + Markdown instruction body."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("missing or malformed `---` YAML frontmatter")
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"frontmatter is not valid YAML: {e}")
    if not isinstance(fm, dict):
        raise ValueError("frontmatter must be a mapping")
    inputs = fm.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise ValueError("`inputs` must be a mapping with `required`/`optional`")
    return Agent(
        name=str(fm.get("agent", "")),
        version=str(fm.get("version", "")),
        description=str(fm.get("description", "")),
        requires=list(fm.get("requires") or []),
        optional=list(fm.get("optional") or []),
        mcps=list(fm.get("mcps") or []),
        inputs_required=list(inputs.get("required") or []),
        inputs_optional=dict(inputs.get("optional") or {}),
        instruction=m.group(2).strip(),
        path=path,
    )


def validate_agent(agent: Agent) -> list[str]:
    """Return a list of contract violations. Empty list == valid."""
    errors: list[str] = []

    if not agent.name:
        errors.append("frontmatter `agent` (name) is required")
    elif not _NAME_RE.match(agent.name):
        errors.append(f"agent name `{agent.name}` must be kebab-case")

    if not agent.version:
        errors.append("frontmatter `version` is required")
    elif not _SEMVER_RE.match(agent.version):
        errors.append(f"version `{agent.version}` must be semver (X.Y.Z)")

    if not agent.description:
        errors.append("frontmatter `description` is required")

    for cap in list(agent.requires) + list(agent.optional):
        if not _NAME_RE.match(str(cap)):
            errors.append(f"capability `{cap}` must be a kebab-case name")
    for mcp in agent.mcps:
        if not _NAME_RE.match(str(mcp)):
            errors.append(f"mcp `{mcp}` must be a kebab-case name")

    if not agent.instruction:
        errors.append("the instruction body is empty")

    # Core check: every {{placeholder}} in the body is a declared input, and
    # every declared input is actually used. A drifted name fails loudly here
    # rather than handing the spawned agent a literal `{{...}}`.
    used = {m.group(1) for m in _PLACEHOLDER_RE.finditer(agent.instruction)}
    for token in sorted(used - agent.declared_inputs):
        errors.append(f"instruction uses `{{{{{token}}}}}` but it is not a declared input")
    for declared in sorted(agent.declared_inputs - used):
        errors.append(f"input `{declared}` is declared but never used in the instruction")

    return errors


def load_binding(path) -> Binding:
    """Parse a `.binding.yaml` deployment binding."""
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("binding must be a YAML mapping")
    return Binding(
        agent_ref=str(data.get("agent", "")),
        trigger=dict(data.get("trigger") or {}),
        policy=dict(data.get("policy") or {}),
        inputs=dict(data.get("inputs") or {}),
        scope=dict(data.get("scope") or {}),
        path=path,
    )


def validate_binding(binding: Binding, agent: Agent | None = None) -> list[str]:
    """Validate a binding; cross-check against its agent when one is given."""
    errors: list[str] = []

    if not binding.agent_ref or "@" not in binding.agent_ref:
        errors.append("binding `agent` must be `name@version`")
    if not binding.trigger:
        errors.append("binding `trigger` is required")
    if not binding.policy.get("model"):
        errors.append("binding `policy.model` is required")
    # Every agent is spawned ephemerally — there is no supervised long-running
    # "service" kind. `kind` defaults to `task`; a stale `kind: service` is
    # rejected so old bindings surface the change instead of silently implying
    # supervision that does not exist.
    if binding.policy.get("kind", "task") != "task":
        errors.append(
            "binding `policy.kind` must be `task` — the `service` "
            "(long-running) kind was removed"
        )

    if agent is not None:
        if binding.agent_name != agent.name:
            errors.append(
                f"binding targets `{binding.agent_name}` but the agent file is `{agent.name}`"
            )
        if binding.agent_version and binding.agent_version != agent.version:
            errors.append(
                f"binding pins `{binding.agent_version}` but the agent file is `{agent.version}`"
            )
        for key in binding.inputs:
            if key not in agent.declared_inputs:
                errors.append(f"binding sets input `{key}` which the agent does not declare")
        for cap in binding.scope:
            if cap not in set(agent.requires) | set(agent.optional):
                errors.append(f"binding scopes `{cap}` which is not a capability the agent declares")

    return errors


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: agent_def.py <agent.md> [binding.yaml]", file=sys.stderr)
        return 2

    agent_path = Path(argv[0])
    binding_path = Path(argv[1]) if len(argv) > 1 else None

    try:
        agent = load_agent(agent_path)
    except (OSError, ValueError) as e:
        print(f"FAIL  {agent_path}: {e}", file=sys.stderr)
        return 1

    errors = [f"{agent_path}: {e}" for e in validate_agent(agent)]

    if binding_path is not None:
        try:
            binding = load_binding(binding_path)
            errors += [f"{binding_path}: {e}" for e in validate_binding(binding, agent)]
        except (OSError, ValueError) as e:
            errors.append(f"{binding_path}: {e}")

    if errors:
        for e in errors:
            print(f"FAIL  {e}", file=sys.stderr)
        return 1

    detail = (
        f"{len(agent.requires)} required capability(ies), "
        f"{len(agent.mcps)} mcp(s), "
        f"{len(agent.declared_inputs)} input(s)"
    )
    if binding_path is not None:
        detail += ", binding valid"
    print(f"OK    {agent.name}@{agent.version} — {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

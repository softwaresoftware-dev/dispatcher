"""Contract tests for the agent definition format (lib/agent_def.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.agent_def import (
    Agent,
    load_agent,
    load_binding,
    validate_agent,
    validate_binding,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE_AGENT = FIXTURES / "example.agent.md"
EXAMPLE_BINDING = FIXTURES / "example.binding.yaml"


def test_example_agent_is_valid():
    agent = load_agent(EXAMPLE_AGENT)
    assert agent.name == "example"
    assert agent.version == "1.0.0"
    assert agent.requires == ["notification"]
    assert agent.mcps == ["example-system"]
    assert agent.declared_inputs == {"topic", "audience"}
    assert validate_agent(agent) == []


def test_example_binding_is_valid():
    agent = load_agent(EXAMPLE_AGENT)
    binding = load_binding(EXAMPLE_BINDING)
    assert binding.agent_name == "example"
    assert binding.agent_version == "1.0.0"
    assert binding.policy["kind"] == "task"
    assert validate_binding(binding, agent) == []


def _agent(instruction: str, **fm) -> Agent:
    base = dict(
        name="t", version="1.0.0", description="d",
        requires=[], optional=[], mcps=[],
        inputs_required=[], inputs_optional={},
        path=Path("t.agent.md"),
    )
    base.update(fm)
    return Agent(instruction=instruction, **base)


def test_undeclared_placeholder_is_flagged():
    agent = _agent("compose for {{genre}}")
    errors = validate_agent(agent)
    assert any("genre" in e and "not a declared input" in e for e in errors)


def test_unused_declared_input_is_flagged():
    agent = _agent("a static instruction", inputs_required=["window"])
    errors = validate_agent(agent)
    assert any("window" in e and "never used" in e for e in errors)


def test_pure_transform_agent_is_valid():
    # No capabilities, no MCPs — a legitimate instruction-in, output-out agent.
    agent = _agent("transform {{x}} into a summary", inputs_required=["x"])
    assert validate_agent(agent) == []


def test_non_semver_version_is_flagged():
    agent = _agent("hello {{x}}", inputs_optional={"x": 1}, version="1.0")
    assert any("semver" in e for e in validate_agent(agent))


def test_non_kebab_capability_is_flagged():
    agent = _agent("hello {{x}}", inputs_optional={"x": 1}, requires=["Notify_Me"])
    assert any("capability" in e and "kebab-case" in e for e in validate_agent(agent))


def test_non_kebab_mcp_is_flagged():
    agent = _agent("hello {{x}}", inputs_optional={"x": 1}, mcps=["Beats_Server"])
    assert any("mcp" in e and "kebab-case" in e for e in validate_agent(agent))


def test_binding_agent_mismatch_is_flagged():
    agent = load_agent(EXAMPLE_AGENT)
    binding = load_binding(EXAMPLE_BINDING)
    binding.agent_ref = "other-agent@1.0.0"
    assert any("other-agent" in e for e in validate_binding(binding, agent))


def test_binding_scopes_unknown_capability_is_flagged():
    agent = load_agent(EXAMPLE_AGENT)
    binding = load_binding(EXAMPLE_BINDING)
    binding.scope = {"telepathy": "#void"}
    assert any("telepathy" in e for e in validate_binding(binding, agent))


def test_missing_frontmatter_raises():
    tmp = FIXTURES / "_no_frontmatter.tmp.md"
    tmp.write_text("# just a body, no frontmatter\n", encoding="utf-8")
    try:
        with pytest.raises(ValueError, match="frontmatter"):
            load_agent(tmp)
    finally:
        tmp.unlink()

---
name: validate-agent
description: Validate an agent definition (.agent.md) and optionally its binding (.binding.yaml) against the dispatcher's agent-definition contract. Use when authoring or reviewing an agent definition, or before registering one.
---

# Dispatcher — Validate Agent

Check an agent definition against the format contract before it is registered.

Run the validator (bundled with this plugin):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/lib/agent_def.py" <agent.md> [binding.yaml]
```

It prints `OK …` when the definition is well-formed, or one `FAIL …` line per
violation — an undeclared `{{placeholder}}`, a non-semver version, malformed
frontmatter, a binding whose `agent@version` doesn't match its agent file, and
so on.

Report the result. On failure, walk the user through each `FAIL` line and what
to change. The contract: an agent is a parameterized instruction plus a
declaration of capabilities (`requires`/`optional`) and named MCP servers
(`mcps`); every `{{placeholder}}` in the instruction must be a declared input.

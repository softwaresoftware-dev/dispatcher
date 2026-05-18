---
agent: example
version: 1.0.0
description: A minimal valid agent — fixture for the agent_def validator tests.

requires:
  - notification
mcps:
  - example-system

inputs:
  required:
    - topic
  optional:
    audience: team
---

# Example

Summarize **{{topic}}** for the {{audience}}. Pull detail from the
example-system. Deliver the summary — use an available skill or tool.

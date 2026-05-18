# agents

Agent definitions — the portable half of a dispatcher recipe: a parameterized
instruction (`<name>.agent.md`) plus a deployment binding (`<name>.binding.yaml`).
Agent + binding compile to a runnable recipe at registration.

The format contract and validator live in `lib/agent_def.py` (also the
`/dispatcher:validate-agent` skill):

```bash
python -m lib.agent_def agents/<name>.agent.md agents/<name>.binding.yaml
```

These are the first-party / seed definitions, versioned with the dispatcher.
A live deployment's agents — seeds installed in, plus customer-authored ones —
live in the dispatcher's runtime directory (`~/.dispatcher/agents/`).

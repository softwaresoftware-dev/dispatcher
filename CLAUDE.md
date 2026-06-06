# CLAUDE.md — dispatcher

Provider plugin for the `event-routing` capability. It **bundles** the
dispatcher-ingress service — a FastAPI webhook receiver — and runs it as a
managed daemon. The plugin is the whole dispatcher: skills, service, the
agent-definition contract, and seed agents, in one repo.

## Structure

| Path | What |
|------|------|
| `skills/` | `setup`, `route`, `validate-agent` |
| `app/` | the dispatcher-ingress service (FastAPI: ingress, routing, audit, spawn helper) |
| `lib/agent_def.py` | the agent-definition format contract + validator (also a CLI) |
| `agents/` | seed agent definitions (`<name>.agent.md` + `<name>.binding.yaml`) |
| `tests/` | pytest suite — `api`, `channels`, `spawn`, `agent_def` |

## What it does

- Runs the bundled dispatcher-ingress service as a managed daemon (via the
  `daemon` capability) — reboot-persistent.
- Generates a default `channels.yaml` routing table.
- Exposes ingress on the configured port; every POST needs a bearer token.
- Routes events: deterministic (`(source, event_type) → target`) when
  `channels.yaml` matches, LLM-routed via the dispatcher session otherwise.

## Capability contract

Provides `event-routing`. Consumers (e.g. mindframe) declare
`requires: ["event-routing"]` and use the HTTP API to register routes and POST
events. The contract is HTTP, not skill-based — anything speaking the same
`/api/event` shape can substitute.

Depends on: `daemon` (run the service), `agent-spawning` (taskpilot's daemon
`POST /tasks/create_and_spawn` on :8912 is the `spawn:<recipe>` target),
`session-mesh` (session-bridge is the `session:<name>` target). Optional:
`deploy` (public HTTPS ingress).

## The service

- FastAPI + uvicorn + SQLite (WAL) audit log. Default port `8911`.
- Endpoints: `GET /api/health` (no auth), `POST /api/event` (bearer, LLM/static
  routed), `POST /api/direct/{session}` (bearer, explicit forward),
  `GET /api/events` + `/api/events/summary` (bearer, audit log).
- Env: `DISPATCHER_INGEST_TOKEN`, `SESSION_BRIDGE_URL`,
  `DISPATCHER_CHANNELS_FILE`, `DISPATCHER_RECIPES_DIR`,
  `DISPATCHER_DEDUPE_WINDOW_MINUTES`.
- **Static routing** (`channels.yaml`): each route maps `(source, event_type)`
  to `session:<name>` or `spawn:<recipe>`; first match wins; re-read per request.
- **Idempotency**: `/api/event` dedupes on `<source>:<event_id>` within the
  dedupe window. `/api/direct` is not deduped.
- **Audit**: every ingest writes a row to `~/.dispatcher/events.db`. A global
  exception handler also writes synthetic `_internal` rows for uncaught errors.

## Runtime layout

`$INSTALL_DIR` (default `~/.dispatcher`) holds runtime state — the venv,
`channels.yaml`, `recipes/`, `agents/`, and the audit DB. The service *code*
lives in this plugin (`${CLAUDE_PLUGIN_ROOT}/app/`).

## Commands

```bash
make start    # uvicorn app.main:app on port 8911
make stop
make status
make test     # pytest tests/
```

## Skills

- `/dispatcher:setup` — install + start the daemon on this machine.
- `/dispatcher:route` — add a route to `channels.yaml`.
- `/dispatcher:validate-agent` — validate an `.agent.md` against the contract.

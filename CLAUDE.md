# CLAUDE.md — dispatcher

Provider plugin for the `event-routing` capability. It **bundles** two ingestion
paths over one shared routing core — a **poller** (poll-first, primary) and the
**dispatcher-ingress** FastAPI service (webhook, deprecated) — and runs them as
managed daemons. The plugin is the whole dispatcher: skills, services, the
agent-definition contract, and seed agents, in one repo.

**Ingestion is poll-first.** On a NAT'd host there is no public endpoint a
webhook can reach, so the poller (`app/poller.py`) reads Event Source
declarations (`~/.dispatcher/event-sources/*.yaml`), polls each system on an
interval via its adapter (`app/adapters/`), tracks a per-source watermark in
`cursors.db`, and routes new items through the shared core (`app/core.py`). The
`/api/event` webhook still works for consumers that speak its HTTP contract but
is **deprecated** (responds with a `Deprecation` header). Both paths share the
same dedupe, audit, and forward/spawn primitives (`app/main.py`).

**Role in the mindframe stack:** dispatcher is the **Event ingress** layer. It
acquires external events (poll or webhook), dedupes and routes them, and spawns
ephemeral agents through the Agent runtime (taskpilot). It is a standalone
provider any consumer can use; mindframe is one such consumer. See
`../../frameworks/mindframe/docs/architecture.md`.

## Structure

| Path | What |
|------|------|
| `skills/` | `setup`, `route`, `validate-agent` |
| `app/poller.py` | **poll-first ingestion (primary)** — loop over event-sources, route new items |
| `app/event_sources.py` | load `event-sources/*.yaml` declarations |
| `app/adapters/` | one poll adapter per `system` (`github` → org Events API over httpx, ETag conditional requests) |
| `app/cursors.py` | per-source poll watermark (`cursors.db`); migrates legacy schema |
| `app/core.py` | shared routing core — dedupe, channels lookup, forward/spawn, audit |
| `app/main.py` | dispatcher-ingress FastAPI service (deprecated `/api/event`; `/api/direct`, `/api/events`, `/api/health`) |
| `lib/agent_def.py` | the agent-definition format contract + validator (also a CLI) |
| `agents/` | seed agent definitions (`<name>.agent.md` + `<name>.binding.yaml`) |
| `tests/` | pytest suite — `api`, `channels`, `spawn`, `agent_def`, `poller`, `core`, `cursors`, `github_adapter` |

## What it does

- Runs the **poller** as a managed daemon (`make poll`) — reboot-persistent.
  Reads event-sources, polls each on `DISPATCHER_POLL_INTERVAL_S` (default 60s).
- Runs the dispatcher-ingress service as a managed daemon (`make start`) for the
  deprecated webhook path + the audit/direct/health endpoints.
- Generates a default `channels.yaml` routing table.
- Routes events: deterministic (`(source, event_type) → target`) when
  `channels.yaml` matches, LLM-routed via the dispatcher session otherwise.
- **Cold start is safe:** a source's first poll establishes the watermark at the
  newest existing item and routes nothing (no stampede of agents on history).
  Set `DISPATCHER_POLL_BACKFILL=1` to deliberately replay history once.

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
# Poll-first ingestion (primary)
make poll                 # run the poller daemon (DISPATCHER_POLL_INTERVAL_S, default 60)
make poll-once            # a single tick, then exit (dev / cron)
make poll-once \
  ARGS=--dry-run          # or: python -m app.poller --once --dry-run  (resolve routes, act on nothing)
make poll-stop / poll-status

# Webhook ingress + audit/direct/health (webhook deprecated)
make start                # uvicorn app.main:app on port 8911
make stop
make status
make test                 # pytest tests/
```

Poller env: `DISPATCHER_POLL_INTERVAL_S` (default 60), `DISPATCHER_POLL_BACKFILL`
(replay history on first sight), `DISPATCHER_EVENT_SOURCES_DIR`,
`DISPATCHER_CURSORS_DB`.

## Skills

- `/dispatcher:setup` — install + start the daemon on this machine.
- `/dispatcher:route` — add a route to `channels.yaml`.
- `/dispatcher:validate-agent` — validate an `.agent.md` against the contract.

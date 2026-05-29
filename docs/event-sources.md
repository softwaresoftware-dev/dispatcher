# Event Sources — architecture spec

Status: draft (2026-05-29). For discussion before any code lands.

## Why

Today the dispatcher accepts events via `POST /api/event` and routes them downstream. That's a *webhook ingest* model: every enrolled source system needs an outbound HTTP path to the dispatcher, which means every host running the dispatcher needs a publicly-reachable endpoint. That's fine for cloud installs and untenable for the primary mindframe deployment shape — one install per employee laptop, behind NAT, often on a Tailscale tailnet, never with a public IP.

This spec replaces "the dispatcher is a webhook receiver" with "the dispatcher is an event router fed by a set of Event Sources, with transport (poll vs webhook) as an implementation detail." Webhook ingest stays as an opt-in transport for the cloud case.

## The user-facing primitive: Event Source

An Event Source is a named subscription. It describes *what to watch*, *where*, and *with whose credentials* — not how the watching happens.

```yaml
# ~/.dispatcher/event-sources/softwaresoftware-prs.yaml
name: softwaresoftware-prs
system: github                     # which adapter handles it
scope:
  orgs: [softwaresoftware-dev]
watching:
  - pull_request.opened
  - pull_request.review_requested
  - issues.opened
credentials_ref: github            # FK → vault Integration note (or system keyring)
transport: auto                    # auto | poll | webhook | both
```

The operator never types "interval," "cursor," or "webhook URL." Those belong to the adapter and the dispatcher runtime.

User-facing surfaces:

- **CLI**: `dispatcher event-source add` walks the operator through a list of installed source adapters, asks for scope + credentials, validates with a live probe, writes the YAML.
- **Dashboard**: a list of green/red dots per source, with "add source" / "test" / "remove" affordances. The dashboard never says the word "poller."

## The implementation primitive: Source Adapter

An Adapter is a capability provider that implements one or both transports for one source system. Adapters ship as plugins; the dispatcher loads them by capability at boot.

### Capability contract

Each adapter `provides` a parameterized capability:

```
event-source:<system>
```

Examples: `event-source:github`, `event-source:sentry`, `event-source:slack`, `event-source:gmail`.

Two adapters can both provide the same capability — one poll-only, one webhook-only. The capability resolver picks based on environment (public endpoint available? webhook secret configured? otherwise → poll). Same pattern the `notification` capability already uses.

### Adapter interface

An Adapter is a Python module discovered by capability. It implements either or both of:

```python
def pull(scope: dict, cursor: dict | None) -> tuple[list[Event], dict]:
    """Fetch events newer than `cursor`. Return (events, new_cursor).

    `cursor` is opaque to the dispatcher — adapters define their own shape
    (last_event_id, etag, timestamp, whatever the upstream API exposes).
    Empty `cursor` means "we just enrolled — go-forward only, do not backfill."
    """

def handle_push(payload: dict, headers: dict) -> list[Event]:
    """Webhook handler. Adapter validates signature, parses, normalizes to Event."""
```

An `Event` is the same shape `POST /api/event` already accepts: `{source, event_type, event_id, data, occurred_at}`. Adapters synthesize these from upstream payloads; the dispatcher doesn't care whether they came from poll or webhook.

### Why parameterized

Marketplace listings stay clean — installer asks "do you want `event-source:github`?" Plural systems can ship in one plugin or many; `requires`/`optional` reference the specific subset the consumer cares about. Mindframe's manifest becomes:

```json
"optional": [
  "event-source:github",
  "event-source:sentry",
  "event-source:slack",
  "event-source:gmail",
  "event-source:pagerduty"
]
```

`/mindframe:setup` walks the optional list, asks the operator which to enable, installs the providers, and helps them add the first Event Source for each.

## Transport

Two transports, both first-class, both feeding the same routing table.

### Poll (default)

Driven by the dispatcher's **poller runtime** — a single scheduler inside the daemon that, for each Event Source with poll enabled, runs an adapter's `pull()` on a tick.

- Default tick: **60s**. Configurable per source (`tick: 30s` in the YAML). Per-source minimum enforced by the adapter (GitHub etag-based polling can go to 10s safely; Sentry public REST API should not).
- The runtime supervises pollers like the taskpilot daemon supervises tasks — crash → log → restart with backoff.
- Cursor lives in `~/.dispatcher/cursors.db` (SQLite) keyed by event-source name. The dispatcher owns durability; adapters are stateless between calls.

### Webhook (opt-in)

Same `POST /api/event` shape that exists today. For an Event Source with `transport: webhook`, the operator (or `/dispatcher:setup` in cloud mode) configures the upstream system to POST to `https://<their-public-endpoint>/api/event/<event-source-name>`. The dispatcher delegates payload parsing to the adapter's `handle_push()`.

### Hybrid (`transport: both`)

Pollers run *and* the webhook handler accepts pushes. The dispatcher's existing idempotency table dedupes on `<source>:<event_id>` so the same upstream event arriving via both paths is absorbed once. Use case: cloud install that wants webhook latency but also wants poll-based recovery after a downtime gap.

### `transport: auto`

Default. Dispatcher picks: webhook if a public endpoint is configured for the dispatcher *and* the adapter implements `handle_push`, else poll. This is the value `/dispatcher:setup` writes for new sources unless the operator overrides.

## Cursor and dedupe

Cursors:

- One row per Event Source in `~/.dispatcher/cursors.db`. Schema: `(source_name TEXT PRIMARY KEY, cursor BLOB, updated_at TEXT)`.
- Adapter owns the cursor's *shape*; dispatcher owns its *persistence*.
- On adapter upgrade, the adapter is responsible for migrating an old cursor or returning a new one. Dispatcher passes the cursor through untouched.

Dedupe:

- `<source>:<event_id>` within `DISPATCHER_DEDUPE_WINDOW_MINUTES`. Already exists. Reused unchanged.
- Adapters must produce stable `event_id`s — for GitHub that's the X-GitHub-Delivery (push) or the event id in the events API (poll). For systems with no stable id, hash a canonical subset of the payload.

## Initial state

Go-forward only by default. When `pull()` is called with `cursor=None`, the adapter records "now" as the cursor and returns *no* events. The first real call (one tick later) returns only events that arrived since enrollment.

`--backfill <window>` flag on `event-source add` opts into a single backfill pass. Adapter interprets the window (e.g., `--backfill 30d` → GitHub adapter queries events from 30 days ago). One-shot — subsequent ticks go forward only. Backfill events are written to the audit log with a `backfill=true` field so downstream agents can opt in to or out of processing them.

## Dispatcher refactor

What changes inside the daemon:

| Subsystem | Today | After |
|-----------|-------|-------|
| `POST /api/event` | Single entry point. Bearer-authenticated. Hits routing table. | Unchanged. Webhook path still POSTs here. |
| Static routing (`channels.yaml`) | (source, event_type) → target | Unchanged. Pollers emit through the same routing layer. |
| LLM dispatcher fallback | Composes brief, spawns recipe | Unchanged. |
| Idempotency | `<source>:<event_id>` dedupe window | Unchanged. Catches dupes from poll-vs-webhook hybrid. |
| Audit log (`events.db`) | One row per ingest | Unchanged; gains an `ingress` column (`poll` / `webhook`) for observability. |
| **Poller runtime** | — | New. Loads adapters by capability. Reads `~/.dispatcher/event-sources/*.yaml`. Schedules ticks. Calls `adapter.pull()`. Persists cursor. Forwards events to the routing layer (in-process, not via HTTP). |
| **Cursor store** | — | New. SQLite, one row per source. |
| **Adapter loader** | — | New. At boot, scans installed providers for `event-source:*` capabilities; registers each as a known system. |

What stays the same:

- `channels.yaml`, the LLM routing fallback, the recipe spawn path, taskpilot integration, the audit log, the bearer auth model for the webhook path.
- The existing `test-stream/calendar-check` and `manual/infra-survey` routes — both keep working without modification.

## Capability graph

```
mindframe
   ├── requires: event-routing (the dispatcher)
   ├── requires: agent-spawning, session-mesh, knowledge-base, ...
   └── optional: event-source:github
                 event-source:sentry
                 event-source:slack
                 event-source:gmail
                 event-source:pagerduty

dispatcher
   ├── provides: event-routing
   ├── requires: daemon, agent-spawning, session-mesh
   └── optional: deploy   # only used when operator opts into webhook transport

event-source:github   (provider plugin)
   ├── provides: event-source:github
   └── requires: (gh CLI or GITHUB_TOKEN — declared via environment.binary/env)

event-source:sentry   (provider plugin)
   ├── provides: event-source:sentry
   └── requires: SENTRY_AUTH_TOKEN
```

Adapter plugins look like any other provider in the marketplace. Their `setup` skill validates credentials and walks the operator through adding the first Event Source.

## User flow — `/mindframe:setup` after the migration

1. Bundle config (`deployment_name`, `vault_path`). Unchanged.
2. Walk the **optional** `event-source:*` capabilities mindframe declares. For each:
   - Ask: *"Watch <system>?"* (Y/N).
   - If yes: install the provider (resolver handles dependencies), run the provider's setup skill, add at least one Event Source.
3. Bootstrap KB from validated source systems. Unchanged.
4. ~~Wire dispatcher webhook ingress~~ — gone. The poller runtime is already running and the Event Sources added in step 2 are already producing events.
5. Smoke test — fire a synthetic event via `dispatcher event-source test <name>` (adapter implements a `test_event()` hook). Confirm the dispatcher routes, taskpilot spawns, the agent writes to the vault.

Result: the operator never opens a port, never configures a webhook URL, never edits DNS. Setup is "I have these credentials, watch these things." Cloud operators get the same flow plus an opt-in step that flips the webhook switch on supported adapters.

## Migration path

Phased so the existing dispatcher keeps working at every step.

1. **Land the poller runtime alongside the webhook path.** New subsystem, no removals. `~/.dispatcher/event-sources/` is empty; runtime no-ops. Existing `POST /api/event` paths are untouched.
2. **Land the cursor store and adapter loader.** Still no-op without adapters.
3. **Ship the first adapter — `event-source:github`.** Smallest surface, gentlest API, you already have `gh auth`. Validate end-to-end: enrol a source, watch a real PR fire a route. Write the adapter author guide from the experience.
4. **Ship `event-source:slack` and `event-source:gmail`.** Slack via the existing `slack` MCP's conversations.history API; Gmail via gmail-organizer's already-loaded creds. Both poll-only initially.
5. **Update mindframe's optional list and `/mindframe:setup`.** Setup gains the source-enrollment step; doctor learns to probe Event Sources.
6. **Webhook becomes opt-in.** Keep accepting `/api/event`; new sources default to `transport: auto` which picks poll when no public endpoint is configured. Old hand-rolled webhook routes (`source: test-stream`, `source: manual`) still work — they're unrelated to Event Sources, just direct POSTs.
7. (Eventually.) Add `handle_push()` to GitHub adapter so cloud installs can flip to `transport: webhook` or `both`. Same dispatcher binary, same routing, different transport.

No flag day. Webhook-only operators see no change. Poll operators get a new capability that didn't exist before.

## Open questions

- **Where do adapters live in the filesystem at runtime?** Today providers live under `~/.claude/plugins/cache/...`; the dispatcher would have to walk this on boot to discover capabilities. Cleaner: the softwaresoftware resolver could publish a capability index the dispatcher reads. Worth a short separate spec.
- **Adapter versioning.** What happens when an adapter's cursor format changes? Probably an explicit `cursor_version` in the stored row, with the adapter rejecting unknown versions. TBD.
- **Backpressure when the routing layer is overwhelmed.** Today there's no internal queue between ingest and spawn; a 200-event poll batch immediately tries to spawn 200 agents. Need a per-source concurrency cap and a small queue. Likely a follow-up spec once the runtime exists.
- **Multi-host poller coordination.** If a user runs the dispatcher on two laptops (rare) both will poll. For idempotent dedupe this is harmless wasted API calls. Worth flagging but not solving v1.
- **Webhook signature secrets at the adapter boundary.** Adapter's `handle_push` validates per-system signatures. Where does the secret live? Probably the same credentials_ref the poll path uses, augmented with a separate signing-key field. Adapter-specific; spec it per adapter.

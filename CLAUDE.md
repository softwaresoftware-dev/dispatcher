# CLAUDE.md — dispatcher

Provider plugin for the `event-routing` capability. Wraps the upstream `dispatcher-ingress` FastAPI service (`softwaresoftware-dev/dispatcher-ingress`) as a managed daemon installed on the customer's machine.

## What it does

- Installs dispatcher-ingress (clones the repo, creates a venv, installs deps)
- Generates a default `channels.yaml` routing table
- Registers the service with the `daemon` capability provider so it survives reboots
- Exposes ingress on the configured port; the service expects a bearer token on every POST
- Routes events: deterministic (`(source, event_type) → target`) when channels.yaml matches, LLM-routed via the dispatcher session otherwise

## Capability contract

Provides: `event-routing`. Consumer plugins (e.g. mindframe) declare `requires: ["event-routing"]` and use the dispatcher's HTTP API to register routes and POST events. The contract is HTTP, not skill-based — anything that speaks the same `/api/event` shape can substitute (Inngest, Temporal, AWS EventBridge, a customer's existing webhook router).

## Hard dependencies on other capabilities

- `daemon` — to run dispatcher-ingress as a managed background service
- `agent-spawning` — taskpilot's `spawner_cli.py` is the target for `spawn:<recipe>` routes
- `session-mesh` — session-bridge is the target for `session:<name>` routes

## Optional

- `deploy` — when present, `/dispatcher:setup` can put the dispatcher behind a public HTTPS URL (Nginx + Cloudflare Tunnel). Without it, the dispatcher is reachable only on the local network or via the customer's own ingress.

## Skills

- `/dispatcher:setup` — install + start the daemon on this machine
- `/dispatcher:route` — add a route to channels.yaml without hand-editing

## What's NOT here

- The dispatcher service code itself — that lives in `softwaresoftware-dev/dispatcher-ingress`. This plugin clones that repo at install time. Keeping them separate means dispatcher-ingress can be deployed as a standalone service in non-Claude contexts (k8s, CI, etc.).

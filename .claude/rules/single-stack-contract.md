---
description: Workspace-derived routing couplings the mindframe single-stack relies on
globs: ["app/*.py"]
---

# Single-stack runtime contract (dispatcher side)

dispatcher is the event ingress for mindframe's **single-stack** model: ONE
dispatcher serves every workspace by **deriving the workspace from the event
source**. Keep these in lockstep (all in `app/`):

- `event_sources.load_all_sources()` aggregates event-sources across partitions
  and **tags each with its workspace** (scans
  `DISPATCHER_WORKSPACES_ROOT/<id>/.mindframe/dispatcher/event-sources`).
- `poller.py` namespaces cursors by workspace and passes `workspace=` into routing.
- `core.route_event(workspace=)` resolves that workspace's `channels.yaml`
  (`channels.lookup_route(channels_file=)`) + recipes, and spawns with the
  workspace's HOME via `spawn_helper.spawn_recipe(home=)` → **taskpilot's per-task
  HOME**.
- `db.py` events table carries a `workspace` column (`main._log_event`).

`DISPATCHER_WORKSPACES_ROOT` is set by **mindframe** (`setup/install.txt` §3.6a +
the dev harness `disp_env`), not by generic dispatcher setup. The spawn `home`
pairs with taskpilot's per-task `$HOME` — change them together.

Full replication map + sync checklist live in the mindframe repo:
`plugins/frameworks/mindframe/docs/single-stack-contract.md`.

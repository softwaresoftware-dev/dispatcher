---
name: route
description: Add or remove a route in the dispatcher's channels.yaml. Routes map (source, event_type) pairs to a target — either a long-running session or a spawn:recipe that creates an ephemeral agent per event. Use when the user says "route X events to Y", "add a route for Sentry", "wire up GitHub PR comments", or when another skill (e.g. mindframe setup) needs to install a route.
---

# Dispatcher — Add/Remove Route

The dispatcher's static routing table lives at `$INSTALL_DIR/channels.yaml` (where `INSTALL_DIR` is `CLAUDE_PLUGIN_OPTION_INSTALL_DIR`, default `~/.dispatcher`). This skill edits it safely and, if a daemon provider is available, restarts the dispatcher so changes take effect.

## Inputs to gather (ask the user if missing)

- **source** — string identifying the event origin, e.g. `sentry`, `github`, `softwaresoftware-relay`. The integration must POST with this in its event payload.
- **event_type** — string. Optional — omit to match any event_type from that source.
- **target** — `session:<name>` (forward to a long-running session via session-bridge) or `spawn:<recipe>` (taskpilot spawns a fresh agent per event using `~/.dispatcher/recipes/<recipe>/`).

## Steps

1. **Read the current `channels.yaml`.** Parse it. If absent, treat as `{routes: []}`.
2. **Append (or remove) the route.** Preserve existing routes; do not reorder. Use a clear, unique route — if the same `(source, event_type)` already maps to the same target, no-op and tell the user.
3. **For `spawn:<recipe>` targets,** verify `$INSTALL_DIR/recipes/<recipe>/` exists. If not, scaffold it (mkdir + an empty `recipe.yaml`, `brief.json`, and `CLAUDE.md` placeholder) and tell the user the recipe still needs to be filled in before spawned agents will be useful.
4. **Restart the dispatcher daemon.** Use an available skill or tool from the `daemon` capability provider. Pass it the service name `dispatcher`. If no daemon provider is available, tell the user to restart it manually.
5. **Confirm the change.** Re-read `channels.yaml` and echo back the final routes table.

## `spawn:` routes — brief overrides vs. event data

A `spawn:<recipe>` route may carry a `brief:` block that fills the recipe's `brief.json` `{{placeholders}}`. There are two distinct substitution surfaces, and they do NOT cross:

- **Route `brief:` overrides** resolve only `{event_id}` and `{task_id}`. They CANNOT reference event `data` fields. A route like `brief: { meeting_title: "{meeting_title}" }` expecting dispatcher to pull `data.meeting_title` is wrong — the literal string `{meeting_title}` passes straight through, and the recipe's `{{meeting_title}}` renders as that literal text. Brief overrides are for static, route-authored context only.
- **The event `data`** reaches the spawned agent ONLY via `{payload}` in the recipe's `starter_prompt`, where it is substituted as the full event `data` rendered as pretty JSON. To act on event fields, the recipe's `starter_prompt` must parse them out of `{payload}`.

### Worked example — meeting-prep done correctly

Route in `channels.yaml`:

```yaml
routes:
  - source: calendar
    event_type: meeting-prep
    target: spawn:meeting-prep
    # brief: overrides are optional and only for static context;
    # do NOT try to map event data fields here.
```

An event the poller ingests (e.g. from a calendar event-source):

```json
{"source": "calendar", "event_type": "meeting-prep",
 "data": {"meeting_title": "Q3 review", "attendees": ["a@x.com"]}}
```

Recipe `recipe.yaml` reads the data via `{payload}`:

```yaml
starter_prompt: |
  A meeting needs prep. The full calendar event is below as JSON.
  Read meeting_title and attendees from it and produce a prep doc.

  Event data:
  {payload}
```

## Notes

- This skill writes to a config file; it does not touch the dispatcher's audit log or state.
- For removal: ask the user which route to remove if the inputs are ambiguous (multiple matches).

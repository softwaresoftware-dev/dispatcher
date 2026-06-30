---
name: setup
description: Install and start the dispatcher event-routing daemons on this machine — sets up a Python venv, writes a default channels.yaml, registers the poller and the ingress with the daemon capability provider, and verifies health. Use when setting up event-routing for a new mindframe deployment, or whenever dispatcher needs to be (re)installed on this machine.
---

# Dispatcher — Setup

You are installing the dispatcher event-routing daemons on this machine. The
code is **bundled in this plugin** (`${CLAUDE_PLUGIN_ROOT}/app/`) — there is
nothing to clone. Setup creates a runtime directory and a venv, then registers
two managed daemons: the **poller** (poll-first ingestion, the primary path)
and the **ingress** service (`/api/direct`, `/api/events`, `/api/health`).

The customer has provided a bearer token in `CLAUDE_PLUGIN_OPTION_BEARER_TOKEN`.
Other config:

- `CLAUDE_PLUGIN_OPTION_INSTALL_DIR` (default `~/.dispatcher`) — the runtime
  directory: venv, channels.yaml, recipes, agents, and the audit DB.
- `CLAUDE_PLUGIN_OPTION_PORT` (default `8911`)
- `CLAUDE_PLUGIN_OPTION_SESSION_BRIDGE_URL` (default `http://127.0.0.1:8910`)

If `CLAUDE_PLUGIN_OPTION_BEARER_TOKEN` is empty, stop and tell the user to set
`pluginConfigs.dispatcher.options.bearer_token` in `~/.claude/settings.json`,
then run `/reload-plugins`.

## Steps

1. **Create the runtime directory and a Python venv.** Make `$INSTALL_DIR`,
   then a venv at `$INSTALL_DIR/venv` and install the bundled requirements:
   ```bash
   mkdir -p "$INSTALL_DIR"
   python3 -m venv "$INSTALL_DIR/venv"
   "$INSTALL_DIR/venv/bin/pip" install -r "${CLAUDE_PLUGIN_ROOT}/requirements.txt"
   ```

2. **Write a default `channels.yaml`** at `$INSTALL_DIR/channels.yaml` if missing:
   ```yaml
   # routes: list of (source, event_type) → target rules
   # target shapes: session:<name> | spawn:<recipe>
   routes: []
   ```
   Do not overwrite if the file exists.

3. **Compose the start command.** The service code is bundled in the plugin —
   run it from there:
   ```
   $INSTALL_DIR/venv/bin/uvicorn app.main:app --port $PORT --host 127.0.0.1
   ```
   Working directory: `${CLAUDE_PLUGIN_ROOT}`.
   Environment:
   - `DISPATCHER_INGEST_TOKEN=$BEARER_TOKEN`
   - `SESSION_BRIDGE_URL=$SESSION_BRIDGE_URL`
   - `DISPATCHER_CHANNELS_FILE=$INSTALL_DIR/channels.yaml`
   - `DISPATCHER_RECIPES_DIR=$INSTALL_DIR/recipes`

4. **Register the service as a managed daemon.** Use an available skill or tool
   from the `daemon` capability provider — pass it the start command, working
   directory, environment variables, and a stable service name like
   `dispatcher`. The provider handles whether it lands as a systemd unit,
   launchd plist, or Task Scheduler job.

5. **Verify health.** After the daemon reports running, POST to
   `http://127.0.0.1:$PORT/api/health`. Expect 200. If it doesn't come up
   within 10s, tail the daemon's stderr/stdout via the daemon provider's status
   tool and report what failed.

6. **Register the poller as a second managed daemon.** Poll-first ingestion is
   the primary path — without it, Event Source declarations never feed events
   in. Command:
   ```
   $INSTALL_DIR/venv/bin/python -m app.poller
   ```
   Working directory: `${CLAUDE_PLUGIN_ROOT}`. Environment:
   - `DISPATCHER_CHANNELS_FILE=$INSTALL_DIR/channels.yaml`
   - `DISPATCHER_RECIPES_DIR=$INSTALL_DIR/recipes`
   - `DISPATCHER_EVENT_SOURCES_DIR=$INSTALL_DIR/event-sources`
   - optionally `DISPATCHER_POLL_INTERVAL_S` (default 60)
   Service name: `dispatcher-poller`. Also `mkdir -p $INSTALL_DIR/event-sources`
   so the operator (or a mindframe agent) has somewhere to drop declarations.
   Verify by tailing the daemon's log for a `poller started` line; a tick
   against zero sources is a clean no-op.

7. **No public URL needed.** Ingestion is poll-first — the dispatcher pulls
   from each source on an interval, so nothing inbound needs exposing. The
   ingress endpoints (`/api/direct`, `/api/events`, `/api/health`) are
   operator-facing on localhost.

## What to report back

A short summary: runtime dir, port, public URL (if set), and whether the health
check passed. The user wants to know it's running, not the install transcript.

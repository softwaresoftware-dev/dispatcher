---
name: setup
description: Install and start dispatcher-ingress as a managed daemon. Clones the upstream service, sets up a Python venv, writes a default channels.yaml, registers with the daemon capability provider, and verifies the health endpoint. Use when setting up event-routing for a new mindframe deployment, or whenever dispatcher needs to be (re)installed on this machine.
---

# Dispatcher — Setup

You are installing the dispatcher event-routing daemon on this machine. The customer has provided a bearer token in `CLAUDE_PLUGIN_OPTION_BEARER_TOKEN`. Other config:

- `CLAUDE_PLUGIN_OPTION_INSTALL_DIR` (default `~/.dispatcher`)
- `CLAUDE_PLUGIN_OPTION_PORT` (default `8911`)
- `CLAUDE_PLUGIN_OPTION_SESSION_BRIDGE_URL` (default `http://127.0.0.1:8910`)

If `CLAUDE_PLUGIN_OPTION_BEARER_TOKEN` is empty, stop and tell the user to set `pluginConfigs.dispatcher.options.bearer_token` in `~/.claude/settings.json`, then run `/reload-plugins`.

## Steps

1. **Clone the dispatcher-ingress repo** into `$INSTALL_DIR/src` if it does not already exist:
   ```bash
   git clone https://github.com/softwaresoftware-dev/dispatcher-ingress.git "$INSTALL_DIR/src"
   ```
   If it does exist, `git -C "$INSTALL_DIR/src" pull --ff-only`.

2. **Create a Python venv** at `$INSTALL_DIR/venv` and install requirements:
   ```bash
   python3 -m venv "$INSTALL_DIR/venv"
   "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/src/requirements.txt"
   ```

3. **Write a default `channels.yaml`** at `$INSTALL_DIR/channels.yaml` if missing:
   ```yaml
   # routes: list of (source, event_type) → target rules
   # target shapes: session:<name> | spawn:<recipe>
   routes: []
   ```
   Do not overwrite if the file exists.

4. **Compose the start command**:
   ```
   $INSTALL_DIR/venv/bin/uvicorn app.main:app --port $PORT --host 127.0.0.1
   ```
   Working directory: `$INSTALL_DIR/src`.
   Environment:
   - `DISPATCHER_INGEST_TOKEN=$BEARER_TOKEN`
   - `SESSION_BRIDGE_URL=$SESSION_BRIDGE_URL`
   - `DISPATCHER_CHANNELS_FILE=$INSTALL_DIR/channels.yaml`
   - `DISPATCHER_RECIPES_DIR=$INSTALL_DIR/recipes`

5. **Register the service as a managed daemon.** Use an available skill or tool from the `daemon` capability provider — pass it the start command, working directory, environment variables, and a stable service name like `dispatcher`. The provider handles whether it lands as a systemd unit, launchd plist, or Task Scheduler job.

6. **Verify health.** After the daemon reports running, POST to `http://127.0.0.1:$PORT/api/health`. Expect 200. If it doesn't come up within 10s, tail the daemon's stderr/stdout via the daemon provider's status tool and report what failed.

7. **Public URL (optional).** If a `deploy` capability provider is loaded, ask the user whether to expose the dispatcher publicly. If yes, use that provider's deploy skill to put `127.0.0.1:$PORT` behind an HTTPS hostname. Save the resulting URL where the user can find it (suggest writing it into the project's CLAUDE.md or the customer's vault). If no `deploy` provider is loaded, skip this step and tell the user the dispatcher is reachable only on the local network — they need their own ingress to receive Sentry/GitHub/etc. webhooks.

## What to report back

A short summary: install dir, port, public URL (if set), and whether health check passed. That's it — the user wants to know it's running, not the install transcript.

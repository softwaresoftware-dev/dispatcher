PORT := 8911
POLL_INTERVAL ?= 60
APP_DIR := $(shell dirname $(realpath $(lastword $(MAKEFILE_LIST))))

.PHONY: start stop status test poll poll-once poll-stop poll-status

start:
	cd $(APP_DIR) && nohup python -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT) > /tmp/dispatcher-ingress.log 2>&1 &
	@echo "started on port $(PORT)"

stop:
	@pkill -f "uvicorn app.main:app.*$(PORT)" 2>/dev/null || echo "not running"

status:
	@curl -sf http://127.0.0.1:$(PORT)/api/health -o /dev/null && echo "running on port $(PORT)" || echo "not running"

# Poll-first ingestion — the primary path. Runs alongside (not instead of) the
# ingress; the ingress now serves /api/direct, /api/events, /api/health and the
# deprecated /api/event.
poll:
	cd $(APP_DIR) && DISPATCHER_POLL_INTERVAL_S=$(POLL_INTERVAL) nohup python -m app.poller > /tmp/dispatcher-poller.log 2>&1 & echo $$! > /tmp/dispatcher-poller.pid
	@echo "poller started (interval $(POLL_INTERVAL)s) — log: /tmp/dispatcher-poller.log"

poll-once:
	cd $(APP_DIR) && python -m app.poller --once

# Scoped to the pidfile — never pkill -f a broad pattern (it would also match
# an editor open on app/poller.py).
poll-stop:
	@test -f /tmp/dispatcher-poller.pid && kill "$$(cat /tmp/dispatcher-poller.pid)" 2>/dev/null && rm -f /tmp/dispatcher-poller.pid && echo "poller stopped" || echo "poller not running"

poll-status:
	@test -f /tmp/dispatcher-poller.pid && kill -0 "$$(cat /tmp/dispatcher-poller.pid)" 2>/dev/null && echo "poller running (pid $$(cat /tmp/dispatcher-poller.pid))" || echo "poller not running"

test:
	python -m pytest tests/ -v

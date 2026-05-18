PORT := 8911
APP_DIR := $(shell dirname $(realpath $(lastword $(MAKEFILE_LIST))))

.PHONY: start stop status test

start:
	cd $(APP_DIR) && nohup python -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT) > /tmp/dispatcher-ingress.log 2>&1 &
	@echo "started on port $(PORT)"

stop:
	@pkill -f "uvicorn app.main:app.*$(PORT)" 2>/dev/null || echo "not running"

status:
	@curl -sf http://127.0.0.1:$(PORT)/api/health -o /dev/null && echo "running on port $(PORT)" || echo "not running"

test:
	python -m pytest tests/ -v

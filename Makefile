# Dispatch v0 · run the Northwind demo locally
# Needs Python 3.11+ and Node 20+. No database server: SQLite in data/runtime.

PY ?= python
API_DIR = apps/api
UI_DIR = apps/control-room

.PHONY: install api ui demo reset check

install:
	cd $(API_DIR) && $(PY) -m pip install -e .
	cd $(UI_DIR) && npm install

api:
	cd $(API_DIR) && $(PY) -m uvicorn dispatch.main:app --host 127.0.0.1 --port 8787

ui:
	cd $(UI_DIR) && npm run dev

# load the dataset and queue the overnight inbox (the API must be running)
demo:
	curl -s -X POST http://127.0.0.1:8787/api/demo/reset >/dev/null
	curl -s -X POST http://127.0.0.1:8787/api/demo/run
	@echo "\nOpen http://localhost:3100"

reset:
	curl -s -X POST http://127.0.0.1:8787/api/demo/reset

check:
	curl -s http://127.0.0.1:8787/api/health

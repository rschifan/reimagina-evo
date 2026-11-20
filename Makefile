# reimagina-evo — developer Makefile.
#
# One-command shortcuts for the day-to-day loop. Runs locally what CI
# will run on push, so "make check" passing is a strong signal your PR
# will be green. Everything here should be safe to run on any machine
# with the repo, .venv, and apps/web/node_modules installed.

.DEFAULT_GOAL := help
SHELL := /bin/bash

# Use `uv run` — it resolves to the project venv, installs missing deps,
# and works the same on every machine without each contributor having to
# remember `source .venv/bin/activate` first.
PYTHON := uv run python
PYTEST := uv run pytest
# Both linters go through uv, for the same reason as everything else here: the
# version that runs is the one in uv.lock. This used to call bare `ruff` on the
# assumption it was "commonly installed globally" — which meant `make lint` and
# CI agreed only as long as your global ruff happened to match the workflow's
# pin. A formatter whose output *is* the gate cannot be left to happenstance.
MYPY := uv run mypy
RUFF := uv run ruff
# Same reason again, and this one was a real break rather than a latent one:
# bare `uvicorn` resolves to whichever install happens to be first on PATH, and
# on a machine with a system-wide Python that is not the project venv — so the
# server started under an interpreter with none of the project's dependencies
# and died on `import dbos`.
UVICORN := uv run uvicorn

# ─── Help ────────────────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "; printf "\nUsage: make \033[36m<target>\033[0m\n\nTargets:\n"} \
	     /^[a-zA-Z_.-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ─── Install ─────────────────────────────────────────────────────────────────

.PHONY: install
install: ## Install Python + web dependencies.
	uv sync --locked --extra dev
	cd apps/web && npm ci

# ─── Checks (match CI) ───────────────────────────────────────────────────────

.PHONY: lint
lint: ## Run ruff check + format-check + mypy (mirrors CI).
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY) packages/py/reimagina apps scripts

.PHONY: format
format: ## Reformat with ruff format (writes changes).
	$(RUFF) format .

.PHONY: test
test: ## Run fast Python tests (unit + contract).
	$(PYTEST) tests/unit tests/contract -q

.PHONY: test-integration
test-integration: ## Run integration tests (requires Postgres + PostGIS running).
	$(PYTEST) tests/integration -q

.PHONY: test-conformance
test-conformance: ## Assert the Orchestrator port contract (requires Postgres).
	$(PYTEST) tests/conformance -q --no-header -rA

.PHONY: test-e2e
test-e2e: ## Open every page in a real browser (needs the API + web dev server).
	REIMAGINA_E2E=1 $(PYTEST) tests/e2e -q --no-header -rs

.PHONY: test-recovery
test-recovery: ## Kill a running executor and prove it resumes (needs >=20k blocks).
	REIMAGINA_RECOVERY_TEST=1 $(PYTEST) tests/recovery -q --no-header -rs

.PHONY: test-workloads
test-workloads: ## Measure how this ops plane schedules (see tests/workloads/README.md).
	REIMAGINA_BENCH=1 $(PYTEST) tests/workloads -q --no-header -rA

.PHONY: build-web
build-web: ## Typecheck + build the web app.
	cd apps/web && npm run build

.PHONY: test-web
test-web: ## Run the web app's vitest suite.
	cd apps/web && npm test

.PHONY: check
check: lint test build-web test-web ## Lint + the DB-free suites + web. Run before opening a PR.
	@echo
	@echo 'Note: CI also runs `make test-integration` and `make test-conformance`,'
	@echo 'which need Postgres. Run those too before pushing anything that'
	@echo 'touches the data or ops-plane layers.'

# ─── Bootstrap (data ingest entrypoints) ─────────────────────────────────────

.PHONY: migrate
migrate: ## Run all DB migrations on $DATABASE_URL (see .env), creating the DBs if needed.
	$(PYTHON) -m apps.cli.app bootstrap-db --with-test-db

.PHONY: verify-sources
verify-sources: ## Smoke-test that every upstream source is reachable.
	$(PYTHON) -m apps.cli.app data verify-sources

# ─── Dev servers ─────────────────────────────────────────────────────────────

.PHONY: stack-up
stack-up: ## Bring up the local Docker stack (Postgres + pgbouncer + Martin).
	docker compose -f infra/docker/docker-compose.yml --env-file .env up -d

.PHONY: stack-down
stack-down: ## Tear down the local Docker stack (preserves volumes).
	docker compose -f infra/docker/docker-compose.yml down

# The API port is a variable because 8000 is not available on every machine —
# another project owns it here, which is why `vite.config.ts` deliberately
# proxies to 8001 instead. Override either for a one-off:
#   make dev API_PORT=8123 WEB_PORT=5199
# `dev` exports the chosen API port to Vite as REIMAGINA_API_URL, so the proxy
# follows it. It did not use to: the target was pinned at 8001, and overriding
# API_PORT meant the web app silently proxied to whatever else was on 8001 —
# or to nothing — while appearing to work.
API_PORT ?= 8001
# Vite's port, passed through to `vite.config.ts` so this and the banner
# cannot disagree with where the server actually listens.
WEB_PORT ?= 5173

.PHONY: check-ports
check-ports: ## Fail early if the dev ports are taken, naming what holds them.
	@$(PYTHON) scripts/preflight_ports.py $(API_PORT):API $(WEB_PORT):Web

.PHONY: dev
# Python loads dotenv without overriding caller exports or executing shell text.
# Tile routing is deliberately /api/tiles unless the caller exports an override.
DEV_ENV = _dev_exports="$$( $(PYTHON) scripts/runtime_env.py dev )" && eval "$$_dev_exports"

dev: check-ports ## Start the API, the web dev server and the worker together. Ctrl-C stops all three.
	@$(PYTHON) scripts/dev_supervisor.py --python '$(PYTHON)' --uvicorn '$(UVICORN)' --api-port '$(API_PORT)' --web-port '$(WEB_PORT)'

.PHONY: dev-api
dev-api: ## Start the FastAPI read-side API with reload (same DSN and tiles as `make dev`).
	@$(PYTHON) scripts/preflight_ports.py $(API_PORT):API
	@$(DEV_ENV) && \
	$(UVICORN) apps.api.app:app --reload --host 127.0.0.1 --port $(API_PORT)

.PHONY: dev-web
dev-web: ## Start the Vite dev server for the web app (proxies /api to API_PORT).
	@$(PYTHON) scripts/preflight_ports.py $(WEB_PORT):Web
	cd apps/web && VITE_PORT=$(WEB_PORT) REIMAGINA_API_URL=http://127.0.0.1:$(API_PORT) npm run dev

.PHONY: dev-worker
dev-worker: ## Start the DBOS ops-plane worker (runs stay queued without it).
	@$(DEV_ENV) && $(PYTHON) -m apps.worker

# ─── Git / PR workflow ───────────────────────────────────────────────────────
# Usage: `make branch NAME=fix/indicator-formatting`
#        `make pr`                (opens PR with gh; title from last commit)
#        `make pr TITLE="fix: ..." BODY="..."`

.PHONY: branch
branch: ## Create a new branch off latest main. NAME=feat/short-name
	@if [ -z "$(NAME)" ]; then echo "Usage: make branch NAME=feat/short-name"; exit 1; fi
	git checkout main && git pull --ff-only
	git checkout -b $(NAME)

.PHONY: push
push: ## Push current branch with upstream tracking.
	git push -u origin HEAD

.PHONY: pr
pr: push ## Push + open a PR on GitHub. Optional TITLE and BODY.
	@if [ -n "$(TITLE)" ]; then \
	  gh pr create --title "$(TITLE)" --body "$(BODY)"; \
	else \
	  gh pr create --fill; \
	fi

.PHONY: pr-merge
pr-merge: ## Squash-merge the current branch's PR and delete the branch.
	gh pr merge --squash --delete-branch

.PHONY: ship
ship: check pr ## `check` + open PR. Use when you're confident.

# ─── Housekeeping ────────────────────────────────────────────────────────────

.PHONY: clean
clean: ## Remove build artifacts and caches.
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
	rm -rf apps/web/dist

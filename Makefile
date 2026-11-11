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
	uv sync --extra dev
	cd apps/web && npm install

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
migrate: ## Run all DB migrations on the configured DATABASE_URL.
	$(PYTHON) -m apps.cli.app bootstrap-db

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
# proxies to 8001 instead. Override for a one-off: `make dev API_PORT=8123`.
# The Vite proxy target still points at 8001, so a different port here means
# hitting the API directly rather than through the web app.
API_PORT ?= 8001

.PHONY: dev
# ⚠ Deliberately NO `set -m` here, which is the opposite of the obvious fix.
#
# Ctrl-C is what stops this, and Ctrl-C signals the terminal's whole foreground
# process group. Left alone, every process here — the recipe shell, `uv`, the
# python it spawns, `npm`, the vite it spawns — shares that group, so all of
# them receive the interrupt directly and shut down on their own.
#
# `set -m` breaks exactly that. It gives each background job its own process
# group, which isolates the servers from the terminal's signal and leaves the
# trap as the only thing that can stop them — and a trap cannot reliably reach
# them, because neither job IS the server: `uv run uvicorn` spawns a child
# python, `npm run dev` spawns a child vite. Measured, both ways: with `set -m`
# and a group kill, vite died and uvicorn kept holding its port.
#
# The trap that remains is for the other exit: if one server dies on its own,
# `wait` returns and it tears the survivor down rather than leaving it running.
#
# ⚠ The DSN is read from `.env`'s DATABASE_URL when REIMAGINA_APP_DSN is unset.
# The API reads REIMAGINA_APP_DSN (apps/api/deps.py) and `.env` carries
# DATABASE_URL, so a machine with a perfectly good `.env` still had to pass the
# DSN by hand — while `scripts/acceptance_run.sh` and `scripts/mexicali_run.sh`
# both source `.env` and need no argument. One command should mean one command.
#
# It fails loudly rather than defaulting: the API's own fallback DSN points at a
# database called `reimagina`, and silently serving an empty or wrong database
# looks like a broken frontend, not a missing variable.
#
# ⚠ Tiles are pointed at the API, not at Martin, and without this `make dev`
# produces a blank map.
#
# `tile_live_base_url()` defaults to `http://localhost:3000/get_tile` — Martin —
# because Martin is the recommended production path. But `make dev` starts two
# processes and Martin is not one of them, so every tile request went to a
# server nobody had started and the map drew nothing. The console reads
# `AJAXError: Failed to fetch (0) http://localhost:3000/get_tile/...`, which is
# easy to mistake for a data problem.
#
# `/api/tiles` is relative on purpose. The absolute form
# `http://localhost:8001/tiles` is cross-origin from Vite's 5173 and fails CORS
# — measured earlier at 53 requests, 53 `net::ERR_FAILED`, with no console
# error object to find them by. Relative goes through Vite's `/api` proxy,
# which strips the prefix, so the API's MVT router sees
# `/tiles/get_tile/{z}/{x}/{y}` — Martin's own contract, which is why that
# router exists.
#
# Assigned with `:=` so an explicit value still wins, and Martin goes back with
# `REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:3000/get_tile make dev`.
#
# ⚠ The value is captured BEFORE `.env` is sourced and restored after, so
# only the invoking environment can override it. Without that, a `.env`
# carrying `REIMAGINA_TILE_LIVE_BASE_URL` wins over the default and `make dev`
# quietly points the map at Martin again — the exact failure this block
# exists to prevent, reintroduced by the line two above it. `.env` is for
# machine configuration like the DSN; which tile server a *development* run
# talks to is this target's business, and it starts only one of them.
dev: ## Start the API and the web dev server together. Ctrl-C stops both.
	@_tiles_from_env="$$REIMAGINA_TILE_LIVE_BASE_URL"; \
	if [ -z "$$REIMAGINA_APP_DSN" ] && [ -f .env ]; then \
	  set -a; . ./.env; set +a; \
	  REIMAGINA_APP_DSN="$$DATABASE_URL"; export REIMAGINA_APP_DSN; \
	fi; \
	REIMAGINA_TILE_LIVE_BASE_URL="$$_tiles_from_env"; \
	if [ -z "$$REIMAGINA_APP_DSN" ]; then \
	  echo "REIMAGINA_APP_DSN is unset and .env has no DATABASE_URL."; \
	  echo "  REIMAGINA_APP_DSN=postgresql://localhost/your_db make dev"; \
	  exit 1; \
	fi; \
	: $${REIMAGINA_TILE_LIVE_BASE_URL:=/api/tiles}; \
	export REIMAGINA_TILE_LIVE_BASE_URL; \
	echo "DSN  → $$REIMAGINA_APP_DSN"; \
	echo "Tiles→ $$REIMAGINA_TILE_LIVE_BASE_URL"; \
	echo "API  → http://127.0.0.1:$(API_PORT)"; \
	echo "Web  → http://localhost:5173  (proxies /api to the API)"; \
	echo; \
	trap 'kill $$api $$web 2>/dev/null' EXIT; \
	$(UVICORN) apps.api.app:app --reload --host 127.0.0.1 --port $(API_PORT) & api=$$!; \
	( cd apps/web && npm run dev ) & web=$$!; \
	wait

.PHONY: dev-api
dev-api: ## Start the FastAPI read-side API with reload.
	$(UVICORN) apps.api.app:app --reload --host 127.0.0.1 --port $(API_PORT)

.PHONY: dev-web
dev-web: ## Start the Vite dev server for the web app.
	cd apps/web && npm run dev

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

# ─── Release ─────────────────────────────────────────────────────────────────
# Usage: `make release VERSION=v0.2.0`

.PHONY: release
release: ## Tag + publish a GitHub release. VERSION=v0.2.0
	@if [ -z "$(VERSION)" ]; then echo "Usage: make release VERSION=v0.2.0"; exit 1; fi
	@if [ "$$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then \
	  echo "release must be cut from main (you are on $$(git rev-parse --abbrev-ref HEAD))"; exit 1; \
	fi
	git pull --ff-only
	git tag -a $(VERSION) -m "$(VERSION)"
	git push origin $(VERSION)
	gh release create $(VERSION) --generate-notes --title "$(VERSION)"

# ─── Housekeeping ────────────────────────────────────────────────────────────

.PHONY: clean
clean: ## Remove build artifacts and caches.
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
	rm -rf apps/web/dist

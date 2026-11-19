---
title: "Technical documentation"
subtitle: "Reimagina · Conceptualización y diseño de una centralidad urbana en Mexicali — Schedule A, Work 4"
author: "Rossano Schifanella"
date: "20 November 2026"
---

## Document control

| | |
|---|---|
| **Project** | Conceptualización y diseño de una centralidad urbana en Mexicali |
| **Deliverable** | Schedule A, Work 4 — Technical documentation |
| **Version** | 1.0 |
| **Status** | Issued |
| **Audience** | A technically competent reader installing, configuring and operating the platform |

| Version | Date | Author | Summary |
|---|---|---|---|
| 0.1 | 2026-11-08 | Rossano Schifanella | First complete draft |
| 0.2 | 2026-11-08 | Rossano Schifanella | Added the credential-free install path (§7.1) and the acceptance run |
| 0.3 | 2026-11-09 | Rossano Schifanella | How to verify the orchestration module (§6.1) |
| 0.4 | 2026-11-10 | Rossano Schifanella | Verifying live tiles with or without Martin (§6.2); using the map, including the keyboard surface; corrected DSN ports (§4.1); re-measured install appendix |
| 0.5 | 2026-11-11 | Rossano Schifanella | Re-issued with the 11 November delivery |
| 0.6 | 2026-11-11 | Rossano Schifanella | §5 leads with `make dev`, the single command for the read side, and says why the dev split is not architecture |
| 0.7 | 2026-11-13 | Rossano Schifanella | Install appendix re-measured at `r049`; acceptance run timing and the scenario-authoring steps brought up to date; a note on which figures were re-measured |
| 0.8 | 2026-11-18 | Rossano Schifanella | §5: `make dev` now starts the worker too; the separate-terminal path uses `make dev-worker` |
| 0.9 | 2026-11-19 | Rossano Schifanella | §1 names the scenario workspace (`/scenarios.html`); §10 re-measured at `r050` from a fresh clone, with the run's deviations from a fresh machine stated |
| 0.10 | 2026-11-19 | Rossano Schifanella | §7.2: the load gives the scenario inputs a baseline (census household size, Overture storeys, census-fitted synthetic values) |
| 1.0 | 2026-11-20 | Rossano Schifanella | Issued. Prerequisites corrected (Python 3.12, the Node versions the web app accepts, GDAL only for PMTiles export); every Python command runs through `uv run`; ports and `API_PORT`/`WEB_PORT` (§1, §4.2, §5); every environment variable with its default (§4); tile serving under `make dev` (§6.2); the crash-recovery remedy corrected (§6.1); a complete data-load runbook (§7.2); an operating chapter for each page and every CLI group (§8); troubleshooting renumbered (§9); §10 re-measured at `r051` |

### Provenance of the figures

Re-measured on 19 November 2026, at migration `r051`: the install appendix in
§10 (revision, table count, every test count), the acceptance run's duration
in §7.1 and the size and generation time of the recovery territory in §6.1.
Carried forward from an earlier measurement and not re-measured for this
revision: the tile timing in §6.2 and the third-party dependency inventory.

### About this document

This document covers installation, configuration and operation. It assumes
familiarity with PostgreSQL, Python and the command line, but no prior
knowledge of this system.

The commands were executed against a clean clone of the delivered revision;
§10 records that run, and §9 the failures met while writing it and what they
mean.

Architecture is Work 1; the schema and endpoint reference is Work 3.

---

## 1. What you will end up with

A working installation is a database, three processes — the API, the web
application and the worker — and, in the container stack, two optional tile
hosts:

| Part | Address | Purpose |
|---|---|---|
| API | `http://localhost:8001` (`API_PORT`) | Features, indicators, downloads, scenarios, run control; OpenAPI at `/docs` |
| Web application | `http://localhost:5173` (`WEB_PORT`) | Map (`/`), scenario workspace (`/scenarios.html`), indicator catalogue (`/catalog.html`), run dashboard (`/ops.html`). Press `⌘K` for the command palette, `?` for the shortcut list |
| Worker | no port | Executes the runs the API and the CLI submit. Without it runs stay queued |
| Vector tiles, from the API | `/api/tiles` through the web application; `http://localhost:8001/tiles` directly | Live MVT tiles served by the API itself. What `make dev` uses (§6.2) |
| Vector tiles, from Martin | `http://localhost:3000` | The same tiles from a dedicated tile server. Path A only, optional (§6.2) |
| Snapshot tiles | `http://localhost:3001` | PMTiles exports of stable reference layers. Path A only |
| Database, pooled | `localhost:5432` | Path A: the **connection pooler**, holding one database only. The API reads through it. Path B: PostgreSQL itself |
| Database, direct | `localhost:5433` | Path A only: PostgreSQL behind the pooler. Migrations, bootstrap, the test suites and the ops plane all connect here, not to `5432` — §4.1 says why |

`8001` and `5173` are defaults. Every `make` target that starts a server
takes `API_PORT` and `WEB_PORT`, and checks the ports are free before it
starts (§5).

An installation without access to the source data lake still runs: you can
install, migrate, start every process, run the test suites, build the web
application and load a generated territory (§7.1). Loading the real territory
needs the lake mirror and internet access (§7.2).

## 2. Prerequisites

| Requirement | Version | Why |
|---|---|---|
| Python | 3.12 or later | The backend (`requires-python = ">=3.12"`). `uv` provides it if the system has none |
| `uv` | recent | Creates the project environment from `uv.lock` |
| Node.js | `^22.22.2 \|\| ^24.15.0 \|\| >=26` | The web application (`engines` in `apps/web/package.json`); 23.x and 25.x are not accepted. CI uses 22 |
| PostgreSQL | 16 or later | The database. The container stack and CI use 16 |
| PostGIS | 3.4 or later | All spatial types and functions |
| pgRouting | 3.6 or later | Network-distance indicators (migration `r037`) |
| GDAL (`ogr2ogr`) | 3.8 or later | **Only** for `tiles export` (PMTiles snapshots), which needs the PMTiles driver. The Python packages (`rasterio`, `pyogrio`) bundle their own GDAL |
| Docker with Compose | recent | **Optional**: only for the container stack (Path A, §3.1) |
| `make`, `bash`, `curl` | any | The `make` targets and scripts below |

> **PostGIS and pgRouting are not in the Ubuntu archive.** They come from the
> PostgreSQL Global Development Group repository. pgRouting is also not a
> *trusted* extension, so `CREATE EXTENSION` requires a superuser — see §9.4.

**Every Python command in this document runs through `uv run`**, which uses the
project environment `make install` creates in `.venv`. A bare `python` or
`pytest` resolves to whatever interpreter is first on `PATH`, which does not
have the project's dependencies (or, on many systems, does not exist). If you
prefer, run `source .venv/bin/activate` once per shell and drop the `uv run`
prefix.

## 3. Installation

### 3.1 Choose a database path

Two paths are supported, and the choice only affects where PostgreSQL comes
from.

**Path A — container stack.** Brings up PostgreSQL with PostGIS and
pgRouting, a connection pooler, Martin and a static tile host:

```bash
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD, and the same password in the five DSNs
make stack-up
```

The password appears six times in `.env`: once in `POSTGRES_PASSWORD` and
once in each of the five DSNs (§4.1). Change all six together. The stack
publishes host ports `5432`, `5433`, `3000` and `3001`; they must be free —
see §9.10.

**Path B — host PostgreSQL.** If you already run PostgreSQL locally, copy
`.env.example` to `.env`, comment out the Path A block and uncomment Path B.
`make migrate` (§3.3) creates both databases if they are absent; to create
them yourself instead:

```bash
createdb reimagina_app
createdb reimagina_test
```

> Path B's DSNs name no user, so they assume your own operating-system
> username is a PostgreSQL role that can connect without a password and create
> databases — the usual Homebrew or Postgres.app arrangement. If yours is not,
> put a user and password into the five Path B DSNs.

### 3.2 Install dependencies

```bash
make install        # uv sync --extra dev, then npm ci in apps/web
```

`npm ci` installs exactly what `apps/web/package-lock.json` records and never
rewrites it. It ends with an `npm audit` summary; §9.6 says how to read one.

### 3.3 Create and migrate the databases

```bash
make migrate
```

This creates the application and test databases if they are absent, enables
PostGIS and pgRouting, and migrates both to the latest revision (`r051`). It
takes no arguments: every DSN is read from `.env`, including
`DATABASE_URL_ADMIN`, which is the connection used to *create* the other two.

A successful run prints, for each database, whether it was created and that it
was migrated. On Path A the application database already exists — the
container created it as `POSTGRES_DB` — so it reports `created: false`:

```json
{"app":  {"created": false, "dsn": "…/reimagina",      "migrated": true},
 "test": {"created": true,  "dsn": "…/reimagina_test", "migrated": true}}
```

Confirm the schema landed:

```bash
uv run python -m apps.cli.app verify-schema
```

Every CLI command reads `.env`, so no connection flags are needed. `--dsn`
remains available and overrides the environment when given. One exception:
`raster fetch-worldpop` does not read `.env` for its `--dsn`; pass it when you
pass `--aoi-id` (§7.2).

## 4. Configuration

All configuration is environment variables, read from `.env` at the
repository root. `.env.example` is the template; it is never read at runtime.
A variable already set in the shell wins over `.env`.

### 4.1 Database connections

Five DSNs, deliberately distinct so that one tool never takes another's pool.

| Variable | Read by | Port in Path A | Notes |
|---|---|---|---|
| `DATABASE_URL` | Migrations, loaders, CLI, workers | `5433` | The default DSN for the storage layer. Carries DDL and `CREATE EXTENSION` |
| `REIMAGINA_APP_DSN` | The API only | `5432` | The one pooled DSN, and the one the pooler is for: short read-only handlers, `SET LOCAL` only. Default `postgresql://localhost/reimagina_app`; `make dev` and `make dev-api` fall back to `DATABASE_URL` |
| `DATABASE_URL_DIRECT` | Ops plane, Alembic, `LISTEN`/`NOTIFY` | `5433` | **Must bypass the pooler.** See §9.2 |
| `TEST_DATABASE_URL` | The integration, conformance and recovery suites | `5433` | The database name must contain `test` (or equal `REIMAGINA_TEST_DB_ALLOW_NAME`); the fixture refuses anything else, because its teardown drops every schema — §9.5 |
| `DATABASE_URL_ADMIN` | `make migrate` only | `5433` | The database connected to in order to *create* the other two. Needs a role that may create databases and extensions |

Only one of the five is pooled, and that is not an oversight. In Path A,
`localhost:5432` is PgBouncer in **transaction** pool mode, configured with a
single database (`POSTGRES_DB`). Two consequences follow, and `.env.example`
is written around both:

1. **A name PgBouncer does not hold does not connect.** `postgres` and
   `reimagina_test` are not in its database list, so `DATABASE_URL_ADMIN` and
   `TEST_DATABASE_URL` pointed at `5432` fail before they reach PostgreSQL.
2. **A session handed back between statements loses session state.**
   `CREATE DATABASE` cannot run inside a transaction block, `LISTEN`/`NOTIFY`
   is session-scoped, and a migration lock has to outlive its statement. None
   of those survive transaction pooling.

Path A also reads `POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD`; the
container stack creates its role and database from them.

### 4.2 Ports and the API process

| Variable | Default | Purpose |
|---|---|---|
| `API_PORT` (`make` variable) | `8001` | The API's port for `make dev`, `make dev-api` and `make check-ports`. `make dev` and `make dev-web` pass it to Vite's proxy. Set it on the command line: `make dev API_PORT=8123` |
| `WEB_PORT` (`make` variable) | `5173` | The web dev server's port for `make dev` and `make dev-web` |
| `REIMAGINA_API_URL` | `http://127.0.0.1:8001` | Where Vite's `/api` proxy sends requests. The `make` targets set it from `API_PORT` |
| `REIMAGINA_API_HOST` | `127.0.0.1` | Bind address for `uv run python -m apps.api`. ⚠ The API has no authentication; do not bind it beyond loopback |
| `REIMAGINA_API_PORT` | `8001` | Bind port for `uv run python -m apps.api` (the `make` targets use `API_PORT`) |
| `REIMAGINA_API_LOG_LEVEL` | `info` | uvicorn's log level for `python -m apps.api` |
| `REIMAGINA_API_CORS_ORIGINS` | `http://localhost:5173,http://localhost:8001` | Comma-separated origins allowed to call the API from a browser. Only matters for an absolute tile or API URL; the `/api` proxy is same-origin |
| `REIMAGINA_API_POOL_MIN` | `1` | Minimum connections in the API's pool |
| `REIMAGINA_API_POOL_MAX` | `16` | Maximum connections in the API's pool |
| `REIMAGINA_API_POOL_WAIT_S` | `30` | How long a request waits for a free connection before answering 503 with `Retry-After` |
| `REIMAGINA_LOG_LEVEL` | `INFO` | Log level of the `reimagina` packages (CLI, worker) |

### 4.3 Tiles and files

| Variable | Default | Purpose |
|---|---|---|
| `REIMAGINA_TILE_LIVE_BASE_URL` | `http://localhost:3000/get_tile` (Martin) | Where the browser fetches live vector tiles. `/api/tiles` serves them from the API. **`make dev` and `make dev-api` set `/api/tiles` and ignore this variable in `.env`**; only the invoking shell can override it — §6.2 |
| `REIMAGINA_MARTIN_URL` | `http://localhost:3000` | Where `GET /health/tile-server` probes Martin |
| `REIMAGINA_TILE_SNAPSHOT_BASE_URL` | `http://localhost:3001` | Static host serving PMTiles snapshots |
| `REIMAGINA_TILE_RASTER_BASE_URL` | derived | Base URL of raster (COG) tiles. Unset, it is `REIMAGINA_API_BASE_URL` + `/cog`, else `http://localhost:8001/cog` |
| `REIMAGINA_API_BASE_URL` | unset | The API's public base URL; only used to derive the raster tile URL. Set it when the API is not on `localhost:8001` |
| `REIMAGINA_TILE_MAX_AGE_S` | `60` | `Cache-Control: max-age` on API-served tiles |
| `REIMAGINA_TILE_STALE_WHILE_REVALIDATE_S` | `300` | `stale-while-revalidate` on API-served tiles |
| `REIMAGINA_TILE_TIMEOUT_MS` | `5000` | Statement timeout of one API-served tile |
| `REIMAGINA_DATA_DIR` | `<repository>/data` | Root of downloaded sources, staged files and COGs. `data/` is git-ignored |
| `REIMAGINA_OVERTURE_RELEASE` | `2026-10-21.0` | The Overture release the extract commands read. Overture removes old releases; `docs/BOOTSTRAP.md` says how to find a current one |

### 4.4 Ops plane

| Variable | Default | Purpose |
|---|---|---|
| `REIMAGINA_ORCHESTRATOR` | `dbos` | Which orchestrator the factory builds. `dbos` is the only deployable value |
| `REIMAGINA_DBOS_EXECUTION_MODE` | `client` | `client` submits and reads runs but executes nothing (the API); `executor` executes them (`python -m apps.worker` sets this itself); `dev` does both in one process |
| `REIMAGINA_EXECUTOR_ID` | `<hostname>:<role>` | Identifies this process to crash recovery. Must be **stable** across restarts and **unique** between concurrent processes — see §9.3 |
| `REIMAGINA_EXECUTOR_ROLE` | `app` | Distinguishes co-located processes. `python -m apps.worker` sets `worker` itself |
| `REIMAGINA_RUN_CONCURRENCY` | `2` | How many runs one worker executes at once (the `runs` queue) |
| `REIMAGINA_UNIT_CONCURRENCY` | `8` | How many indicator units one worker executes at once (the `units` queue) |
| `REIMAGINA_POLL_INTERVAL_SEC` | `0.1` | How often a worker polls its queues |
| `REIMAGINA_APP_VERSION` | derived | Pins the version gating recovery. Set deliberately or not at all — see §9.9 |
| `REIMAGINA_EXECUTOR_HEARTBEAT_S` | `30` | How often a process publishes liveness |
| `REIMAGINA_EXECUTOR_STALE_S` | `300` | How silent an executor must be before a peer presumes it dead — see §9.9 |
| `REIMAGINA_RECLAIM_SWEEP_S` | half the window, at least 60 | How often the worker looks for dead executors |
| `REIMAGINA_RECLAIM_ENABLED` | `1` | Set `0` to disable the sweep entirely |
| `REIMAGINA_PROGRESS_SUBSCRIBERS` | `32` | How many live-progress streams (`/jobs/{run_id}/events`) one API process serves at once |
| `REIMAGINA_RUN_PLAN_TTL_HOURS` | `24` | Age after which an orphaned run plan is swept |
| `REIMAGINA_HAMILTON_CACHE` | off | `1` enables the indicator dataflow's result cache. Development only |

### 4.5 Test and benchmark switches

These are read only by the test suites and benchmark scripts.

| Variable | Purpose |
|---|---|
| `REIMAGINA_REQUIRE_DB=1` | A database-backed suite fails, instead of skipping, when the test database is unreachable. CI sets it |
| `REIMAGINA_TEST_DB_ALLOW_NAME` | Allows one exact `TEST_DATABASE_URL` database name that does not contain `test` |
| `REIMAGINA_POOLED_TEST_DSN` | A DSN through a transaction-mode PgBouncer; enables the one integration test that needs one |
| `REIMAGINA_RECOVERY_TEST=1` | Enables `tests/recovery` (`make test-recovery` sets it) |
| `REIMAGINA_E2E=1` | Enables `tests/e2e` (`make test-e2e` sets it) |
| `REIMAGINA_E2E_API`, `REIMAGINA_E2E_WEB` | Where the browser suite finds the API and the web app; default `http://127.0.0.1:8001` and `http://localhost:5173` |
| `REIMAGINA_BENCH=1`, `REIMAGINA_BENCH_SCALE`, `REIMAGINA_BENCH_REPS` | Enable and size `tests/workloads` and the benchmark scripts |

## 5. Running the platform

The whole platform is one command:

```bash
make dev
```

It starts the API on `:8001`, the web application on `:5173` and the worker,
and Ctrl-C stops all three. It reads `REIMAGINA_APP_DSN`, falling back to
`DATABASE_URL` from `.env`, and refuses to start rather than silently serving
the API's own default database — an empty database looks like a broken
frontend, not a missing variable. It points the map's tiles at the API
(`/api/tiles`, §6.2).

Open the web application at **`http://localhost:5173`**. Use `localhost`, not
`127.0.0.1`: on machines where `localhost` resolves to IPv6, Vite listens on
`[::1]` only and `127.0.0.1:5173` does not answer.

Before it starts anything it checks that both ports are free and names the
process holding one if not. To use other ports:

```bash
make dev API_PORT=8123 WEB_PORT=5199
make check-ports API_PORT=8123 WEB_PORT=5199   # the check on its own
```

To run the three processes in separate terminals:

```bash
make dev-api      # FastAPI on API_PORT; same DSN and tiles as make dev
make dev-web      # Vite on WEB_PORT; proxies /api to API_PORT
make dev-worker   # drains runs; reads DATABASE_URL_DIRECT from .env
```

Pass the same `API_PORT` to `make dev-api` and `make dev-web`.

The worker is required for anything to compute. Without it the run dashboard
shows submitted runs that never progress, which looks like a hang and is not;
the dashboard says so in its health strip.

The split between the API and Vite is a development artefact, not
architecture: Vite serves the frontend itself because it does hot module
replacement and the TypeScript and Svelte transforms that the API cannot. A
deployment runs `make build-web` and serves the static output in
`apps/web/dist/` behind one reverse proxy that maps `/api` to the API, which is
the mapping `vite.config.ts` performs in development.

## 6. Verifying the installation

With `make dev` running:

```bash
curl -s localhost:8001/health          # {"status":"ok","db":"reachable"}
curl -s localhost:8001/openapi.json | head -c 80
open http://localhost:5173             # xdg-open on Linux
```

Then the suites. Four Python suites and the web unit tests are gated in CI;
four further suites are run by hand.

```bash
make test                # unit + contract: no services needed
make test-integration    # needs PostgreSQL + PostGIS (TEST_DATABASE_URL)
make test-conformance    # needs PostgreSQL + PostGIS (TEST_DATABASE_URL)
make test-web            # the web unit tests (vitest)
make build-web           # typecheck + production build of the web app
```

The `make` targets call `uv run pytest`; to call pytest yourself, name the
suites, e.g. `uv run pytest tests/integration`.

> **Do not run a bare `pytest`, and run the suites as separate processes.** A
> bare `pytest` collects every suite, including `tests/integration`, whose
> session fixture ends by dropping every managed schema from
> `TEST_DATABASE_URL`; a suite collected after it in the same process finds no
> schema. CI runs each suite as its own process, and so do the `make` targets.

For an end-to-end check that the platform *does* something rather than merely
starts — a populated database, every shipped indicator computed, tiles with
features in them, a run drained by the worker — run `scripts/acceptance_run.sh`
(§7.1). It needs no credentials.

The four hand-run suites:

| Suite | Command | What it needs |
|---|---|---|
| `tests/performance` | `uv run pytest tests/performance` | The test database; benchmark budgets and explain-plan shape |
| `tests/workloads` | `make test-workloads` | Nothing; measures how the ops plane schedules synthetic DAGs (`tests/workloads/README.md`) |
| `tests/recovery` | `make test-recovery` | A test database with at least 20,000 blocks — §6.1 |
| `tests/e2e` | `make test-e2e` | Both servers running and Playwright — below |

**The browser suite** opens every page in a real Chromium and asserts what the
page reports about itself. One-time setup, inside the web application so its
driver resolves Playwright:

```bash
cd apps/web
npm install --no-save playwright
npx playwright install chromium
cd ../..
```

Then, with a populated database (§7.1) and `make dev` running in another
terminal:

```bash
make test-e2e
```

It expects the API on `http://127.0.0.1:8001` and the web application on
`http://localhost:5173`. If you moved the ports, say where they are:

```bash
REIMAGINA_E2E_API=http://127.0.0.1:8123 REIMAGINA_E2E_WEB=http://localhost:5199 make test-e2e
```

`tests/e2e/README.md` describes what it checks.

### 6.1 Verifying the orchestration module

Schedule A names the orchestration module as a deliverable, and it is the
component whose behaviour is least visible from the outside — a run that
succeeds looks the same whether or not it would have survived the worker
being killed. These are the checks that distinguish the two, in increasing
order of what they cost you.

| Check | Command | What a pass means |
|---|---|---|
| The port contract | `make test-conformance` | 11 behaviours — submit, retrieve, cancel, subscribe, partial failure and the rest — hold against **both** shipped implementations: 22 items, one of which skips because it does not apply to the synchronous orchestrator. A new engine that cannot satisfy them is rejected by a test rather than by an argument |
| It runs through the queue | `scripts/acceptance_run.sh`, step 12 | A separate `python -m apps.worker` process announced itself, dequeued a run, and drained it. Step 12b prints each unit's start offset: units at the same offset ran concurrently, a later one waited for a dependency |
| A resubmission is not a second run | `scripts/acceptance_run.sh`, step 12c | The same request submitted twice returned one run id, the second flagged `deduplicated` |
| It survives its executor dying | `make test-recovery` | A process `SIGKILL`ed mid-compute was resumed — by itself on restart, and by a *different* executor through the liveness sweep |

⚠ **`make test-recovery` runs against `TEST_DATABASE_URL`, needs at least
20,000 blocks there, and skips below that.** The territory in §7.1 has
sixteen, and the integration suite leaves the test database without schemas —
and a skip is one character away from a pass in pytest's output. Prepare the
test database first:

```bash
make migrate                                     # restores the test DB's schemas
DATABASE_URL=$TEST_DATABASE_URL uv run python -m apps.cli.app indicators seed-catalog
DATABASE_URL=$TEST_DATABASE_URL uv run python scripts/synthetic_territory.py \
    --blocks 150 --ageb-side 10                  # 22,500 blocks, under a minute, no credentials
make test-recovery                               # 2 passed
```

`$TEST_DATABASE_URL` must be set in the shell for the two middle commands —
`set -a; . ./.env; set +a` exports everything in `.env`.

The size is not arbitrary. The suite kills a worker *while a unit is
running*, so the run has to take long enough for the kill to land inside it.
On a sixteen-block territory every unit finishes in milliseconds and the
crash arrives after the work is already done, which would prove the opposite
of what the test claims.

**What none of these cover.** `scenario recompute --sync` (and
`evaluate-scenario --sync`) runs the coordinator in-process with no ops plane
at all. It is the path that still works when the engine is down, and steps 10
and 11 of the acceptance run use it — so a green acceptance run before step 12
says nothing about the orchestrator. That is why step 12 exists.

### 6.2 Live vector tiles: the API or Martin

Live vector tiles are a Postgres function plus an HTTP wrapper, and the
wrapper is interchangeable. The contract is the **function signature** —
every tile function takes `(z, x, y, query_params json)` and returns
`bytea` — which is Martin's function-source convention, so anything
implementing it is a drop-in.

| Server | Address | What it is |
|---|---|---|
| **The API** | `/api/tiles` through the web app; `:8001/tiles` directly | `apps/api/routers/mvt.py`, same functions, same process as the rest of the API. What `make dev` uses |
| **Martin** | `:3000` | `ghcr.io/maplibre/martin`, started by the container stack (Path A) and connecting to PostgreSQL directly. Optional |

**What `make dev` does.** `make dev` and `make dev-api` start no tile server,
so they point the map at the API: they set
`REIMAGINA_TILE_LIVE_BASE_URL=/api/tiles` and ignore that variable in `.env`.
Only the invoking shell can override it. To use Martin from the container
stack instead:

```bash
REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:3000 make dev
```

A process started any other way (`uv run python -m apps.api`) reads the
variable from the environment or `.env`, and its default is Martin's
`http://localhost:3000/get_tile`. Nothing else changes between the two: not
`infra/tiles/layers.json`, not `/tile-sources`, not the frontend.
`GET /health/tile-server` reports whether Martin answers.

⚠ **Use the relative form for the API.** An absolute URL such as
`http://localhost:8001/tiles` makes every tile request cross-origin from the
web application, so it depends on the API's CORS allowlist
(`REIMAGINA_API_CORS_ORIGINS`), which by default names
`http://localhost:5173` and therefore rejects a browser that opened the page
as `http://127.0.0.1:5173`. The symptom is a map that draws nothing, with no
console error naming the cause. `/api/tiles` is served through the same proxy
the rest of the API uses, so the question does not arise. A contract test
asserts both forms resolve to the same URL shape, because that property is
the only thing that makes two implementations safe to keep.

**Which to use.** Martin in production: a Rust tile server with its own
connection pool is the better machine when many clients pan at once. At the
scale of a single reviewer or a demo, the API is enough — measured here,
`serving_geo.get_tile` answers a populated z16 tile in **~1.5 ms** — and
running one fewer service is worth more than the headroom.

**Verify a tile without a browser:**

```bash
# A tile with features: 200, and a vector-tile content type
curl -sD- -o /tmp/tile.mvt "http://localhost:8001/tiles/get_tile/16/11748/26478" | head -5

# Decode it — no dependencies, the decoder is in this repository
uv run python scripts/mvt_inspect.py /tmp/tile.mvt

# A tile outside the loaded territory: 204 No Content, empty by design
curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:8001/tiles/get_tile/14/0/0"

# TileJSON, for a client that prefers discovery
curl -s "http://localhost:8001/tiles/get_tile" | uv run python -m json.tool
```

The z16 tile above lies on the generated territory of §7.1.
`scripts/mvt_inspect.py` prints the layer name, the spec version, the extent,
and a breakdown by geometry type and `type_slug` — so "the tile has features"
is a statement about decoded content rather than about a byte count.

The API path is covered end-to-end by
`tests/integration/test_mvt_route_serves_tiles.py`, which fetches real tiles
over HTTP and decodes them. Martin is an external binary and has no
equivalent coverage here; `docs/architecture/TILE_SERVING.md` has the full
argument for keeping both.

## 7. Loading data

### 7.1 Without credentials — a generated territory

If you have no access to the data lake, this is the path to a populated
system. It takes about a minute and a half and needs no network:

```bash
scripts/acceptance_run.sh
```

⚠ **The script drops and recreates the database named in `DATABASE_URL`.**
Point `DATABASE_URL` at a database you can lose — on Path A the stack's own
`reimagina` database is fine on a fresh install; never a database holding a
real load. `--keep` skips the drop.

It migrates the database from empty, generates a small territory on Mexicali
coordinates — AGEBs containing blocks containing buildings, a pedestrian
street graph, four amenities, an area of interest and a single-band population
raster — and then runs every analysis stage over it: feature overlaps, every
shipped indicator (all twelve) including the pgRouting network family, raster ingest
through COG staging, zonal statistics, an observation rollup, a scenario, a
run drained by a separate worker process, and the live tile functions. Any
failure stops the run.

To generate the territory without the rest:

```bash
uv run python scripts/synthetic_territory.py --blocks 6
uv run python scripts/synthetic_territory.py --raster-out /tmp/population.tif
```

Everything is written through the production loaders, so what lands passes
the same triggers and carries the same provenance columns as an ingested
dataset. ⚠ **The geometry is a regular grid, not a city.** Use it to exercise
and demonstrate the platform; never to draw a conclusion about Mexicali.

### 7.2 With credentials — the real dataset

The real territory comes from two kinds of source:

| Source | Where it comes from | Needs |
|---|---|---|
| INEGI Marco Geoestadístico 2024 (blocks, AGEBs, municipalities, states) | The lake mirror on disk | Lake credentials, once |
| INEGI 2020 census (block attributes) | The lake mirror on disk | Lake credentials, once |
| Municipal amenities (health, schools, …) | The lake mirror on disk | Lake credentials, once |
| Overture buildings, places and administrative divisions | Overture's public S3 bucket, at load time | Internet access |
| OpenStreetMap pedestrian network | Overpass, through `osmnx`, at load time | Internet access |
| WorldPop population raster (optional) | WorldPop's public REST API, at load time | Internet access |

**The lake mirror is an offline copy.** The private Azure data lake
(`cfcetlsadls/raw`) is copied once to `data/sources/azure/` with the
credentials; the loaders then read those files and never contact the lake.
`docs/AZURE_DATA_INVENTORY.md` documents the expected layout. `data/` is
git-ignored, so a fresh clone has none of it.

Without the mirror there are no blocks, so no indicator can run on real data;
§7.1 is the alternative. Overture alone still gives a real municipal boundary
and real buildings on the map (`overture divisions`, `overture
aoi-from-division`, `overture extract`, `overture load`; see
`docs/BOOTSTRAP.md`).

**`scripts/mexicali_run.sh` is the runbook**, as one command. It checks the
mirror first and names each missing path, drops and rebuilds the database
named in `DATABASE_URL` (unless `--keep`), keeps the step order that matters,
and treats the optional sources as non-fatal so a rate-limited Overpass does
not discard the INEGI load:

```bash
scripts/mexicali_run.sh                  # drops and rebuilds $DATABASE_URL
scripts/mexicali_run.sh --skip-network   # without OSM: no network-distance indicators
```

The steps it runs, for running a part of it or another municipality:

```bash
CLI="uv run python -m apps.cli.app"
BBOX=-115.70,32.40,-115.20,32.80          # Mexicali's urban core

$CLI data verify-sources                  # mirror paths, Overture S3, WorldPop reachable
$CLI feat seed-types
$CLI indicators seed-catalog

# INEGI geometry for Baja California (state 02)
$CLI inegi-marco bootstrap --cve-ent 02 \
  --layers areas_estatales,areas_municipales,agebs_urbanas,manzanas_urbanas

# The area of interest: the INEGI municipality polygon. Prints the AOI id.
$CLI cdmx aoi-from-alcaldia --cve-ent 02 --cve-mun 002 \
  --name Mexicali --project-name "Mexicali Pilot"
export AOI_ID=…                           # the aoi_id it printed

# Attributes and destinations
$CLI inegi-censo bootstrap --cve-ent 02
$CLI points bootstrap --layers mexicali_amenities
$CLI overture places --bbox $BBOX --country MX --aoi-id "$AOI_ID" \
  --output-path data/overture_places_mexicali.parquet

# Buildings (Overture) and the street network (OSM)
$CLI overture extract --bbox $BBOX --output-path data/overture_buildings_mexicali.parquet
$CLI overture load --raw-path data/overture_buildings_mexicali.parquet \
  --source-name "Overture Buildings - Mexicali BBox" --aoi-id "$AOI_ID"
$CLI network bootstrap --aoi-id "$AOI_ID" --clip-bbox $BBOX

# Membership, after every feature exists; then overlaps
$CLI aoi compute-features --aoi-id "$AOI_ID"
$CLI overlap declare-pair --source-context global --source-slug building \
  --target-context MEX --target-slug block
$CLI overlap declare-pair --source-context MEX --source-slug block \
  --target-context MEX --target-slug ageb
$CLI overlap compute-all --aoi-id "$AOI_ID"

# Baseline of the scenario inputs
$CLI inegi-censo bootstrap --cve-ent 02 --columns prom_ocup
$CLI overture block-storeys --aoi-id "$AOI_ID"
uv run python scripts/seed_demo_attributes.py --aoi-id "$AOI_ID" --fit-to-census

# Indicators, inputs before the indicators that read them
$CLI indicators compute --slug urban_form_block_compactness --aoi-id "$AOI_ID"
```

Three ordering rules matter:

- **`aoi compute-features` runs after every load.** Area membership is what
  AOI-scoped reads, tiles and indicators filter on; a feature loaded after it
  belongs to no area, and its tiles come back empty.
- **`--country MX` on `overture places` is not optional.** The Mexicali box
  crosses the border, and without it a block is reported near a park in
  California.
- **Indicators compute inputs first.** The order is the one in
  `scripts/mexicali_run.sh` step 10: the three urban-form indicators, then
  planned floors, the edificability family, the four network-distance
  indicators and the livability score. With the worker running, a run of
  "what is out of date" on the run dashboard (§8.4) computes all of them in
  dependency order instead.

The scenario inputs need a baseline, which the "baseline of the scenario
inputs" block supplies: household size comes from the census and storeys from
Overture where they exist; the rest is synthetic, fitted so each block's
dwellings equal the census count.

**WorldPop (optional).** A population raster, fetched from WorldPop (the
country file, about 1 GB for Mexico, is cached under `data/cache/worldpop/`),
clipped to the AOI and ingested as a COG:

```bash
$CLI raster fetch-worldpop --country MEX --year 2020 --aoi-id "$AOI_ID" \
  --dsn "$DATABASE_URL" --output-path data/worldpop_mexicali_2020_100m.tif
$CLI raster ingest --raw-path data/worldpop_mexicali_2020_100m.tif \
  --layer-slug worldpop_mexicali_2020_100m --aggregation-kind extensive \
  --source-name "WorldPop · MEX · 2020 (100m, Mexicali clip)" \
  --provider WorldPop --version 2020-100m
```

`docs/BOOTSTRAP.md` is the longer runbook, including Mexico City, WorldPop and
rolling forward to a new Overture release.

## 8. Operating the platform

The four pages share one top bar that links them. Every page reads the API
through the web application's `/api` proxy; none needs anything but `make dev`.

### 8.1 Map (`/`)

The map shows one indicator over one area of interest.

1. **Pick the area** under *Area*; the map fits to it (`f` refits).
2. **Pick the indicator** under *Indicator*. The map opens on the first
   indicator that has values, so it never opens blank. *About this indicator*
   opens its catalogue entry (§8.3). Where an indicator exists at more than
   one grain, a toggle switches between them (blocks, AGEBs).
3. **Read the legend**, on the map. The colour ramp spans the middle 90 % of
   values, so one outlier cannot flatten it; outside that span colours
   saturate. Below the ramp, the distribution strip shows the indicator's
   values across the area (grey bars), the median (dashed rule) and the span
   the ramp resolves (lighter band). The distribution is computed by the
   server over the whole area, not from what the map has drawn. An indicator
   with a **cap** — a distance indicator truncated at, say, 1,500 m, where a
   reading means "this far or more" — draws capped blocks in a separate cap
   colour (amber) that belongs to neither end of the ramp, and the legend
   states the share of blocks at the cap.
4. **Hover** a block to see its value; **click** it to pin the block
   inspector on the map. The inspector shows the block's id (click it to
   select and copy), its value with its **exact rank** among the area's blocks
   ("Higher than 31% of blocks", "Farther than …" for distances, "At the cap,
   with …" at the cap), and its other indicator values; click one of those to
   switch the map to it. `esc` or ✕ closes it.
5. **Draw a selection** with the circle, rectangle or polygon tool at the
   top left of the map (`c`, `r`, `p`). The selection card shows how many
   blocks fall inside and their median, beside the whole area's median.
   `esc` closes the block inspector first, then clears the selection.
6. **Layers** lists the spatial layers — buildings, blocks, municipalities,
   streets and any raster overlays. All are **off by default**: the opening
   view is the indicator alone. Streets appear from zoom 12.
7. **Download** the indicator on the map as CSV, GeoJSON or GeoParquet. The
   files are scoped to the area and grain drawn, not the whole database.

There is no external basemap, on purpose — no tile provider to depend on and
no third-party licence to carry. Orientation comes from the platform's own
street network, published as `global:road` features and served through the
same tile path as the blocks. The map credits whatever data it is showing,
bottom-right, from the licence recorded against each loaded source — so a
real OpenStreetMap network is credited as ODbL requires, and a generated
territory credits the generator instead.

**On a phone or narrow window.** Below 860 px the panel becomes a drawer that
starts closed, so the map gets the whole screen; the toggle is top-left, and
`esc` or a tap outside closes it. Below 768 px the legend becomes a one-line
strip, and the block inspector and the selection card share one bottom sheet:
the one opened last is in front.

**Keyboard.** The command palette (`⌘K`, `Ctrl-K`) lists every area,
indicator, layer, draw mode and page, and is the shortest route to all of
them.

| Key | |
|---|---|
| `⌘K` / `Ctrl-K` | Command palette |
| `?` | The shortcut list |
| `\` | Show or hide the panel |
| `f` | Fit the map to the active area |
| `r` / `p` / `c` | Draw a rectangle, polygon or circle selection |
| `esc` | Close the block inspector, then clear the selection |

### 8.2 Scenarios (`/scenarios.html`)

A scenario is a named what-if over a pilot area: rules that change the
storeys, dwelling size and household size of the blocks inside a drawn area,
and the indicators those changes recompute.

1. **Pick a pilot.** The table lists every scenario on it side by side, with
   the baseline city first, on the indicators a rule can change — one number
   per cell, a total for counts and a median for rates.
2. **Create a scenario**: type a name under *New scenario name* and submit.
3. **Select a scenario** (click its row) and pick an indicator. The map draws
   the scenario's difference from the baseline city — green is an
   improvement, red is worse, and the legend states which direction is
   better for that indicator — and the panel lists its rules. The URL records
   the selection, so a link reopens it.
4. **Write a rule.** Draw an area with the tools at the map's top left: drag
   out a circle or rectangle, or click each corner of a polygon and click the
   last one again to close it. Set *Storeys*, *m² per dwelling* and *People
   per household*, then *Apply to drawn area*. The storeys field will not
   exceed what the floor and density ceilings allow. Each rule appears under
   *Rules*; × removes the area and all its edits.
5. **Recompute.** This materialises the rules and submits a run that
   recomputes only what they changed; the worker executes it. The page
   follows the run and refreshes when it lands; a recompute that takes longer
   is handed over to the run dashboard (§8.4), where every run is followed.
6. **Compare to baseline** summarises, in one line, how the chosen indicator
   under the scenario differs from the baseline.
7. **Delete** removes the scenario and everything it wrote, after a
   confirmation.

The same operations are in the CLI (`scenario create`, `set-attribute`,
`recompute`, `compare`) and the API (`/scenarios`); `scripts/acceptance_run.sh`
steps 10 and 11 use the CLI.

### 8.3 Catalogue (`/catalog.html`)

What each indicator is and where it comes from.

- **Find an indicator**: search by name, slug or description, and filter by
  family, kind and grain.
- **Open one** (click its row) for its definition, the values observed in
  the active area (percentiles against the declared range, and a histogram),
  its lineage — the indicators it is computed from and those computed from
  it — and the datasets upstream of it with their freshness. *Open on map*
  shows it on the map page; the download links give its values as CSV,
  GeoJSON or GeoParquet.
- **Ask what would recompute.** The impact check takes a change — an
  attribute of a dataset, or a whole dataset — and lists the indicators that
  would recompute, without submitting anything.

### 8.4 Runs (`/ops.html`)

Every computation — an indicator, a scenario recompute — is a run of units,
one unit per indicator, executed by the worker.

- **The health strip** at the top says whether a worker is alive. If none
  is, nothing will run until one is started (`make dev-worker`).
- **The run list** shows every run with its status and unit counts; filter by
  status, *Clear* to reset.
- **Launch a run** under *New run*: recompute *what is out of date*, *what
  changed* (pick the attributes or datasets that changed), or *indicators I
  choose*; optionally for one scenario (by id; the baseline if empty) and one
  area (the whole dataset if none). Before you submit, the form shows what the
  planner would recompute, so an empty run is visible in advance.
- **Open a run** for its units — status, duration and in-run prerequisites —
  and its timeline. The timeline draws each unit as a bar that grows from left
  to right while it runs, on a time axis that starts at 10 s and doubles each
  time the run reaches its edge. Rows are ordered by dependency depth and
  never move. Nothing is predicted: there is no estimated duration, and no
  progress number other than finished units over total units.
- **Act on a run.** *Cancel run* stops a live run (it asks for confirmation;
  a cancelled unit is not resumed). *Rerun N unfinished* submits a new run
  for the units that did not succeed. *Delete run* forgets a finished run's
  history; the indicator values it produced are kept.

The same actions through the API:

| Action | Endpoint |
|---|---|
| Submit | `POST /jobs` |
| Preview what would recompute | `POST /jobs/preview` |
| List, with filters | `GET /jobs` |
| Detail, including units | `GET /jobs/{run_id}/detail` |
| Live progress | `GET /jobs/{run_id}/events` (server-sent events) |
| Cancel | `POST /jobs/{run_id}/cancel` |
| Re-run | `POST /jobs/{run_id}/rerun` |
| Delete one finished run, or all | `DELETE /jobs/{run_id}`, `DELETE /jobs` |

### 8.5 The command line

`uv run python -m apps.cli.app --help` lists everything; each group takes
`--help` too. Top-level commands:

| Command | What it does |
|---|---|
| `status` | Prints the CLI's status |
| `bootstrap-db` | Creates the application database if needed and migrates it (`--with-test-db` for both). `make migrate` runs it |
| `verify-schema` | Checks that the canonical schemas, tables and views exist |
| `evaluate-scenario` | Recomputes the indicators a scenario edit affects |

The seventeen command groups:

| Group | What it holds |
|---|---|
| `explore` | Read-only inspection: layers, sources, feature and serving statistics, executor health (§8.6) |
| `feat` | The feature-type catalogue (`seed-types`) |
| `inegi-marco` | INEGI Marco Geoestadístico 2024 ingest from the lake mirror |
| `inegi-censo` | INEGI 2020 census ingest, block grain |
| `cdmx` | INEGI-derived areas of interest (`aoi-from-alcaldia`, `aoi-from-state`) and the Mexico City open-data layers |
| `points` | Per-city amenity ingest from the lake mirror |
| `overture` | Overture buildings, places and divisions; block storeys |
| `network` | The OSM pedestrian network of an area, loaded and published as roads |
| `aoi` | Areas of interest: list, delete, compute membership |
| `overlap` | Feature-to-feature overlap pairs: declare and compute |
| `raster` | Raster ingest (COG), WorldPop fetch, zonal statistics, derivations |
| `indicators` | Indicator catalogue (`seed-catalog`), compute, area summaries |
| `scenario` | Create a scenario, draw rules, recompute, compare |
| `tiles` | The tile manifest, resolved tile sources, PMTiles export and snapshots |
| `assets` | The asset graph: lineage, freshness, observation of external sources |
| `data` | Cross-source checks (`verify-sources`) |
| `perf` | Performance benchmarks and observability |

### 8.6 Inspection

Inspection goes through the `explore` schema, which exists for humans and is
never an application dependency:

```bash
uv run python -m apps.cli.app explore layers
uv run python -m apps.cli.app explore sources
uv run python -m apps.cli.app explore feature-stats
uv run python -m apps.cli.app explore serving-stats
uv run python -m apps.cli.app explore executor-health
```

## 9. Troubleshooting

Each of these was encountered while verifying this document.

### 9.1 `make migrate` fails with `fe_sendauth: no password supplied`

The admin connection has no credentials. `make migrate` reads
`DATABASE_URL_ADMIN` from `.env`; if it is absent the built-in default is
`postgresql://localhost/postgres`, which carries no user and no password and
will be refused by any PostgreSQL that asks for one. Set it, with a role that
may create databases and extensions.

### 9.2 Runs are submitted but nothing computes; or checkpoints vanish

First, is a worker running? Without one, runs stay queued; the run
dashboard's health strip says so.

If one is, `DATABASE_URL_DIRECT` is unset or points at the pooler. The ops
plane needs a real server connection: progress streams use `LISTEN`/`NOTIFY`,
which cannot cross a transaction-mode pooler. When the variable is missing
entirely, DBOS would fall back to a local SQLite file — the process works, and
its checkpoints are private and unrecoverable. The code raises rather than
allow this, so the symptom is a loud error naming the variable.

⚠ **Changing `DATABASE_URL_DIRECT` after the process has started does not
move it.** The ops plane builds a pooled DBOS client the first time it needs
one and keeps it for the life of the process. That client is keyed by DSN, so
pointing the variable somewhere else and re-running a command in the same
process gets a client for the new database — but a long-lived process that
already launched against the old one keeps its executor and its queues there.
Restart it.

### 9.3 A restarted worker reclaims nothing, or two processes fight over work

`REIMAGINA_EXECUTOR_ID` identifies a process to crash recovery, which reclaims
only work recorded against that exact id. It must be stable across restarts,
or a restarted process reclaims nothing; and unique between concurrent
processes, or one reclaims work another is still running — a double execution.
Unset, it derives `<hostname>:<role>`, which is correct for at most one
process per role per host.

### 9.4 `CREATE EXTENSION pgrouting` is refused

pgRouting is not a trusted extension, so it needs a superuser. This holds for
the container stack, for CI, and for a local install. On a managed instance
that hands out a non-superuser role, migration `r037` cannot run.

### 9.5 The integration suite drops your schemas

That is by design, and it is why the database `TEST_DATABASE_URL` names must
contain `test` — the fixture refuses anything else unless
`REIMAGINA_TEST_DB_ALLOW_NAME` names it exactly. Never point it at a database
you care about. `make migrate` restores the test database's schemas
afterwards.

### 9.6 `npm audit` reports vulnerabilities after installation

At this revision it reports none. Advisories published later will appear in
the summary `npm ci` prints. `npm audit` does not distinguish what ships to
the browser from what only builds it; `docs/THIRD_PARTY_LICENSES.md` does, in
its `Distribution` column, and that is the first thing to check before acting
on an entry.

### 9.7 `make stack-up` fails before any container starts

`make stack-up` reads `.env`, and Compose refuses the stack definition at
configuration time — before any container is created, naming the variable —
if a variable it requires is unset. Copy `.env.example` to `.env` first and
keep the Path A block uncommented. In Path A, `DATABASE_URL_DIRECT` must name
port **5433**, PostgreSQL behind the pooler, not 5432, which is the pooler
itself.

### 9.8 The map draws no tiles

The map asks for tiles at `REIMAGINA_TILE_LIVE_BASE_URL` (§6.2). Under
`make dev` that is the API (`/api/tiles`) and nothing else needs to run. If
the API process was started another way, its default is Martin at
`localhost:3000`; with no Martin running, nothing draws. Restart it under
`make dev-api`, or set the variable to `/api/tiles`:

```bash
REIMAGINA_TILE_LIVE_BASE_URL=/api/tiles uv run python -m apps.api
```

If tiles are requested but empty, the area has no members: run
`aoi compute-features` after the last load (§7.2). An area outside the loaded
territory is empty by design — the API answers `204`.

### 9.9 A dead worker's run is stuck at `running` forever

Two different recoveries exist and they cover different failures.

`DBOS.launch()` reclaims the workflows recorded against *its own*
`executor_id`, which covers a process that comes back — a restart, a redeploy,
an OOM kill with a supervisor behind it. It does nothing for a process that
never comes back. A destroyed host or a scaled-away replica leaves PENDING
workflows recorded against an id nothing will ever claim, and the run never
reaches a terminal state, because from the database's point of view the work
is still in progress.

A live worker sweeps those. Every `REIMAGINA_RECLAIM_SWEEP_S` seconds it asks
which executors have stopped publishing liveness for longer than
`REIMAGINA_EXECUTOR_STALE_S`, and re-enqueues their pending workflows onto
itself. So the first thing to check is whether a worker is running at all:

```bash
uv run python -m apps.cli.app explore executor-health
```

If the dead executor is still listed with a large `heartbeat_age`, no live
worker has swept it yet — wait one sweep interval. If it is not listed and the
run is still `running`, the executor died before it ever published liveness;
restart a worker with that same `REIMAGINA_EXECUTOR_ID` and `launch()` will
reclaim it directly.

⚠ **Do not lower `REIMAGINA_EXECUTOR_STALE_S` to make the sweep react faster.**
The window must stay at least 4x the heartbeat interval, and startup refuses a
configuration where it is not. Reclaiming a dead executor late costs latency;
reclaiming a live one re-enqueues work it is still executing and runs that
indicator twice.

**If the worker log says it left an executor unreclaimed**, the sweep found
work it is not allowed to take:

```
reclaim sweep left vm:worker-3 unreclaimed — work remains open against
them that this application version cannot recover
```

Recovery is scoped by `application_version` as well as by `executor_id`, so a
worker pinned to a different `REIMAGINA_APP_VERSION` than the dead executor
recovers nothing from it. This is the expected state mid-deploy. The sweep
keeps that executor's liveness row rather than tidying it away, because the
row is the last pointer to the stranded work. Find the version and start a
process pinned to it:

```sql
SELECT executor_id, application_version, status, count(*)
FROM dbos.workflow_status
WHERE status IN ('PENDING', 'ENQUEUED')
GROUP BY 1, 2, 3 ORDER BY 1, 2;
```

Or cancel the runs and resubmit — writes are replace-in-place, so a
resubmission is safe.

### 9.10 The container stack will not start, or starts slowly

- **A port is taken.** The stack publishes fixed host ports — `5432`
  (pooler), `5433` (PostgreSQL), `3000` (Martin) and `3001` (snapshots). A
  host PostgreSQL already on `5432` makes the stack fail to start. Stop it, or
  use Path B with that PostgreSQL instead.
- **Apple silicon.** The PostgreSQL + PostGIS + pgRouting image is published
  for `amd64` only, so on an ARM Mac Docker runs it under emulation. It works;
  it is slower than a native PostgreSQL, which is a reason to prefer Path B
  there.
- **Credentials.** The pooler authenticates with SCRAM against the role the
  stack creates from `POSTGRES_USER` and `POSTGRES_PASSWORD`. A password
  changed in `POSTGRES_PASSWORD` but not in the DSNs (or the reverse) is
  refused — change all six together (§3.1). A password changed after the
  first `make stack-up` does not reach the existing data volume; remove the
  volume (`docker compose -f infra/docker/docker-compose.yml down -v`, which
  deletes the database) or change the role's password in PostgreSQL.

## 10. Appendix — a verified clean install

The sequence below was executed in a fresh checkout of the delivered
revision, in this order, and each step is reported with its actual result.

| Step | Command | Result |
|---|---|---|
| 1 | `cp .env.example .env` (Path B, §3.1) | OK |
| 2 | `make install` | OK; `npm ci` reports 0 vulnerabilities |
| 3 | `make migrate` | Both databases created and migrated to `r051`; 25 tables in `curated_geo` |
| 3b | `uv run python -m apps.cli.app verify-schema` | `all_present: true` across the five canonical relations |
| 4 | `make dev API_PORT=8057 WEB_PORT=5197` | `/health` 200, `{"status":"ok","db":"reachable"}`; `/docs` 200; `/`, `/scenarios.html`, `/catalog.html`, `/ops.html` all 200 |
| 5 | `make test` | 1,025 passed |
| 6 | `make test-integration` | 548 passed, 1 skipped |
| 7 | `make test-conformance` | 21 passed, 1 skipped |
| 8 | `uv run pytest tests/performance` | 6 passed, 1 skipped |
| 9 | `make test-web` | 401 passed (28 files) |
| 10 | `make build-web` | OK |
| 11 | `scripts/acceptance_run.sh` | Every step passed, in 1 min 27 s |
| 12 | The recovery preparation of §6.1, then `make test-recovery` | 22,500 blocks generated in 16 s; 2 passed |
| 13 | `make test-e2e` | <<E2E>> of 33 passed, in a real browser against both servers |

Measured against PostgreSQL 17.10, PostGIS 3.5.6 and pgRouting 3.6.3, with
Python 3.12.11 (from `uv`) and Node.js 24.22.2. CI and the container stack use
PostgreSQL 16, PostGIS 3.4 and pgRouting 3.6.1; the schema is identical
across both.

Three things about this run differ from a fresh machine, and are stated
rather than smoothed over. It used Path B (§3.1) with the databases named
`reimagina_wse_app_test` and `reimagina_wse_test`, because the machine's
other work already uses the default names and the integration suite drops
schemas (§9.5). The API and Vite ran on ports 8057 and 5197 (`API_PORT`,
`WEB_PORT`, §5), because 8001 and 5173 were in use. And step 13 ran after
loading the generated territory (§7.1): the browser suite asserts on what the
map draws and on the legend's distribution, and an empty database has
neither.

The skips: one integration test needs a database reached through a
transaction-mode PgBouncer (`REIMAGINA_POOLED_TEST_DSN`); one conformance
test does not apply to the synchronous orchestrator, which finishes a run
before `submit_run` returns; one performance test benchmarks overlaps on the
real Mexico City dataset and skips without it. On a machine whose `ogr2ogr` lacks the PMTiles driver (GDAL
older than 3.8) one more integration test skips.

The database-backed suites are run with `REIMAGINA_REQUIRE_DB=1`. Without it
`pytest` exits 0 when every database test *skips*, and a summary of skips
reads as success to anyone who looks only at the exit code. With it, an
unreachable database fails instead of skipping.

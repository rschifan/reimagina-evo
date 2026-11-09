---
title: "Technical documentation"
subtitle: "Reimagina · Conceptualización y diseño de una centralidad urbana en Mexicali — Schedule A, Work 4"
author: "Rossano Schifanella"
date: "9 November 2026"
---

## Document control

| | |
|---|---|
| **Project** | Conceptualización y diseño de una centralidad urbana en Mexicali |
| **Deliverable** | Schedule A, Work 4 — Technical documentation |
| **Status** | Draft for review |
| **Audience** | A technically competent reader installing, configuring and operating the platform |

| Version | Date | Author | Summary |
|---|---|---|---|
| 0.1 | 2026-11-08 | Rossano Schifanella | First complete draft |
| 0.2 | 2026-11-08 | Rossano Schifanella | Added the credential-free install path (§7.1) and the acceptance run |
| 0.3 | 2026-11-09 | Rossano Schifanella | How to verify the orchestration module (§6.1) |

### About this document

This document covers installation, configuration and operation. It assumes
familiarity with PostgreSQL, Python and the command line, but no prior
knowledge of this system.

Every command here was executed against a clean clone while writing it, and
§9 records the failures that were met and what they mean. Commands that could
not be executed in that environment are marked, rather than presented as
though they had been.

Architecture is Work 1; the schema and endpoint reference is Work 3.

---

## 1. What you will end up with

A working installation serves four things:

| Surface | Address | Purpose |
|---|---|---|
| API | `http://localhost:8001` | Features, indicators, downloads, job control; OpenAPI at `/docs` |
| Web application | `http://localhost:5173` | Map, indicator catalogue (`/catalog.html`), run dashboard (`/ops.html`) |
| Vector tiles | `http://localhost:3000` | Live MVT tiles, served by Martin (the default) |
| Vector tiles, in-process | `http://localhost:8001/tiles` | The same tiles from the API itself, behind the same URL contract. §6.2 says when to use which |
| Snapshot tiles | `http://localhost:3001` | PMTiles exports of stable reference layers (Path A only) |
| Database, pooled | `localhost:5432` | Path A: the **connection pooler**, holding one database only. The API reads through it. Path B: PostgreSQL itself |
| Database, direct | `localhost:5433` | Path A only: PostgreSQL behind the pooler. Migrations, bootstrap, the test suites and the ops plane all connect here, not to `5432` — §4.1 says why |

An installation without access to the source data lake still runs: you can
install, migrate, start every process, run the test suites and build the web
application. You cannot load the territory. §7 says what that needs.

## 2. Prerequisites

| Requirement | Version | Why |
|---|---|---|
| Python | 3.11 or later | The backend; CI gates on 3.12 |
| Node.js | 20 or later | The web application |
| `uv` | recent | Dependency installation; CI installs from `uv.lock` |
| PostgreSQL | 16 | The database |
| PostGIS | 3.4 or later | All spatial types and functions |
| pgRouting | 3.6 or later | Network-distance indicators (migration `r037`) |
| GDAL | 3.6 or later | Native dependency of `fiona` and `rasterio` |
| Docker | recent | Only for the container stack in §3.1 |

On Debian or Ubuntu, GDAL headers come from `libgdal-dev`; on macOS, `brew
install gdal`.

> **PostGIS and pgRouting are not in the Ubuntu archive.** They come from the
> PostgreSQL Global Development Group repository. pgRouting is also not a
> *trusted* extension, so `CREATE EXTENSION` requires a superuser — see §9.4.

## 3. Installation

### 3.1 Choose a database path

Two paths are supported, and the choice only affects where PostgreSQL comes
from.

**Path A — container stack (recommended).** Brings up PostgreSQL with PostGIS
and pgRouting, a connection pooler, Martin and a static tile host:

```bash
cp .env.example .env     # then set POSTGRES_PASSWORD
make stack-up
```

**Path B — host PostgreSQL.** If you already run PostgreSQL locally, comment
out the Path A block in `.env`, uncomment Path B, and create the databases
yourself:

```bash
createdb reimagina_app reimagina_test
```

> Path B's DSNs name no user, so they assume your own operating-system
> username is a PostgreSQL role that can connect without a password and create
> databases — the usual Homebrew or Postgres.app arrangement. If yours is not,
> put a user and password into the four Path B DSNs.

### 3.2 Install dependencies

```bash
make install        # uv sync --extra dev, then npm install in apps/web
```

This prints an `npm audit` summary on completion. §9.6 explains what it
reports and why one entry matters.

### 3.3 Create and migrate the databases

```bash
make migrate
```

This creates the application and test databases if they are absent, enables
PostGIS and pgRouting, and migrates both to the latest revision. It takes no
arguments: every DSN is read from `.env`, including `DATABASE_URL_ADMIN`,
which is the connection used to *create* the other two databases.

A successful run prints, for each database, whether it was created and that it
was migrated:

```json
{"app":  {"created": true, "dsn": "…/reimagina_app",  "migrated": true},
 "test": {"created": true, "dsn": "…/reimagina_test", "migrated": true}}
```

Confirm the schema landed:

```bash
python -m apps.cli.app verify-schema
```

Every CLI command reads `.env`, so no connection flags are needed. `--dsn`
remains available and overrides the environment when given.

## 4. Configuration

All configuration is environment variables, read from `.env` at the
repository root. `.env.example` is the template; it is never read at runtime.

### 4.1 Database connections

Four DSNs, deliberately distinct so that one tool never takes another's pool.

| Variable | Read by | Port in Path A | Notes |
|---|---|---|---|
| `DATABASE_URL` | Migrations, loaders, CLI, workers | `5433` | The default DSN for the storage layer. Carries DDL and `CREATE EXTENSION` |
| `REIMAGINA_APP_DSN` | The API only | `5432` | The one pooled DSN, and the one the pooler is for: short read-only handlers, `SET LOCAL` only |
| `DATABASE_URL_DIRECT` | Ops plane, Alembic, `LISTEN`/`NOTIFY`, Martin | `5433` | **Must bypass the pooler.** See §9.2 |
| `TEST_DATABASE_URL` | The integration and conformance suites | `5433` | Must end in `_test`; the fixture creates and drops schemas |
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

### 4.2 Ops plane

| Variable | Default | Purpose |
|---|---|---|
| `REIMAGINA_ORCHESTRATOR` | `dbos` | Which orchestrator the factory builds. `dbos` is the only deployable value |
| `REIMAGINA_EXECUTOR_ID` | `<hostname>:<role>` | Identifies this process to crash recovery. Must be **stable** across restarts and **unique** between concurrent processes — see §9.3 |
| `REIMAGINA_EXECUTOR_ROLE` | `app` | Distinguishes co-located processes. `python -m apps.worker` sets `worker` itself |
| `REIMAGINA_UNIT_CONCURRENCY` | `2` | How many indicator units one process runs at once |
| `REIMAGINA_APP_VERSION` | derived | Pins the version gating recovery. Set deliberately or not at all — see §9.3 |
| `REIMAGINA_EXECUTOR_HEARTBEAT_S` | `30` | How often a process publishes liveness |
| `REIMAGINA_EXECUTOR_STALE_S` | `300` | How silent an executor must be before a peer presumes it dead — see §9.9 |
| `REIMAGINA_RECLAIM_SWEEP_S` | half the window | How often the worker looks for dead executors |
| `REIMAGINA_RECLAIM_ENABLED` | `1` | Set `0` to disable the sweep entirely |

## 5. Running the platform

Three processes, each in its own terminal.

```bash
make dev-api      # FastAPI on :8001
make dev-web      # Vite on :5173
DATABASE_URL_DIRECT=... python -m apps.worker
```

The worker is required for anything to compute. Without it the run dashboard
shows submitted runs that never progress, which looks like a hang and is not.

## 6. Verifying the installation

```bash
curl -s localhost:8001/health          # {"status":"ok","db":"reachable"}
curl -s localhost:8001/openapi.json | head -c 80
open http://localhost:5173
```

Then the suites. Four are gated in CI; three are hand-run.

```bash
pytest tests/unit tests/contract       # fast, no services
pytest tests/integration               # needs PostgreSQL + PostGIS
pytest tests/conformance               # needs PostgreSQL + PostGIS
cd apps/web && npm test                # web unit tests
```

> **Do not run a bare `pytest`.** It collects every suite, including
> `tests/integration`, whose session fixture ends by dropping every managed
> schema from `TEST_DATABASE_URL`. Name the suites you want.

For an end-to-end check that the platform *does* something rather than merely
starts — a populated database, eleven computed indicators, tiles with features
in them — run `scripts/acceptance_run.sh` (§7.1). It needs no credentials.

The three hand-run suites are `performance` (benchmark budgets and
explain-plan shape), `workloads` (how an ops plane schedules, over synthetic
DAG topologies) and `recovery` (crash recovery across a real process kill;
opt-in through `REIMAGINA_RECOVERY_TEST=1`).

### 6.1 Verifying the orchestration module

Schedule A names the orchestration module as a deliverable, and it is the
component whose behaviour is least visible from the outside — a run that
succeeds looks the same whether or not it would have survived the worker
being killed. These are the checks that distinguish the two, in increasing
order of what they cost you.

| Check | Command | What a pass means |
|---|---|---|
| The port contract | `make test-conformance` | 17 behaviours — submit, retrieve, cancel, subscribe, partial failure — hold against **both** shipped implementations. A new engine that cannot satisfy them is rejected by a test rather than by an argument |
| It runs through the queue | `scripts/acceptance_run.sh`, step 12 | A separate `python -m apps.worker` process announced itself, dequeued a run, and drained it. Step 12b prints each unit's start offset: units at the same offset ran concurrently, a later one waited for a dependency |
| A resubmission is not a second run | `scripts/acceptance_run.sh`, step 12c | The same request submitted twice returned one run id, the second flagged `deduplicated` |
| It survives its executor dying | `make test-recovery` | A process `SIGKILL`ed mid-compute was resumed — by itself on restart, and by a *different* executor through the liveness sweep |

⚠ **`make test-recovery` needs a database with at least 20,000 blocks and
skips below that.** The territory in §7.1 has sixteen, so running it against
that database skips both tests — and a skip is one character away from a pass
in pytest's output. The skip message names the remedy; it is:

```bash
DATABASE_URL=$TEST_DATABASE_URL python scripts/synthetic_territory.py \
    --blocks 150 --ageb-side 10          # 22,500 blocks, ~40 s, no credentials
REIMAGINA_RECOVERY_TEST=1 make test-recovery
```

The size is not arbitrary. The suite kills a worker *while a unit is
running*, so the run has to take long enough for the kill to land inside it.
On a sixteen-block territory every unit finishes in milliseconds and the
crash arrives after the work is already done, which would prove the opposite
of what the test claims.

**What none of these cover.** `evaluate-scenario --sync` runs the coordinator
in-process with no ops plane at all. It is the path that still works when the
engine is down, and steps 10 and 11 of the acceptance run use it — so a green
acceptance run before step 12 says nothing about the orchestrator. That is
why step 12 exists.

### 6.2 Verifying live vector tiles, with or without Martin

Live vector tiles are a Postgres function plus an HTTP wrapper, and the
wrapper is interchangeable. The contract is the **function signature** —
every tile function takes `(z, x, y, query_params json)` and returns
`bytea` — which is Martin's function-source convention, so anything
implementing it is a drop-in.

| Server | Address | What it is |
|---|---|---|
| **Martin** | `:3000` | `ghcr.io/maplibre/martin`, connecting to PostgreSQL directly. The default |
| **The API** | `:8001/tiles` | `apps/api/routers/mvt.py`, same functions, same process as the rest of the API |

Choosing between them is one environment variable:

```bash
REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:3000   # Martin (default)
REIMAGINA_TILE_LIVE_BASE_URL=/api/tiles              # the API, same-origin
```

Nothing else changes: not `infra/tiles/layers.json`, not `/tile-sources`, not
the frontend.

⚠ Use the **relative** form for the API. An absolute URL such as
`http://localhost:8001/tiles` makes every tile request cross-origin from the
web application, so it depends on the API's CORS allowlist — which defaults
to `http://localhost:5173` and rejects a browser that opened the page as
`http://127.0.0.1:5173`. The symptom is a map that draws nothing, with no
console error naming the cause. `/api/tiles` is served through the same proxy
the rest of the API uses, so the question does not arise. A contract test asserts both forms resolve to the same URL
shape, because that property is the only thing that makes two
implementations safe to keep.

**Which to use.** Martin in production: a Rust tile server with its own
connection pool is the better machine when many clients pan at once.
Measured here, `serving_geo.get_tile` answers a populated z16 tile in
**~1.5 ms**, so at the scale of a single reviewer or a demo neither Postgres
nor the API is the constraint, and running one fewer service is worth more
than the headroom.

**Verify a tile without a browser:**

```bash
# A tile with features: 200, and a vector-tile content type
curl -sD- -o /tmp/tile.mvt   "http://localhost:8001/tiles/get_tile/16/11748/26478" | head -5

# Decode it — no dependencies, the decoder is in this repository
python scripts/mvt_inspect.py /tmp/tile.mvt

# A tile outside the loaded territory: 204 No Content, empty by design
curl -s -o /dev/null -w '%{http_code}
'   "http://localhost:8001/tiles/get_tile/14/0/0"

# TileJSON, for a client that prefers discovery
curl -s "http://localhost:8001/tiles/get_tile" | python -m json.tool
```

`scripts/mvt_inspect.py` prints the layer name, the spec version, the extent,
and a breakdown by geometry type and `type_slug` — so "the tile has features"
is a statement about decoded content rather than about a byte count.

This path is covered end-to-end by
`tests/integration/test_mvt_route_serves_tiles.py`, which fetches real tiles
over HTTP and decodes them. Martin is an external binary and has no
equivalent coverage here, which is the other reason both exist:
`docs/architecture/TILE_SERVING.md` has the full argument.

## 7. Loading data

### 7.1 Without credentials — a generated territory

If you have no access to the data lake, this is the path to a populated
system. It takes about a minute and needs no network:

```bash
scripts/acceptance_run.sh
```

The script drops the database named in `DATABASE_URL`, migrates it from
empty, generates a small territory on Mexicali coordinates — AGEBs containing
blocks containing buildings, a pedestrian street graph, three amenities, an
area of interest and a single-band population raster — and then runs every
analysis stage over it: feature overlaps, all eleven indicators including the
pgRouting network family, raster ingest through COG staging, zonal
statistics, an observation rollup, a scenario, and the live tile functions.
Any failure stops the run.

To generate the territory without the rest:

```bash
python scripts/synthetic_territory.py --blocks 6
python scripts/synthetic_territory.py --raster-out /tmp/population.tif
```

Everything is written through the production loaders, so what lands passes
the same triggers and carries the same provenance columns as an ingested
dataset. ⚠ **The geometry is a regular grid, not a city.** Use it to exercise
and demonstrate the platform; never to draw a conclusion about Mexicali.

`docs/delivery/05_capability_proof.md` records what one run of the script
produced, capability by capability.

### 7.2 With credentials — the real dataset

The pipeline ingests from a private Azure data lake (`cfcetlsadls/raw`) that
mirrors INEGI 2024 marco geoestadístico, INEGI 2020 census, SIGCDMX/EXE
amenities, Overture and WorldPop. `docs/AZURE_DATA_INVENTORY.md` documents
the expected layout under `data/sources/azure/`.

**You need read access to that container to reproduce the dataset.** Without
it, ingestion fails at the source-existence check; §7.1 is the alternative.
`docs/BOOTSTRAP.md` is the full fresh-machine runbook.

The shape of a load, once the mirror is in place:

```bash
python -m apps.cli.app feat seed-types
python -m apps.cli.app inegi-marco bootstrap --cve-ent 02
python -m apps.cli.app points bootstrap
python -m apps.cli.app indicators seed-catalog
python -m apps.cli.app indicators compute --slug urban_form_block_compactness
```

## 8. Operating the platform

### 8.1 Inspection

Inspection goes through the `explore` schema, which exists for humans and is
never an application dependency:

```bash
python -m apps.cli.app explore layers
python -m apps.cli.app explore sources
python -m apps.cli.app explore feature-stats
python -m apps.cli.app explore serving-stats
python -m apps.cli.app explore executor-health
```

### 8.2 Runs

Runs are submitted, observed and controlled through the API and the run
dashboard at `/ops.html`:

| Action | Endpoint |
|---|---|
| Submit | `POST /jobs` |
| List, with filters | `GET /jobs` |
| Detail, including units | `GET /jobs/{run_id}/detail` |
| Live progress | `GET /jobs/{run_id}/events` (server-sent events) |
| Cancel | `POST /jobs/{run_id}/cancel` |
| Re-run | `POST /jobs/{run_id}/rerun` |

There is no numeric progress field anywhere in the system. Progress is
derivable only as terminal units over total units, and the dashboard computes
it that way.

### 8.3 Command groups

`python -m apps.cli.app --help` lists them all. The groups are: `explore`,
`tiles`, `assets`, `feat`, `overlap`, `indicators`, `raster`, `aoi`, `data`,
`perf`, and the per-source ingest groups `cdmx`, `inegi-marco`, `inegi-censo`,
`points`, `overture`.

## 9. Troubleshooting

Each of these was encountered while verifying this document.

### 9.1 `make migrate` fails with `fe_sendauth: no password supplied`

The admin connection has no credentials. `make migrate` reads
`DATABASE_URL_ADMIN` from `.env`; if it is absent the built-in default is
`postgresql://localhost/postgres`, which carries no user and no password and
will be refused by any PostgreSQL that asks for one. Set it, with a role that
may create databases and extensions.

### 9.2 Runs are submitted but nothing computes; or checkpoints vanish

`DATABASE_URL_DIRECT` is unset or points at the pooler. The ops plane needs a
real server connection: progress streams use `LISTEN`/`NOTIFY`, which cannot
cross a transaction-mode pooler. When the variable is missing entirely, DBOS
falls back to a local SQLite file — the process works, and its checkpoints are
private and unrecoverable. The code raises rather than allow this, so the
symptom is a loud error naming the variable.

⚠ **Changing `DATABASE_URL_DIRECT` after the process has started does not
move it.** The ops plane builds a pooled DBOS client the first time it needs
one and keeps it for the life of the process. That client is keyed by DSN, so
pointing the variable somewhere else and re-running a command in the same
process gets a client for the new database — but a long-lived process that
already launched against the old one keeps its executor and its queues there.
Restart it.

This was worse until recently: the client was a single global built on first
use, so the *first* DSN a process ever saw became the database every later
query read. Nothing raised. `get_run` uses the engine as its existence oracle,
so a client bound to the wrong database reported a live run as absent, `cancel`
returned false for a run that was running, and a progress subscription yielded
`unknown` and closed — three confident wrong answers and no error. It is fixed
and covered by `tests/unit/test_dbos_client_cache.py`.

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

That is by design, and it is why `TEST_DATABASE_URL` must name a database
ending in `_test` — the fixture refuses anything else. Never point it at a
database you care about.

### 9.6 `npm audit` reports vulnerabilities after installation

It currently reports eight, **none critical**. Only one is reported against
the production tree, and it does not reach the browser: `devalue` arrives
through `svelte`, and no `devalue` code appears anywhere in `dist/` — verified
by searching the built output rather than assumed. The rest are build-time
only.

`npm audit` does not distinguish what ships from what builds;
`docs/THIRD_PARTY_LICENSES.md` does, in its `Distribution` column.

### 9.7 Path A requires `DATABASE_URL_DIRECT` before the stack will start

Martin's service definition interpolates `DATABASE_URL_DIRECT`, so
`make stack-up` fails at configuration time if it is unset — before any
container is created. In Path A it must name port **5433**, PostgreSQL
behind the pooler, not 5432, which is the pooler itself.

### 9.9bis The map has no tiles and Martin is not running

Nothing is broken; the deployment simply has no tile server at `:3000`. Live
vector tiles have two servers behind one contract, and the API can serve them
itself:

```bash
REIMAGINA_TILE_LIVE_BASE_URL=http://localhost:8001/tiles python -m apps.api
```

That is the whole change. §6.2 explains the choice.

### 9.8 Known verification caveat

The containers themselves were never started in the environment used to verify
this document: Docker Hub refused anonymous image pulls with HTTP 429. What
*was* verified there is that the stack definition parses and that every
variable it requires resolves — `docker compose config` validates clean with a
complete Path A environment, and the service, image and port mapping in §1 is
read from that resolved configuration rather than from the file by eye.

Everything else in this document was executed end to end.

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
python -m apps.cli.app explore executor-health
```

If the dead executor is still listed with a large `heartbeat_age`, no live
worker has swept it yet — wait one sweep interval. If it is not listed and the
run is still `running`, the executor died before it ever published liveness,
which means it was running code older than this feature; restart a worker with
that same `REIMAGINA_EXECUTOR_ID` and `launch()` will reclaim it directly.

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

## 10. Appendix — a verified clean install

The sequence below was executed in a fresh clone, in this order, and each step
is reported with its actual result.

| Step | Command | Result |
|---|---|---|
| 1 | `cp .env.example .env` | OK |
| 2 | `make install` | OK |
| 3 | `make migrate` | Both databases created and migrated to `r047`; 24 tables in `curated_geo` |
| 3b | `python -m apps.cli.app verify-schema` | `all_present: true` across the five canonical relations |
| 4 | `make dev-api` | `/health` 200, `{"status":"ok","db":"reachable"}`; `/docs` 200 |
| 5 | `make dev-web` | Vite ready; `/`, `/catalog.html`, `/ops.html` all 200 |
| 6 | `pytest tests/unit tests/contract` | 655 passed |
| 7 | `pytest tests/integration` | 341 passed, 1 skipped |
| 8 | `pytest tests/conformance` | 17 passed, 1 skipped |
| 9 | `pytest tests/unit tests/contract tests/integration tests/conformance tests/e2e` | 1,013 passed, 14 skipped |
| 10 | `cd apps/web && npm test` | 73 passed |

Measured against PostgreSQL 16.15, PostGIS 3.7 and pgRouting 4.0.2. CI pins
PostGIS 3.4 and pgRouting 3.6.1; the schema is identical across both.

Step 9 is the whole tree in one process, and it is listed separately because
it is a different assertion from steps 6–8 run in sequence. CI runs each suite
as its own process, so nothing there exercises state cached across suites — and
a reader's first instinct is `pytest`, not four invocations. One such leak
existed and is fixed (`dbos_orchestrator._client()` cached a client against
the first DSN the process saw; see §9.2). The skips are the browser suite,
which needs servers running, and two fixtures that need upstream data.

The database-backed suites are run with `REIMAGINA_REQUIRE_DB=1`. Without it
`pytest` exits 0 when every database test *skips*, and "322 skipped, 6 passed"
reads as success in every summary anyone looks at. With it, an unreachable
database fails instead of skipping.

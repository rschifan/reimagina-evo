# reimagina-evo

[![CI](https://github.com/rschifan/reimagina-evo/actions/workflows/ci.yml/badge.svg)](https://github.com/rschifan/reimagina-evo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Architectural prototype of a platform that computes urban indicators and explores planning scenarios. A feature/observation/indicator/scenario data model on PostGIS, served as live vector tiles (from the API itself, or Martin behind the same URL contract) and Cloud-Optimized GeoTIFFs (titiler) to a MapLibre-GL web application.

> **Release 1.0.0.** The delivery documents — architecture, database and API
> reference, technical documentation — are in
> [`docs/delivery/`](docs/delivery). Installation, configuration and operation
> are covered in full by the
> [technical documentation](docs/delivery/04_technical_documentation.md);
> this README is the short path.

## What's in here

| Path | Purpose |
|---|---|
| `apps/api/` | FastAPI API: tile sources, live vector tiles, indicators, downloads, scenarios, run control, in-process titiler |
| `apps/worker/` | Ops-plane worker — drains the `runs` and `units` queues that compute indicators and scenario recomputes |
| `apps/web/` | Web application (Vite + TypeScript, Svelte 5, MapLibre-GL): map, scenario workspace, indicator catalogue, run dashboard |
| `apps/cli/` | Operator CLI for ingest, compute, scenarios, tiles, inspection |
| `packages/py/reimagina/` | Backend domain packages (`assets`, `core`, `data`, `db`, `indicators`, `jobs`, `orchestration`, `perf`, `pipeline`, `scenarios`, `storage`, `tiles`) |
| `infra/` | Alembic migrations (`infra/db/`), the container stack (`infra/docker/`), the tile manifest (`infra/tiles/`) |
| `scripts/` | `acceptance_run.sh` (credential-free end-to-end run), `mexicali_run.sh` (the real Mexicali load), generators and tools |
| `docs/` | [`delivery/`](docs/delivery) (the delivery documents), [`architecture/`](docs/architecture) (per-layer references), [`BOOTSTRAP.md`](docs/BOOTSTRAP.md) (data-load runbook) |

## Quickstart

Requirements: Python 3.12+ (`uv` installs it if needed), [`uv`](https://docs.astral.sh/uv/),
Node.js `^22.22.2 || ^24.15.0 || >=26`, PostgreSQL 16+ with PostGIS 3.4+ and
pgRouting 3.6+ (`CREATE EXTENSION pgrouting` needs a superuser). Docker is
optional — only for the container stack. GDAL ≥ 3.8 (`ogr2ogr`) is needed
only to export PMTiles snapshots; the Python packages bundle their own GDAL.

```bash
# 1. Configure environment
cp .env.example .env                 # Path A: set POSTGRES_PASSWORD, and the
                                     # same password in the five DSNs

# 2. Bring up the stack (Postgres + PostGIS + pgRouting, pgbouncer, Martin)
make stack-up                        # skip on Path B (host PostgreSQL)

# 3. Install deps + apply migrations
make install                         # uv sync + npm ci
make migrate                         # creates both DBs, then alembic upgrade head

# 4. Run the API, the web app and the worker — Ctrl-C stops all three
make dev                             # API :8001, web http://localhost:5173
```

Ports are `make` variables: `make dev API_PORT=8123 WEB_PORT=5199`. `make dev`
checks they are free first, and points the map's tiles at the API
(`/api/tiles`), so no tile server is needed. To run the processes in separate
terminals: `make dev-api`, `make dev-web`, `make dev-worker` (pass the same
`API_PORT` to the first two). Without the worker, runs stay queued.

Python commands go through `uv run` (e.g. `uv run python -m apps.cli.app --help`),
which uses the environment `make install` created; bare `python`/`pytest` do
not have the project's dependencies.

`make migrate` takes no arguments: it reads `DATABASE_URL`,
`TEST_DATABASE_URL` and `DATABASE_URL_ADMIN` from your `.env`. The last one is
the connection it uses to *create* the other two databases — creating a
database requires a connection to a different one — so it needs a role that
may create databases and extensions. `.env.example` sets all three for both
paths.

In Path A only one of the five DSNs goes through the pooler, and that is
deliberate: `localhost:5432` is PgBouncer in transaction mode holding a single
database, so migrations, bootstrap, the test suites and the ops plane all
connect to PostgreSQL itself on `localhost:5433`. `.env.example` says why
against each line.

**Path B — host PostgreSQL.** Uncomment the Path B block in `.env` instead of
Path A and skip step 2. `make migrate` creates both databases (or create them
yourself: `createdb reimagina_app` and `createdb reimagina_test`). Path B's
DSNs name no user, so they assume your OS username is a Postgres role that can
connect without a password and create databases — the usual Homebrew or
Postgres.app arrangement. If yours is not, put a user and password in the five
Path B DSNs.

## Verify

```bash
scripts/acceptance_run.sh
```

Builds the platform from an empty database and exercises every layer: it
generates a small territory on Mexicali coordinates, computes all twelve
indicators, ingests a raster, authors a scenario, has a worker drain a run and
decodes live tiles. It needs no credentials and no network, and takes about a
minute and a half.

⚠ **It drops and recreates the database named by `DATABASE_URL`.** Point
`DATABASE_URL` at a dedicated database you can lose — never one holding a real
load.

## Data

The real territory needs two things the repository does not contain:

- **The lake mirror** — INEGI Marco Geoestadístico 2024, the INEGI 2020
  census and municipal amenities, copied once from a private Azure data lake
  (`cfcetlsadls/raw`) to `data/sources/azure/` with credentials (contact the
  maintainer). The loaders read that offline copy; layout in
  [`docs/AZURE_DATA_INVENTORY.md`](docs/AZURE_DATA_INVENTORY.md).
- **Internet access** at load time — Overture buildings and places (public
  S3), the OpenStreetMap pedestrian network (Overpass) and, optionally,
  WorldPop.

`scripts/mexicali_run.sh` loads Mexicali end to end;
[`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md) is the step-by-step runbook. Without
the mirror everything else works — install, migrate, run, test, build, and the
generated territory above.

## Tests

```bash
make test                # unit + contract, no services
make test-integration    # needs PostgreSQL + PostGIS (TEST_DATABASE_URL)
make test-conformance    # needs PostgreSQL + PostGIS (TEST_DATABASE_URL)
make test-web            # web unit tests (vitest)
```

CI gates lint (ruff, mypy), these four Python suites (unit and contract,
integration, conformance), the web build and the web unit tests. Four further
suites are hand-run:

| Suite | Why it is not gated |
|---|---|
| `tests/performance` | benchmark harness; budgets live in `docs/architecture/PERFORMANCE.md` |
| `tests/workloads` | measures *how* an ops plane schedules, and records what two planes may differ on — `make test-workloads`, see [`tests/workloads/README.md`](tests/workloads/README.md) |
| `tests/recovery` | kills real processes with SIGKILL and needs a test database with at least 20,000 blocks — `make test-recovery`; the technical documentation §6.1 says how to build one |
| `tests/e2e` | opens every page in a real browser; needs both servers and Playwright — `make test-e2e`, see [`tests/e2e/README.md`](tests/e2e/README.md) |

⚠ `TEST_DATABASE_URL` must name a database whose name contains `test`: the
integration suite's session fixture ends by dropping every managed schema from
it. For the same reason a bare `pytest` — which collects every suite in one
process — is the wrong command; name the suites you want.

`tests/conformance` asserts the `Orchestrator` port contract against each
implementation the branch ships; see
[`tests/conformance/README.md`](tests/conformance/README.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE). If you use this in research, please cite via [CITATION.cff](CITATION.cff).

# reimagina-evo

[![CI](https://github.com/rschifan/reimagina-evo/actions/workflows/ci.yml/badge.svg)](https://github.com/rschifan/reimagina-evo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Urban planning scenario modelling platform. A feature/observation/indicator/scenario data model on PostGIS, served as live vector tiles (Martin) and Cloud-Optimized GeoTIFFs (titiler) to a MapLibre-GL read surface.

> **Status:** functional prototype — API and schema contracts are not yet stable. See [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) for the current state of the system.

> **⚠ This branch is `feat/ops-plane-hatchet` — one of two ops-plane reference implementations.**
> The fork is deliberate and the two branches **never merge**; the deliverable is the
> side-by-side, not a winner.
>
> | Branch | Ops-plane | Shape |
> |---|---|---|
> | `feat/ops-plane-hatchet` (this one) | [Hatchet](https://hatchet.run), self-hosted | durable execution as a **service** — a standing engine owns scheduling *and* run state; our scheduler and run tables are deleted |
> | `feat/ops-plane-dbos` | [DBOS Transact](https://dbos.dev) | durable execution as a **library** — no standing service; the scheduler and run tables stay |
>
> Both are MIT with no gated features. `main` carries only engine-neutral work.
> [`JOB_LAYER.md`](docs/architecture/JOB_LAYER.md) documents the split,
> [`OPS_PLANE_RUNBOOK.md`](docs/architecture/OPS_PLANE_RUNBOOK.md) runs the engine, and
> [`PHASE3_OPS_PLANE_MEASUREMENT.md`](docs/product/PHASE3_OPS_PLANE_MEASUREMENT.md) defines
> what is measured.
>
> **A worker is mandatory here.** Nothing executes without one, and a submitted run is not
> even *observable* until a worker has registered the workflow.

## What's in here

| Path | Purpose |
|---|---|
| `apps/api/` | FastAPI read-side API (tile-sources, indicators, downloads, in-process titiler) |
| `apps/worker/` | Ops-plane worker — registers the indicator DAG with the Hatchet engine and executes its tasks. Validates credentials, database identity, migrations and runner coverage, then starts slots; no scheduler, no heartbeat, no reaper |
| `apps/web/` | MapLibre-GL web app (Vite + TypeScript, no framework) |
| `apps/cli/` | Operator CLI for ingest, publishing, status |
| `packages/py/reimagina/` | Backend domain packages (`data`, `pipeline`, `storage`, `tiles`, `indicators`, `scenarios`, `orchestration`, `jobs`) |
| `infra/db/` | Alembic migrations + seeds |
| `docs/` | [`SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) (presentation) + [`architecture/`](docs/architecture) (canonical references) |

## Data access (read first)

The pipeline ingests from a private Azure data lake (`cfcetlsadls/raw`)
that mirrors INEGI 2024 marco geoestadístico, INEGI 2010 census,
SIGCDMX/EXE amenities, and other upstream sources.
[`docs/AZURE_DATA_INVENTORY.md`](docs/AZURE_DATA_INVENTORY.md)
documents the expected layout under `data/sources/azure/`.

You need read access to that container to reproduce the prototype's
dataset. **Contact the maintainer** for credentials before following the
bootstrap runbook below.

Without lake access you can still: install the stack, run migrations,
run unit + contract tests, lint, type-check, and build the web app —
but ingest steps 2/5/7 in [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md) will
fail at the source-existence check.

## Quickstart

Requirements: Python 3.11+, Node 20+, Postgres 16 + PostGIS 3.4, Docker
(for the local stack), GDAL ≥3.6 (for `fiona`/`rasterio` —
`brew install gdal` on macOS, `apt install libgdal-dev` on
Debian/Ubuntu).

```bash
# 1. Configure environment
cp .env.example .env                 # fill in POSTGRES_PASSWORD etc.

# 2. Bring up the stack (Postgres + PostGIS + pgbouncer + Martin tiles)
make stack-up                        # or: docker compose -f infra/docker/docker-compose.yml --env-file .env up -d

# 3. Install deps + apply migrations
make install                         # uv + npm
make migrate                         # alembic upgrade head

# 4. Run dev servers (two terminals)
make dev-api                         # FastAPI on :8000
make dev-web                         # Vite on :5173
```

If you prefer host-installed Postgres over the compose stack, switch to
"Path B" inside `.env.example`, run `createdb reimagina_app reimagina_test`
yourself, and skip step 2.

### The ops-plane (needed to run a scenario evaluation)

Reads work without it. Submitting a run does not — this branch has no
in-process mode.

```bash
createdb hatchet && psql -d hatchet -c "ALTER DATABASE hatchet SET TIMEZONE='UTC'"
docker compose -f infra/docker/docker-compose.hatchet.yml up -d
```

Then mint a token for the **`default`** tenant from the dashboard on
<http://localhost:8888>, export it as `HATCHET_CLIENT_TOKEN`, and start a worker:

```bash
python -m apps.worker
```

⚠ Supervise that worker as a **process tree** — its own container as PID 1, or a
supervisor that signals the process group. The SDK runs its action listener in a
spawned child process, so signalling only the parent leaves an orphan that keeps
heartbeating, and the engine keeps assigning it work it can no longer execute.
[`OPS_PLANE_RUNBOOK.md`](docs/architecture/OPS_PLANE_RUNBOOK.md) has the details,
the TLS/credential checklist, and the retention job that must outlive engine run
retention.

See [`docs/architecture/`](docs/architecture) for per-layer deep-dives.
For the full fresh-machine data bootstrap, follow
[`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md).

## Tests

| Suite | Needs | Gated by CI | What it is for |
|---|---|---|---|
| `tests/unit` `tests/contract` | nothing | ✓ | fast, no services |
| `tests/integration` | Postgres + PostGIS | ✓ | real SQL against a real database |
| `tests/conformance` | Postgres **+ engine + worker** | ✓ (engine job) | the `Orchestrator` port contract, asserted against every implementation this branch ships — [README](tests/conformance/README.md) |
| `tests/qualification` | engine | ✓ (engine job) | whether the **engine** can carry worker lifecycle. If it fails, the answer is a different engine release, never an application-side registry — [README](tests/qualification/README.md) |
| `tests/workloads` | Postgres + engine | — | how this ops-plane *schedules*, measured. `REIMAGINA_BENCH=1` — [README](tests/workloads/README.md) |
| `tests/performance` | Postgres | — | hand-run harness |

```bash
make test                # unit + contract
make test-integration
make test-conformance
make test-qualification
make test-workloads      # REIMAGINA_BENCH=1, needs the bench database
```

⚠ **A skip in `tests/conformance` or `tests/qualification` is a result, not a pass.**
Without `HATCHET_CLIENT_TOKEN` they skip, and pytest exits 0 — so CI could stand an
engine up, register nothing, and report success having executed zero engine
assertions. With a token set they **fail** instead of skipping.

⚠ A bare `pytest` collects every suite, including `tests/integration` — whose
session fixture ends by dropping every managed schema from `TEST_DATABASE_URL`.
Name the suites you want.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE). If you use this in research, please cite via [CITATION.cff](CITATION.cff).

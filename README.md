# reimagina-evo

[![CI](https://github.com/rschifan/reimagina-evo/actions/workflows/ci.yml/badge.svg)](https://github.com/rschifan/reimagina-evo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Urban planning scenario modelling platform. A feature/observation/indicator/scenario data model on PostGIS, served as live vector tiles (Martin) and Cloud-Optimized GeoTIFFs (titiler) to a MapLibre-GL read surface.

> **Status:** functional prototype — API and schema contracts are not yet stable. See [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) for the current state of the system.

## What's in here

| Path | Purpose |
|---|---|
| `apps/api/` | FastAPI read-side API (tile-sources, indicators, downloads, in-process titiler) |
| `apps/worker/` | Ops-plane worker — drains the `compute` + `control` queues that recompute indicators after a scenario edit |
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
make migrate                         # creates both DBs, then alembic upgrade head

# 4. Run dev servers (two terminals)
make dev-api                         # FastAPI on :8001
make dev-web                         # Vite on :5173
```

`make migrate` takes no arguments: it reads `DATABASE_URL`,
`TEST_DATABASE_URL` and `DATABASE_URL_ADMIN` from your `.env`. The last one is
the connection it uses to *create* the other two databases — creating a
database requires a connection to a different one — so it needs a role that
may create databases and extensions. `.env.example` sets all three for both
paths; if you change the password, change it there too.

If you prefer host-installed Postgres over the compose stack, switch to
"Path B" inside `.env.example`, run `createdb reimagina_app reimagina_test`
yourself, and skip step 2. Path B's DSNs name no user, so they assume your own
OS username is a Postgres role that can connect without a password and create
databases — the usual Homebrew or Postgres.app arrangement. If yours is not,
put a user and password in the four Path B DSNs.

See [`docs/architecture/`](docs/architecture) for per-layer deep-dives.
For the full fresh-machine data bootstrap, follow
[`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md).

## Tests

```bash
pytest tests/unit tests/contract       # fast, no services
pytest tests/integration               # requires Postgres+PostGIS
pytest tests/conformance               # requires Postgres+PostGIS
```

CI gates all three. `tests/performance` is a hand-run harness and is gated
nowhere.

⚠ A bare `pytest` collects every suite, including `tests/integration` — whose
session fixture ends by dropping every managed schema from `TEST_DATABASE_URL`.
Name the suites you want.

`tests/conformance` asserts the `Orchestrator` port contract against each
implementation the branch ships; see
[`tests/conformance/README.md`](tests/conformance/README.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE). If you use this in research, please cite via [CITATION.cff](CITATION.cff).

# reimagina-evo

[![CI](https://github.com/rschifan/reimagina-evo/actions/workflows/ci.yml/badge.svg)](https://github.com/rschifan/reimagina-evo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Urban planning scenario modelling platform. A feature/observation/indicator/scenario data model on PostGIS + DuckDB, served as live vector tiles (Martin) and Cloud-Optimized GeoTIFFs (titiler) to a MapLibre-GL read surface.

> **Status:** functional prototype — API and schema contracts are not yet stable. See [`docs/SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) for the current state of the system.

## What's in here

| Path | Purpose |
|---|---|
| `apps/api/` | FastAPI read-side API (tile-sources, indicators, downloads, in-process titiler) |
| `apps/worker/` | Async job runner (overlap, rollups, raster zonal stats, aggregation recipes) |
| `apps/web/` | MapLibre-GL web app (Vite + TypeScript, no framework) |
| `apps/cli/` | Operator CLI for ingest, publishing, status |
| `packages/py/reimagina/` | Backend domain packages (`data`, `pipeline`, `storage`, `tiles`, `jobs`) |
| `infra/db/` | Alembic migrations + seeds (dual Postgres+DuckDB) |
| `docs/` | [`SYSTEM_OVERVIEW.md`](docs/SYSTEM_OVERVIEW.md) (presentation) + [`architecture/`](docs/architecture) (canonical references) |

## Quickstart

Requirements: Python 3.11+, Node 20+, Postgres 16 + PostGIS 3.4, Docker (for Martin).

```bash
# 1. Python deps (uv recommended; pip also works)
uv sync                 # or: pip install -e '.[dev]'

# 2. DB schema (Postgres + DuckDB)
cp .env.example .env    # fill DSNs
alembic upgrade head

# 3. Run the API + web app (two terminals)
uvicorn apps.api.app:app --reload
cd apps/web && npm install && npm run dev
```

See [`docs/architecture/`](docs/architecture) for per-layer deep-dives.

## Tests

```bash
pytest tests/unit tests/contract       # fast, no services
pytest tests/integration               # requires Postgres+PostGIS
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE). If you use this in research, please cite via [CITATION.cff](CITATION.cff).

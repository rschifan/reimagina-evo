# r-evo

[![CI](https://github.com/rschifan/reimagina-evo/actions/workflows/ci.yml/badge.svg)](https://github.com/rschifan/reimagina-evo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

r-evo is an architectural prototype of a platform that computes urban indicators and explores planning scenarios. A feature/observation/indicator/scenario data model on PostGIS, served as live vector tiles (from the API itself, or Martin behind the same URL contract) and Cloud-Optimized GeoTIFFs (titiler) to a MapLibre-GL web application.

Start with the tracked [installation guide](docs/INSTALLATION.md) for macOS
and Linux, and the [architecture guide](docs/architecture/README.md) for the
system design. The [verification record](docs/INSTALLATION_VERIFICATION.md)
states the tested environments and limits. These instructions cover local
evaluation; keep the unauthenticated API on loopback.

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
| `docs/` | [`architecture/`](docs/architecture) (per-layer references), [`BOOTSTRAP.md`](docs/BOOTSTRAP.md) (data-load runbook) |

## Installation

Follow [docs/INSTALLATION.md](docs/INSTALLATION.md) for the macOS and Linux
prerequisites, Docker or native PostgreSQL setup, all five database URLs,
migrations and startup. Use Python 3.12 for the verified installation path;
Node must satisfy `^22.22.2 || ^24.15.0 || >=26`. Linux ARM64 also needs GEOS
headers and a C++ build toolchain for `exactextract`.

After configuration and database readiness, `make install` installs locked
Python and web dependencies, `make migrate` bootstraps both databases, and
`make dev` starts the API (:8001), web (:5173) and worker. Without data the map
is empty; without the worker submitted runs stay queued.

For a generated territory, use the guide's
[scratch verification procedure](docs/INSTALLATION.md#7-verify-with-synthetic-data).
`scripts/acceptance_run.sh` **drops and recreates its target database**;
configure a separate scratch checkout before running it.

## Data

The real territory needs two things the repository does not contain:

- **The lake mirror** — INEGI Marco Geoestadístico 2024, the INEGI 2020
  census and municipal amenities, copied once from a private Azure data lake
  (`cfcetlsadls/raw`) to `data/sources/azure/` with credentials (contact the
  maintainer). The loaders read that offline copy; the layout comes with the
  credentials.
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
| `tests/recovery` | kills real processes with SIGKILL and needs a test database with at least 20,000 blocks — `make test-recovery`; see [`tests/recovery/test_crash_recovery.py`](tests/recovery/test_crash_recovery.py) for prerequisites |
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

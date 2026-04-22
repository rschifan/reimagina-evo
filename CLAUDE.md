# CLAUDE.md

Guidance for coding agents working in this repository.

## Layout

- `apps/` — deployable entrypoints (`api`, `worker`, `web`, `cli`)
- `packages/py/reimagina/` — backend namespace (`core`, `db`, `data`, `pipeline`, `storage`, `tiles`, `jobs`, reserved `scenarios`/`indicators`)
- `packages/ts/` — frontend/shared TS
- `infra/db/` — migrations + seeds
- `docs/architecture/` — source of truth (read order: `TERMINOLOGY` → `DATA_LAYER` → `SCENARIOS_LAYER` → `JOB_LAYER` → `INDICATORS_LAYER` → `TILE_SERVING` → `API` → `PERFORMANCE` → `GEOSPATIAL_GRID_ARCHITECTURE` → `TESTING` → `TODO`)
- `tests/` — `unit`, `integration`, `contract`, `performance`

Rule: app code may import `reimagina.*`; domain packages must not import app code.

## Data model

Pipeline: `Bronze → Silver → curated_geo → serving_geo → API/workers`

Routing:
- `feature` = stable spatial objects with geometry + identity
- `observation` = measured/derived values about existing features

| `storage_type` | Lands in |
|---|---|
| `vector` | `feature` |
| `tabular` | `observation` |
| `raster` | asset + zonal stats → `observation` |
| `imagery` | asset + detections → `feature` |
| `point_cloud` | asset + derived metrics → `observation` |

`integration_role`: `canonical_entity` | `enrichment` | `model_output`.

## Schema contracts

| Schema | Role |
|---|---|
| `curated_geo` | canonical source of truth |
| `serving_geo` | app/worker read contract (ONLY read surface) |
| `explore` | engineer inspection (humans only; never imported by app/worker) |
| `app_core` | app/scenario tables |
| `jobs_runtime` | job execution state |

Inspection flow: `explore` → `serving_geo` → `curated_geo` (for lineage).

DB-ready check: `python -m apps.cli.app cdmx status`.

## Execution rules

- Baseline is never mutated by scenarios; workers apply overrides in memory.
- `feature` is the shared anchor across scenarios, observations, indicators.
- Workers must be idempotent.

## Tile serving

- Live MVT (`serving_geo.get_tile()` + Martin) is primary.
- PMTiles = snapshot artifacts for stable/context layers only.
- Vector tile classification: `infra/tiles/layers.json`; PMTiles metadata: `curated_geo.tile_snapshot_registry`; raster layers: `curated_geo.raster_layer`.
- Anything current/filtered/scenario-aware stays `live_mvt`.

## Testing

| Layer | Purpose |
|---|---|
| `tests/unit` | pure logic, routing, delegation |
| `tests/integration` | real DuckDB + Postgres/PostGIS behaviour |
| `tests/contract` | layout/boundary guardrails |
| `tests/performance` | benchmarks + SLO gates (see `PERFORMANCE.md`) |

Non-negotiable:
- Don't bend tests to code. Fix the code, or change the contract+docs+tests together.
- Prefer behaviour assertions over implementation-detail assertions.
- DuckDB green ≠ Postgres green for SQL/PostGIS semantics.
- New migrations and read surfaces require tests.

## DB/system change checklist

1. Update migration(s) — both backends when applicable.
2. Update `serving_geo` if app-facing read contract changed.
3. Update `explore` if engineers need a new inspection surface.
4. Update CLI inspection commands (they read `explore`).
5. Add/update tests at the correct layer.
6. Update docs when the contract changes.

If a change makes an engineer ask "how do I inspect this now?", the answer usually lives in `explore`.

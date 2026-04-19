# CLAUDE.md

Guidance for coding agents working in this repository.

## Repo shape

- `apps/`: deployable entrypoints (`api`, `worker`, `web`, `cli`)
- `packages/py/reimagina/`: shared backend namespace
- `packages/ts/`: frontend/shared TS packages
- `infra/db/`: migrations and DB seeds
- `docs/architecture/`: architecture source of truth
- `tests/`: `unit`, `integration`, `contract`, future `e2e`

Read the architecture docs in this order:
1. `TERMINOLOGY.md`
2. `DATA_LAYER.md`
3. `SCENARIOS_LAYER.md`
4. `JOB_LAYER.md`
5. `INDICATORS_LAYER.md`
6. `TILE_SERVING.md`
7. `API.md`
8. `PERFORMANCE.md`
9. `GEOSPATIAL_GRID_ARCHITECTURE.md`
10. `TESTING.md`
11. `TODO.md`

## Python namespace

- `reimagina.core`: shared utilities
- `reimagina.db`: connection and migration helpers
- `reimagina.data`: canonical data-layer logic
- `reimagina.pipeline`: bronze/silver/curated ingestion
- `reimagina.storage`: backend-agnostic storage Protocols + DuckDB/Postgres implementations
- `reimagina.tiles`: PMTiles snapshot + live MVT manifest helpers
- `reimagina.jobs`: async job orchestration (overlap computation, rollup aggregation)
- `reimagina.scenarios`, `reimagina.indicators`: reserved next-layer modules

Rule: app code may import `reimagina.*`; domain packages must not import app code.

## Data model in one page

Pipeline:

```text
Bronze -> Silver -> curated_geo -> serving_geo -> API/workers
```

Core routing rule:

- `feature`: stable spatial objects with geometry and identity
- `observation`: measured/derived values about existing features

Typical storage routing:

| `storage_type` | Lands in |
|---|---|
| `vector` | `feature` |
| `tabular` | `observation` |
| `raster` | asset storage + zonal stats -> `observation` |
| `imagery` | asset storage + detections -> `feature` |
| `point_cloud` | asset storage + derived metrics -> `observation` |

Integration roles:

| `integration_role` | Meaning |
|---|---|
| `canonical_entity` | defines stable city objects |
| `enrichment` | adds measurements to existing features |
| `model_output` | externally imported computed outputs |

## Database layer contracts

| Schema | Role |
|---|---|
| `curated_geo` | source of truth |
| `serving_geo` | app/worker read contract |
| `explore` | engineer-facing inspection contract |
| `app_core` | app/scenario tables |
| `jobs_runtime` | job execution state |

Keep these roles separate:

- API/workers read only `serving_geo`
- `curated_geo` stays canonical
- `explore` is read-only and for humans only
- app/worker logic must not depend on `explore`

Current `explore` surface:

- `explore.layer_catalog_v`
- `explore.source_runs_v`
- `explore.feature_type_stats_v`
- `explore.serving_stats_v`

Current `serving_geo` read surface (what API/workers consume):

- `serving_geo.feature_v` + role/type projections (`admin_v`, `blocks`, …)
- `serving_geo.get_tile()` (Martin-backed MVT path)
- `serving_geo.current_indicator_v` (Layer 2 read path; status='current' only, joined with catalog metadata)
- `serving_geo.aoi_summary_v` (cached AOI-level indicator values; populated by `reimagina.jobs.aoi_summary.summarise_aoi`)

Inspection workflow:

1. start from `explore`
2. go to `serving_geo` for consumer-facing checks
3. go to `curated_geo` for lineage/debugging

Recommended tools:

- DBeaver Community for SQL exploration
- QGIS only for visual geometry/map checks

Quickest way to confirm a DB has real data loaded:

```bash
python -m apps.cli.app cdmx status   # {"counts": {...}, "ready": true/false}
```

## Scenario / execution rules

- baseline is never mutated by scenarios
- workers read baseline from `serving_geo`, apply overrides in memory, write results back
- `feature` is the shared anchor across scenarios, observations, and indicators
- workers must be idempotent

## Tile serving rules

- live MVT (`serving_geo.get_tile()` + Martin) remains the primary map-serving path
- PMTiles are snapshot artifacts for stable reference/context layers only in v1
- if a layer must be current, filtered, scenario-aware, or fast-changing, it must stay `live_mvt`
- tile-layer classification lives in `infra/tiles/layers.json`
- PMTiles metadata lives in `curated_geo.tile_snapshot_registry`

## Testing rules

Testing is part of the architecture.

| Layer | Use for |
|---|---|
| `tests/unit` | pure logic, routing, delegation, normalization, config |
| `tests/integration` | real DuckDB + PostgreSQL/PostGIS behavior |
| `tests/contract` | repo/layout/boundary/architectural guardrails |
| `tests/performance` | benchmark smoke checks, explain-plan contracts, perf harness coverage |
| `docs/architecture/TESTING.md` | DB contract target and pgTAP intent |

Non-negotiable rules:

- do not bend tests to the code
- fix code to satisfy the intended contract, or intentionally change the contract and update docs/tests together
- prefer behavior/contract assertions over implementation-detail assertions
- DuckDB success does not prove Postgres correctness for SQL semantics, triggers, views, indexes, or PostGIS behavior
- new migrations and new read surfaces must come with tests
- tests should stay deterministic and small-scope
- `docs/architecture/PERFORMANCE.md` is the source of truth for benchmark workloads, SLOs, and perf gates

## Required maintenance on every DB/system change

When changing DB structure, read paths, or inspection surfaces:

1. update the migration(s)
2. update both backends when applicable (DuckDB + Postgres)
3. update `serving_geo` if the app-facing read contract changed
4. update `explore` if engineers need a new inspection surface
5. update CLI inspection commands to keep reading from `explore`
6. add/update tests at the correct layer
7. update docs when the contract or inspection workflow changes

If a change makes an engineer ask “how do I inspect this now?”, the answer should usually be reflected in `explore` and the database exploration docs.

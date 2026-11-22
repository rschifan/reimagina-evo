# Design Docs Guide

The `docs/architecture/` folder is the architecture set of the prototype. Start here when you need to understand how data enters the platform, how scenarios are defined, how jobs run, and where computed outputs are stored.

## Reading order

1. `TERMINOLOGY.md` — canonical vocabulary and routing rules
2. `DATA_LAYER.md` — baseline city model, ingestion, serving views
3. `SCENARIOS_LAYER.md` — projects, scenarios, drawn rules, attribute overrides
4. `JOB_LAYER.md` — the `Orchestrator` port, run state, the engine behind it
5. `OPS_PLANE_RUNBOOK.md` — operating the DBOS engine: processes, queries, recovery
6. `INDICATORS_LAYER.md` — indicator catalog, dependencies, results
7. `ASSET_LAYER.md` — asset graph, versions, freshness
8. `TILE_SERVING.md` — live MVT vs PMTiles snapshot serving rules
9. `API.md` — HTTP surface + download formats
10. `PERFORMANCE.md` — benchmark corpus, observability, performance gates
11. `TESTING.md` — the schema contract cases and the test suites that run
12. `GEOSPATIAL_GRID_ARCHITECTURE.md` — the geospatial model: feature grains, rasters, and how values move between them
13. `TODO.md` — open issues and design decisions

## The six major flows

1. **Dataset onboarding**
   Register a source in `data_source_catalog`, classify it by `storage_type` and `integration_role`, land Bronze (or use an existing immutable mirror as Bronze), clean to Silver, then start an `ingestion_run` for the curated load. (The raster pipeline starts its `ingestion_run` before landing Bronze, so a failure at any step is recorded.)
2. **Canonical city assembly**
   Load stable spatial entities into `curated_geo.feature` and measured or derived facts into `curated_geo.observation`.
3. **Serving/read contract**
   Expose baseline data to the API and the tile servers through thin `serving_geo` views such as `feature_v` and `current_indicator_v`, and through the `serving_geo` tile functions (`get_tile`, `indicator_overlay_tile`, `aggregation_overlay_tile`). Indicator computes read `curated_geo` directly.
4. **Scenario authoring**
   Create a `project` and a named scenario in it (`app_core.scenario`, bound to an AOI), draw rules over it as `curated_geo.scenario_rule` rows — one area and the numbers set over it — and materialise them into `curated_geo.scenario_override` rows, one per `(scenario_id, feature_id, attribute_key)`.
5. **Async computation**
   Submit a run through the `Orchestrator` port: the change-set (or, with nothing named, asset freshness) resolves to a dirty set, which persists as `orchestration_run` + `job_unit`; each unit's runner reads `curated_geo`, overlays the scenario's overrides on read (`effective_numeric`), opens its own `job_run` at compute time, and writes outputs. Units are dispatched **as each one unblocks** — a unit starts the moment its last dependency succeeds, bounded only by the `units` queue's worker concurrency (`REIMAGINA_UNIT_CONCURRENCY`, default 8 per worker process) and the dependency graph's own critical path. The API only enqueues; the worker process (`python -m apps.worker`) executes.
6. **Indicator output and comparison**
   Write long-format `indicator_result` rows, read current values through `serving_geo.current_indicator_v`, and compare base vs scenario per feature with `scenarios.comparison.compare` (`GET /scenarios/{scenario_id}/comparison`).

## Which document owns what

| Document | Primary responsibility |
|---|---|
| `TERMINOLOGY.md` | Shared language, decision rules, onboarding vocabulary |
| `DATA_LAYER.md` | Canonical city data, ingestion lineage, serving views |
| `SCENARIOS_LAYER.md` | User-authored planning state — scenarios, rules, overrides — and the scenario routes |
| `JOB_LAYER.md` | Orchestrator port, run and unit state, **scheduling and dispatch**, execution provenance |
| `OPS_PLANE_RUNBOOK.md` | Running and diagnosing the DBOS ops plane: required processes, stuck runs, recovery |
| `INDICATORS_LAYER.md` | Indicator metadata, dependency graph, computed results |
| `ASSET_LAYER.md` | Asset graph, version algebra, freshness-driven recompute |
| `TILE_SERVING.md` | Live MVT serving, PMTiles snapshots, tile-layer classification |
| `API.md` | HTTP surface (FastAPI routes, download formats, ops knobs) |
| `PERFORMANCE.md` | Production performance contract, benchmarks, observability |
| `TESTING.md` | Contract cases for invariants, serving behavior, and performance expectations; which test suites actually run |
| `GEOSPATIAL_GRID_ARCHITECTURE.md` | Feature grains and rasters, how they get in, and how values move between grains (AOI membership, overlap, zonal statistics, rollups); grid cells (H3, fishnet) are not built |
| `TODO.md` | Open issues and design decisions, with implementation status |

## Fast answers

- **Where does a new dataset land?** Start in `TERMINOLOGY.md`, then follow the onboarding playbook in `DATA_LAYER.md`.
- **How do scenarios use baseline data?** Read `SCENARIOS_LAYER.md` and `JOB_LAYER.md` together.
- **Where do outputs go?** `INDICATORS_LAYER.md`.
- **What is actually out of date?** `ASSET_LAYER.md` — the attribute path answers
  "an edit named X"; freshness answers the larger question, including reloads,
  logic changes and crashes that no edit names.
- **How are map layers served?** `TILE_SERVING.md`.
- **How do we monitor production-read performance?** `PERFORMANCE.md`.
- **In what order do a run's indicators execute?** `JOB_LAYER.md` — the dependency graph decides what is *legal*, the queue decides what is *actual*.
- **Why is a run stuck in `queued`, and what must be running?** `OPS_PLANE_RUNBOOK.md`.
- **How do we know the rules are enforced?** `TESTING.md`.
- **What is still undecided?** `TODO.md`.

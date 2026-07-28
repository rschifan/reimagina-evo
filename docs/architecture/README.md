# Design Docs Guide

The `docs/architecture/` folder is the authoritative architecture set for Reimagina. Start here when you need to understand how data enters the platform, how scenarios are defined, how jobs run, and where computed outputs are stored.

## Reading order

1. `TERMINOLOGY.md` — canonical vocabulary and routing rules
2. `DATA_LAYER.md` — baseline city model, ingestion, serving views
3. `SCENARIOS_LAYER.md` — projects, scenarios, attribute overrides
4. `JOB_LAYER.md` — the `Orchestrator` port, run state, the engine behind it
5. `INDICATORS_LAYER.md` — indicator catalog, dependencies, results
6. `TILE_SERVING.md` — live MVT vs PMTiles snapshot serving rules
7. `API.md` — read-side HTTP surface + download formats
8. `PERFORMANCE.md` — benchmark corpus, observability, performance gates
9. `TESTING.md` — pgTAP contract tests for the whole design

## The six major flows

1. **Dataset onboarding**
   Register a source in `data_source_catalog`, classify it by `storage_type` and `integration_role`, land Bronze, clean to Silver, then start an `ingestion_run`.
2. **Canonical city assembly**
   Load stable spatial entities into `curated_geo.feature` and measured or derived facts into `curated_geo.observation`.
3. **Serving/read contract**
   Expose baseline data to the API and model workers through thin `serving_geo` views such as `feature_v`.
4. **Scenario authoring**
   Create a `project`, mint a `scenario_id`, and record the scenario's attribute edits as `curated_geo.scenario_override` rows — one per `(scenario_id, feature_id, attribute_key)`.
5. **Async computation**
   Submit a run through the `Orchestrator` port: the change-set resolves to a dirty-set, which persists as `orchestration_run` + `job_unit`; each unit's runner reads baseline from `serving_geo`, overlays the scenario's overrides on read, opens its own `job_run` at compute time, and writes outputs.
6. **Indicator output and comparison**
   Write long-format `indicator_result` rows, then read base vs scenario results through `serving_geo.current_indicator_v`.

## Which document owns what

| Document | Primary responsibility |
|---|---|
| `TERMINOLOGY.md` | Shared language, decision rules, onboarding vocabulary |
| `DATA_LAYER.md` | Canonical city data, ingestion lineage, serving views |
| `SCENARIOS_LAYER.md` | User-authored planning state and scenario overlays |
| `JOB_LAYER.md` | Orchestrator port, run and unit state, execution provenance |
| `INDICATORS_LAYER.md` | Indicator metadata, dependency graph, computed results |
| `TILE_SERVING.md` | Live MVT serving, PMTiles snapshots, tile-layer classification |
| `API.md` | Read-side HTTP surface (FastAPI routes, download formats, ops knobs) |
| `PERFORMANCE.md` | Production performance contract, benchmarks, observability |
| `TESTING.md` | Contract tests for invariants, serving behavior, and performance expectations |

## Fast answers

- **Where does a new dataset land?** Start in `TERMINOLOGY.md`, then follow the onboarding playbook in `DATA_LAYER.md`.
- **How do scenarios use baseline data?** Read `SCENARIOS_LAYER.md` and `JOB_LAYER.md` together.
- **Where do outputs go?** `INDICATORS_LAYER.md`.
- **How are map layers served?** `TILE_SERVING.md`.
- **How do we monitor production-read performance?** `PERFORMANCE.md`.
- **How do we know the rules are enforced?** `TESTING.md`.

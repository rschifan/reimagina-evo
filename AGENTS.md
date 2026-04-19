# AGENTS.md

Operational rules for agents working in this repo.

## Repo map

- `apps/`: deployable entrypoints
- `packages/py/reimagina/`: shared backend namespace
- `infra/db/`: migrations and seeds
- `docs/architecture/`: architecture source of truth
- `tests/`: `unit`, `integration`, `contract`, future `e2e`
- `tests/performance`: benchmark smoke checks and perf harness coverage

## Core architecture

Three DB layers must stay distinct:

| Schema | Role |
|---|---|
| `curated_geo` | source of truth |
| `serving_geo` | app/worker read contract |
| `explore` | engineer-facing inspection contract |

Rules:

- API/workers read only `serving_geo`
- scenarios never mutate the baseline
- `feature` is the canonical spatial anchor
- `observation` stores measured/derived facts about features
- `explore` is for humans, not application dependencies
- live MVT is the primary tile path; PMTiles are snapshot exports for stable reference layers only
- if a layer must be live, filtered, scenario-aware, or fast-changing, it cannot move to PMTiles

## Inspection workflow

Default order:

1. `explore`
2. `serving_geo`
3. `curated_geo`

Current `explore` contract:

- `explore.layer_catalog_v`
- `explore.source_runs_v`
- `explore.feature_type_stats_v`
- `explore.serving_stats_v`

Recommended tools:

- DBeaver Community for SQL exploration
- QGIS only for map/geometry checks

CLI inspection commands:

```bash
python -m apps.cli.app explore layers
python -m apps.cli.app explore sources
python -m apps.cli.app explore feature-stats
python -m apps.cli.app explore serving-stats
```

## Testing rules

Testing is non-optional.

| Layer | Use for |
|---|---|
| `tests/unit` | pure logic, delegation, config, normalization |
| `tests/integration` | real DuckDB and PostgreSQL/PostGIS behavior |
| `tests/contract` | architectural boundaries and repo guardrails |
| `tests/performance` | benchmark smoke checks and explain-plan contracts |
| `docs/architecture/TESTING.md` | DB contract target |

Rules:

- do not weaken tests to fit the current code
- fix code to match the intended contract, or intentionally change the contract and update tests/docs together
- add tests at the narrowest correct layer
- DuckDB success is not enough when production semantics depend on Postgres/PostGIS behavior
- new migrations and new read surfaces require tests
- `docs/architecture/PERFORMANCE.md` owns benchmark workloads, budgets, and perf gates

## Required maintenance checklist

When changing DB structure, read paths, or inspection surfaces:

1. update migrations
2. keep DuckDB and Postgres aligned where applicable
3. update `serving_geo` if the app-facing contract changed
4. update `explore` if the engineer-facing inspection surface changed
5. keep CLI inspection commands reading from `explore`
6. add/update tests
7. update `CLAUDE.md`, `AGENTS.md`, `infra/tiles/layers.json`, and relevant docs when the contract changes

If a system change makes inspection harder, that is a maintenance failure.

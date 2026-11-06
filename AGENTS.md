# AGENTS.md

Operational rules for agents working in this repo.

## Repo map

- `apps/`: deployable entrypoints
- `packages/py/reimagina/`: shared backend namespace
- `infra/db/`: migrations and seeds
- `docs/architecture/`: architecture source of truth
- `tests/`: `unit`, `integration`, `contract`, `conformance`, future `e2e`
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
- `explore.ops_executor_health_v`

Recommended tools:

- DBeaver Community for SQL exploration
- QGIS only for map/geometry checks

CLI inspection commands:

```bash
python -m apps.cli.app explore layers
python -m apps.cli.app explore sources
python -m apps.cli.app explore feature-stats
python -m apps.cli.app explore serving-stats
python -m apps.cli.app explore executor-health

# Tile snapshots and live-MVT source resolution
python -m apps.cli.app tiles manifest
python -m apps.cli.app tiles snapshots
python -m apps.cli.app tiles sources

# Overlap pair declaration and compute
python -m apps.cli.app feat seed-types
python -m apps.cli.app overlap declare-pair \
    --source-context MEX --source-slug lot \
    --target-context MEX --target-slug block
python -m apps.cli.app overlap list-pairs
python -m apps.cli.app overlap compute-all --aoi-id <uuid>

# Indicator catalog + compute + rollup + AOI summary (Layer 2)
python -m apps.cli.app indicators seed-catalog
python -m apps.cli.app indicators compute \
    --slug urban_form_block_compactness [--aoi-id <uuid>]
python -m apps.cli.app indicators compute \
    --slug urban_form_block_building_density [--aoi-id <uuid>]
python -m apps.cli.app indicators rollup \
    --slug urban_form_block_compactness \
    --source-type block --target-type ageb
python -m apps.cli.app indicators aoi-summary \
    --aoi-id <uuid> --slug urban_form_block_compactness
```

Web read-side (API + map frontend):

```bash
# Start both from two terminals
REIMAGINA_APP_DSN=postgresql://localhost/reimagina_app python -m apps.api
cd apps/web && npm install && npm run dev
# Map at /, indicator catalog at /catalog.html, run dashboard at /ops.html.
# The dashboard needs a worker to show anything running:
#   DATABASE_URL_DIRECT=... python -m apps.worker

# Then
open http://localhost:8001/docs      # OpenAPI / Swagger
open http://localhost:5173           # Map with input-layer toggles,
                                     # indicator picker, download buttons
```

## Testing rules

Testing is non-optional.

| Layer | Use for |
|---|---|
| `tests/unit` | pure logic, delegation, config, normalization |
| `tests/integration` | real PostgreSQL/PostGIS behavior |
| `tests/contract` | architectural boundaries and repo guardrails |
| `tests/conformance` | the `Orchestrator` port contract, per implementation |
| `tests/performance` | benchmark smoke checks and explain-plan contracts |
| `tests/workloads` | how an ops plane schedules; asserts what must hold for any engine, records what two may differ on |
| `tests/recovery` | crash recovery across a real SIGKILL; opt-in via `REIMAGINA_RECOVERY_TEST=1` |
| `docs/architecture/TESTING.md` | DB contract target |

CI gates `unit`, `contract`, `integration` and `conformance` only. The other
three are hand-run.

Rules:

- do not weaken tests to fit the current code
- fix code to match the intended contract, or intentionally change the contract and update tests/docs together
- add tests at the narrowest correct layer
- new migrations and new read surfaces require tests
- `docs/architecture/PERFORMANCE.md` owns benchmark workloads, budgets, and perf gates

## Required maintenance checklist

When changing DB structure, read paths, or inspection surfaces:

1. update migrations
2. update `serving_geo` if the app-facing contract changed
3. update `explore` if the engineer-facing inspection surface changed
4. keep CLI inspection commands reading from `explore`
5. add/update tests
6. update `AGENTS.md`, `infra/tiles/layers.json`, and relevant docs when the contract changes

This file is the only agent-instruction file in the repo. It used to name a
`CLAUDE.md` alongside itself; there has never been one, and agent harnesses
load this file directly, so the duplicate was a maintenance instruction
pointing at nothing. Keep it that way — one file, no copy to drift from.

If a system change makes inspection harder, that is a maintenance failure.

## Authorship — non-negotiable

**Rossano Schifanella is the only actor on this repository's history.** Every
commit, in every situation, without exception:

- author and committer are `Rossano <rossano.schifanella@gmail.com>`
- no `Co-Authored-By` trailer for any agent or tool
- no `Claude-Session`, session URL, or any other agent attribution, in commit
  messages, pull request titles and descriptions, code comments, or anything
  else written into this repository
- an agent that cannot push under that identity hands the work over as a patch
  for Rossano to apply and push; it does not push under its own credential

This is enforced, not merely documented. `.claude/settings.json` sets
`attribution` to empty for commits and PRs and turns off `sessionUrl`, and a
`PreToolUse` hook refuses `git commit` whenever `user.email` is anything else.
Keep both in place; deleting either silently returns the repository to
tool-attributed history.

A `CLAUDE.md`-style filename appearing in a commit message because the
*codebase* references one is not attribution and is fine.

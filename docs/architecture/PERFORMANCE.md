# Performance

Performance contract of the prototype.

## Summary

- Performance is **DB-first** in v1.
- **PostgreSQL + PostGIS** is the production performance truth.
- **Viewport/AOI reads** are the primary SLO driver because they determine whether a frontend map can stay responsive.
- Performance work is split into:
  1. **benchmark corpus**
  2. **live observability**
  3. **performance gates**

## Primary workloads

These are the named workloads the platform must monitor continuously.

| Benchmark | Purpose |
|---|---|
| `bbox_features_small` | small viewport read over `serving_geo.feature_v` |
| `bbox_features_dense` | denser / larger viewport read over `serving_geo.feature_v` |
| `polygon_features_small` | small polygon read over `serving_geo.feature_v` |
| `polygon_features_dense` | more complex polygon read over `serving_geo.feature_v` |
| `bbox_features_with_current_attributes` | viewport read with current observations already merged |
| `selected_features_current_observations` | current observation lookup for selected visible features |
| `feature_detail_by_id` | single-feature detail fetch |
| `aoi_membership_read` | precomputed AOI membership read through `aoi_feature` |
| `tile_z14_small` | `serving_geo.get_tile` at z14: full detail, merged current attributes |
| `tile_z12_dense` | `serving_geo.get_tile` at z12: simplified, base attributes only |
| `tile_z8_overview` | `serving_geo.get_tile` at z8: overview zoom, context features only |
| `tile_z14_filtered_type` | `serving_geo.get_tile` at z14 with `type_slug=building` |
| `overlap_block_to_ageb_cuauhtemoc` | dry-run block → AGEB geodesic overlap inside the Cuauhtémoc AOI, on real INEGI geometry |

These are the workloads that must be benchmarked before adding any frontend performance claims.

All thirteen are declared in `reimagina.perf.manifest.BENCHMARKS`. ⚠ The four
`tile_*` benchmarks are declared but cannot run yet: `perf.runner._build_params`
has no case for them. `perf run` without `--benchmark` leaves them out, and
naming one fails with "has no parameter builder yet". A default run on a
database without the Cuauhtémoc AOI lists
`overlap_block_to_ageb_cuauhtemoc` under `skipped` instead of failing.

## SLOs and measured signals

Every benchmark report must record:

- `p50`, `p95`, `p99` latency
- row count returned
- shared buffer hits / reads
- temp read / write blocks
- plan shape
- index names used
- relation scans

The benchmark manifest stores machine-checkable budgets per dataset tier:

- `smoke`: CI/local verification
- `representative`: production-like Mexico City profile
- `stress`: larger observation/AOI amplification profile

Budgets, p50 / p95 / p99 in ms, as `perf/manifest.py` declares them:

| Benchmark | `smoke` | `representative` | `stress` |
|---|---|---|---|
| `bbox_features_small` | 25 / 80 / 150 | 120 / 350 / 600 | 180 / 450 / 800 |
| `bbox_features_dense` | 40 / 120 / 220 | 250 / 900 / 1500 | 350 / 1200 / 2000 |
| `polygon_features_small` | 35 / 90 / 180 | 180 / 450 / 800 | 250 / 700 / 1200 |
| `polygon_features_dense` | 50 / 120 / 240 | 300 / 1200 / 2000 | 450 / 1600 / 2600 |
| `bbox_features_with_current_attributes` | 50 / 130 / 250 | 350 / 1200 / 1800 | 500 / 1600 / 2400 |
| `selected_features_current_observations` | 25 / 80 / 150 | 120 / 400 / 700 | 180 / 600 / 1000 |
| `feature_detail_by_id` | 10 / 40 / 80 | 20 / 80 / 150 | 30 / 100 / 180 |
| `aoi_membership_read` | 20 / 70 / 140 | 120 / 350 / 700 | 180 / 550 / 900 |
| `tile_z14_small` | 20 / 60 / 120 | 80 / 200 / 400 | 120 / 300 / 600 |
| `tile_z12_dense` | 15 / 50 / 100 | 50 / 150 / 300 | 80 / 220 / 450 |
| `tile_z8_overview` | 5 / 20 / 40 | 15 / 50 / 100 | 25 / 80 / 160 |
| `tile_z14_filtered_type` | 15 / 50 / 100 | 60 / 180 / 350 | 90 / 250 / 500 |
| `overlap_block_to_ageb_cuauhtemoc` | 450 / 600 / 750 | 450 / 600 / 750 | 900 / 1200 / 1500 |

Each benchmark also declares the plan nodes and index names it expects, the
hot tables it must not sequentially scan, and a minimum row count. A run
passes only if all five checks hold: expected node, expected index, row
count, no catastrophic seq scan, latency budget. The node and index checks
apply only when a hot table holds more than 10,000 rows, and a seq scan
fails only on such a table (the runner sizes `aoi_feature` by the feature
count).

## Dataset tiers

The benchmark framework uses deterministic tiers:

| Tier | Purpose | Features seeded with observations | Selected features | Iterations |
|---|---|---|---|---|
| `smoke` | fast CI/local checks | 250 | 25 | 3 |
| `representative` | Mexico City baseline with realistic observation volume | 25,000 | 250 | 5 |
| `stress` | higher observation and AOI volume for deeper capacity checks | 100,000 | 500 | 7 |

`feature` scale comes from the loaded canonical dataset.
`observation` scale is produced by deterministic seeding
(`perf seed-observations`, `reimagina.perf.seed`):

- five attribute keys per feature (`population_total`, `ndvi_mean`,
  `footfall_index`, `land_use_class`, `access_profile`)
- only one current row per `(feature_id, attribute_key)`; a re-seed skips
  pairs that already have one
- numeric, text, and JSON values

The same command creates the four benchmark AOIs (`perf_bbox_small`,
`perf_bbox_dense`, `perf_polygon_small`, `perf_polygon_dense`).

⚠ These writes stay in whatever database it targets until `perf cleanup`
removes them: synthetic `is_current` observations on real features, which
surface through `serving_geo.feature_v`, and the AOIs through `aoi list`. So
`perf seed-observations` takes no `DATABASE_URL` fallback: `--dsn` is
required. Seed a benchmark copy, not a database people read.

`perf cleanup --dsn ...` deletes, in one transaction, every row the seed wrote
and nothing else: observations and ingestion runs of the `reimagina-perf`
source, the source itself, the four `perf_*` AOIs of the 'Performance Benchmark
Project' (with their `aoi_feature` and `aoi_indicator_summary` rows), and the
project once it holds nothing else. It finds them by the marks the seed has
always written, so older seeds are cleaned too. It prints the JSON count per
table, `--dry-run` prints the same counts and keeps the rows (it runs the deletes and rolls them back, so it holds the same row locks while it runs), and a second run
removes 0. It refuses while a scenario, aggregation recipe or orchestration run
points at a perf AOI, because deleting the AOI would change that row.

## Baseline numbers (Cuauhtémoc, Apr 2026)

Captured against real INEGI Marco Geoestadístico 2020 geometry loaded via
`python -m apps.cli.app cdmx bootstrap`; host is a local dev macOS laptop running
PostgreSQL 17 with PostGIS 3.4. These are the numbers the overlap benchmark's
budgets are derived from — update them alongside any significant code or
schema change that touches the overlap path.

Loaded dataset:

| Layer | Rows | Load time (Postgres) |
|---|---|---|
| MEX:municipality | 16 | included in 7.1 s total |
| MEX:ageb | 2 431 | ″ |
| MEX:block (manzanas) | 66 789 | ″ |

Cuauhtémoc AOI (`CVE_MUN=015`): ~34.5 km² planar, ~32 km² after geodesic
correction. 2 853 MEX:block features intersect the AOI polygon.

Overlap compute `(MEX:block) → (MEX:ageb)` scoped to Cuauhtémoc:

| Backend | Rows written | Wall-clock | Per-feature |
|---|---|---|---|
| Postgres (PostGIS geography) | 2 853 | 0.92 s | ~0.32 ms |

All 2 853 overlap_ratio values equal 1.0 (blocks nest cleanly inside AGEBs —
the INEGI hierarchy is pre-snapped). `overlap_measure` range: 16.9 m² →
208 788.8 m² (mean 8 187.9 m²), consistent with urban city-block areas.

PMTiles export (`admin_reference`, 16 alcaldías): 35.3 KB artifact. `ogr2ogr`
on macOS emits the benign `OGR2SQLITE_Setup` post-write warning;
`reimagina.tiles.export._run_export_command` detects the warning and
verifies the artifact on disk before treating the run as successful.

Benchmark `overlap_block_to_ageb_cuauhtemoc` (5 iterations, warm cache,
`representative` tier):

| Percentile | Observed | Budget |
|---|---|---|
| p50 | 269 ms | 450 ms |
| p95 | 279 ms | 600 ms |
| p99 | 280 ms | 750 ms |

Plan shape: `Nested Loop` + `Index Scan` on `feature_geometry_gist` for both
block↔AGEB and block↔AOI joins. 55 K shared hit blocks, 0 temp blocks — the
working set fits in memory on any reasonable dev box. Budgets follow the
`observed × (1.5 p50 / 2.0 p95 / 2.5 p99)` policy rounded up to 50 ms (the
p99 budget is 750 ms where the policy gives 700 ms), so normal run-to-run
variance has headroom but genuine regressions (e.g. a plan flip to
sequential scan) will trip the budget. `smoke` carries the same budgets as
`representative` because the data is the same; `stress` doubles them.
`tests/performance/test_overlap_benchmarks.py` runs this benchmark at the
`representative` tier against `REIMAGINA_PERF_DSN` (falling back to
`REIMAGINA_APP_DSN`) and skips when the CDMX data or the Cuauhtémoc AOI is
missing.

## Baseline numbers — Layer 2 first indicator (Apr 2026)

Captured on the same Cuauhtémoc + full-CDMX dataset as Layer 1, running
`urban_form_block_compactness` (Polsby-Popper) end-to-end through the CLI
on PostgreSQL 17 + PostGIS 3.4.

Compute + AOI summary (the block → AGEB indicator rollup measured alongside
them at the time no longer exists; every indicator is block-only):

| Step | Scope | Rows | Wall-clock |
|---|---|---|---|
| `indicators compute --slug urban_form_block_compactness` | 66 789 blocks CDMX-wide | 66 789 | 5.9 s |
| `indicators aoi-summary --aoi-id <Cuauhtémoc> --slug urban_form_block_compactness` | 3 020 features weighted, 3 006 usable, 14 prorated | 1 | <0.5 s |

Cuauhtémoc block-compactness (weighted-mean): **0.636** (ratio_0_1).

CDMX-wide block-level distribution:

| Quantile | Value |
|---|---|
| min   | 0.026 |
| p05   | 0.223 |
| p50   | 0.583 |
| p95   | 0.780 |
| max   | 1.000 |

### Building density — `urban_form_block_building_density`

Uses Overture buildings (5.9M loaded; ~1.5M attribute to CDMX INEGI
blocks via centroid-in-polygon). Dual-filter SQL on Postgres:
`block.geometry && bldg.geometry AND ST_Within(ST_Centroid(bldg), block)`,
with GIST on `feature.geometry` doing the heavy lifting.

| Step | Scope | Rows | Wall-clock |
|---|---|---|---|
| `overture load` | 5 888 557 building features landed into `curated_geo.feature` | 5 888 557 | ~5 min (background) |
| `indicators compute --slug urban_form_block_building_density` | 66 789 blocks CDMX-wide; 1 511 860 buildings attributed | 66 789 | 8.9 s |
| `indicators aoi-summary --aoi-id <Cuauhtémoc> --slug urban_form_block_building_density` | 3 020 features weighted, 3 006 usable | 1 | <0.5 s |

Cuauhtémoc building-density (weighted-mean): **26.56 buildings/ha**.

CDMX-wide block-level distribution:

| Quantile | Value (buildings/ha) |
|---|---|
| min  | 0.00 |
| p05  | 0.00 |
| p50  | 30.94 |
| p95  | 89.24 |
| max  | 411.30 |

All values are inside the catalog's `valid_max = 500/ha` safety cap.

If a regression pushes any of these numbers into a different ballpark
(e.g. full CDMX compute over 20 s, or AOI summary taking seconds
instead of <0.5 s), re-run EXPLAIN and check whether the partial unique
indexes on `indicator_result` are still selected.

## Scenario authoring (r048-r049)

Two costs belong to scenarios: what **materialising** a drawn edit costs (which
is also how long the scenario's row lock is held), and what the **delta tile**
costs to render. The first is pinned by a plan contract and recorded as
measurements; the second is recorded as measurements only.

### Materialisation: plan contract

`tests/performance/test_scenario_materialize_plan.py` runs `EXPLAIN` on the
statement `_resolve_edits` executes (captured as it runs, not copied) against a
10,000-block fixture with a selective drawn area, and requires:

- the access path is `feature_centroid_gist` with
  `Index Cond: (geometry_centroid @ e.geometry)`;
- no `Seq Scan on feature`;
- the predicate is written against the stored `f.geometry_centroid`.

The fixture is all one feature type (`MEX:block`), which is part of why the
planner chooses `feature_type_id_idx` below; reproduce it with a mixed-type
table and the unindexed plan may differ.

This is a **design pin plus an index-use pin**, stable across machines: "we use
the stored centroid, and the plan reaches it through `feature_centroid_gist`".
It is **not** shown that the alternative is slower. Mutating the predicate to
`f.geometry` was tried: the planner uses `feature_geometry_gist` and plans
fine, with no scan, and with the index-name assertions removed the plan test
passes. The `Seq Scan` assertion alone is not enough: with the index dropped, the planner does not fall back to
`Seq Scan on feature` on this fixture. It walks `feature_type_id_idx` and
applies `ST_Covers` to every feature of the type, which is the same cost under
a different name. The index-name assertion is the load-bearing one of the plan test, and two
controls in the file show each assertion is reachable (index and bitmap scans
forced off gives the `Seq Scan`; index dropped loses the index name).

A full-district edit legitimately plans a scan, since it selects most of the
table; the contract is about the selective case, which is what an index is
for.

### Materialisation: measured cost

Method: a committed synthetic territory of N x N square blocks (about 210 m
side) in one AOI covering all of them, one `num_levels` edit drawn over the
whole grid, `materialize_scenario` then `COMMIT`, wall clock from
`time.perf_counter()` on the client, one fresh scenario per round. Phase times
come from wrapping the step functions. Revision asserted to be r049 before each
run. `apply_overrides` is the shipped one; nothing was patched.

Machine: Linux 6.18 microVM, 4 vCPU, PostgreSQL 16.15, PostGIS 3.7, client and
server on the same host over TCP localhost. Not a CI machine and not the macOS
laptop of the Cuauhtémoc baselines above; compare ratios, not absolutes.
Run-to-run variance on this host is 10-30%: two 5-round runs of the 20,164-block
case gave a 3.8-4.2 s spread and a 4.3-5.7 s spread.

| Scale | Rounds | Full-district first materialisation, lock to commit |
|---|---|---|
| 20,164 blocks (pilot scale) | 5 | min 3.79 s, median 3.99 s |
| 20,164 blocks, second run | 3 | min 4.31 s, median 4.37 s |
| 90,000 blocks (above CDMX's 66,789) | 3 | min 20.1 s, median 20.3 s |

Roughly linear: about 0.2 ms per block.

Where the 20,164-block time goes (median of the first run):

| Phase | ms | Share |
|---|---|---|
| `_resolve_edits` (the planned SELECT) | 85 | 2% |
| `_refuse_ceiling_breaches` | 516 | 13% |
| `_delete_unimplied_overrides` | 81 | 2% |
| `apply_overrides` | 3,244 | 81% |
| `publish_scenario_version` | 14 | under 1% |

The planned SELECT is not the cost. `apply_overrides` is, because it calls
`apply_override` once per row: one round trip each, about 160 us per row on
localhost, and more over a real network. At 90,000 blocks it was 16.2 s of
20.3 s.

Other shapes at 20,164 blocks:

| Operation | Time |
|---|---|
| Re-materialise unchanged (everything upserted again) | min 4.1 s, median 4.3-4.5 s |
| Remove the only edit, with 20,164 current results to supersede | 1.1 s (1.06 s of it step 7) |
| `compute_block_housing_density` on the same scenario, for contrast | 3.0-3.3 s, one indicator |

### Decision: two transactions, not one, and not the job layer

`recompute` stays synchronous for materialisation and uses the job layer for
evaluation. The numbers behind it:

- **Lock scope.** A second writer on the same scenario waited 4.29 s for a
  4.43 s materialisation. A writer on a different scenario took 4 ms. The lock
  is per scenario row, so the blast radius is that scenario's own editors.
- **The split is vindicated.** One indicator's compute is as long as the
  materialisation (3.0-3.3 s against 4.0-4.4 s) and there are twelve. Holding
  the lock across the compute would multiply the window several times for no
  correctness gain; materialisation commits first and evaluation starts after.
- **Acceptable at district scale, strained at city scale.** About 4 s at 20k
  blocks is a tolerable request; about 20 s at 90k is not a request anyone
  should wait on. If city-scale authoring becomes real, the remedy is to
  batch the upsert in `apply_overrides` (81% of the time), not to move
  materialisation into a job: a job would give up the synchronous refusal
  (`CeilingExceeded`, `ScenarioHasNoAOI`), which the caller needs to see.

**Evaluation is scoped to the scenario's footprint.** `POST
/scenarios/{id}/recompute` passes `MaterializeResult.footprint_features` and
`footprint_attributes` to `submit_run`: every block a current rule overrides,
plus every block whose override this materialisation removed, with their
attributes. Blocks outside it carry no override and read the baseline through
the overlay. Planned from freshness instead, as it was before, every indicator
reading a changed attribute recomputed every block of the AOI under the
scenario — 19,058 blocks in Mexicali for a rule over 2,400, about 20 s a run,
as recorded in `recompute_route`'s docstring. The footprint-scoped run has not
been measured with this document's method.

Not measured at 90,000 blocks: the removal and the `compute_block_housing_density`
contrast. `compute_block_housing_density` died there with "SSL connection has
been closed unexpectedly"; the cause was not investigated. Those rows exist only
for 20,164 blocks.

Also not measured: a network hop between application and database, concurrent
materialisations of different scenarios contending on `scenario_override`, and
a database whose `feature` table holds the whole country rather than the
fixture's 10-90k rows.

### Delta tile (`serving_geo.indicator_overlay_tile` with `delta`)

There is no separate delta function: r049 added a `delta` parameter to
`indicator_overlay_tile`, and the delta view is one branch of it.

These tile figures were measured against the r049 tile path at pilot scale.
They were not re-measured when the materialisation figures in this document
were taken; they are recorded here with their method.

Measured at pilot scale: 19,881 blocks, 20,181 current results, 400 features in
a z=14 tile. Each figure is 15 timed calls through the shipped function, min and
median.

| Tile | Min | Median |
|---|---|---|
| Value view at head (r049) | 8.9 ms | 9.5 ms |
| Value view with r048's body swapped in, same transaction, rolled back | 9.5 ms | 11.6 ms |
| Delta view | 12.6 ms | 13.8 ms |

- **No regression to the value view.** r049 adds a `CASE` mode switch and the
  unchosen arm is evaluated lazily.
- **Delta costs about 1.5 times a value tile.**
- **Join form.** The shipped delta is a `FULL OUTER JOIN` of baseline and
  scenario results. A per-feature lateral probe was built as the alternative:
  the tiles are byte-identical (`SELECT (A) = (B)` returned true), the
  alternative was about 1.2 times faster in wall clock, and the planner's
  *cost* favoured the shipped form by 3.2 times. Cost and wall clock disagreed,
  and wall clock was measured with interleaved A/B rounds, which is the one to
  believe. The shipped form was kept because it mirrors `comparison._joined`
  clause for clause, so parity with `GET /scenarios/{id}/comparison` is
  structural. The price of that choice is about 1.8 ms per 400-feature tile.

Known and accepted:

- Delta mode refetches the whole comparison on every render in the frontend.
  A server-side p95 would be the optimisation if it feels slow at Mexicali
  scale.
- The comparison endpoint takes no `feature_type`, so a multi-grain indicator
  would scale the delta ramp across grains. Latent: every shipped indicator is
  block-grain.

## Benchmark rules

- Benchmark the **real serving contract** whenever possible.
- Use `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` as the canonical measurement shape.
- Do not use generic synthetic SQL that the app will never run.
- Do not rely on latency alone:
  - plan shape and index usage are part of the contract
  - a hot-path sequential scan on a large table is a failure even if one local run looks fast

### Hot-path rule

For viewport and polygon reads, compare:

1. live spatial filtering on `feature.geometry`
2. precomputed AOI membership through `aoi_feature`
3. serving-layer reads through `serving_geo.feature_v` / `feature_aoi_v`

If a serving view becomes a proven hot path, it is acceptable to introduce a **Postgres-only optimized read surface**. Performance portability is not a goal if it harms production behavior.

## Observability requirements

Postgres environments used for production-like benchmarking should enable and inspect:

- `pg_stat_statements`
- `auto_explain`
- `ANALYZE` / planner statistics freshness
- relation and index size growth
- sequential scan frequency on hot tables
- top queries by total and mean execution time

The CLI exposes (`perf report` is built by `reimagina.perf.observability`:
extension and preload status, relation and index sizes, per-table seq/index
scan counts and last (auto)analyze, and the top `pg_stat_statements` queries
by total time):

```bash
python -m apps.cli.app perf seed-observations --dsn postgresql://localhost/reimagina_perf
python -m apps.cli.app perf cleanup --dsn postgresql://localhost/reimagina_perf --dry-run
python -m apps.cli.app perf run --dataset-tier smoke --benchmark bbox_features_small
python -m apps.cli.app perf explain bbox_features_dense
python -m apps.cli.app perf report
python -m apps.cli.app perf compare baseline.json candidate.json
```

The viewport and polygon boxes, and the four `perf_*` AOIs, are cut from the
centre of every feature's extent. With more than one city loaded that centre
falls between them and the boxes read nothing; pass `--aoi-id` (the same one
to `seed-observations`, `run` and `explain`) to cut them from one AOI instead.

`perf run` exits 1 when any benchmark fails a check. `perf compare` marks a
benchmark `regressed` when its p95 rose more than 10% over the baseline, and
exits 1 on a regression or a failed candidate.

## Test and release policy

What runs today: `tests/performance` holds three files, run by
`pytest tests/performance` (or `pytest tests`) on a developer machine.

- `test_postgres_perf_smoke.py` — seeds a small fixture and runs four smoke
  benchmarks (`bbox_features_small`, `selected_features_current_observations`,
  `feature_detail_by_id`, `aoi_membership_read`, two iterations) plus the
  observability report, against the migrated test database.
- `test_overlap_benchmarks.py` — `overlap_block_to_ageb_cuauhtemoc` on real
  CDMX data; skips without it.
- `test_scenario_materialize_plan.py` — the materialisation plan contract
  above.

⚠ **No performance gate is wired into CI.** `.github/workflows/ci.yml` runs
`tests/unit`, `tests/contract`, `tests/integration` and `tests/conformance`;
nothing in it runs `tests/performance` or `perf run`, and its weekly schedule
runs the same jobs as a pull request. The two subsections below are the
policy, not a description of the pipeline.

### CI / every PR

- run correctness tests as usual
- run smoke-tier performance checks on Postgres
- fail on:
  - missing benchmark coverage
  - catastrophic seq scan on hot large tables
  - missing expected index family / access path

### Scheduled / staging

- run representative or stress benchmark suites
- store JSON artifacts
- compare candidate vs baseline
- flag:
  - latency regression
  - plan regression
  - index regression
  - explosive table/index growth

## Current defaults

- performance framework is **DB-first**
- primary workload is **viewport/AOI reads**
- Postgres/PostGIS is the only authoritative performance backend
- frontend/browser benchmarking is out of scope for v1

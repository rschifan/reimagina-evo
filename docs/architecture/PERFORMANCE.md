# Performance

Production performance contract for Reimagina.

## Summary

- Performance is **DB-first** in v1.
- **PostgreSQL + PostGIS** is the production performance truth.
- **Viewport/AOI reads** are the primary SLO driver because they determine whether a frontend map can stay responsive.
- Performance work is split into:
  1. **benchmark corpus**
  2. **live observability**
  3. **performance gates**

DuckDB remains useful for correctness and local development, but it is **not** the backend that decides whether production performance is acceptable.

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

These are the workloads that must be benchmarked before adding any frontend performance claims.

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

## Dataset tiers

The benchmark framework uses deterministic tiers:

| Tier | Purpose |
|---|---|
| `smoke` | fast CI/local checks |
| `representative` | Mexico City baseline with realistic observation volume |
| `stress` | higher observation and AOI volume for deeper capacity checks |

`feature` scale comes from the loaded canonical dataset.
`observation` scale is produced by deterministic seeding:

- multiple attribute keys per feature
- only one current row per `(feature_id, attribute_key)`
- numeric, text, and JSON values

## Baseline numbers (Cuauhtémoc, Apr 2026)

Captured against real INEGI Marco Geoestadístico 2020 geometry loaded via
`reimagina-cli cdmx bootstrap`; host is a local dev macOS laptop running
PostgreSQL 17 with PostGIS 3.4. These are the numbers the overlap benchmark's
budgets are derived from — update them alongside any significant code or
schema change that touches the overlap path.

Loaded dataset:

| Layer | Rows | Load time (Postgres) | Load time (DuckDB) |
|---|---|---|---|
| MEX:municipality | 16 | included in 7.1 s total | included in 2.6 s total |
| MEX:ageb | 2 431 | ″ | ″ |
| MEX:block (manzanas) | 66 789 | ″ | ″ |

Cuauhtémoc AOI (`CVE_MUN=015`): ~34.5 km² planar, ~32 km² after geodesic
correction. 2 853 MEX:block features intersect the AOI polygon.

Overlap compute `(MEX:block) → (MEX:ageb)` scoped to Cuauhtémoc:

| Backend | Rows written | Wall-clock | Per-feature |
|---|---|---|---|
| Postgres (PostGIS geography) | 2 853 | 0.92 s | ~0.32 ms |
| DuckDB (pyproj.Geod) | 2 853 | 3.48 s | ~1.22 ms |

All 2 853 overlap_ratio values equal 1.0 (blocks nest cleanly inside AGEBs —
the INEGI hierarchy is pre-snapped). `overlap_measure` range: 16.9 m² →
208 788.8 m² (mean 8 187.9 m²), consistent with urban city-block areas.

PMTiles export (`admin_reference`, 16 alcaldías): 35.3 KB artifact. `ogr2ogr`
on macOS emits the benign `OGR2SQLITE_Setup` post-write warning;
`reimagina.tiles.service._run_export_command` detects the warning and
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
`observed × (1.5 p50 / 2.0 p95 / 2.5 p99)` policy rounded up to 50 ms, so
normal CI variance has headroom but genuine regressions (e.g. a plan flip to
sequential scan) will trip the budget.

## Baseline numbers — Layer 2 first indicator (Apr 2026)

Captured on the same Cuauhtémoc + full-CDMX dataset as Layer 1, running
`urban_form_block_compactness` (Polsby-Popper) end-to-end through the CLI
on PostgreSQL 17 + PostGIS 3.4.

Compute + rollup + AOI summary:

| Step | Scope | Rows | Wall-clock |
|---|---|---|---|
| `indicators compute --slug urban_form_block_compactness` | 66 789 blocks CDMX-wide | 66 789 | 5.9 s |
| `indicators rollup --source-type block --target-type ageb` | 153 AGEBs covered by populated `feature_overlap` pairs | 153 | 8.0 s |
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

Cuauhtémoc AGEB-grain rollup spread (weighted-mean of child block values
per AGEB): bottom three 0.423–0.431 (irregular historic street patterns),
top three 0.757–0.760 (more-planned grids).

Plan shape of the rollup SELECT: `Nested Loop` over
`feature_overlap_target_idx` into `indicator_result.ir_base_slug_idx`, no
sequential scans on either `feature_overlap` or `indicator_result`.

If a regression pushes any of these numbers into a different ballpark
(e.g. full CDMX compute over 20 s, or AOI summary taking seconds
instead of <0.5 s), re-run EXPLAIN and check whether the partial unique
indexes on `indicator_result` are still selected.

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

The CLI should expose at least:

```bash
python -m apps.cli.app perf seed-observations
python -m apps.cli.app perf run --dataset-tier smoke
python -m apps.cli.app perf explain bbox_features_dense
python -m apps.cli.app perf report
python -m apps.cli.app perf compare baseline.json candidate.json
```

## Test and release policy

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

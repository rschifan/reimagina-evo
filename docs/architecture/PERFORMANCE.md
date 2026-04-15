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

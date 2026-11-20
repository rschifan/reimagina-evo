# Open Issues and Design Decisions

Unresolved questions and gaps identified during architecture review. Each item records both the original decision and the current implementation status. Items that have since been built or removed are no longer listed; the layer documents describe what exists.

---

## Status summary

**Production-readiness items from architecture review (see detail below):**

| # | Item | Status |
|---|---|---|
| T2-6 | Table partitioning on `observation` | **Deferred** — before 10M rows |
| T2-9 | Row-Level Security | **Policies only** — r011 (`app_core.project`, `curated_geo.area_of_interest`) and r048 (`app_core.scenario`). ⚠ Inert in practice: permissive whenever `app.current_project_id` is unset, and nothing in the app sets it (only a test does), so every current caller sees every row. r011 deferred FORCE RLS and a dedicated non-owner app role "until the first API route is deployed"; neither has been added. Not a tenancy boundary yet. |
| T3-10 | OpenTelemetry distributed tracing | **Not built** |
| T3-11 | STAC alignment for `file_asset` | **Not built** — low priority |
| T3-12 | OGC API Features compliance | **Not built** — low priority |

### Prototype-OK technical debt (tracked, not urgent)

| # | Item | Where | Notes |
|---|---|---|---|
| TD-1 | Backend-kind guard in `reimagina.tiles.export.export_pmtiles_snapshot` | `tiles/export.py` | `backend_name_for_dsn(dsn) != "postgres"` is a policy check, not a branch to be abstracted. `backend_name_for_dsn` returns `"postgres"` or raises, so the branch cannot fire while Postgres is the only storage backend; tracked only so a future multi-backend export plan finds it |
| TD-2 | `perf/*` reads `backend.name` directly | `perf/runner.py`, `perf/seed.py`, `perf/observability.py` | Each module's `_require_postgres` raises `ValueError` for a non-Postgres backend. `backend_for_connection` only ever returns the Postgres backend (anything else is a `TypeError`), so the guards cannot fire today. The same direct check sits in `scenarios/overrides.py`, `assets/registry.py`, `assets/version.py` and `orchestration/runstore.py`; move them behind a `reimagina.storage` helper if a second backend is ever added |
| TD-3 | The run index undercounts a deep queue | `apps/api/routers/jobs.py` (`list_runs`) | Queued runs are folded in from the engine with `list_enqueued_runs(limit=limit)`, so past `limit` queued runs the queued chip undercounts; an engine-side count is the upgrade |
| TD-4 | Recompute after new source data is full-extent | `apps/cli/indicators.py` (`submit_after_ingest`) | Every affected indicator recomputes every feature it applies to; narrow it when `impact.FeatureExpander` has a real implementation |
| TD-5 | `FeatureExpander` has only the identity default | `orchestration/impact.py` | Maps the features a change touched to the features whose value changes; proximity (neighbours) and grain (child → parent) expansion need the spatial feature graph injected |
| TD-6 | A scenario change set is a flat union of attributes and features | `scenarios/overrides.py` (`resolve_change_set`) | A safe over-approximation: a dependent may recompute a feature whose own changed attribute it does not read. Per-attribute scoping is the upgrade path |
| TD-7 | Synthetic workload DAGs are generated in-repo | `perf/workloads.py` (`layered_random`) | About 30 lines instead of a WfCommons dependency; swap the function if trace-derived workflows become representative of indicator DAGs |
| TD-8 | Demo attributes are fixture data | `scripts/seed_demo_attributes.py` | Seeds the attributes the edificability and accessibility demos read, because nothing in the shipped pipeline populates a scenario-editable attribute; delete it when a real cadastre and zoning table land |

---

## Production-readiness items not yet built

These items were identified in the Layer 1 architecture review. Tier 1 items are all complete. The following remain open.

### T2-6: Table partitioning on `observation`

**Deferred** — do before `observation` reaches 10M rows.

`observation` is a single unbounded table. At city scale with multiple indicators across multiple sources and time windows, this table will grow to hundreds of millions of rows. Autovacuum, index rebuilds, and partition pruning all become problems.

**Approach when needed:** RANGE partitioning on `valid_from` (year or quarter), in a new Alembic migration. The application's queries are raw psycopg2 SQL against `curated_geo.observation` and are partition-transparent.

```sql
CREATE TABLE curated_geo.observation (...) PARTITION BY RANGE (valid_from);
CREATE TABLE curated_geo.observation_2024 PARTITION OF curated_geo.observation
    FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
```

⚠ Not schema-only in one respect. Postgres requires a unique constraint or index on a partitioned table to include the partition key, and neither the `id` primary key nor `obs_one_current` — the partial unique index on `(feature_id, attribute_key) WHERE is_current` that enforces one current row per attribute — includes `valid_from`. Both need a replacement design before this migration can be written.

---

### T3-10: OpenTelemetry distributed tracing

**Not built.** No distributed tracing. When a job fails or a viewport read is slow, there is no trace connecting API → worker → DB query → result.

**Approach:** Add `opentelemetry-sdk`, `opentelemetry-instrumentation-psycopg2`, `opentelemetry-instrumentation-fastapi` to dependencies. Auto-instrument at app startup; OTLP export to Jaeger or Grafana Tempo. Record a `trace_id` with each run so DB-side failures are linkable to the originating trace — no `jobs_runtime` table has a JSON details column to put it in (`job_unit.detail` is text), so that is a column on `jobs_runtime.orchestration_run` or `jobs_runtime.job_run`.

**Files needed:** `apps/api/` and `apps/worker/` startup instrumentation, plus that migration — no domain code changes.

---

### T3-11: STAC alignment for `file_asset`

**Not built** — low priority.

`curated_geo.file_asset` has no STAC Item JSON representation. `band_description` is a freeform JSONB column, not a STAC Asset object.

**Approach:** Add nullable `stac_item JSONB` column to `file_asset`; add `explore.file_asset_stac_v` view; add API endpoint `GET /collections/{source_id}/items/{asset_id}`.

---

### T3-12: OGC API Features compliance

**Not built** — low priority.

`GET /features` returns a cursor-paginated page of `{id, type_slug, role, name, attributes}` rows with no geometry: `ServingReader.get_features` does not select it. Frontend mapping libraries (MapLibre, Leaflet) expect GeoJSON FeatureCollections. QGIS and other GIS tools can auto-discover OGC API Features endpoints.

**Approach:** Restructure API routes to conform to OGC API Features Part 1 (Core):

```
GET /collections/{type_slug}/items?bbox=...&limit=...
→ GeoJSON FeatureCollection + Links header
```

**Files needed:** `apps/api/` route restructuring, and a read path that returns geometry (`ServingReader.get_features` or a sibling) — not a route rename alone.

---

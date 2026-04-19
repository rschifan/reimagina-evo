# Open Issues and Design Decisions

Unresolved questions and gaps identified during architecture review. Each item records both the original decision and the current implementation status.

---

## Status summary

| # | Item | Status |
|---|---|---|
| 1 | Spatial aggregation mechanism | Schema done (r009); rollup worker contract documented in `JOB_LAYER.md`; `reimagina.jobs.aggregation` pure module shipped; worker body waits for Layer 2 `indicator_result` |
| 2 | AOI-level aggregate indicator values | Decided (Option B); schema and worker not yet built |
| 3 | Non-hierarchical spatial aggregation | Resolved — `feature_overlap` covers this |

**Production-readiness items from architecture review (see detail below):**

| # | Item | Status |
|---|---|---|
| T1-1 | Materialise `feature_current_attributes_mv` | **Done** — r008 |
| T1-2 | PgBouncer connection pooling | **Done** — docker-compose + martin config |
| T1-3 | Cursor-based API pagination | **Done** — `_utils.py` + Protocol |
| T1-4 | `feature_overlap` table | **Done** — r009 schema + `reimagina.jobs.overlap` orchestrator (AOI-scoped) + CLI; rollup consumer lands Layer 2 |
| T1-5 | Vector tile serving (`martin` + `get_tile`) | **Done** — r010 + martin sidecar |
| T2-6 | Table partitioning on `observation` | **Deferred** — before 10M rows |
| T2-7 | `aoi_indicator_summary` table | **Partial** — r015 schema + serving view + repo on both backends; populating worker waits for Layer 2 `indicator_result` |
| T2-8 | SSE for job status | **Not built** — needs API + LISTEN/NOTIFY |
| T2-9 | Row-Level Security | **Done** — r011 |
| T3-10 | OpenTelemetry distributed tracing | **Not built** |
| T3-11 | STAC alignment for `file_asset` | **Not built** — low priority |
| T3-12 | OGC API Features compliance | **Not built** — low priority |

---

## 1. Spatial aggregation mechanism (INDICATORS_LAYER)

> **Decision: Option B — rollup worker via `feature_overlap`.**
> `feature_overlap` and `feature_overlap_type_pair` tables have been added (r009).
> The rollup worker should read `feature_overlap` instead of `feature.parent_id`,
> covering both hierarchical and non-hierarchical cases uniformly.
> This issue is resolved at the schema level; the rollup worker contract still
> needs to be added to `JOB_LAYER.md` once the first indicator requires it.

**Where:** `indicator_catalog.aggregation_method`, `indicator_result`, `JOB_LAYER.md`

**The gap:**
`aggregation_method` (`weighted_mean`, `sum`, etc.) declares the intent for rolling up indicator values from a finer spatial grain to a coarser one — for example, from block to AGEB, or from lot to block. But nothing in the framework defines who performs this rollup or when. The field is currently documentation intent, not a wired pipeline step.

**What needs a decision:**

| Option | How it works | Trade-off |
|---|---|---|
| **A — Model computes at each grain independently** | The worker receives features at each declared grain and computes the indicator directly at that scale | Simple worker contract; coarser results are not derived from finer ones — they are independently computed and may not be numerically consistent |
| **B — Spatial rollup worker** | After fine-grain results are written, a dedicated rollup worker walks `feature.parent_id`, groups child results by parent, applies `aggregation_method`, and writes a new `indicator_result` for the parent feature | Coarser results are always consistent with finer ones; requires defining a new worker type and queue in JOB_LAYER |

**Decision needed:** which option is adopted, and for which indicators?

If Option B: the rollup worker contract must be added to `JOB_LAYER.md` (queue name, trigger condition, input/output contract) and a use-case example must be added to `INDICATORS_LAYER.md`.

---

## 2. AOI-level aggregate indicator values (INDICATORS_LAYER + DATA_LAYER)

> **Decision: Option B — `aoi_indicator_summary` table.**
> A city planning frontend's primary UX is the AOI scorecard (single headline number
> per indicator per AOI). Recomputing this on every page load from `aoi_feature` +
> `indicator_result` via an area-weighted join is too expensive at production data volume.
>
> **Schema not yet built.** Requires: new Alembic migration adding
> `curated_geo.aoi_indicator_summary`, a `serving_geo.aoi_summary_v` view, and a
> rollup step triggered after `indicator_result` batch writes complete.
> Depends on the rollup worker (issue 1) being wired.

**Where:** `indicator_result`, `area_of_interest`, `aoi_feature`

**The gap:**
`area_of_interest` is not a `feature` — it lives in `curated_geo.area_of_interest`, not in `curated_geo.feature`. It therefore has no `feature_id` and cannot have an `indicator_result` row. There is no way to store a pre-computed single summary value such as "the liveability index of this AOI = 0.71".

The current AOI query in `INDICATORS_LAYER.md` returns per-feature results within the AOI, with `overlap_ratio` available as a display weight. The AOI-level aggregate must be recomputed at query time every time it is needed.

**What needs a decision:** does the platform need a stored, cacheable AOI-level summary, or is on-the-fly aggregation acceptable?

| Option | How it works | Trade-off |
|---|---|---|
| **A — Compute on-the-fly** | Add a canonical area-weighted aggregation query to `INDICATORS_LAYER.md` as the standard AOI summary pattern; no schema change | No schema change; consistent with current data at all times; slower at read time for large AOIs |
| **B — New `aoi_indicator_summary` table** | A new table keyed on `(aoi_id, scenario_id, indicator_id)` stores pre-computed AOI summaries; populated by a rollup step after fine-grain results are written | Cacheable and consistent with the rest of the result pattern; requires schema addition, a new worker step, and a staleness/refresh mechanism parallel to `indicator_result` |

**Proposed schema (when built):**

```sql
CREATE TABLE curated_geo.aoi_indicator_summary (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aoi_id           UUID NOT NULL REFERENCES curated_geo.area_of_interest(id),
    scenario_id      UUID REFERENCES app_core.scenario(id),  -- NULL = baseline
    indicator_id     UUID NOT NULL REFERENCES curated_geo.indicator_catalog(id),
    value            DOUBLE PRECISION,
    aggregation_note TEXT,
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (aoi_id, scenario_id, indicator_id)
);
```

---

## 3. Non-hierarchical spatial aggregation (INDICATORS_LAYER + DATA_LAYER)

> **Resolved.** `feature_overlap` (r009) covers both hierarchical and non-hierarchical
> cases.  The rollup worker in issue 1 above should use `feature_overlap` as its
> join source for all spatial aggregations.

**Where:** `indicator_catalog.aggregation_method`, `feature.parent_id`, `aoi_feature`

**The gap:**
Issue 1's Option B (rollup worker via `feature.parent_id`) only works when the two spatial grains are part of the same administrative hierarchy — for example, lots nested inside blocks, blocks nested inside AGEBs. When the source and target grains are **not hierarchical** — for example, blocks and a 100m analysis grid, lots and road segments, AGEBs and a custom project grid — `parent_id` is `NULL` and the hierarchy cannot be traversed. There is no structural relationship to follow, and the rollup worker has no way to know how much of a source feature falls inside a target feature.

The only pre-computed spatial intersection in the schema is `aoi_feature`, which handles AOI ↔ feature overlap. There is no equivalent for feature ↔ feature overlap across arbitrary type pairs.

**What needs a decision:**

| Option | How it works | Trade-off |
|---|---|---|
| **A — Model computes independently at each grain** | Each grain is treated as a fully independent computation; no relationship between results at different scales is assumed or enforced | Simple; always possible; results at different grains are not numerically consistent with each other |
| **B — `feature_overlap` pre-computed intersection table** | A new table generalises `aoi_feature` to feature-to-feature: keyed on `(source_feature_id, target_feature_id)`, storing `overlap_basis`, `overlap_measure`, `feature_measure`, `overlap_ratio` for any two feature types. A background job populates it for declared type-pair combinations. The rollup worker reads it instead of `parent_id`. | Consistent cross-grain aggregation for any type pair; requires schema addition, a background intersection job, and a refresh trigger when features change |

**Relationship to Issue 1:** Option B here is the generalisation of Issue 1's Option B. If a rollup worker is adopted, it should use `feature_overlap` (not `parent_id`) as its spatial join source — covering both hierarchical and non-hierarchical cases uniformly. `parent_id` would then become a special case of a known 1:1 containment overlap.

**Decision needed:** are non-hierarchical cross-grain aggregations required by any model family or by the frontend? If yes, `feature_overlap` should be designed alongside `aoi_feature`, and Issues 1 and 3 should be resolved together.

---

## Production-readiness items not yet built

These items were identified in the Layer 1 architecture review. Tier 1 items are all complete. The following remain open.

### T2-6: Table partitioning on `observation`

**Deferred** — do before `observation` reaches 10M rows.

`observation` is a single unbounded table. At city scale with multiple indicators across multiple sources and time windows, this table will grow to hundreds of millions of rows. Autovacuum, index rebuilds, and partition pruning all become problems.

**Approach when needed:** RANGE partitioning on `valid_from` (year or quarter). New Alembic migration — no Python code changes required; SQLAlchemy and raw psycopg2 queries are partition-transparent.

```sql
CREATE TABLE curated_geo.observation (...) PARTITION BY RANGE (valid_from);
CREATE TABLE curated_geo.observation_2024 PARTITION OF curated_geo.observation
    FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
```

---

### T2-7: `aoi_indicator_summary` table

**Not built** — depends on rollup worker (issue 1 above) being wired. See issue 2 for the schema and rationale.

**Files needed:** new Alembic migration + `serving_geo.aoi_summary_v` serving view + `ServingReader` Protocol update.

---

### T2-8: Server-Sent Events for job status

**Not built.** There is no mechanism for the frontend to know when a job completes. Clients must poll.

**Approach:** SSE endpoint + PostgreSQL `LISTEN/NOTIFY`.

```
GET /jobs/{job_id}/events  →  text/event-stream
data: {"status": "running", "progress": 0.45}
data: {"status": "complete", "result_url": "..."}
```

Workers call `NOTIFY jobs_status, '{"job_id": "...", "status": "complete"}'` after writing to `jobs_runtime`.

**Note:** PgBouncer transaction mode is incompatible with `LISTEN/NOTIFY` (session-level). The SSE connection must bypass PgBouncer or use a separate pool in session mode.

**Files needed:** `apps/api/` new SSE route; worker `NOTIFY` call after result write; optional `notify_job_status()` trigger on `jobs_runtime.status`.

---

### T3-10: OpenTelemetry distributed tracing

**Not built.** No distributed tracing. When a job fails or a viewport read is slow, there is no trace connecting API → worker → DB query → result.

**Approach:** Add `opentelemetry-sdk`, `opentelemetry-instrumentation-psycopg2`, `opentelemetry-instrumentation-fastapi` to dependencies. Auto-instrument at app startup; OTLP export to Jaeger or Grafana Tempo. Add `trace_id` to `jobs_runtime.details` JSON so DB-side failures are linkable to the originating trace.

**Files needed:** `apps/api/` and `apps/worker/` startup instrumentation only — no domain code changes.

---

### T3-11: STAC alignment for `file_asset`

**Not built** — low priority.

`curated_geo.file_asset` has no STAC Item JSON representation. `band_description` is a freeform JSONB column, not a STAC Asset object.

**Approach:** Add nullable `stac_item JSONB` column to `file_asset`; add `explore.file_asset_stac_v` view; add API endpoint `GET /collections/{source_id}/items/{asset_id}`.

---

### T3-12: OGC API Features compliance

**Not built** — low priority. Route rename only, no storage layer changes.

The `feature` endpoint returns domain-specific JSON. Frontend mapping libraries (MapLibre, Leaflet) expect GeoJSON FeatureCollections. QGIS and other GIS tools can auto-discover OGC API Features endpoints.

**Approach:** Restructure API routes to conform to OGC API Features Part 1 (Core):

```
GET /collections/{type_slug}/items?bbox=...&limit=...
→ GeoJSON FeatureCollection + Links header
```

**Files needed:** `apps/api/` route restructuring only.

---

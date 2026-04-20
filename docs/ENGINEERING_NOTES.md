# Engineering Notes

Running log of deferred decisions, known limitations, and "when X happens, do Y" 
operational notes. This is not architecture documentation — for that, see `docs/architecture/`.
Add entries here whenever something is intentionally deferred or has a known trigger condition.

---

## Deferred implementations

### Observation table partitioning
**Deferred:** indefinitely, until data volume demands it.  
**Trigger to implement:** any of:
- `SELECT n_dead_tup FROM pg_stat_user_tables WHERE relname = 'observation'` shows 
  autovacuum running constantly with millions of dead tuples after ingestion runs.
- `REFRESH MATERIALIZED VIEW serving_geo.feature_current_attributes_mv` takes > 30s.
- The `obs_one_current` partial index rebuild after a bulk ingestion run takes > 60s.

**What to do when triggered:** `RANGE PARTITION BY valid_from` (yearly or quarterly).  
At that point the migration is a maintenance-window operation (create new partitioned 
table, copy data, rename, drop old). The pyproj/psycopg2 application layer needs 
no changes — partitioning is transparent to queries.

**Why it was deferred:** at prototype stage with unknown data volume there is no 
immediate benefit. The hot path (`WHERE is_current = true`) does not filter by 
`valid_from`, so no partition pruning applies there. The cost of doing it now was 
zero (empty table), but without a known scale target, premature partitioning adds 
operational complexity for no measurable gain.

---

### REFRESH MATERIALIZED VIEW — switch to CONCURRENTLY
**Location:** `packages/py/reimagina/storage/postgres/repositories.py` — 
`PostgresCatalogRepository.complete_ingestion_run`.  
**Current behaviour:** `REFRESH MATERIALIZED VIEW serving_geo.feature_current_attributes_mv`
— non-concurrent, takes a brief `AccessExclusiveLock` on the MV (blocks readers).  
**Trigger to switch:** when ingestion runs overlap with live API traffic (i.e., 
first time a frontend user is actively querying while an ingestion completes).  
**What to do:** `REFRESH MATERIALIZED VIEW CONCURRENTLY` — requires a separate 
`autocommit=True` connection (cannot run inside a transaction). The standard pattern: 
open a second connection, set autocommit, REFRESH, close. Alternatively, promote the 
refresh to a dedicated background worker that polls `ingestion_run.completed_at`.

---

### aoi_indicator_summary table (plan item 7)
**Blocked by:** `curated_geo.indicator_catalog` table does not exist yet (indicators 
layer is not implemented).  
**When to implement:** when the indicators layer (indicator_catalog, indicator_result) 
is migrated. At that point, add:
- `curated_geo.aoi_indicator_summary (aoi_id, scenario_id, indicator_id, value, computed_at)`
- `serving_geo.aoi_summary_v` serving view
- `ServingReader.get_aoi_summary()` Protocol method + both backend implementations  
See plan file for the full schema design.

---

### SSE job status endpoint (plan item 8)
**Blocked by:** `jobs_runtime` tables not yet created (job layer not implemented).  
**What to implement when unblocked:**
1. Add `NOTIFY jobs_status, '{"job_id":"...", "status":"..."}'` call in the 
   worker after writing to `jobs_runtime`.
2. Optional: `CREATE TRIGGER notify_job_status AFTER UPDATE ON jobs_runtime.job ...`
3. API route `GET /jobs/{job_id}/events` → `text/event-stream` using `LISTEN jobs_status`.  
**Critical constraint:** `LISTEN/NOTIFY` is session-level and incompatible with 
PgBouncer transaction mode. The SSE connection MUST use `DATABASE_URL_DIRECT` 
(direct Postgres connection), not the PgBouncer DSN. See `infra/env/.env.example`.

---

### OpenTelemetry distributed tracing (plan item 10)
**When to implement:** before the first real production deploy; the
FastAPI read-side is up under `apps/api/`, so the integration point
exists. Not a prototype blocker.
**What to add:**
```python
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # if FastAPI
Psycopg2Instrumentor().instrument()
```
Add `trace_id` to `jobs_runtime.details` JSON so DB failures link back to the 
originating HTTP request.

---

### Row-Level Security — extend to future tables (plan item 9, partial)
**Current state:** RLS is enabled on `app_core.project` and `curated_geo.area_of_interest`.  
**When to extend:** as each project-scoped table is created, add the same pattern:
```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON <table>
    USING (
        current_setting('app.current_project_id', true) = ''
        OR project_id::text = current_setting('app.current_project_id', true)
    );
```
Tables that will need RLS when created: `app_core.scenario`, `indicator_result`, 
`aoi_indicator_summary`.  
**API middleware required:** at the start of every request transaction:
```python
conn.execute("SET LOCAL app.current_project_id = %s", (project_id,))
```
**Role note:** table owners bypass RLS by default in PostgreSQL. For enforcement to 
apply to the application role, either create a separate `reimagina_api` role that is 
not the table owner, or use `ALTER TABLE ... FORCE ROW LEVEL SECURITY`. The current 
migration uses standard RLS (no FORCE) — add FORCE and a dedicated API role when the 
first API route is deployed.

---

### martin tile server — PostGIS version requirement
**Constraint:** `serving_geo.get_tile` uses `ST_TileEnvelope(z, x, y, margin => ...)`.  
The `margin` parameter was added in **PostGIS 3.1**. The docker-compose stack uses 
`postgis/postgis:16-3.4` which satisfies this. If deploying on an older PostGIS, 
replace with:
```sql
ST_Transform(ST_Expand(ST_TileEnvelope(z, x, y), <tile_width_meters> * 64.0/4096), 4326)
```
where `<tile_width_meters> = 40075017 / power(2, z)`.

---

### Rollup worker contract (from TODO.md item 1 resolution)
**Status:** `feature_overlap` and `feature_overlap_type_pair` tables exist (r009), 
resolving the schema side of TODO #1 and #3. The rollup worker contract (queue name, 
trigger condition, input/output spec) still needs to be added to 
`docs/architecture/JOB_LAYER.md` once the first indicator that requires cross-grain 
aggregation is implemented.

---

## Known limitations

### Stuck ingestion runs in `running` status
**Location:** `packages/py/reimagina/pipeline/loader.py` — `load_vector_source` and  
`load_tabular_source`.  
**How it happens:** Both loaders commit the `ingestion_run` row to status `'running'` 
immediately after creating it (before any data is written). This is intentional — it 
decouples the run-tracking transaction from the data transaction, so that a data 
failure can still update the run to `status='failed'`. The side-effect is that if the 
process is killed between the early commit and the subsequent 
`complete_ingestion_run(..., status='failed')` call, the run row remains in 
`status='running'` indefinitely.  
**Cleanup query** (run as a maintenance task or on startup):
```sql
UPDATE curated_geo.ingestion_run
   SET status = 'failed',
       completed_at = now(),
       details = '{"error_message": "Process killed; run never completed"}'::jsonb
 WHERE status = 'running'
   AND started_at < now() - interval '2 hours';
```
**When to implement automated cleanup:** when the first scheduled worker is deployed; 
add this query to its startup sequence or as a daily pg_cron job.

---

### DuckDB bulk insert atomicity
**Location:** `DuckDBFeatureRepository.bulk_insert_features` and 
`DuckDBObservationRepository.bulk_upsert_current`.  
**Limitation:** the row count returned is from a separate SELECT COUNT(*) statement 
issued after the INSERT. Under concurrent writes to the same DuckDB connection the 
count may not reflect only the current operation's rows. This is an accepted 
prototype limitation — DuckDB's default isolation does not guarantee 
statement-level atomicity across separate SQL calls.  
**Impact:** dev/test only. The Postgres backend does not have this issue.

### PgBouncer transaction mode and session-level SQL
The following SQL constructs are incompatible with PgBouncer transaction mode and 
must use `DATABASE_URL_DIRECT`:
- `LISTEN` / `NOTIFY` (session-level)
- `SET` (not `SET LOCAL`) for session variables
- Advisory locks (`pg_advisory_lock`)
- Prepared statements (if not using `server_reset_query = DISCARD ALL`)

Current code uses `SET LOCAL` for `app.current_project_id` (RLS variable), which 
is safe with transaction mode.

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

### SSE job status endpoint (plan item 8) — **shipped**
**Built as:** `GET /jobs/{run_id}/events` → `text/event-stream`
(`apps/api/routers/jobs.py:169-178`), streaming over `LISTEN`/`NOTIFY` on one
fixed channel, `job_events` (`orchestration/events.py:19`). Progress is keyed by
**run**, not by a per-job id — see `JOB_LAYER.md` — *Progress*.

**Constraint that held:** `LISTEN/NOTIFY` is session-level and incompatible with
PgBouncer transaction mode. The subscriber opens its own direct connection, and
`DATABASE_URL_DIRECT` is mandatory and fails loudly when unset
(`orchestration/queue_app.py:30-31`). See `infra/env/.env.example`.

**Two things learned in the build, worth not re-deriving.** `emit` must be called
*before* the commit that persists the same status change, since NOTIFY is
delivered on commit (`events.py:24-27`). And no trigger-based `NOTIFY` was added:
emitting from application code is what lets the event be gated on whether the
status write actually landed, which is what keeps a cancelled unit from being
announced as `failed`.

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
Tables that will need RLS: `curated_geo.scenario_override`, `indicator_result`, 
`aoi_indicator_summary`. None of them carries a `project_id`, so the policy above 
cannot be applied verbatim. `aoi_indicator_summary` can reach one by joining 
`aoi_id → area_of_interest.project_id` (r004:21). `scenario_override` and 
`indicator_result` cannot: they are keyed by `scenario_id`, which is a bare uuid 
with no FK and no link to a project (r016:7-10). Project-scoping those two needs a 
scenario→project association that does not exist in the schema.  
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

### Rollup contract (from TODO.md item 1 resolution) — **superseded**
**Status:** the indicator rollup this item asked for was built, then deliberately
removed. `39ab70a` dropped `jobs/rollup.py`, the `indicators rollup` CLI command
and `rollup_child_rows`, taking the catalog from 14 indicators to 11, all
`applies_to=("block",)`. r016's write trigger rejects a result at an undeclared
grain, so block-only is now enforced rather than intended.

`feature_overlap` and `feature_overlap_type_pair` (r009) remain, and are still
read by AOI summaries and by the *observation* rollup
(`jobs/observation_rollup.py`). A coarser-grain number for an indicator is
produced as an AOI summary, not as a second `indicator_result` row. See
`docs/architecture/JOB_LAYER.md` — *Indicator rollup — removed, and what
replaced each part of it*.

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

### PgBouncer transaction mode and session-level SQL
The following SQL constructs are incompatible with PgBouncer transaction mode and 
must use `DATABASE_URL_DIRECT`:
- `LISTEN` / `NOTIFY` (session-level)
- `SET` (not `SET LOCAL`) for session variables
- Advisory locks (`pg_advisory_lock`)
- Prepared statements (if not using `server_reset_query = DISCARD ALL`)

Current code uses `SET LOCAL` for `app.current_project_id` (RLS variable), which 
is safe with transaction mode.

# Ops-plane runbook (DBOS)

For whoever is holding the pager. Every query here was run against a real
database with a real run in it — the first drafts were wrong, and are noted
where the mistake is easy to repeat.

## What is running

There is no engine to deploy. DBOS is a library, so the ops-plane is however
many processes import it:

| process | required | why |
|---|---|---|
| API (`apps/api`) | yes | accepts submits, serves status and the SSE stream |
| worker (`python -m apps.worker`) | **no** | scales throughput; a run submitted by the API is driven by the API if nothing else picks it up |

A worker is not required for correctness. If throughput is fine without one,
not running one is a supported configuration — that is the point of a library.

Checkpoints live in the **same database** as the domain data, under the `dbos`
schema. There is no second database to back up, and a `pg_dump -n curated_geo -n
jobs_runtime` is still coherent on its own.

## Configuration that will bite you

| variable | unset behaviour | why it matters |
|---|---|---|
| `DATABASE_URL_DIRECT` | **startup fails** | Without it DBOS writes checkpoints to a local SQLite file. Guarded. |
| — pointing at a wrong DSN | **startup fails** | DBOS *creates* a database rather than failing, so a typo would otherwise start cleanly and checkpoint nowhere useful. Guarded by a domain-table check. |
| `REIMAGINA_EXECUTOR_ID` | derives `<hostname>:<role>`, logs a warning | Recovery reclaims **only** work recorded against this exact id. Must be stable across restarts *and* unique between live processes — see below. |
| `REIMAGINA_UNIT_CONCURRENCY` | 2 | Units per process. Non-numeric fails at startup. |
| `REIMAGINA_APP_VERSION` | derived from workflow source | ⚠ **Sticky.** DBOS records versions in `dbos.application_versions`; once you set one, every process on a different version logs "not the latest version" until something newer is recorded. Set it deliberately or never. |

### The executor id is the thing to get right

Both failure modes are silent:

- **Not stable across restarts** → a restarted process reclaims *nothing*. Its
  predecessor's in-flight runs are orphaned with no error.
- **Shared by two live processes** → one reclaims work the other is still
  running. On dbos 2.28.0 recovery is not idempotent, so that is a double
  execution.

The default is safe for at most one process per role per host. Run two workers
on one host and you **must** set it per replica.

## Is a run stuck?

Both planes at once. Our tables say what the run means; the engine says whether
anything is still driving it.

```sql
SELECT r.id::text, r.status AS ours, w.status AS engine, w.executor_id,
       r.submitted_at,
       (SELECT count(*) FROM jobs_runtime.job_unit u WHERE u.run_id = r.id
          AND u.status NOT IN ('succeeded','failed','skipped','cancelled')) AS unfinished
FROM jobs_runtime.orchestration_run r
LEFT JOIN dbos.workflow_status w ON w.workflow_uuid = r.id::text
WHERE r.status NOT IN ('succeeded','partial','failed','cancelled')
ORDER BY r.submitted_at;
```

⚠ Do **not** join `job_unit` and `job_run` in the same query — see the gap
below. Use subqueries, as above. The obvious join silently multiplies counts.

Read the two status columns together:

| ours | engine | meaning |
|---|---|---|
| `running` | `PENDING` | normal, in flight |
| `running` | `ENQUEUED` | waiting for a queue slot — check `REIMAGINA_UNIT_CONCURRENCY` |
| `running` | `SUCCESS`/`ERROR` | the workflow finished without closing the run. A bug; capture it. |
| `running` | *no row* | the run was submitted against a different database, or the engine's rows were purged |
| `running` | `PENDING`, `executor_id` of a process that no longer exists | **orphaned** — see below |

## An orphaned run

```sql
SELECT executor_id, count(*) FROM dbos.workflow_status
WHERE status = 'PENDING' GROUP BY executor_id;
```

If an `executor_id` there belongs to no live process, its runs are stuck.
**There is no automatic recovery across executors on this branch** — a process
reclaims only its own id, at startup. Options, in order of preference:

1. Start a process with that exact `REIMAGINA_EXECUTOR_ID`. It reclaims its own
   work at launch. This is why a stable id matters.
2. Cancel the runs (`POST /jobs/{id}/cancel` semantics via the port) and
   resubmit. Safe: writes are replace-in-place.

Do **not** reach for `DBOS._recover_pending_workflows` to sweep another
executor's work. It is a private method, and on 2.28.0 recovery is not
idempotent — if the "dead" executor is actually alive, that is a double
execution. The sweep is deliberately unbuilt until dbos 2.29.

## A unit that will not stop

Cancellation is three mechanisms because none suffices alone. If a unit is
still burning CPU after a cancel:

```sql
SELECT jr.id, jr.backend_pid, jr.status, a.state, a.query_start
FROM jobs_runtime.job_run jr
LEFT JOIN pg_stat_activity a ON a.pid = jr.backend_pid
WHERE jr.run_id = :run_id AND jr.status NOT IN ('succeeded','failed','cancelled');
```

`pg_cancel_backend(backend_pid)` stops the statement. A unit that has not yet
opened its `job_run` row has no pid to signal — `s_compute` re-checks the run
status before starting, so it will skip, but a cancel landing *after* that check
and *before* the row exists has nothing to grab.

## Timing, for the measurement spec

```sql
SELECT count(*) AS units,
       round(sum(extract(epoch FROM (completed_at - started_at)))::numeric, 3) AS runner_secs
FROM jobs_runtime.job_run WHERE run_id = :run_id;
```

Wall clock is measured at the caller, not here — the gap between submit and the
first unit starting is part of what is being compared, and no table records when
a client asked.

### Known gap: a `job_run` cannot be attributed to a unit

`job_run` carries `run_id` (r031) but nothing links it to an *indicator*.
`job_unit.job_run_id` has existed since r028 and is **never written** — verified:
0 of 3 rows populated on a live run. So:

- per-**run** aggregates are correct (the sum above);
- per-**unit** attribution is impossible, and any query joining the two tables
  produces a cartesian product. Three units and three job_runs join to nine rows.

Fixing it means writing `job_unit.job_run_id` from the compute step. Worth doing
before per-unit numbers are quoted anywhere.

## Footprint

Measured, one process each, idle after launch:

| | connections |
|---|---|
| worker, idle | 4 |
| DBOS's own pool | `pool_size=5, max_overflow=0` per client |
| each in-flight unit | +1 (its own psycopg2 connection) |

Budget as `processes × (4 + REIMAGINA_UNIT_CONCURRENCY)` against
`max_connections`. The runners' connections dominate, not DBOS's.

## What is not ready

Stated plainly so nobody discovers it at 3am:

- **No cross-executor recovery.** Waiting on dbos 2.29's idempotent recovery.
- **No metrics.** No counters, no histograms, nothing to alert on. Diagnosis is
  the SQL above.
- **Never load-tested**, never run multi-process beyond a two-process spike,
  never deployed.

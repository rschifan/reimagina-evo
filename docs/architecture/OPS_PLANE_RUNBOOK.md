# Ops-plane runbook (DBOS)

For whoever is holding the pager. Every query here was run against a real
database with a real run in it — the first drafts were wrong, and are noted
where the mistake is easy to repeat.

## What is running

There is no orchestration service to deploy. DBOS is a library backed by the
application Postgres, but production separates submitters from executors:

| process | required | why |
|---|---|---|
| API (`apps/api`) | yes | accepts submits, serves status and the SSE stream |
| worker (`python -m apps.worker`) | **yes in production** | executes queued runs and units; scale this fleet for compute throughput |

For local development, `REIMAGINA_DBOS_EXECUTION_MODE=dev` permits one-process
execution. Do not use it for API deployments: it lets a web request process
compete with PostGIS compute for CPU, memory, and database connections.

Checkpoints live in the **same database** as the domain data, under the `dbos`
schema. There is no second database to back up, and a `pg_dump -n curated_geo -n
jobs_runtime` is still coherent on its own.

## Configuration that will bite you

| variable | unset behaviour | why it matters |
|---|---|---|
| `DATABASE_URL_DIRECT` | **startup fails** | Without it DBOS writes checkpoints to a local SQLite file. Guarded. |
| — pointing at a wrong DSN | **startup fails** | DBOS *creates* a database rather than failing, so a typo would otherwise start cleanly and checkpoint nowhere useful. Guarded by a domain-table check. |
| — naming a different database from the API DSN | **submit fails** | Planning must not read one database while DBOS executes in another. Guarded by a server-side database-name check. |
| `REIMAGINA_DBOS_EXECUTION_MODE` | `client` | API-safe default. Use `executor` for workers and explicit `dev` only for local one-process execution. |
| `REIMAGINA_EXECUTOR_ID` | derives `<hostname>:<role>`, logs a warning | Recovery reclaims **only** work recorded against this exact id. Must be stable across restarts *and* unique between live processes — see below. |
| `REIMAGINA_RUN_CONCURRENCY` | 2 | Parent workflows per executor on the `runs` queue. |
| `REIMAGINA_UNIT_CONCURRENCY` | 8 | Units per process. Non-numeric fails at startup. Measured on the real catalog: 2 → 3614 ms, 4 → 2181 ms, 8 → 1691 ms, 16 → 1629 ms. 8 is the *latency* choice at 0.49 efficiency; **lower it toward 4** (0.76) where many runs execute concurrently and throughput matters more. Above 8 the graph runs out of parallelism — a DAG's average parallelism is `work/span`, here 4.03, and 16 slots measure exactly that. |
| `REIMAGINA_EXECUTOR_HEARTBEAT_S` | 30 | Worker heartbeat interval; must be positive. |
| `REIMAGINA_PROGRESS_SUBSCRIBERS` | 32 | Maximum in-process DBOS progress stream pumps; excess SSE clients reconnect. |
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
| `running` | `ENQUEUED` | parent waiting for a `runs` slot — check `REIMAGINA_RUN_CONCURRENCY`; child waits are visible as queued units and use `REIMAGINA_UNIT_CONCURRENCY` |
| `running` | `SUCCESS`/`ERROR` | the workflow finished without closing the run. A bug; capture it. |
| `running` | *no row* | DBOS state was purged or the database was restored inconsistently; new submissions reject direct-DSN/database mismatches |
| `running` | `PENDING`, `executor_id` of a process that no longer exists | **orphaned** — see below |

## An orphaned run

```sql
SELECT executor_id, count(*) FROM dbos.workflow_status
WHERE status = 'PENDING' GROUP BY executor_id;
```

Inspect `explore.ops_executor_health_v` before declaring an executor dead. It
records the worker's direct-Postgres heartbeat and its age without requiring a
control-plane service. If an `executor_id` there belongs to no live process,
its runs are stuck.
Two recoveries exist, and they are not the same mechanism.

**Its own, at startup.** `DBOS.launch()` reclaims the workflows recorded
against *this* `executor_id`, and only this one. That covers a restart and
needs nothing from an operator — which is why a **stable** id matters.

**Someone else's, by sweep.** `orchestration/reclaim.py` runs in every worker
(`python -m apps.worker`) and periodically asks which executors have stopped
publishing liveness, then asks DBOS to recover their pending workflows onto
itself. An executor whose host was destroyed or whose replica was scaled away
is reclaimed without intervention.

| Setting | Default | Meaning |
|---|---|---|
| `REIMAGINA_RECLAIM_ENABLED` | `1` | `0` turns the sweep off. A worker then logs a warning saying a dead executor's runs will stay in progress forever |
| `REIMAGINA_EXECUTOR_STALE_S` | `300` | How stale a heartbeat must be before its executor is presumed dead. Refused if it is under 4x `REIMAGINA_EXECUTOR_HEARTBEAT_S` |
| `REIMAGINA_RECLAIM_SWEEP_S` | `max(stale/2, 60)` | How often to sweep. Refused if not positive and finite |

⚠ **Why the liveness gate is a correctness requirement, not a courtesy.**
DBOS 3's `_recovery._recover_workflow` re-enqueues rather than executing
directly, so every recovered workflow starts through the queue's atomic
`ENQUEUED`→`PENDING` dequeue, which admits exactly one runner. That makes two
sweepers racing on the same dead executor safe. It does **not** make sweeping
a *live* executor safe: re-enqueuing a workflow its original runner is still
executing admits a second one alongside it. The idempotency guarantee is about
duplicate recovery *requests*, not about recovering work that was never
abandoned. Hence the deliberately wide threshold — a late heartbeat is cheap,
a wrongly-declared death is not — and hence the refusal to configure one
narrower than four beats.

An executor with **no** heartbeat row at all is never swept. Absence is not
evidence of death, and the one process that can be in that state is running
code old enough not to publish liveness, which is exactly when guessing is
worst.

⚠ **A sweep cannot cross an application version.** Recovery asks
`get_pending_workflows(executor_id, app_version)`, and that query filters on
`application_version = <the sweeper's own version>`. A worker pinned to a
different `REIMAGINA_APP_VERSION` than the executor that died recovers
**zero** workflows from it, every time, however much work is stranded — and
this is the normal state during a blue-green deploy, which deliberately leaves
old-version `PENDING` workflows lying around.

The sweep detects this rather than papering over it: an executor that still
has unfinished workflows after recovery keeps its liveness row, and the worker
logs

```
reclaim sweep left <id> unreclaimed — work remains open against them
that this application version cannot recover
```

To drain it, start a process pinned to the stranded version
(`REIMAGINA_APP_VERSION=<that version>`); its own startup recovery, or its
sweep, will reclaim the work. Find the version with:

```sql
SELECT executor_id, application_version, status, count(*)
FROM dbos.workflow_status
WHERE status IN ('PENDING', 'ENQUEUED')
GROUP BY 1, 2, 3 ORDER BY 1, 2;
```

If you would rather not, cancel the runs (`POST /jobs/{id}/cancel` semantics
via the port) and resubmit. Safe: writes are replace-in-place.

Do **not** call `DBOS._recover_pending_workflows` by hand against an executor
you have not checked against `explore.ops_executor_health_v`. It is a private
method, and if the "dead" executor is actually alive, that is a duplicate
execution of a unit. Use the sweep, which holds the gate.

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

⚠ That default is now **8**, so a worker peaks at ~12 connections rather than
~6. Against Postgres's stock `max_connections = 100` this caps you near 8 worker
processes; check it before scaling out, and lower the concurrency rather than
the process count if throughput matters more than one run's latency.

## What is not ready

Stated plainly so nobody discovers it at 3am:

- **Cross-version strands need a human.** The sweep reclaims a dead
  executor's work only within its own application version, reports what it
  could not take, and keeps the evidence — but draining a stranded version
  still means starting a process pinned to it. See above.
- **No metrics.** No counters, no histograms, nothing to alert on. Diagnosis is
  the SQL above.
- **Never load-tested**, never run multi-process beyond a two-process spike,
  never deployed.

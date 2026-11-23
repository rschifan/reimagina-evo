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

### Every submit answers 503 and names `bootstrap-db`

The ops plane's schema is missing from that database. `bootstrap-db` — what
`make migrate` runs — provisions it; **Alembic alone does not**, because DBOS
owns the `dbos` schema and the API submits as a `DBOSClient`, which never runs
migrations. Starting a worker once also creates it, as a side effect of
`launch()`.

Symptom to recognise: the read side is entirely healthy — the dashboard lists
indicators, the run list renders, health is green — and only submits fail:
`POST /jobs`, `POST /jobs/{id}/rerun` and `POST /scenarios/{id}/recompute`.
Nothing is wrong with the request or with shipped code, which is why this is a
503 and not a 500.

```bash
# Is it provisioned?
psql "$DATABASE_URL_DIRECT" -c "SELECT to_regclass('dbos.workflow_status')"
# NULL  -> not provisioned
python -m apps.cli.app bootstrap-db --app-dsn "$REIMAGINA_APP_DSN"
```

It connects to `--admin-dsn` (`$DATABASE_URL_ADMIN`, default
`postgresql://localhost/postgres`) first, even when the database already
exists, so that DSN must be reachable too.

Idempotent, so it is safe to run against a database that already has it, and it
creates nothing you did not ask for: the test database is opt-in behind
`--with-test-db`, which only `make migrate` passes. Without it, this command
used to create and migrate `reimagina_test` on whatever host it ran on —
including this one.

Pass the *application's* DSN; every DDL step inside — Alembic and the
ops-plane schema both — takes `--direct-dsn`, which defaults to
`$DATABASE_URL_DIRECT` and falls back to `--app-dsn` when neither is set. The
flag beats the variable, so a stale `DATABASE_URL_DIRECT` in the shell cannot
quietly redirect the migration. A direct DSN naming a *different* database is
refused rather than used: it is another route to the same one, so only host
and port may differ. With neither set, `--app-dsn` pointing at pgbouncer is
refused too, and the error names `--direct-dsn`: the ops-plane schema cannot be
installed through a transaction-mode pooler, and the command detects one by
asking the connection, not by reading the DSN. The refusal comes at the
ops-plane step, after Alembic (which survives a pooler) and the seeds have
already run through `--app-dsn`; rerun with `--direct-dsn` to finish.

## Configuration that will bite you

| variable | unset behaviour | why it matters |
|---|---|---|
| `DATABASE_URL_DIRECT` | **startup fails** | Without it DBOS writes checkpoints to a local SQLite file. Guarded. |
| — pointing at a wrong DSN | **startup fails** | DBOS *creates* a database rather than failing, so a typo would otherwise start cleanly and checkpoint nowhere useful. Guarded by a domain-table check. |
| — naming a different database from the API DSN | **submit fails** | Planning must not read one database while DBOS executes in another. Guarded by a server-side database-name check. |
| `REIMAGINA_DBOS_EXECUTION_MODE` | `client` | API-safe default. `python -m apps.worker` sets `executor` itself when unset; use `dev` only for local one-process execution. Any other value fails. |
| `REIMAGINA_EXECUTOR_ID` | derives `<hostname>:<role>` (`<hostname>:cli:<pid>` for the CLI's embedded `evaluate-scenario`), logs a warning at launch | Recovery reclaims **only** work recorded against this exact id. Must be stable across restarts *and* unique between live processes — see below. |
| `REIMAGINA_EXECUTOR_ROLE` | `app`; the worker sets `worker` | The `<role>` in the derived executor id and the `role` column of the heartbeat. |
| `REIMAGINA_RUN_CONCURRENCY` | 2 | Parent workflows per executor on the `runs` queue. |
| `REIMAGINA_UNIT_CONCURRENCY` | 8 | Units per process. Non-numeric fails at startup. Measured on the real catalog: 2 → 3614 ms, 4 → 2181 ms, 8 → 1691 ms, 16 → 1629 ms. 8 is the *latency* choice at 0.49 efficiency; **lower it toward 4** (0.76) where many runs execute concurrently and throughput matters more. Above 8 the graph runs out of parallelism — a DAG's average parallelism is `work/span`, here 4.03, and 16 slots measure exactly that. |
| `REIMAGINA_EXECUTOR_HEARTBEAT_S` | 30 | Heartbeat interval of every process that launches an executor; must be positive. |
| `REIMAGINA_POLL_INTERVAL_SEC` | 0.1 | DBOS dispatch polling: the queues' dequeue loop and a parent's wait on its children. DBOS's own default is 1.0 s, which measured ~1.18 s per wave; 0.1 s measured 137 ms per wave for 5x the idle query load. |
| `REIMAGINA_RUN_PLAN_TTL_HOURS` | 24 | Age after which an orphaned `jobs_runtime.run_plan` row is swept, on the next submit. A plan whose workflow is still queued or running is never swept. |
| `REIMAGINA_PROGRESS_SUBSCRIBERS` | 32 | Maximum in-process DBOS progress stream pumps; excess SSE clients reconnect. |
| `REIMAGINA_APP_VERSION` | an MD5 of the workflow source; each unpinned launch records itself as the latest | ⚠ DBOS records versions in `dbos.application_versions`, and **only an executor on the latest recorded version dequeues API submits** — a client enqueue carries no version. A process on any other version logs "is not the latest version" at launch and dequeues only work stamped with its own version. Unpinned, the most recent launch wins, so two unpinned executors on different source strand whichever launched first: pin it when running more than one, and pin it deliberately. |

### The executor id is the thing to get right

Both failure modes are silent:

- **Not stable across restarts** → a restarted process reclaims *nothing*. Its
  predecessor's in-flight runs are orphaned with no error.
- **Shared by two live processes** → one reclaims work the other is still
  running. Recovery re-enqueues a workflow its original runner is still
  executing, so that is a double execution. DBOS 3's idempotent recovery covers
  duplicate recovery *requests*, not this.

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
| `running` | `ENQUEUED` | the parent started once and was re-enqueued by recovery (a restart or a sweep), and is waiting for a `runs` slot on an executor of its version — check `REIMAGINA_RUN_CONCURRENCY` and `application_version`; child waits are visible as queued units and use `REIMAGINA_UNIT_CONCURRENCY` |
| `running` | `SUCCESS`/`ERROR` | the workflow finished without closing the run. A bug; capture it. |
| `running` | *no row* | DBOS state was purged or the database was restored inconsistently; new submissions reject direct-DSN/database mismatches |
| `running` | `PENDING`, `executor_id` of a process that no longer exists | **orphaned** — see below |

A run nothing has dequeued yet has no `orchestration_run` row — the workflow's
first step writes it — so it is not in the result above. It is an `ENQUEUED`
`run_workflow` row in `dbos.workflow_status`, and `GET /jobs` lists it as
`queued`. With no worker running, every new submit looks like this:

```sql
SELECT workflow_uuid, status, application_version,
       to_timestamp(created_at / 1000.0) AS created   -- epoch milliseconds
FROM dbos.workflow_status
WHERE name = 'run_workflow' AND status IN ('ENQUEUED', 'DELAYED')
ORDER BY created_at;
```

## An orphaned run

```sql
SELECT executor_id, count(*) FROM dbos.workflow_status
WHERE status = 'PENDING' GROUP BY executor_id;
```

Inspect `explore.ops_executor_health_v` (or `python -m apps.cli.app explore
executor-health`) before declaring an executor dead. It records the
direct-Postgres heartbeat of every process that launched an executor — workers,
and `dev` processes too — and its age, without requiring a control-plane
service. If an `executor_id` there belongs to no live process, its runs are
stuck.

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
| `REIMAGINA_RECLAIM_ENABLED` | `1` | `0` turns the sweep off (the worker's, and the one an embedded `evaluate-scenario` runs while it waits). A worker then logs a warning saying a dead executor's runs will stay in progress forever |
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

Exercised on a scratch database with `REIMAGINA_EXECUTOR_HEARTBEAT_S=2`,
`REIMAGINA_EXECUTOR_STALE_S=8`, `REIMAGINA_RECLAIM_SWEEP_S=3`: worker `A`
killed with `SIGKILL` mid-run left the run `running`/`PENDING` against `A` with
one unit unfinished; worker `B`, started with its own id, logged `reclaim sweep:
2 workflow(s) from A` three seconds after launch, the run finished `succeeded`,
and `A`'s heartbeat row was gone. A worker stopped cleanly keeps its row too
(shutdown stops the beat, it does not delete the row); a live peer's sweep
forgets it once it is stale and has nothing open.

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

To drain it, start a worker pinned to the stranded version
(`REIMAGINA_APP_VERSION=<that version>`); its sweep will reclaim the work. (Its
startup recovery reclaims only its own executor id, so that helps only if you
also start it with the dead executor's `REIMAGINA_EXECUTOR_ID` — safe only when
that process is certainly gone.) Find the version with:

```sql
SELECT executor_id, application_version, status, count(*)
FROM dbos.workflow_status
WHERE status IN ('PENDING', 'ENQUEUED')
GROUP BY 1, 2, 3 ORDER BY 1, 2;
```

If you would rather not, cancel the runs (`POST /jobs/{id}/cancel`) and
resubmit their unfinished indicators (`POST /jobs/{id}/rerun`, which starts a
new run). Safe: writes are replace-in-place.

Do **not** call `DBOS._recover_pending_workflows` by hand against an executor
you have not checked against `explore.ops_executor_health_v`. It is a private
method, and if the "dead" executor is actually alive, that is a duplicate
execution of a unit. Use the sweep, which holds the gate.

## A unit that will not stop

Cancel with `POST /jobs/{id}/cancel` (409 once the run is terminal).
Cancellation is three mechanisms because none suffices alone: it cancels the
workflow and its children, signals every compute backend of the run with
`pg_cancel_backend`, and closes the units as `cancelled`. If a unit is still
burning CPU after a cancel (in `psql`, `\set run_id <uuid>` first; the query
below quotes it with `:'run_id'`, so feed it on stdin or interactively, not
through `psql -c`):

```sql
SELECT pid, state, query_start, left(query, 60)
FROM pg_stat_activity
WHERE application_name = 'reimagina-run:' || :'run_id';
```

Every unit computes on a connection named `reimagina-run:<run_id>`, from the
moment it connects, and `pg_cancel_backend(pid)` stops its statement. ⚠ Do not
look for it through `jobs_runtime.job_run.backend_pid`: the unit writes that row
inside the one transaction that runs its compute, so it is invisible to you
until the unit has finished — on a live run that join returns no rows. A unit
that has not started its compute yet is skipped by `s_compute`'s own re-check of
the run status.

Observed on a Mexicali scratch database: 15 active backends named after the
run, `POST /jobs/{id}/cancel`, and 2 s later none active, all 12 units and the
run `cancelled`, and one `failed` `job_run` row per interrupted unit
(re-recorded after the rollback, as provenance).

## Clearing finished runs

`DELETE /jobs` forgets every terminal run; `DELETE /jobs/{id}` one (404 if
unknown, 409 while it is still live — cancel it first). Both remove the
`orchestration_run` and `job_unit` rows and the engine's workflows
(`dbos.workflow_status`, children included), and keep what the runs produced:
`indicator_result` and the `job_run` provenance stay. The Ops page's "Clear
history" button (click twice within 4 s to confirm) calls `DELETE /jobs`.

```bash
curl -X DELETE "$API/jobs"          # {"deleted": <n>}
```

## Timing, for the measurement spec

```sql
SELECT count(*) AS units,
       round(sum(extract(epoch FROM (completed_at - started_at)))::numeric, 3) AS runner_secs
FROM jobs_runtime.job_run WHERE run_id = :'run_id';
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
| worker, idle | 6 (DBOS 3.5.0, measured 2026-11-23; one of them the progress `LISTEN`) |
| DBOS's own pool | an executor (`DBOS()`): `pool_size=20, max_overflow=0`, DBOS's default, not overridden here; a `DBOSClient` (the API, one per DSN): `pool_size=5, max_overflow=0` |
| each in-flight unit | +1 (its own psycopg2 connection) |

Budget as `processes × (6 + REIMAGINA_UNIT_CONCURRENCY)` against
`max_connections`. The runners' connections dominate, not DBOS's. The 6 is an
idle measurement: an executor's DBOS pool may grow toward its cap of 20 under
load, and each status step and heartbeat opens a short-lived connection of its
own.

⚠ That default is now **8**, so a worker peaks at ~14 connections. Against
Postgres's stock `max_connections = 100` this caps you near 7 worker
processes; check it before scaling out, and lower the concurrency rather than
the process count if throughput matters more than one run's latency.

## What is not ready

Stated plainly so nobody discovers it at 3am:

- **Cross-version strands need a human.** The sweep reclaims a dead
  executor's work only within its own application version, reports what it
  could not take, and keeps the evidence — but draining a stranded version
  still means starting a process pinned to it. See above.
- **No metrics.** No counters, no histograms, nothing to alert on. Diagnosis is
  the SQL above, the Ops page (`GET /jobs`: runs, status counts and executor
  heartbeat ages; `GET /jobs/{id}/detail`: per-unit timings), and
  `python -m apps.cli.app explore executor-health`.
- **Never load-tested**, never run multi-process beyond a two-process spike,
  never deployed.

# Ops-plane runbook (Hatchet)

For whoever is holding the pager, and for anyone who wants to *see* a run rather
than query one. The counterpart to Track A's runbook, which is deliberately all
SQL — that difference is the point.

## What is running

Unlike the library track, there is an engine, and it must be up.

| process | required | why |
|---|---|---|
| engine (`hatchet-lite`) | **yes** | schedules everything; nothing runs without it |
| worker (`python -m apps.worker`) | **yes** | there is no in-process mode |
| API (`apps/api`) | for HTTP | submits and reads; does not execute units |

⚠ **A worker is not an optimisation here, it is a precondition.** `run_no_wait`
returns a workflow-run id, but `runs.get()` on it returns **404** until a worker
has registered the workflow definition. A run id can exist and be unqueryable.
The library track has no equivalent state.

```bash
docker compose -f infra/docker/docker-compose.hatchet.yml up -d
```

Postgres is **not** in that compose file — the engine uses the host instance,
the same one holding domain data, in its own `hatchet` database. Deliberate: it
keeps the comparison honest by not charging this track for a database the other
track also needs. ⚠ That database's timezone must be UTC or the engine refuses
to start.

## Testing it in a browser

This is the capability Track A does not have at any price, so it is worth
knowing what is actually there.

**http://localhost:8888** — dashboard and REST API on the same port. Log in with
the seeded credentials, which are defaults in the engine's own source
(`pkg/config/database/config.go:59-60`), not something we chose:

```
admin@example.com
Admin123!!
```

⚠ Development defaults, hardcoded and public. Change them before this is
reachable from anywhere but localhost.

What is worth clicking, in the order it answers questions:

| view | what it answers | Track A equivalent |
|---|---|---|
| **Runs** list | which runs exist, status, duration | `SELECT` on `orchestration_run` |
| filter by metadata | "every run for scenario X" — key `scenario_id` | `WHERE scenario_id = …` |
| a run's **DAG view** | which indicators ran, which skipped, what is blocked on what | nothing — the shape is implicit in `wave.py` |
| a task's **Logs** tab | `ctx.log()` output, per unit, searchable | process stdout |
| a task's **Input/Output** | the exact `RunInput` and what came back | `job_unit` row |
| **Replay** button | re-run from the failure, no resubmit | `fork_workflow`, CLI only |
| **Workers** | is a worker connected, how many slots free | `ps` |

⚠ **The DAG view renders a skipped unit the same green as a computed one.** The
engine's status enum has no `skipped` (spike B7), so a non-dirty indicator shows
COMPLETED. To tell them apart, open the run's **output** — the finalizer writes a
per-unit verdict there (spike B8):

```json
{"overall": "partial",
 "units": {"urban_form_block_compactness": {"status": "succeeded"},
           "urban_form_block_planned_floors": {"status": "skipped"}}}
```

That is also what `GET /jobs/{run_id}` returns, so the API and the dashboard do
not disagree — but the dashboard's *status chip* and the run's *verdict* can,
and the verdict is the one to believe.

### Driving a run from the browser

⚠ The workflow is named **`reimagina-run-v<shape>`**, where the suffix is a
digest of the indicator graph's nodes and edges. Print the one this deployment
uses before going looking for it:

```bash
python -c "from reimagina.orchestration.hatchet_app import workflow_name, graph_version; print(workflow_name(), graph_version())"
```

An incompatible graph change mints a new name, so old workers keep serving old
runs until they drain and a rolling deployment is simply "both names registered
for a while". If the dashboard lists a name nothing is running, that is a
half-applied rollout — and `POST /jobs` will refuse with
`graph_version_unavailable` rather than accepting work nothing can execute.

Timeouts and retries are *not* in the name: they are compatible with in-flight
runs and the engine versions them under one name. They ride in `graph_version`
on the run input and in its metadata, so "which runs used the graph before the
retune" is a `runs.list()` filter.

The dashboard triggers runs directly: **Workflows → `reimagina-run-v…` → Trigger
Run**, then paste a `RunInput`:

```json
{"run_id": "", "dirty": ["urban_form_block_compactness"],
 "plan_id": "<uuid present in jobs_runtime.run_plan>",
 "scenario_id": null, "aoi_id": "cuauhtemoc",
 "code_version": "dev", "concurrency_key": "baseline"}
```

⚠ **The feature ids are not in this payload.** They live one row per dirty
indicator in `jobs_runtime.run_plan`, keyed by `plan_id`, and a hand-triggered
run gives you no way to create one — so borrow a `plan_id` from a live run or
accept that this only works for a plan that already exists:

```sql
SELECT plan_id, indicator_slug, array_length(feature_ids, 1) AS features
FROM jobs_runtime.run_plan;
```

A run whose `plan_id` is absent — never written, or past the retention horizon
— fails every unit with `no run_plan row for plan …`. That error means the
plan is missing, **not** that the feature set is empty. It is deliberately loud:
a unit computing zero features would report success and leave the indicator
stale with nothing to attribute it to.

⚠ `dirty` is computed by the submitter, not by the workflow — trigger it by hand
and you are asserting the dirty set yourself. Useful for testing one unit,
wrong for anything real. Note also that `concurrency_key` collides with live
runs: `max_runs=1` with `GROUP_ROUND_ROBIN` queues a hand-triggered run behind
the in-flight writer for that scope.

### Why the payload is a handle

Carrying the ids inline made a full-city recompute **10,088,804 bytes against
gRPC's 4,194,304 default** — 86,225 blocks × 3 dirty indicators = 258,675 uuids
— and the engine refused the submission before seeing any work. The ceiling was
~35,800 blocks, **42% of the city**. Measured, then removed: the same run now
submits in 0.64 s and finishes in 26 s, with **three rows** in `run_plan`.

That is the Claim Check pattern, and it is what Temporal and Step Functions
prescribe for the same limit. The reason applies here too — a workflow input is
persisted in the engine's history, so a large one is paid on every read, every
replay and for the whole retention period, not once at submit. Raising
`grpc_max_send_message_length` would move the wall while leaving ~10 MB per run
in the engine database under a 720 h retention default.

⚠ **The plan retention horizon must exceed the engine's run retention**, not
merely the longest run. `runs.replay()` re-executes from the original input —
which on this track is a `plan_id` handle — so a plan that expires before the
run it belongs to turns a replayable run into one that fails every unit with the
missing-plan error above. With `SERVER_LIMITS_DEFAULT_TENANT_RETENTION_PERIOD`
at its 720 h default, the horizon is 45 days, not 30: the margin absorbs a unit
that started just before the boundary.

Retention is a **scheduled Postgres operation**, `scripts/reclaim_run_plans.sql`,
run by the platform's scheduler:

```bash
make reclaim-run-plans                     # horizon from REIMAGINA_PLAN_RETENTION
# or directly — note the value is NOT pre-quoted; the script quotes it:
psql "$DATABASE_URL_DIRECT" -v horizon="45 days" -f scripts/reclaim_run_plans.sql
```

⚠ It is deliberately **not** on the submit path and **not** a Hatchet cron. It
was an age sweep inside `submit_run` with a 24 h horizon, which deleted live
runs' plans — any horizon short enough to be worth sweeping opportunistically is
shorter than the engine's retention. Putting it in the engine instead would make
plan retention depend on the engine being healthy, which is backwards for the
table that exists to outlive it.

Plans are no longer deleted when a run reaches a terminal state. Two deletes
remain in application code, both provable: an idempotency collision (the
accepted run carries a different `plan_id`) and a trigger the engine refused
outright. Anything ambiguous — a deadline, an unavailable engine — retains,
because the run may have been accepted and a retry deduplicates onto it.

### If the dashboard will not load

`/api/v1/meta` answers unauthenticated and is the fastest check that the engine
is serving at all:

```bash
curl -s http://localhost:8888/api/v1/meta
```

Expect `{"auth":{"schemes":["basic"]},...}`. ⚠ `/api/v1/readiness` and
`/api/v1/liveness` return **403**, not 200 — they are not anonymous health
checks, so do not wire a probe to them expecting otherwise.

Two failure modes seen more than once:

- **Login succeeds then immediately bounces** — `SERVER_AUTH_COOKIE_DOMAIN` is
  unset or does not match the host you are browsing.
- **Engine will not start** — the `hatchet` database's timezone is not UTC.

## The worker is a process tree — supervise it as one

⚠ **The single most important operational requirement on this track**, and the
resolution of the "stale workers never expire" finding.

The SDK runs its action listener in a **spawned child process**. Signal only the
parent and that child survives, is reparented to init, keeps its gRPC stream
open and **keeps heartbeating** — so the engine goes on reporting the worker
ACTIVE, entirely correctly, because something really is alive claiming to be it.
It keeps assigning work the orphan can no longer execute, and runs sit at
`running` with no units until timeouts fire.

Measured on a development machine: **27 orphaned `spawn_main` processes**, all
`PPID 1`, the oldest over a day old, each holding a registration open. Killing
them dropped the engine's ACTIVE count to **zero** immediately, with no change
to the engine and no reaping API called.

So the engine's liveness model is not the problem, and needs no help from us:
kill the whole process group and the registration clears in **~5 seconds**
(`tests/qualification/test_worker_lifecycle.py`).

**What this requires of a deployment:**

- Run the worker as **PID 1 in its own container**, so the container runtime's
  signal reaches the whole tree. `docker stop` / Kubernetes termination then do
  the right thing with no extra configuration.
- If instead it runs under a supervisor, that supervisor must signal the
  **process group** (systemd's `KillMode=control-group`, the default).
- Never `kill <pid>` a worker by hand. Use `kill -- -<pgid>`.

**When a suite or a deployment looks wedged, check `ps` before the engine:**

```bash
ps -eo pid,ppid,etime,command | awk '$2==1 && /spawn_main/'
```

Orphans there mean the engine is being told the truth by a process that can no
longer do any work. There is no engine-side fix and no API to call; kill them.

A *clean* exit needs none of this: `exit_gracefully` calls
`_pause_task_assignment` — "tell the engine that the worker is paused" — before
anything else. ⚠ It is `async def`, so calling it from sync code returns a
coroutine nobody awaits and does **nothing at all**. That bug is the origin of
the belief that a clean exit leaks exactly like a kill.

## Is a run stuck?

One plane, not two. There is no table of ours to cross-check against, which
is faster when the engine is healthy and leaves nothing when it is not.

```bash
# needs HATCHET_CLIENT_TOKEN
hatchet-sdk equivalent: hatchet.runs.list(statuses=[RUNNING], since=…)
```

In the dashboard: **Runs**, filter status `Running`, sort by duration. Anything
older than `REIMAGINA_UNIT_TIMEOUT_MIN` should have been killed already; if it
has not, the worker is not reporting.

| symptom | meaning |
|---|---|
| run QUEUED, no worker in **Workers** | nothing is registered; start the worker |
| run QUEUED, workers present, slots full | saturation — check `schedule_timeout` headroom |
| task RUNNING far past its timeout | worker lost the engine but kept the statement; see below |
| run FAILED, verdict says `partial` | some units succeeded; do **not** blanket-retry |

## A unit that will not stop

Unchanged from the other branch, because neither ops-plane solves it:
cancellation is cooperative and nothing interrupts a blocking psycopg call
(spike B4). The engine marks the task CANCELLED immediately while the SQL keeps
running.

```sql
SELECT jr.id, jr.backend_pid, jr.status, a.state, a.query_start
FROM jobs_runtime.job_run jr
LEFT JOIN pg_stat_activity a ON a.pid = jr.backend_pid
WHERE jr.run_id = :run_id AND jr.status NOT IN ('succeeded','failed','cancelled');
```

`pg_cancel_backend(backend_pid)` is what actually ends it. `job_run` survives on
every branch precisely because of this — the answer lives outside both engines.

⚠ Cancelling this way raises `QueryCanceled` in the runner, which **inherits from
`OperationalError`** — the class the retry classifier treats as transient. It is
tested for first and classified non-retryable, so a unit you stopped stays
stopped. Reorder those checks and the engine re-runs a cancelled unit up to
`REIMAGINA_UNIT_RETRIES` more times, each free to burn a full
`REIMAGINA_UNIT_TIMEOUT_MIN`. `tests/unit/test_unit_retry_policy.py` fails if the
order is swapped.

## Two databases, one plane

The API derives a plan on its own connection; the worker executes it against
`DATABASE_URL_DIRECT`. If those are not the same server *and* the same database,
the API produces a plan for rows the worker never sees — silent and total.

`assert_same_domain_database` compares
`(current_database(), inet_server_addr(), current_setting('port'))` on both sides
and fails closed when one reports a server address and the other does not.

| rejected alternative | why |
|---|---|
| `current_database()` alone | two servers both hosting `reimagina` compare equal — the staging/production split the guard exists to catch |
| `inet_server_port()` | NULL over a Unix socket, which is exactly where two local clusters need telling apart |
| `pg_control_system()` | the precise answer, and superuser-only: on managed Postgres the app role gets permission-denied on **every submit** |

Residual limitation, stated rather than implied: with both sides on Unix sockets
`inet_server_addr()` is NULL on each, so identity rests on database name plus
port. Sound wherever sockets mean localhost — an assumption about deployment
shape, not proof of identity.

The direct-side probe is memoised per DSN; the caller side is read every request.

## `GET /jobs/{run_id}/events` — the error frame

⚠ **The stream reconnects before it gives up.** The engine's progress stream is
a gRPC connection and it drops for ordinary reasons — a restart, a rolling
deploy, a blip — while the run carries on executing. So a dropped listener is
retried with bounded backoff until the run is terminal or
`REIMAGINA_SSE_OUTAGE_DEADLINE_S` (default 60) of *continuous* failure; the
budget resets on any successful resubscription. Reconnecting also reconciles,
because `subscribe_progress` sends a snapshot first, and replayed frames are
de-duplicated so catching up is not mistaken for new progress.

Only once that budget is spent does the frame below appear. Seeing it means the
engine was unreachable for a solid minute, not that one packet went missing.

The stream can fail after its headers are on the wire, and at that point the
status code is settled: a 503 is no longer reachable. The route emits one
terminal frame instead, then closes.

```
event: error
data: {"reason":"orchestrator_unavailable"}
```

An SSE **event type**, not a run status — `RUN_TERMINAL_STATES` is the port's
closed vocabulary and a transport failure is not an orchestration outcome. A
client that does not know `error` sees the stream close, as before, minus the
ambiguity with a run that simply finished. The reason is a fixed token; the
underlying exception text never reaches the wire.

## Configuration that will bite you

| variable | unset behaviour | why it matters |
|---|---|---|
| `HATCHET_CLIENT_TOKEN` | client/worker startup fails | required to register workflows and call the engine; pure imports do not need it |
| `DATABASE_URL_DIRECT` | submit/worker startup fails | planning and execution must resolve to the same migrated domain database |
| `REIMAGINA_UNIT_TIMEOUT_MIN` | 120 | ⚠ Hatchet's own default is **60 seconds** — it would kill a healthy unit a minute in |
| `REIMAGINA_QUEUE_TIMEOUT_HOURS` | 6 | ⚠ Hatchet's default is **5 minutes**, and timed-out queued work reports FAILED — indistinguishable from broken compute |
| `REIMAGINA_UNIT_RETRIES` | 2 | with backoff; `NonRetryableException` opts out deterministic failures |
| `REIMAGINA_DEDUP_TTL_S` | 60 | idempotency window — a debounce, not a result cache |
| `REIMAGINA_SSE_OUTAGE_DEADLINE_S` | 60 | how long a *continuous* listener outage may last before the stream gives up |
| `HATCHET_CLIENT_TLS_STRATEGY` | SDK default is TLS | ⚠ the compose sets `SERVER_GRPC_INSECURE`, so a client left at the default dies on a handshake with `WRONG_VERSION_NUMBER` |
| `HATCHET_IMAGE` | the pinned **digest** | a tag is a mutable pointer; this branch already ran a year of spikes against an image its compose did not name |

⚠ `SERVER_LIMITS_DEFAULT_TENANT_RETENTION_PERIOD` is **720h — runs older than 30
days are deleted.** On this track that is the run history, since there is no
table of ours. Track A's history lives in `orchestration_run` and is bounded only
by disk.

**It is pinned to a second value.** `runs.replay()` re-executes from the original
input — a `plan_id` handle into `jobs_runtime.run_plan` — so the retention
horizon in `scripts/reclaim_run_plans.sql` must be **longer** than this, or a
replayable run finds no plan and fails every unit. 720h here means 45 days there.
Change one, change the other.

## Two things to watch, and what they mean

Metrics are on (`SERVER_OBSERVABILITY_ENABLED`, `SERVER_PROMETHEUS_ENABLED`), so
these are engine-native rather than something to instrument:

| alert on | native signal | what it means |
|---|---|---|
| worker availability | ACTIVE workers serving the current `reimagina-run-v<shape>` | zero means submissions are being refused and readiness is 503 |
| queue delay | `REQUEUED_NO_WORKER`, `SCHEDULING_TIMED_OUT` task events | work is waiting on capacity, not failing |
| retries | `RETRYING` task events | transient database contention, or a unit that should be `NonRetryableException` |
| timeouts | `TIMED_OUT` task events | ⚠ a unit hitting `REIMAGINA_UNIT_TIMEOUT_MIN` also hits `statement_timeout`, and is **not** retried |
| task failures | `FAILED` task events | genuine compute failures; check `job_run` for provenance |
| engine reachability | `/api/v1/meta` (answers unauthenticated) | ⚠ `/api/v1/readiness` and `/api/v1/liveness` return **403**, so do not wire a probe to them |
| orphaned listeners | `ps -eo pid,ppid,command \| awk '$2==1 && /spawn_main/'` | the process-tree hazard above; check this before blaming the engine |

## Backup, retention and upgrade

The engine's Postgres database **is** this track's run history. There is no table
of ours to fall back on, which is the comparison row and also an operational
obligation:

- **Back up the `hatchet` database** on the same footing as domain data. Losing
  it loses every run's history, logs and replay capability at once.
- **Retention is two values, pinned together** — see above. Neither is a default
  worth inheriting.
- **Upgrading the engine re-runs the qualification suite.** The pin is a digest
  precisely so an upgrade is a decision; `tests/qualification/` is what makes it
  a checked one. Bump `HATCHET_IMAGE`, run `make test-qualification` and
  `make test-conformance`, and record the delta before rolling it out.
- **A graph change is a rolling deployment.** Workers for the old
  `reimagina-run-v<shape>` keep serving until their runs drain; do not delete the
  old workflow while runs reference it.

## What is not ready

Stated plainly so nobody discovers it at 3am:

- **No unbounded baseline cron.** A whole-database recompute has no safe scope
  contract. Submit bounded AOI/scenario work explicitly; add a dedicated
  baseline selector before automating it.
- **`rate_limits` and `slot_cost` are not wired up.** They are the engine-native
  answer to N workers saturating one Postgres, and unmeasured.
- **`ctx.refresh_timeout` is not wired up**, so a wedged unit burns the full
  `REIMAGINA_UNIT_TIMEOUT_MIN` rather than dying in minutes. Needs a runner
  progress callback that does not exist yet.
- **Adding an indicator requires a redeploy**, not just a catalog row — the DAG
  is declared at import. Track A reads the registry at runtime.
- **TLS and credentials are development values.** The compose runs plaintext
  gRPC, insecure cookies, and the engine's own hardcoded seeded login. All three
  are grouped and commented in `docker-compose.hatchet.yml`; none is safe off
  localhost.

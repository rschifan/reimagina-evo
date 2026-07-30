# Track A — DBOS ops-plane

**Branch:** `feat/ops-plane-dbos` (worktree `~/Desktop/projects/reimagina-dbos`)
**Engine:** [DBOS Transact](https://github.com/dbos-inc/dbos-transact-py) `>=2.28,<3` — MIT, ungated
**One line:** durable execution as a *library*. There is no orchestration service. Checkpoints live in the application's own Postgres, and we keep the scheduler we wrote.

---

## 1. The shape of the decision

DBOS is a Python library that makes a function call survive a process death. It gives us
**durability** and nothing else — no scheduler, no run-status model, no dashboard. So this
track keeps everything we already had (`wave.py`, `orchestration_run`, `job_unit`) and
borrows exactly one property: a workflow that was interrupted resumes from its last
completed step instead of starting over.

Everything in this document follows from that. Track B's document is the mirror image.

---

## 2. Shared foundation (identical on both tracks)

Both branches fork from the same `main`, and these modules are **byte-identical** between
them. They are what makes the comparison a comparison rather than two different products.

| Module | Role |
|---|---|
| `orchestration/port.py` | The `Orchestrator` Protocol — 4 methods, plain dataclasses, no engine types leak. Also the status vocabulary (`UNIT_TERMINAL_STATES`, `RUN_TERMINAL_STATES`, `overall_status()`). |
| `orchestration/plan.py` | `plan_run()` — resolve the change-set, derive the dirty-set, in dependency order. The "what" of a run; an engine only replaces the "how". |
| `orchestration/impact.py` | `compute_dirty_set()` — which indicators an edit invalidates. |
| `orchestration/graph.py` | Engine-neutral `topological_order` / `dependents_of` / `downstream_closure`. Pure `graphlib`, no engine, no DB. |
| `orchestration/coordinator.py` | `run_dirty_set()` — the in-process `--sync` executor, and the `_dep_map` derivation. |
| `orchestration/runners.py` | slug → compute function. A runner owns its own `job_run` row. |
| `orchestration/frames.py` | SSE wire framing. The API formats whatever `subscribe_progress` yields, so the engine never knows it feeds an HTTP endpoint. |
| `jobs_runtime.job_run` + `backend_pid` (migration r031) | Domain provenance, and the **only** mechanism that actually stops a running statement. Survives on both branches. |
| `tests/conformance/` | The port contract, asserted through `factory.get_orchestrator` only. |
| `tests/workloads/` | 9 synthetic DAG shapes for measuring how an engine schedules. |

### The port

```python
class Orchestrator(Protocol):
    def submit_run(self, conn, *, changed_attributes, edited_features,
                   scenario_id=None, aoi_id=None, code_version="dev") -> RunHandle: ...
    def get_run(self, conn, run_id: str) -> RunStatus | None: ...
    def cancel_run(self, conn, run_id: str) -> bool: ...
    def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]: ...
```

`orchestration/factory.py` is the single place either engine is named. Swapping the
ops-plane is a rewrite of that file, not a hunt through the API router, the CLI and four
test modules.

---

## 3. Deployment shape

```mermaid
flowchart LR
    subgraph host["one host, one database"]
        API["API process<br/>apps/api<br/>MODE=client"]
        W1["worker 1<br/>python -m apps.worker<br/>MODE=executor"]
        W2["worker N<br/>MODE=executor"]
        PG[("PostgreSQL<br/>curated_geo · jobs_runtime<br/><b>dbos</b> schema = checkpoints")]
    end
    Client -->|POST /jobs| API
    API -->|DBOSClient.enqueue| PG
    W1 <-->|dequeue · checkpoint · LISTEN/NOTIFY| PG
    W2 <-->|dequeue · checkpoint| PG
    API -->|read_stream SSE| PG
```

**Processes to operate: 2 kinds (API, worker). Databases: 1. Containers beyond Postgres: 0.**

| process | required | why |
|---|---|---|
| API (`apps/api`) | yes | accepts submits, serves status and the SSE stream. Does **not** execute PostGIS. |
| worker (`python -m apps.worker`) | yes in production | executes queued runs and units. Scale this fleet for throughput. |

`REIMAGINA_DBOS_EXECUTION_MODE` gates this: `client` (default, API-safe), `executor`
(workers), `dev` (explicit one-process execution for local CLI and integration tests).
Because DBOS is a library, the submitting process *can* also drain — a worker fleet is how
this scales, not how it works at all.

---

## 4. Component map

| File | LOC | What it owns |
|---|---:|---|
| `orchestration/dbos_app.py` | 580 | Queues, steps, workflows, executor identity, database guards, progress publishing |
| `orchestration/dbos_orchestrator.py` | 432 | The port implementation — submit / read / cancel / subscribe |
| `orchestration/runstore.py` | 200 | `orchestration_run` + `job_unit` SQL, with terminal-state guards |
| `orchestration/wave.py` | 75 | Pure wave scheduler — readiness, skip propagation, final status |
| `orchestration/factory.py` | 81 | Modes: `dbos`, `sync` |
| `orchestration/executor_health.py` | 32 | Postgres-native worker heartbeat |
| `apps/worker/app.py` | 65 | Launch DBOS, start the heartbeat, block |

Diff against the fork point, orchestration package only: **+1186 / −773 across 15 files.**

---

## 5. How each feature is implemented

### 5.1 Submit

`DBOSOrchestrator.submit_run` (`dbos_orchestrator.py:105`):

1. `assert_same_domain_database(conn)` — compares `current_database()` on the caller's
   connection against `DATABASE_URL_DIRECT`. Planning in one database while workers execute
   in another is silent and total.
2. `plan_run(...)` derives the dirty-set. Empty → `noop_handle`, no run is persisted.
3. **The dirty-set is serialised into the workflow input**, together with each unit's
   feature scope and its dependency list.
4. `validate_workflow_input()` rejects duplicates, malformed feature ids and dependency
   cycles *before* the workflow is persisted.
5. In `client` mode: `DBOSClient.enqueue(EnqueueOptions(workflow_name=..., queue_name="runs",
   workflow_id=run_id), ...)`. The API never imports or launches an executor.
6. Return `RunHandle(run_id, status=None)` — 202, caller polls or streams.

> **The run id *is* the DBOS workflow id.** No mapping table. A run is addressable from any
> process, so the API can cancel or stream a run a worker is executing, and re-submitting
> the same id attaches to the existing workflow instead of starting a second one.

### 5.2 Why the DAG is a workflow *input*

This is the single most load-bearing design decision on the branch, and it is a
**correctness requirement, not a style rule** (spike 2).

DBOS matches replayed steps by `(position, function name)`. Our design calls the *same*
step function once per indicator, so position alone decides which checkpointed output a
replayed call receives. Measured:

```
registry at resume : ["vNEW", "v0", "v1"]      (vNEW inserted at the front)
module-global wf   -> "G:v0, G:v0, G:v1"       no error raised
input-passing wf   -> "I:v0, I:v1"             unaffected
```

Position 0 asked for `vNEW` and got **`v0`'s recorded output**. No exception, no warning, a
plausible-looking result — one indicator's value silently attributed to another. A workflow
body that read the catalog from a module global would corrupt data on any catalog edit
mid-run.

**Rule enforced throughout `dbos_app.py`: the workflow body branches only on workflow
inputs and step results.** Reading a global *inside a step* is fine — a step's output is
checkpointed; it is the body's control flow that must be reproducible.

### 5.3 Scheduling — we still own it

`run_workflow` drives the dirty-set wave by wave:

```python
while True:
    wave = plan_wave([(slug, statuses[slug]) for slug in order], registry)
    for slug in wave.skip:                      # unsatisfied input
        statuses[slug] = s_settle_unit(run_id, slug, "skipped")
        _emit(run_id, slug, statuses[slug], "unsatisfied input")
    if not wave.dispatch:
        break
    handles = [(slug, units_queue().enqueue(unit_workflow, run_id, slug,
                                            features[slug], params))
               for slug in wave.dispatch]
    for slug, handle in handles:                # fixed order, see below
        try:    handle.get_result(); outcome = "succeeded"
        except Exception: outcome = "failed"
        statuses[slug] = s_settle_unit(run_id, slug, outcome)
        _emit(run_id, slug, statuses[slug])
final = overall_status(statuses.values())
s_finalize(run_id, final); _emit(run_id, None, final)
DBOS.close_stream(PROGRESS_STREAM)
```

Two non-obvious constraints encoded here:

- **Enqueue in a fixed order, await in the same one.** Child workflow ids are positional
  (`parent_id + function_id`), so an as-completed loop would shift the ids of everything
  after it and mint duplicate children.
- **Branch on `s_settle_unit`'s return, not on `get_result`.** `get_result` looks
  checkpointed but is not — DBOS records it and never reads it back, so a replay re-queries
  the child's *live* status. A step's output is genuinely checkpointed, so branching on
  that keeps the body reproducible.

`s_settle_unit` also **reconciles**: only the child writes a unit's status, so a child whose
process died leaves `job_unit` stuck at `running` forever while the parent closes the run.

### 5.4 Execute a unit

`s_compute` (`@DBOS.step(retries_allowed=False)`) is where the minutes of blocking PostGIS
happen. Four deliberate properties:

| property | why |
|---|---|
| **Not retried** | A runner exception is deterministic — a retry burns the same minutes to reach the same failure. Infrastructure faults surface as a failed *connect*, retried by the step that owns it. |
| **One transaction, committed at the end** | The write path is stale-then-insert. On autocommit, a `pg_cancel_backend` between the flip and the insert leaves features with **zero** current rows — and a cancelled workflow never re-runs the step to repair it. `serving_geo` exposes only `current`, so that was a permanent hole in served values. |
| **Re-checks cancellation** | DBOS's own check fires when the step *begins*. Without this re-check a unit would run a full compute and commit results for a run already reported terminal. |
| **Re-records the failed `job_run` on a fresh DSN** | The rollback erases the `failed` row the runner opened. Reconnecting from `conn.dsn` fails auth (psycopg2 masks the password as literal `xxx`), so the original DSN string is used. |

### 5.5 Status vocabulary — ours, in our tables

`runstore.py` writes `jobs_runtime.orchestration_run` and `jobs_runtime.job_unit`:

- `succeeded | failed | **skipped** | cancelled` per unit;
- `succeeded | **partial** | failed | cancelled` per run.

Both `set_unit_status` and `finalize_run` refuse to move a **terminal** row. That guard is
what makes cancellation stick: a cancelled unit's worker keeps going, catches the query
error and tries to write `failed` over the top — resurrecting a run that was already
closed. Both functions return whether the write landed, and every caller announces only
what the database accepted.

`get_run` reads detail from our tables and **existence from the engine**. A submitted run
is durable the moment `enqueue` returns, but its envelope is written by the workflow's own
first step — so there is a window where the run exists and `jobs_runtime` has nothing to
say. `_from_workflow` reconstructs it from the workflow's own persisted input (which *is*
the unit list). Writing the envelope in `submit_run` instead would reintroduce the
permanent wedge `main` already has: a crash between commit and enqueue leaves a `running`
run nothing will ever drive.

### 5.6 Cancel — three mechanisms, none sufficient alone

```python
_client().cancel_workflow(run_id, cancel_children=True)     # stop scheduling
backend_for_connection(conn).job_run_repo().cancel_run_backends(run_id)  # pg_cancel_backend
for unit in current.units: runstore.set_unit_status(..., "cancelled")    # close in our vocabulary
```

`cancel_children` matters: units are child workflows, and leaving them schedulable lets the
next wave start after the run was closed. If `finalize_run` returns `False` there is no
envelope yet — the cancel landed inside the submit window — so the orchestrator writes the
envelope itself, already terminal. `create_run` is `ON CONFLICT DO NOTHING`, so the
workflow's later attempt is a no-op.

**Neither engine can interrupt a thread blocked in a psycopg C call.** `pg_cancel_backend`
against `job_run.backend_pid` is the real answer, and it lives outside both ops-planes.

### 5.7 Progress / SSE

`_emit` → `DBOS.write_stream(PROGRESS_STREAM, ...)`, called **from the workflow body, never
a step**: a stream write from a body is exactly-once, from a step at-least-once, and a
duplicated terminal event would close a subscriber's stream early.

`subscribe_progress` reads that stream through `DBOSClient` (not the ambient `DBOS`), so the
API can follow a run a worker is executing without hosting the workflow definitions.
`read_stream` is a *synchronous* blocking generator, so it runs on a **dedicated daemon
thread**, not `run_in_executor` — a future returned by `run_in_executor` cannot be cancelled
once running, so an SSE client that disconnects one second into a twenty-minute run pinned a
shared-pool thread for the remaining nineteen. Roughly thirty such disconnects would
deadlock the API's entire thread-offload path.

Bounds: `REIMAGINA_PROGRESS_SUBSCRIBERS` (32) slots, a 128-event handoff queue, and a
snapshot-first yield so a late subscriber is told the outcome instead of waiting forever.

Verified (spike 6): ordered, offset-resumable, closes cleanly.

```
full replay from offset 0 : ['u0','u1','u2','u3','u4', None]
resume at offset=6        : []          (no gap, no duplicate)
```

### 5.8 Recovery

Launching an executor reclaims **its own** interrupted workflows automatically. Scoping is
by `executor_id`, and both failure modes are silent:

- **Not stable across restarts** → a restarted process reclaims *nothing*; its predecessor's
  in-flight runs are orphaned with no error. (A pid-derived id does exactly this.)
- **Shared by two live processes** → one reclaims work the other is still running. On dbos
  2.28.0 recovery is **not idempotent**, so that is a double execution.

Default is `<hostname>:<role>` — safe for at most one process per role per host, and it logs
a warning. Beyond that, set `REIMAGINA_EXECUTOR_ID` per replica (StatefulSet pod name,
systemd instance name).

Spike 1, kill −9 mid-wave, measured:

```
compute_log   u0 runs=1  [exec-A]          <- not recomputed
              u1 runs=1  [exec-A]          <- not recomputed
              u2 runs=2  [exec-A, exec-B]  <- the interrupted one, re-run
app.result    u0=1  u1=1  u2=1             <- replace-in-place, no duplicates
```

**Redundant compute is exactly one unit — the minimum possible.** This is the headline
operational property of the track.

⚠ **Cross-executor recovery is deliberately not built.** A live peer does *not* adopt
orphans (measured). The sweep works (`_recover_pending_workflows(["exec-A"])`), but it is a
private API and 2.28.0 ships without the idempotent-recovery fix (PR #786 merged six hours
after 2.28.0 was published), so a sweep firing against a live executor double-executes.
`executor_health.heartbeat()` + `explore.ops_executor_health_v` exist for *diagnosis* only.

### 5.9 Blue-green deploys

`application_version` gates recovery. Unpinned it is an MD5 of the registered workflow
source, so any edit strands in-flight runs. Pinned, they become recoverable onto
possibly-incompatible code. Spike 5 measured version isolation holding — a `green` process
sweeping the same `executor_id` recovered 0; a `blue` process recovered 1 — so the
blue-green drain is real. Bonus operator signal DBOS logs for free:
`Current version 'blue' is not the latest version.`

⚠ `REIMAGINA_APP_VERSION` is **sticky**: once set, every process on a different version logs
"not the latest version" until something newer is recorded.

### 5.10 API surface: `wait: true` survives here

`POST /jobs` still honours `wait: true` → **200 with the finished status**, because under a
durable-execution library one process can both submit and drain. The wait is a poll loop in
the *route* (`_await_terminal`), not in the port — an engine that needed a separate worker
could not implement a blocking submit at all. Bounded by the stream deadline; a 504 tells the
caller the run is still going and to poll `GET /jobs/{run_id}`.

Track B **rejects `wait: true` with a 422**. That is the clearest single-line expression of
the library-vs-service difference in the HTTP surface.

### 5.11 Guards against the engine being helpful

DBOS prefers making things work over failing. Two guards exist because of it:

| hazard | guard |
|---|---|
| No database URL → DBOS silently uses a **local SQLite file** | `dbos_dsn()` raises unless `DATABASE_URL_DIRECT` is set |
| Wrong DSN → DBOS **creates the database** and installs its schema | `_assert_domain_database()` checks `to_regclass('jobs_runtime.job_run')` at launch |
| API DSN ≠ worker DSN | `assert_same_domain_database()` compares `current_database()` server-side, per submit |

`launch()` is also lock-protected: `DBOS.launch()` marks itself launched *before* it finishes
building the system database, so a second concurrent thread got "already launched" and then
failed in `register_queue` — turning one concurrent submit into a 500.

---

## 6. Database footprint

| | |
|---|---|
| Databases | **1** — checkpoints share the domain database, under the `dbos` schema (11 tables) |
| Backup coherence | `pg_dump -n curated_geo -n jobs_runtime` stays self-contained; **no reference to the `dbos` schema** (spike 8) |
| Migration tracking | `dbos.dbos_migrations` — does not collide with alembic |
| Idle connections | worker 4, DBOS's own pool `pool_size=5, max_overflow=0` per client, **+1 per in-flight unit** |
| Budget rule | `processes × (4 + REIMAGINA_UNIT_CONCURRENCY)` against `max_connections`. The runners dominate, not DBOS. |

Migrations added on this branch: `r032_drop_procrastinate_schema`, `r034_executor_heartbeat`,
`r035_drop_procrastinate_job_id`. `orchestration_run` and `job_unit` (r028) are **kept**.

⚠ Migration histories fork here: `r032` on this branch, `r033` on Track B, both off `r031`
and mutually unresolvable. A database migrated by one worktree makes the other's alembic
fail with `Can't locate revision`. Each track needs its own database.

---

## 7. Configuration

| variable | default | consequence if wrong |
|---|---|---|
| `DATABASE_URL_DIRECT` | — | **startup fails.** Unset → SQLite checkpoints; wrong → DBOS creates a database |
| `REIMAGINA_DBOS_EXECUTION_MODE` | `client` | `executor` for workers, `dev` only locally |
| `REIMAGINA_EXECUTOR_ID` | `<hostname>:<role>`, warns | recovery reclaims **only** this exact id |
| `REIMAGINA_RUN_CONCURRENCY` | 2 | parent workflows per executor |
| `REIMAGINA_UNIT_CONCURRENCY` | 2 | units per process (= connections) |
| `REIMAGINA_PROGRESS_SUBSCRIBERS` | 32 | excess SSE clients get told to reconnect |
| `REIMAGINA_EXECUTOR_HEARTBEAT_S` | 30 | must be positive |
| `REIMAGINA_APP_VERSION` | derived from source | ⚠ sticky once set |

`worker_concurrency`, not `concurrency`, on both queues: the global limit also counts
`PENDING` workflows from earlier application versions, which a blue-green deploy
deliberately leaves lying around.

---

## 8. Measured evidence

### Scheduling (workload suite, 9 synthetic DAGs, `slr` = makespan ÷ critical path)

| workload | slr DBOS | slr Hatchet | head-of-line DBOS | head-of-line Hatchet |
|---|---:|---:|---:|---:|
| `diamond` | 3.19 | 1.13 | 1868 ms | 78 ms |
| `lattice` | 3.54 | 1.21 | 2480 ms | 103 ms |
| `barrier_probe` | 6.80 | 1.44 | 2811 ms | 96 ms |
| `layered_random` | 4.45 | 1.22 | 3023 ms | 1372 ms |
| `chain_deep` | 13.72 | 2.87 | 1956 ms | 80 ms |

**Track A is measurably the slower scheduler**, and the cause is structural: the wave loop
is a barrier. A wave does not dispatch until every unit in the previous wave has settled
through a checkpointed step, so per-hop latency is ~2–3 s and it is paid once per wave.

⚠ **Read this in proportion.** The suite's units are `pg_sleep` and `sha256` loops of
10 ms–3 s, chosen to measure the scheduler rather than geometry. Real indicator units run
for **minutes**. A 2 s wave barrier against a 4-minute unit is <1% overhead; against a
100 ms unit it is 20×. The scheduling comparison (`barrier_probe`, `lattice`, `diamond`) is
solid at ±2%; `chain_deep`/`many_tiny` are overhead-dominated and order-of-magnitude only.

The `sync` control column agrees to within 0.5% across both worktrees (3137 vs 3133 ms),
which is the strongest evidence the harness itself is sound.

### Spike results

| # | Spike | Result |
|---|---|---|
| 1 | Kill −9 mid-wave, cross-executor recovery | **pass** — exactly one unit recomputed |
| 2 | Registry drift under replay | **fail, worse than predicted** — silent wrong data, no error |
| 3 | Dirty-set / wave order stability | **failed, then fixed** — real bug on `main` (`f6ff6ef`) |
| 4 | Exception picklability | **pass — plan was wrong**; psycopg2 errors pickle fine |
| 5 | Blue-green with in-flight runs | **pass** — version isolation holds |
| 6 | Progress streams, offset resume | **pass** — ordered, resumable, closes cleanly |
| 7 | Connection budget | **pass** — DBOS costs ~3 connections idle |
| 8 | Schema co-location | **pass** — `pg_dump` stays self-contained |

---

## 9. Pros

- **Nothing to deploy.** No engine container, no second database, no broker, no credential.
  `make stack-up` + a worker is the whole ops-plane.
- **One database, one backup, one migration story.** Checkpoints are a schema, not a system.
- **Run history is ours and is unbounded.** `orchestration_run` / `job_unit` are joinable,
  indexable, aggregable. *"How many scenarios partially failed this week"* is one `GROUP BY`.
- **`partial` and `skipped` are first-class**, written at the moment they are meant, with a
  reason (`"unsatisfied input"`). No reconstruction from partial signals.
- **Minimum redundant compute on crash — exactly one unit** (measured, spike 1).
- **The submitting process can also drive.** A worker fleet is throughput, not a
  precondition; `dev` mode gives one-process execution for CLI and tests.
- **No engine to be down.** When the ops-plane misbehaves, the diagnosis is SQL against
  tables we wrote, and `--sync` still runs.
- **Adding an indicator is a catalog row.** The registry is read at runtime; no redeploy.
- **Semantics live in code you can read and step through** — `wave.py` is 75 lines.
- **Debuggability**: everything is a Python function in this repo; `pdb` works.

## 10. Cons

- **We own the scheduler.** `wave.py`, wave barriers, skip propagation, final-status
  rollup — all ours to maintain and to get right.
- **Measurably slower dispatch** — 1.9–3.0 s head-of-line vs Hatchet's ~80–100 ms, because
  a wave is a barrier and every settle is a checkpointed step.
- **No cross-executor recovery.** A dead worker's runs stay stuck until a process with that
  exact `REIMAGINA_EXECUTOR_ID` starts. Blocked on an upstream idempotency fix that missed
  2.28.0 by six hours.
- **`executor_id` is a silent-failure config knob.** Wrong in one direction = orphaned runs;
  wrong in the other = double execution.
- **The replay hazard is subtle and permanent.** Any future contributor who "simplifies" the
  DAG-as-input back to a module global reintroduces silent result misattribution. Guarded by
  a test and three comment blocks, not by a type.
- **No dashboard, no DAG visualisation, no per-task log viewer, no replay button.** Operator
  UX is SQL and process stdout.
- **No metrics.** No counters, no histograms, nothing to alert on.
- **No run dedup, no scenario serialisation.** A double-clicked Save starts two identical
  multi-minute runs. Track B gets both as config.
- **No OpenTelemetry.** Track B gets `traceparent` injected for free.
- **`REIMAGINA_APP_VERSION` is sticky** and the blue-green story requires keeping
  old-version processes alive to drain.
- **Never load-tested, never run multi-process beyond a two-process spike, never deployed.**

## 11. Known gaps

1. **No cross-executor recovery** — heartbeats support diagnosis only.
2. **No metrics** — diagnosis is the runbook's SQL.
3. **A `job_run` cannot be attributed to a unit.** `job_unit.job_run_id` exists (r028) and is
   **never written** — verified 0 of 3 rows on a live run. Per-*run* aggregates are correct;
   per-*unit* attribution is impossible, and joining the two tables produces a cartesian
   product (three units × three job_runs = nine rows).
4. **Never load-tested.**

---

## 12. One-paragraph summary for the meeting

Track A buys durability and nothing else. It costs zero new infrastructure — no engine, no
second database, no credential — and keeps full ownership of run vocabulary, run history and
scheduling semantics, all of them queryable in SQL alongside the domain data. It pays for
that with a scheduler we maintain, dispatch latency measured at 1.9–3.0 s per wave, no
operator UI, no metrics, and a recovery story that stops at the boundary of a single
executor. It is the right answer if the units are long and the fleet is small.

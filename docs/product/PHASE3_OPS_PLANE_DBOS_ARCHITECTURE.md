# Track A — DBOS ops-plane

> **Durable execution as a _library_.** No orchestration service. Checkpoints live in the
> application's own Postgres. We keep the scheduler we wrote and borrow one property:
> an interrupted workflow resumes from its last completed step.

**Branch** `feat/ops-plane-dbos` · **Engine** DBOS Transact `>=2.28,<3` · **Licence** MIT, ungated

> Read this beside `PHASE3_OPS_PLANE_HATCHET_ARCHITECTURE.md`. **Sections 3 and 5 use the
> same 15 capabilities, in the same order, in both documents** — the rows pair up one for one.

---

## 1 · Technologies

### The one that defines this track

**DBOS Transact** (`dbos`, `>=2.28,<3`, MIT) — durable execution as a **Python library**.
It checkpoints workflow and step results into Postgres, so a workflow interrupted by a process
death resumes from its last completed step rather than starting over. It supplies queues with
per-worker concurrency, `@workflow` / `@step` decorators, durable progress streams, and
recovery at process launch. **There is no server component**: the library is imported by the
processes that already exist, and its state is a schema (`dbos`) in the application's own
database. It provides durability only — scheduling, run vocabulary and operator tooling remain
ours.

### The rest of the ops-plane stack

| Technology                   | Role in this track                                                                                                                                                             |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **PostgreSQL 17 + PostGIS**  | One database, three tenants: domain data (`curated_geo`), our run state (`jobs_runtime`), DBOS checkpoints (`dbos`). Also the cancellation mechanism, via `pg_cancel_backend`. |
| **psycopg2**                 | The database driver used by unit runners. Its calls block in C, which is why stopping a running unit needs a backend-level signal rather than a Python-level cancel.           |
| **Postgres LISTEN/NOTIFY**   | The transport underneath DBOS's progress streams. Requires a direct connection — it cannot cross a transaction-mode pooler.                                                    |
| **FastAPI + Uvicorn**        | The HTTP surface: submit, read status, and the Server-Sent Events progress endpoint.                                                                                           |
| **Server-Sent Events (SSE)** | The one-way progress protocol to the web app. Its wire shape is ours (`frames.py`), not the engine's.                                                                          |
| **Alembic**                  | Schema migrations (`r0xx`), tracked separately from DBOS's own migration table.                                                                                                |
| **Python `graphlib`**        | Deterministic topological ordering of the indicator graph (`graph.py`). Standard library — no dependency.                                                                      |

### Adjacent, and identical on both tracks

**Hamilton** (`sf-hamilton`) + **Pandera** express and validate the indicator computation
*inside* a unit, and **exactextract** / **rasterio** / **GeoPandas** do the geospatial work.
These are the compute layer, not the ops-plane: an engine choice does not touch them.

---

## 2 · At a glance

| | Track A |
|---|---|
| Process kinds to operate | **2** — API, worker |
| Databases | **1** — checkpoints are a schema (`dbos`) beside the domain data |
| Containers beyond Postgres | **0** |
| Idle Postgres connections | worker 4 · DBOS pool 5 · **+1 per in-flight unit** |
| Ops-plane code we own | **1 504 lines** across 6 modules — 1 152 excluding blanks and comment-only lines |
| Run history | our tables · unbounded · `GROUP BY`-able |
| Operator UI | none — SQL |
| One process can submit *and* execute? | **yes** (`dev` mode) |
| Indicator catalog changes | runtime — the registry is read per submit |

---

## 3 · Who provides what — the capability map

The same 15 capabilities, in the same order, appear in the Hatchet document.
**Owner** is the question the whole comparison turns on.

| # | Capability | Owner | Component providing it |
|---:|---|:---:|---|
| 1 | Change intake → dirty set | shared | `plan.py` + `impact.py` + `graph.py` |
| 2 | Run submission & durability | engine | `dbos_orchestrator.submit_run` → `DBOSClient.enqueue` |
| 3 | Dependency scheduling | **ours** | `orchestration/wave.py` + the `run_workflow` loop |
| 4 | Unit execution | **ours** | `dbos_app.s_compute` inside `unit_workflow` |
| 5 | Failure isolation & propagation | **ours** | `plan_wave()` skip set |
| 6 | Retries | **ours — off by design** | `@DBOS.step(retries_allowed=False)` |
| 7 | Cancellation | shared | `cancel_workflow` + `pg_cancel_backend` + `runstore` |
| 8 | Run status vocabulary | **ours** | `orchestration_run` / `job_unit` tables |
| 9 | Status read | **ours** | `runstore.get_run`, engine as existence oracle |
| 10 | Run history & cross-run querying | **ours** | SQL on our tables |
| 11 | Live progress (SSE) | engine | `DBOS.write_stream` → `read_stream` |
| 12 | Crash recovery | engine — **scoped to one executor** | DBOS startup recovery, per `executor_id` |
| 13 | Admission control (dedup, serialisation) | — | **not part of this track** |
| 14 | Provenance | shared | `jobs_runtime.job_run` (r031) |
| 15 | Observability & operator UX | **ours** | runbook SQL + `ops_executor_health_v` |

**Score: 8 ours · 3 engine · 3 shared · 1 not provided.**

---

## 4 · Architecture

Two process kinds, one database. The API submits and reads; workers dequeue and execute;
both talk only to Postgres, which holds the domain data, our run state and DBOS's checkpoints.

| process | required | role |
|---|---|---|
| API (`apps/api`) | yes | submits, reads status, serves SSE. Does **not** execute PostGIS. |
| worker (`python -m apps.worker`) | yes in production | executes runs and units. Scale for throughput. |

`REIMAGINA_DBOS_EXECUTION_MODE` = `client` (API-safe default) · `executor` (workers) ·
`dev` (explicit one-process execution, local only).

### Component inventory

| File | lines | code | Owns |
|---|---:|---:|---|
| `orchestration/dbos_app.py` | 636 | 475 | queues, steps, workflows, executor identity, DB guards, progress |
| `orchestration/dbos_orchestrator.py` | 480 | 352 | the port — submit / read / cancel / subscribe |
| `orchestration/runstore.py` | 200 | 175 | run + unit state |
| `orchestration/factory.py` | 81 | 62 | modes `dbos`, `sync` |
| `orchestration/wave.py` | 75 | 62 | the wave scheduler |
| `orchestration/executor_health.py` | 32 | 26 | worker heartbeat |
| **owned total** | **1 504** | **1 152** | |
| `orchestration/graph.py` | 72 | 61 | capability 1 — ordering, not scheduling |
| `apps/worker/app.py` | 64 | 46 | launch, heartbeat, block |

`code` excludes blank and comment-only lines. **Both columns are reported because
the two rules disagree about which track owns more** — see §7, *Ownership delta*.

Diff vs the fork point (`3d0a2cc`), orchestration package:
**+1 290 / −773 across 15 files.**

---

## 5 · Capability detail

### 1 · Change intake → dirty set

| | |
|---|---|
| **Component** | `plan.py:plan_run()` · `impact.py:compute_dirty_set()` · `graph.py` |
| **Owner** | shared — same behaviour on both branches. `impact.py` is byte-identical; `plan.py` differs only in its module docstring; `graph.py` also emits declaration order on Track B. See the appendix. |
| **Mechanism** | Resolve the change-set (explicit, or derived from the scenario's override rows), then derive the invalidated indicators in dependency order. |
| **Design principle** | An engine replaces the *execution* of a run, never the decision of what the run contains. An empty dirty set is a `noop`: no run is persisted. |

### 2 · Run submission & durability

|                    |                                                                                                                                                                                                                                                                    |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Component**      | `dbos_orchestrator.submit_run` → `DBOSClient.enqueue(EnqueueOptions(...))`                                                                                                                                                                                         |
| **Owner**          | engine                                                                                                                                                                                                                                                             |
| **Mechanism**      | Verify the planner and the executor share one database → plan → **write the resolved feature set to `jobs_runtime.run_plan` and put its handle in the workflow input** → enqueue on the `runs` queue → return 202. The library persists the named workflow and its complete input atomically.                         |
| **Identity model** | **The run id _is_ the DBOS workflow id.** No mapping table: a run is addressable from any process, so the API can stream or cancel a run a worker is executing, and re-submitting the same id attaches to the existing workflow rather than starting a second one. |
| **Process model**  | In `client` mode the API neither imports nor launches an executor — it submits, reads, cancels and streams only.                                                                                                                                                   |

> **Why the unit list travels as a workflow input.** DBOS matches replayed steps by
> `(position, function name)`, and this design calls the same step once per indicator — so
> the workflow body must branch only on its own inputs and on checkpointed step results.
> Passing the dirty set in makes the run reproducible on replay regardless of what the
> catalog does meanwhile. Reading a global *inside* a step is fine: a step's output is
> checkpointed; it is the body's control flow that must be reproducible.

> **What travels, and what does not.** The input carries one entry per unit —
> `{slug, deps, features: []}` — plus `params.plan_id`. The **slugs and edges travel
> inline**, because they are what the body branches on. The **feature ids do not**: a
> full CDMX recompute is 86 225 blocks × 3 dirty indicators = 258 675 uuids, ~10 MB of
> workflow input per run. They are written once to `jobs_runtime.run_plan` (r036) under a
> handle the submitter mints, and each unit reads its own row. `features` is retained as an
> empty key so a run submitted with the old shape still resolves on resume.
>
> The handle is written **on its own connection and committed before the enqueue**
> (`dbos_orchestrator._persist_plan`): a worker can dequeue the instant the workflow is
> enqueued, so an uncommitted plan is indistinguishable from a lost one. Orphans — a plan
> written for a submit that then failed — are reclaimed by a TTL sweep on the next submit
> (`REIMAGINA_RUN_PLAN_TTL_HOURS`, default 24).
>
> **This is the one substantive change the two tracks made in the same direction.** Track B
> was forced into it by a hard gRPC limit (r050); this track adopted it because the input
> should not scale with the city either way.

### 3 · Dependency scheduling

| | |
|---|---|
| **Component** | `orchestration/wave.py` (75 LOC) + the `run_workflow` loop |
| **Owner** | **ours** |
| **Mechanism** | `plan_wave(units, registry)` → `(dispatch, skip, final)`. The workflow enqueues a wave as child workflows, awaits them in the same fixed order, settles each through a checkpointed step, and repeats until nothing is dispatchable. |
| **Structural property** | **A wave is a barrier** — nothing in wave *n+1* dispatches until every unit in wave *n* has settled. Ordering is fixed because child workflow ids are positional (`parent_id + function_id`). |
| **Measured** | `slr` 3.19 (`diamond`), 3.54 (`lattice`) · head-of-line **1.9–3.0 s** |

### 4 · Unit execution

| | |
|---|---|
| **Component** | `dbos_app.s_compute` (a step) inside `unit_workflow` |
| **Owner** | **ours** |
| **Mechanism** | Open a psycopg2 connection, confirm the run is still live, run the runner under `current_run(run_id)`, **commit once at the end**. |
| **Transaction model** | One transaction per unit, deliberately. The write path is stale-then-insert — flip the previous values to `stale`, then insert replacements — so both halves must land or neither does. A cancel mid-write rolls the flip back with it. |
| **Cost model** | A unit holds one domain-database connection for its entire multi-minute statement, so `REIMAGINA_UNIT_CONCURRENCY` is a connection budget as much as a CPU one. |

### 5 · Failure isolation & propagation

| | |
|---|---|
| **Component** | `plan_wave()` — the `skip` set |
| **Owner** | **ours** (the repo's F6 rule, hand-written) |
| **Mechanism** | A queued unit whose *dirty* input did not succeed becomes `skipped`, carrying the reason `"unsatisfied input"`. Independent branches continue; the run stays alive until they finish. |
| **Rule** | A **non-dirty** input never blocks — it kept its current value, so it is an input to this run rather than work in it. |

### 6 · Retries

| | |
|---|---|
| **Component** | `@DBOS.step(retries_allowed=False)` on `s_compute` |
| **Owner** | **ours — off by design** |
| **Rationale** | A runner exception is deterministic: a retry burns the same minutes to reach the same failure. Infrastructure faults surface as a failed *connect*, which is retried by the step that owns the connection. |
| **Consequence** | There is no transient-vs-deterministic classifier. Retry policy is a decision this track makes once, globally, rather than per exception. |

### 7 · Cancellation

| | |
|---|---|
| **Component** | `cancel_workflow(cancel_children=True)` + `job_run_repo().cancel_run_backends()` + `runstore` |
| **Owner** | shared — the mechanism is identical on both tracks |
| **Mechanism** | Three layers, each stopping something the others cannot: the engine stops further scheduling · **`pg_cancel_backend`** (r031) interrupts the running statement · our tables close the run in our own vocabulary. |
| **Scope** | `cancel_children` is required because units are child workflows; without it the next wave could start after the run closed. |
| **Shared limit** | **No engine can interrupt a thread blocked in a psycopg C call.** The statement-level answer lives outside both ops-planes, in `job_run.backend_pid`. |

### 8 · Run status vocabulary

| | |
|---|---|
| **Component** | `runstore.py` → `jobs_runtime.orchestration_run` + `job_unit` |
| **Owner** | **ours** |
| **Vocabulary** | unit: `succeeded` / `failed` / **`skipped`** / `cancelled` — run: `succeeded` / **`partial`** / `failed` / `cancelled` |
| **Mechanism** | Each status is **written at the moment it is meant**, with a reason, by the component that knows it. Nothing is inferred afterwards. |
| **Invariant** | A terminal row never moves again. Writers return whether their write landed, and callers publish only what the database accepted — so the SSE stream and `GET /jobs/{id}` cannot disagree. |

### 9 · Status read

| | |
|---|---|
| **Component** | `dbos_orchestrator.get_run` |
| **Owner** | **ours**, with the engine as existence oracle |
| **Mechanism** | Detail comes from our tables; **existence** comes from the engine. The run envelope is written by the workflow's own first step, and for the window before that the run is reconstructed from the workflow's persisted input — which carries the unit *slugs*. Only the feature scope lives behind the `plan_id` handle, and `get_run` does not report it. |
| **Why the envelope is not written at submit** | Writing it in the submit transaction would create a run that exists before anything is committed to drive it. Making the engine the existence oracle keeps durability and observability aligned. |
| **Property** | **Correct on arrival.** A submitted run is immediately retrievable and already knows its units. |

### 10 · Run history & cross-run querying

| | |
|---|---|
| **Component** | plain SQL on `orchestration_run` / `job_unit` |
| **Owner** | **ours** |
| **Mechanism** | Ordinary relational tables in the same database as the domain data — joinable, indexable, aggregable, and available to `explore` views. |
| **Consequence** | *"How many scenarios partially failed this week"* is one `GROUP BY`. |
| **Retention** | Unbounded — a function of disk, not of an engine's policy. |

### 11 · Live progress (SSE)

| | |
|---|---|
| **Component** | `_emit` → `DBOS.write_stream` · `subscribe_progress` → `DBOSClient.read_stream` · `frames.py` |
| **Owner** | engine (transport) · ours (wire shape) |
| **Mechanism** | Progress is written **from the workflow body**, so each event is exactly-once and carries indicator identity, ordering and a resumable offset. Reading goes through `DBOSClient`, so the API can follow a run a worker is executing without hosting the workflow definitions. |
| **Concurrency model** | `read_stream` is a blocking synchronous generator, so each subscription runs on its own daemon thread behind a bounded slot count (32) and a bounded handoff queue (128). A late subscriber gets a snapshot first, then live events. |
| **Wire contract** | The orchestrator yields `RunEvent` data; `frames.py` — not the engine — decides the SSE shape. `indicator=None` marks the run-level event that closes the stream. |

### 12 · Crash recovery

| | |
|---|---|
| **Component** | DBOS startup recovery, scoped by `executor_id` · `executor_health.heartbeat()` |
| **Owner** | engine, **within one executor** |
| **Mechanism** | Launching a process reclaims the workflows recorded against its own `executor_id` and resumes each from its last completed step. |
| **Granularity** | **Redundant compute on a crash is exactly one unit** — the one that was in flight. Completed units are not recomputed, and results are replace-in-place, so no duplicates appear. |
| **Model boundary** | Recovery is **per-executor by design**: a process reclaims its own work at a moment when, by definition, it is not running it. Reclaiming *another* executor's work is a different operation and is not part of this track's model. |
| **Operator contract** | `executor_id` must be stable across restarts and unique between live processes. The default `<hostname>:<role>` satisfies both for one process per role per host; beyond that it is supplied per replica. |
| **Deploy model** | `application_version` gates recovery, so a blue-green deploy drains in-flight runs on old-version processes rather than resuming them onto new code. |

### 13 · Admission control (dedup, serialisation)

| | |
|---|---|
| **Component** | — |
| **Owner** | **not part of this track** |
| **Model** | Submissions are independent: each `submit_run` mints a fresh run id, and two runs over the same scope are neither collapsed nor serialised. |
| **Where the equivalent would live** | Application code — this track has no declarative layer to express it in. Track B gets both from engine configuration. |

### 14 · Provenance

| | |
|---|---|
| **Component** | `jobs_runtime.job_run` + `backend_pid` (migration r031) |
| **Owner** | shared — survives on both branches |
| **Mechanism** | A runner owns its own `job_run` row: provenance lives where the compute happens, not in the scheduler. `curated_geo.indicator_result.job_run_id` is NOT NULL with an existence trigger, so every served value traces to a run. |
| **Why it is engine-independent** | It is domain provenance rather than scheduler state, which is why both tracks keep it and why the measurement spec defines runner time from it. |

### 15 · Observability & operator UX

| | |
|---|---|
| **Component** | the runbook's SQL · `explore.ops_executor_health_v` · process logs |
| **Owner** | **ours** |
| **Mechanism** | Two planes read together in one query: `orchestration_run.status` says what a run *means*; `dbos.workflow_status` says whether anything is still *driving* it. Executor liveness comes from a Postgres heartbeat rather than a control-plane service. |
| **Surface** | run and unit status · executor liveness and age · per-run timing · orphan detection. |
| **Not present** | dashboard · DAG visualisation · per-task log viewer · replay control · metrics endpoint · OpenTelemetry. |

---

## 6 · Deployment & configuration surface

| variable | default | what it controls |
|---|---|---|
| `DATABASE_URL_DIRECT` | — | the direct DSN for checkpoints and LISTEN/NOTIFY. Required |
| `REIMAGINA_DBOS_EXECUTION_MODE` | `client` | whether this process may execute workflows |
| `REIMAGINA_EXECUTOR_ID` | `<hostname>:<role>` | recovery scope — see capability 12 |
| `REIMAGINA_EXECUTOR_ROLE` | `app`, `worker` in the worker | the `<role>` half of the default executor id |
| `REIMAGINA_EXECUTOR_HEARTBEAT_S` | 30 | heartbeat period behind `explore.ops_executor_health_v` |
| `REIMAGINA_RUN_CONCURRENCY` | 2 | parent workflows per executor |
| `REIMAGINA_UNIT_CONCURRENCY` | 2 | units per process **= domain DB connections** |
| `REIMAGINA_PROGRESS_SUBSCRIBERS` | 32 | concurrent SSE subscriptions per process |
| `REIMAGINA_RUN_PLAN_TTL_HOURS` | 24 | age at which an orphaned `run_plan` row is swept |
| `REIMAGINA_APP_VERSION` | derived | version gate for blue-green recovery |

**Startup contract.** Three properties are asserted before a process serves traffic: a direct
DSN is configured, it points at a migrated reimagina database, and the planner's database is
the executor's database. All three are checked server-side rather than by comparing DSN text,
because pooled and least-privilege roles are normal.

**Database footprint.** 1 database · `dbos` schema, 11 tables · `pg_dump -n curated_geo -n
jobs_runtime` stays self-contained with no reference to the `dbos` schema · DBOS tracks its
own migrations separately from alembic. Budget connections as
`processes × (4 + REIMAGINA_UNIT_CONCURRENCY)`. `jobs_runtime.run_plan` holds one row per
dirty unit — three per in-flight run — as a `uuid[]` rather than a row per feature, and is
empty in the steady state.

**API surface.** `POST /jobs` honours `wait: true` → **200 with the finished status**, because
one process can both submit and drain. Track B rejects it with a **422** — the clearest
one-line expression of library-vs-service in the HTTP surface.

**Migrations.** `r032` (drop the `procrastinate` schema), `r034` (executor heartbeat +
`explore.ops_executor_health_v`), `r035` (drop `job_run.procrastinate_job_id`), `r036`
(`jobs_runtime.run_plan`). `orchestration_run` and `job_unit` (r028) are kept.

⚠ **The revision ranges are split across the fork, and that rule was bought the hard way.**
Alembic identifies a revision by its id alone, so a number minted on one branch and reused on
the other yields two migrations answering to one name — each branch resolving the other's id
to its own file. That happened with `r033`: a Hatchet migration applied to a database on
*this* lineage and dropped `orchestration_run` and `job_unit` while `alembic current` still
reported "head". **This track keeps r03x/r04x; the Hatchet track takes r050+.** Never
continue the other branch's numbering, and use a separate database per track.

---

## 7 · Comparative measurement

`slr` = makespan ÷ critical path. 1.00 means the scheduler achieved the DAG's lower bound.

| workload | slr **A** | slr B | head-of-line **A** | head-of-line B |
|---|---:|---:|---:|---:|
| `diamond` | 3.19 | 1.13 | 1868 ms | 78 ms |
| `lattice` | 3.54 | 1.21 | 2480 ms | 103 ms |
| `barrier_probe` | 6.80 | 1.44 | 2811 ms | 96 ms |
| `layered_random` | 4.45 | 1.22 | 3023 ms | 1372 ms |
| `chain_deep` | 13.72 | 2.87 | 1956 ms | 80 ms |

**The difference is structural, not incidental.** A wave is a barrier, so per-hop latency is
paid once per wave; Track B evaluates readiness per task.

⚠ **Not every row carries the same weight**, per `tests/workloads/README.md`. Run-to-run
spread on two consecutive full runs of one track:

| stable to ±2 % | noisy |
|---|---|
| `lattice` 0.2 % · `diamond` 0.4 % · `barrier_probe` 1.8 % | `layered_random` 13 % · **`chain_deep` 35 %** |

The split is not random: the stable shapes have real work to do, and the noisy ones are
dominated by per-dispatch latency being measured repeatedly with no parallelism to absorb the
jitter. **So `diamond`, `lattice` and `barrier_probe` are the comparison; `chain_deep` and
`layered_random` are order-of-magnitude only** and should not be read to three significant
figures. Raise `REIMAGINA_BENCH_REPS` before quoting them.

⚠ **Run conditions are not recorded for these figures** — machine, date, `REIMAGINA_BENCH_REPS`,
`REIMAGINA_BENCH_SCALE`, and the engine/library versions. `PHASE3_OPS_PLANE_MEASUREMENT.md`
requires them: "A number without those is not comparable to the other branch's." Fill them in
or re-run before this table is used to decide anything.

> **Read in proportion.** The suite's units are `pg_sleep` and `sha256` loops of 10 ms–3 s,
> chosen to measure the scheduler rather than the geometry. **Real indicator units run for
> minutes**, where a 2 s wave barrier is under 1 % of wall clock; against a 100 ms unit it is
> 20×. The `sync` control column agrees to within 0.5 % across both worktrees — that is
> evidence the *harness* is sound, and says nothing about the reproducibility of any
> individual workload above.

### Ownership delta — state the counting rule, because it decides the sign

This is a spec'd metric and the headline Track B exists to move. The two obvious rules give
**opposite answers**, so neither number means anything unlabelled:

| rule | Track A | Track B | who owns more |
|---|---:|---:|---|
| raw lines of the owned ops-plane modules | 1 504 (6 modules) | 1 646 (4 modules) | **B** |
| excluding blanks and comment-only lines | 1 152 | 1 041 | **A** |
| insertions vs the fork point, `orchestration/` | +1 290 / −773 (15 files) | +1 793 / −1 029 (19 files) | **B** |

Track B deletes `wave.py` (75) and `runstore.py` (196) and still ends up with more lines than
Track A, because a service adapter costs more than the scheduler it replaces:
`hatchet_orchestrator.py` alone is 852 lines to `dbos_orchestrator.py`'s 480. The honest
statement is **"B deletes our scheduler and its run tables, and does not reduce the code we
own"** — the deletion is a shift in *what* we maintain, not in *how much*. Both tracks are
heavily commented by house style, which is why the two rules diverge as far as they do.

### Metrics this document does not report

`slr` and `head_of_line_ms` are the workload suite's own metrics, not the six in
`PHASE3_OPS_PLANE_MEASUREMENT.md`. Of those six, **per-unit overhead, redundant compute and
recovery latency are not reported here on either track.** Idle footprint appears as §2 and §6;
ownership delta is above. Either the spec is amended or those three still have to be measured
before `PHASE3_OPS_PLANE_TRACKS.md` can be written.

⚠ **The two tracks do not have equal evidence.** Track B ships
`scripts/verify_real_data.py` and `tests/workloads/REAL_DATA_RESULT.md`: the shipped catalog
over the real city at full scope — 86 225 blocks × 3 indicators — with four independent
oracles (a neutral scenario reproduces the committed baseline bit for bit; an override dirties
exactly one indicator and only the edited features; one `current` row per key with every result
citing a real `job_run`; two runs of the same input agree value for value), all passing on
both `sync` and `hatchet`. **This track has `scripts/bench_real_data.py` but no verifier and
no equivalent result.** The synthetic suite asserts that a scheduler dispatches a DAG
correctly; it never checks that the numbers are right, because synthetic units return
synthetic values. Until the verifier runs here, the comparison rests on timing alone on this
side.

---

## 8 · Pros

- **Nothing to deploy.** No engine, no second database, no broker, no credential, no container.
- **One database, one backup, one migration story.** Checkpoints are a schema, not a system.
- **Run history is ours and unbounded** — joinable, indexable, `GROUP BY`-able, and readable
  in the same query as the domain data.
- **`partial` and `skipped` are first-class**, written when meant and with a reason, so no
  consumer has to reconstruct them.
- **Crash granularity is one unit.** Completed work is never recomputed.
- **The submitting process can also drive.** A worker fleet is a throughput choice, not a
  precondition; `dev` mode gives one-process execution for the CLI and tests.
- **No engine to be available.** Diagnosis is SQL against tables we wrote, and the `--sync`
  path still runs when the ops-plane does not.
- **Adding an indicator is a catalog row** — the registry is read at runtime, no redeploy.
- **Semantics live in code that can be read and stepped through.** `wave.py` is 75 lines.

## 9 · Cons

- **We own the scheduler.** Waves, skip propagation and status rollup are ours to maintain,
  and every future scheduling feature is ours to write.
- **Dispatch latency is materially higher** — 1.9–3.0 s head-of-line versus ~80–100 ms —
  and the barrier means it compounds with DAG depth.
- **Recovery stops at one executor.** A process's in-flight runs wait for a process with that
  same identity; there is no fleet-level reclaim in the model.
- **Recovery correctness depends on operator-supplied identity.** `executor_id` must be both
  stable and unique, and nothing in the environment can derive that for every topology.
- **No operator UI** — no dashboard, DAG view, log viewer or replay control.
- **No metrics and no tracing** — there is no endpoint to enable.
- **No admission control.** Deduplicating a repeated submit or serialising two runs over one
  scenario would be application code we have not written.
- **Deploys are blue-green by construction**, since version pinning gates recovery.
- **Scaling is a connection budget.** Every concurrent unit is a domain-database connection,
  bounded by the same `max_connections` the API shares.

---

## 10 · Summary line

Track A buys durability and nothing else. Zero new infrastructure, and full ownership of run
vocabulary, history and scheduling semantics — all queryable in SQL beside the domain data.
It pays with a scheduler we maintain, ~2 s per-wave dispatch latency, no operator surface, and
recovery scoped to a single executor.
**The fit is long units and a small fleet.**

---

## Appendix · Cross-references

Branch-local detail: `docs/architecture/OPS_PLANE_RUNBOOK.md` (operations) and
`docs/product/PHASE3_OPS_PLANE_DBOS_SPIKES.md` (engine behaviour established by experiment).
Measurement definitions are shared: `PHASE3_OPS_PLANE_MEASUREMENT.md`.

### Shared foundation — what is actually shared

`factory.py` is the single place either engine is named, on both branches. Below that, the
foundation is shared **in role**, and the two branches have edited most of it. Measured file
by file between the worktrees:

| file | state | what diverged |
|---|---|---|
| `impact.py` | **byte-identical** | — |
| `perf/workloads.py` | **byte-identical** | the fixed half of the benchmark, by design |
| `plan.py` | 8 lines | module docstring only; behaviour identical |
| `coordinator.py` | 17 lines | Track B's `sync` path no longer touches `runstore` |
| `runners.py` | 17 lines | Track B binds runners for the engine's task bodies |
| `graph.py` | 36 lines | Track B also emits parents-first declaration order |
| `frames.py` | 30 lines | Track B adds `error_frame`; the DBOS doc previously called this identical and the Hatchet doc called it "one function" — both were wrong |
| **`port.py`** | **47 lines** | **the contract itself moved — see below** |
| `tests/conformance/`, `tests/workloads/` | differ | `IMPLEMENTATIONS` is the branch-specific half by design; the READMEs and fixtures have also diverged |

⚠ **The port is no longer identical, and `JOB_LAYER.md` still calls it "the part that is
not [replaced]".** Track B made two changes this branch did not adopt:

- **`get_run(self, conn, run_id)` → `get_run(self, run_id)`.** The connection was removed as
  dead weight: no implementation ever read it, and `GET /jobs/{id}` and the SSE route each
  held a pooled connection for a request that never touched the database — the SSE one for the
  lifetime of the stream. This branch still takes `conn`, and genuinely uses it:
  `runstore.get_run` is the detail source.
- **`UNIT_UNSATISFIED_STATES` deleted.** It is the readiness rule's input, so it survives here
  with `wave.py` and has nothing left to describe on a branch whose engine owns readiness.

Both changes are defensible on Track B's own terms. Neither is reconcilable by picking one
side: the parameter is load-bearing here and dead there. **This is a finding of the fork, not
a defect to fix** — but it must be recorded, because "the port never changes" was a Phase 0
design claim and it did not hold.

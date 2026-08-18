# Track B — Hatchet ops-plane

> **Durable execution as a _service_.** The engine owns scheduling, run state, retries,
> admission control and the operator surface. We declare the graph once and **delete our
> scheduler**.

**Branch** `feat/ops-plane-hatchet` · **Engine** `hatchet-sdk >=1.35,<2`, self-hosted
`hatchet-lite v0.86.24` · **Licence** MIT, ungated, no licence key, no quota

> Read this beside `PHASE3_OPS_PLANE_DBOS_ARCHITECTURE.md`. **Sections 3 and 5 use the same
> 15 capabilities, in the same order, in both documents** — the rows pair up one for one.

---

## 1 · Technologies

### The one that defines this track

**Hatchet** — durable execution as a **self-hosted service**, MIT, with no licence key and no
quota. It arrives in two pieces:

| piece | what it is |
|---|---|
| **Hatchet engine** (`hatchet-lite`, pinned by digest `sha256:b1ab44a0…c97f216`, ≈ v0.86.24) | The server: scheduler, run state, retries, timeouts, admission control, REST API and dashboard, in one container. **Pinned by digest, not by tag** — the doc's own rule is that scheduling semantics and migrations move with the image, and a tag does not hold that still. The production shape splits it into engine / API / dashboard / migration services — same software, same configuration. |
| **`hatchet-sdk`** (`>=1.35,<2`) | The Python client: declare a workflow and its tasks, run a worker that executes them, and query or cancel runs. |

We declare the indicator graph and the engine does readiness, ordering, fan-out, failure
propagation, retries and admission control. That is why `wave.py` and `runstore.py` are deleted
on this branch — and why an engine must be running for anything to happen at all.

### The rest of the ops-plane stack

| Technology | Role in this track |
|---|---|
| **PostgreSQL 17 + PostGIS** | Two roles: the domain database (`curated_geo`, `jobs_runtime`), and the engine's own `hatchet` database, which also serves as its **message queue** — so there is no separate broker. Also the cancellation mechanism, via `pg_cancel_backend`. |
| **gRPC** | The worker↔engine transport (`:7077`): workers register the graph and receive tasks; the API submits runs and subscribes to run events. |
| **CEL (Common Expression Language)** | The expression language for `skip_if` conditions, evaluated by the engine against a parent task's output — this is how a non-dirty indicator skips itself. |
| **Pydantic** | `RunInput` is the workflow's validated input contract, enforced by the engine at submit. |
| **Docker Compose** | How the engine is deployed; the compose file is part of this branch. |
| **psycopg2** | The database driver used by unit runners. Its calls block in C, which is why stopping a running unit needs a backend-level signal rather than a Python-level cancel. |
| **FastAPI + Uvicorn** | The HTTP surface: submit, read status, and the Server-Sent Events progress endpoint. |
| **Server-Sent Events (SSE)** | The one-way progress protocol to the web app. Its wire shape is ours (`frames.py`), not the engine's. |
| **OpenTelemetry** | Trace context (`traceparent`) injected into every run's metadata by the engine, at no cost to us. |
| **Prometheus** | Engine metrics, available behind a configuration flag. |
| **Alembic** | Schema migrations (`r0xx`) for the domain database; the engine migrates its own. |
| **Python `graphlib`** | Deterministic topological ordering of the indicator graph (`graph.py`), used to declare tasks parents-first. |

### Adjacent, and identical on both tracks

**Hamilton** (`sf-hamilton`) + **Pandera** express and validate the indicator computation
*inside* a unit, and **exactextract** / **rasterio** / **GeoPandas** do the geospatial work.
These are the compute layer, not the ops-plane: an engine choice does not touch them.

---

## 2 · At a glance

| | Track B |
|---|---|
| Process kinds to operate | **3** — engine, API, worker |
| Databases | **2** — domain + the engine's own `hatchet` |
| Containers beyond Postgres | **1** (`hatchet-lite`; Postgres broker, no RabbitMQ) |
| Idle Postgres connections | **~47** engine · +1 per in-flight unit |
| Ops-plane code we own | **602 executable lines** across 4 modules (1 646 raw) — `wave.py` and `runstore.py` **deleted**; **36 % less code than Track A**, see §7 |
| Run history | the engine's · 720 h retention default · one run at a time |
| Operator UI | dashboard, DAG view, per-task logs, replay |
| One process can submit *and* execute? | **no** — a worker is a precondition |
| Indicator catalog changes | import time — the DAG is declared once, so a change is a redeploy |

---

## 3 · Who provides what — the capability map

The same 15 capabilities, in the same order, appear in the DBOS document.
**Owner** is the question the whole comparison turns on.

| # | Capability | Owner | Component providing it |
|---:|---|:---:|---|
| 1 | Change intake → dirty set | shared | `plan.py` + `impact.py` + `graph.py` |
| 2 | Run submission & durability | engine | `hatchet_orchestrator.submit_run` → `workflow.run()` |
| 3 | Dependency scheduling | **engine** | `hatchet_app.build_dag` — declaration only |
| 4 | Unit execution | **ours** | the task body in `build_dag` |
| 5 | Failure isolation & propagation | **engine** | task parentage — inherited, not written |
| 6 | Retries | **engine** | `TaskDefaults(retries=…)` + a retryability classifier |
| 7 | Cancellation | shared | `runs.cancel` + `pg_cancel_backend` |
| 8 | Run status vocabulary | **ours, in the engine's run output** | `_verdict` in the finalizer tasks |
| 9 | Status read | **engine** | `runs.get()` → verdict, else status projection |
| 10 | Run history & cross-run querying | **engine** | `runs.list(additional_metadata=…)` |
| 11 | Live progress (SSE) | engine + ours | gRPC listener as *wakeup*, `get_run` as *answer* |
| 12 | Crash recovery | **engine** | slot released, work reassigned — no code of ours |
| 13 | Admission control (dedup, serialisation) | **engine** | `TTLBasedIdempotencyConfig` + `ConcurrencyExpression` |
| 14 | Provenance | shared | `jobs_runtime.job_run` (r031) |
| 15 | Observability & operator UX | **engine** | dashboard · per-task logs · replay · OTel · Prometheus |

**Score: 9 engine (2, 3, 5, 6, 9, 10, 12, 13, 15) · 3 shared (1, 7, 14) · 1 ours (4) ·
1 hybrid (11) · 1 split (8).**
Against Track A's **8 ours · 3 engine · 3 shared · 1 not provided** — the three shared rows
pair exactly, and every other row is the comparison.

---

## 4 · Architecture

Three process kinds, two databases. The API submits over gRPC and reads over REST; workers
register the graph with the engine and execute the tasks it assigns them; the engine keeps its
own state and message queue in a separate `hatchet` database, while workers and the API reach
the domain database directly. A browser reaches the engine's dashboard on the same port as its
REST API.

| process | required | role |
|---|---|---|
| engine (`hatchet-lite`) | **yes** | schedules everything; nothing runs without it |
| worker (`python -m apps.worker`) | **yes** | there is no in-process mode |
| API (`apps/api`) | for HTTP | submits and reads; does not execute units |

```bash
docker compose -f infra/docker/docker-compose.hatchet.yml up -d
```

Postgres is **not** in that compose file — the engine uses the host instance in its own
`hatchet` database. Deliberate: it keeps the comparison honest by not charging this track for
a database the other track also needs. The broker is Postgres (`SERVER_MSGQUEUE_KIND`), so
there is no message broker to operate.

> **A worker is a precondition, not an optimisation.** Submitting returns a workflow-run id,
> but the run is only queryable once a worker has registered the workflow definition with the
> engine. Track A has no equivalent state: there, the submitting process can drive and answer
> for its own run.

### Component inventory

| File | lines | code | Owns |
|---|---:|---:|---|
| `orchestration/hatchet_orchestrator.py` | 852 | 341 | the port — submit / read / cancel / subscribe, + `cancel_scenario` |
| `orchestration/hatchet_app.py` | 629 | 194 | the DAG declaration, task defaults, admission control |
| `orchestration/database_identity.py` | 92 | 40 | planner-and-executor database identity |
| `orchestration/factory.py` | 73 | 27 | modes `hatchet`, `sync` |
| **owned total** | **1 646** | **602** | |
| `orchestration/graph.py` | 108 | 59 | capability 1 — ordering and declaration order |
| `apps/worker/app.py` | 106 | 50 | preflight, register the DAG, execute |
| `infra/docker/docker-compose.hatchet.yml` | 129 | 36 | the engine and its configuration |

`code` excludes blank lines, comments **and docstrings**, counted from the AST — house style
puts a great deal of reasoning in docstrings, and this branch more than the other, so a
`wc -l` here is closer to a prose count than a code one. **The `code` column is the one to
compare; the raw column reverses the sign.** See §7, *Ownership delta*.

**Deleted:** `wave.py` (75) · `runstore.py` (196) · `tasks.py` (192) · `queue_app.py` (82) ·
`events.py` (42) · `embedded.py` (167). The `on_success_task` / `on_failure_task` finalizer
pair was also deleted, later than the rest — see capability 8.

Diff vs the fork point (`3d0a2cc`), orchestration package:
**+1 793 / −1 029 across 19 files.**

---

## 5 · Capability detail

### 1 · Change intake → dirty set

| | |
|---|---|
| **Component** | `plan.py:plan_run()` · `impact.py:compute_dirty_set()` · `graph.py` |
| **Owner** | shared — same behaviour on both branches. `impact.py` is byte-identical; `plan.py` differs only in its module docstring; `graph.py` here also emits parents-first declaration order. See the appendix. |
| **Mechanism** | Resolve the change-set (explicit, or derived from the scenario's override rows), then derive the invalidated indicators in dependency order. |
| **Design principle** | The dirty set is derived by the **submitter**, not by the `plan` task — a graph that re-derived its own work could disagree with the one that was scheduled. Same reason the library track passes it as a workflow input. |

### 2 · Run submission & durability

| | |
|---|---|
| **Component** | `hatchet_orchestrator.submit_run` → `get_run_workflow().run(payload, wait_for_result=False)` |
| **Owner** | engine |
| **Mechanism** | Verify planner and executor share one database → plan → write the resolved feature set to `jobs_runtime.run_plan` → build `RunInput` (dirty slugs, **`plan_id` handle**, ids, concurrency key, dedup key) → submit with `additional_metadata` → return 202. |
| **Why a handle** | **The feature ids cannot travel inline.** A full CDMX recompute is 86 225 blocks × 3 dirty indicators = 258 675 uuids — measured at 10 088 804 bytes against gRPC's 4 194 304 default, so the engine refuses with `RESOURCE_EXHAUSTED` before it sees the work. The ceiling was ~35 800 blocks, 42 % of the city. r050 makes the payload a couple of hundred bytes whatever the size of the city. Track A adopted the same shape (r036) for its own reasons. |
| **Identity model** | **The run id _is_ Hatchet's workflow run id.** There is no `orchestration_run` table to hold a mapping, and adding one would re-adopt the state model this track exists to give away. The engine mints the id, so unit bodies read it from their task context rather than from the payload. |
| **Metadata** | `scenario_id`, `aoi_id` and `code_version` travel as `additional_metadata`: readable inside the workflow, and a filter in both the API and the dashboard. |

### 3 · Dependency scheduling

| | |
|---|---|
| **Component** | `hatchet_app.build_dag()` — one task per indicator, parented by its prerequisites |
| **Owner** | **engine** |
| **Mechanism** | We *declare*; the engine does readiness, ordering, fan-out and dispatch. A `plan` task publishes the dirty slugs, and each indicator task carries a `skip_if` CEL condition evaluated against that output, so a non-dirty indicator skips itself. |
| **Declaration properties** | The graph is built at import, from a topologically ordered registry, with deterministic per-condition keys so the API and the worker describe the same workflow. `plan` returns slugs only — every child receives every parent's output, and that is the payload that scales. |
| **Extensibility** | `build_dag(name=…)` lets a caller declare a second, differently-shaped graph without touching the production one; the workload suite uses that. |
| **Measured** | `slr` **1.13** (`diamond`), **1.21** (`lattice`) · head-of-line **~80–100 ms** |

### 4 · Unit execution

| | |
|---|---|
| **Component** | the task body inside `build_dag` |
| **Owner** | **ours** |
| **Mechanism** | Open a psycopg2 connection on `DATABASE_URL_DIRECT` — the engine's database is not the domain database — run the runner under `current_run(ctx.workflow_run_id)`, commit, always close. |
| **Transaction model** | One transaction per attempt. Failure rolls it back and re-records the failed `job_run` on its own connection, once per attempt, since each retry is a separate execution. |
| **Cost model** | A unit holds one domain-database connection for its whole statement, so worker `slots` is a connection budget as much as a CPU one. |
| **Bonus from the engine** | `ctx.log(...)` per unit — per-task, searchable, retained with the run. |

### 5 · Failure isolation & propagation

| | |
|---|---|
| **Component** | task parentage — nothing of ours |
| **Owner** | **engine** |
| **Mechanism** | A failed task's dependents are cancelled; independent branches continue and the run stays alive until they finish. |
| **Why it matters** | **This is exactly the repo's F6 rule, inherited rather than implemented.** It is the strongest single argument for the service track: the behaviour Track A hand-writes in `wave.py` arrives with the engine. |
| **Coupling to the engine** | Skip inheritance interacts with `skip_if` declaration, so the behaviour is tied to the engine version — the image is pinned and the rule is covered by a test. |

### 6 · Retries

| | |
|---|---|
| **Component** | `TaskDefaults(retries, backoff_factor, backoff_max_seconds)` + a retryability classifier + `NonRetryableException` |
| **Owner** | **engine** |
| **Mechanism** | The engine retries with exponential backoff; our classifier decides which exceptions deserve it. Connection and serialization failures retry; deterministic failures and cancelled statements opt out via `NonRetryableException`. |
| **Contrast** | Track A turns retries off globally, because there the decision is binary and ours. Here it is per-exception, and the engine supplies the machinery. |
| **Timeout model** | Two independent deadlines per task: `execution_timeout` (how long a unit may run — 120 min) and `schedule_timeout` (how long it may wait for a slot — 6 h). Both are set explicitly, because the engine's defaults target short tasks rather than multi-minute PostGIS. Sizing rule: `schedule_timeout > slowest_unit × queued_depth ÷ slots`. |

### 7 · Cancellation

| | |
|---|---|
| **Component** | `runs.cancel(run_id)` + `job_run_repo().cancel_run_backends(run_id)` |
| **Owner** | shared — the mechanism is identical on both tracks |
| **Mechanism** | The engine stops scheduling and marks the run cancelled; **`pg_cancel_backend`** against `job_run.backend_pid` (r031) interrupts the statement that is already running. |
| **Consistency model** | The engine's cancel is an accepted *request* — the run reaches terminal state through the engine's own projection rather than at the moment the call returns. |
| **Engine-native extra** | `cancel_scenario()` cancels every in-flight run for a scenario **by metadata rather than by id** — a list-and-bulk-cancel filtered on `additional_metadata`. Deliberately outside the port, since Track A has no equivalent to offer. |
| **Shared limit** | **No engine can interrupt a thread blocked in a psycopg C call.** The statement-level answer lives outside both ops-planes. |

### 8 · Run status vocabulary

| | |
|---|---|
| **Component** | `hatchet_orchestrator.get_run` — rolled up from `runs.get(run_id)` through the shared `overall_status`. **No finalizers, and nothing stored.** |
| **Owner** | **ours — but computed on read, not written anywhere** |
| **The engine's enum** | `QUEUED / RUNNING / COMPLETED / CANCELLED / FAILED` — no `partial`, no `skipped` |
| **Mechanism** | `partial` needs no storage: it is a roll-up of unit outcomes, and the engine already holds every outcome. One construction of the unit list serves both an in-flight and a terminal run. |
| **Derivation** | Each unit is classified from engine-held signals: an error → `failed`; a `SKIPPED` **task event** → `skipped`; the engine's `CANCELLED` → `cancelled`; otherwise `succeeded`. Then `skipped` and every non-terminal status are **excluded from the roll-up**, and the engine's own run status is the default the units must better. |
| **Why those two exclusions** | A skip means different things across the tracks: on `sync` it is an *unsatisfied input*, a real failure to compute, while here the engine reports an unsatisfied dependent as `CANCELLED`, so a skip can only mean "not dirty". Feeding those in made every ordinary run report `partial`. Excluding non-terminal statuses matters because `overall_status` is a terminal roll-up with no case for `queued` or `running` — leaving them in reported a closed run with an undispatched unit as `failed`. |
| **The comparison row** | Not "the engine is lossy" — it is **where the vocabulary lives**. Track A keeps it in a table it can join, aggregate and index. Track B derives it per read, one run at a time. The run's *status* remains `FAILED` on a partial run, so status and verdict answer different questions. |

> **The finalizers are gone, and their removal is the cleaner result.** An
> `on_success_task` / `on_failure_task` pair used to publish the skipped-unit list, because a
> unit skipped for being non-dirty reports `COMPLETED` — indistinguishable from one that
> computed — and `ctx.was_skipped` is readable only inside a finalizer.
>
> That was built on a misreading. The engine emits a `SKIPPED` task event all along, and
> `V1TaskEvent.task_id` carries the `task_external_id` despite its name and despite sitting
> next to `V1TaskSummary.task_id`'s `StrictInt`. The two join deterministically:
> `runs.get()` was already returning the answer, and the finalizers were throwing it away to
> reconstruct it from a hook.
>
> Deleting them buys three things: two fewer tasks per run in the dashboard and in `shape`, no
> scaffolding for the read path to recognise and filter, and **skips visible while the run is
> in flight** rather than only at terminal state. It also removed a second construction of the
> unit list — the verdict's and the engine's — which is how the two had come to disagree about
> what a unit was.

### 9 · Status read

| | |
|---|---|
| **Component** | `hatchet_orchestrator.get_run` → `runs.get(run_id)` |
| **Owner** | **engine** |
| **Mechanism** | One projection of the engine's task statuses onto the port's vocabulary, in flight and at terminal alike. Task names are normalised back to indicator slugs, and the `plan` scaffolding task is filtered out of the unit list — but **its failure is carried out rather than swallowed**: `plan` failing cancels every indicator, so the units would show only cancellations and the run would read `cancelled` for something nobody cancelled. |
| **No connection taken** | **`get_run(self, run_id)` — the `conn` parameter was deleted on this branch, and that is a change to the port itself.** It used to be kept "so the port stays implementable by a track that keeps run tables", and no implementation ever read it: this one ignored it outright and the synchronous one passed it to an in-memory store that ignored it too. The cost was not cosmetic — `GET /jobs/{id}` and the SSE route each held a pooled database connection for a request that never touched the database, the SSE one for the lifetime of the stream. `cancel_run` keeps its connection, because `pg_cancel_backend` genuinely needs one. Track A did **not** adopt this: see the appendix. |
| **Consistency model** | **Eventually consistent through the engine's projection.** A run is retrievable before it reports its units, because the engine creates task rows after accepting the run. |

### 10 · Run history & cross-run querying

| | |
|---|---|
| **Component** | `runs.list(additional_metadata=…)` + the dashboard's search |
| **Owner** | **engine** |
| **Mechanism** | The metadata attached at submit round-trips, and filters both the SDK and the dashboard, so *"show me every run for this scenario"* needs no table of ours. |
| **The comparison row** | It is a **lookup key, not an aggregate**. *"How many scenarios partially failed this week"* is one `GROUP BY` on Track A and N+1 engine round-trips here. |
| **Retention** | The engine's tenant retention policy governs it — 720 h by default. On this track that policy *is* the run-history policy. |

### 11 · Live progress (SSE)

| | |
|---|---|
| **Component** | `hatchet.listener.stream(run_id)` as *wakeup* + `get_run` as *answer* · `frames.py` |
| **Owner** | engine (wakeup) · ours (identity, diffing, wire shape) |
| **Mechanism** | The gRPC listener fires the moment a task starts, completes, fails or is cancelled — no polling interval. The event itself carries `(type, payload)` and no task identity, so the listener wakes the subscription and `get_run` supplies the answer; a diff against what was already sent yields the port's `RunEvent`s. |
| **Contrast** | Track A needs neither half: its progress stream is written by the workflow body, so it carries identity, ordering and a resumable offset natively. |
| **Transport failure** | `frames.error_frame` emits a terminal `event: error` frame, because once the response headers are on the wire an HTTP status is no longer available to express an outage. An SSE event type, not a run status. |

### 12 · Crash recovery

| | |
|---|---|
| **Component** | none of ours |
| **Owner** | **engine** |
| **Mechanism** | A worker that dies mid-task releases its slot and the engine reassigns the work. Nothing is scoped by an executor identity, because run ownership is the engine's to track. |
| **Consequence** | There is no recovery code, no executor-id contract and no heartbeat table on this branch. **Track A spends a runbook section and a migration on exactly this.** |
| **Where the state lives** | Worker registration and run ownership live inside the engine's own database, so they are the engine's to enumerate and expire rather than something queryable from the application. |

### 13 · Admission control (dedup, serialisation)

| | |
|---|---|
| **Component** | `TTLBasedIdempotencyConfig(key_expression="input.dedup_key", ttl=…)` + `ConcurrencyExpression(max_runs=1, GROUP_ROUND_ROBIN)` |
| **Owner** | **engine — declared in ~6 lines** |
| **Dedup** | A short TTL window collapses repeated identical submissions — a debounce, not a result cache. The key fingerprints **the work**: a hash over scenario, AOI, code version, dirty set and per-unit feature scope, so only genuinely identical work collapses. A collision returns the existing run id, since a dedup hit is the feature working. |
| **Serialisation** | The hazard is two runs writing the same rows: the write path is stale-then-insert, so overlapping writers can leave the wrong current value. A concurrency key names the write scope (scenario, else AOI, else the work fingerprint) and the engine keeps one run per scope at a time. Round-robin rather than cancel-in-progress, because cancellation is cooperative and a newer edit must not be dropped. |
| **The comparison row** | Track A implements neither, on any rung. Here both are configuration — which also means they are behaviour that is declared rather than stepped through. |

### 14 · Provenance

| | |
|---|---|
| **Component** | `jobs_runtime.job_run` + `backend_pid` (migration r031) |
| **Owner** | shared — survives on both branches |
| **Mechanism** | A runner owns its own `job_run` row: provenance lives where the compute happens, not in the scheduler. `curated_geo.indicator_result.job_run_id` is NOT NULL with an existence trigger, so every served value traces to a run. |
| **Why it is engine-independent** | It is domain provenance rather than scheduler state, which is why it survived deleting the run tables, and why the measurement spec defines runner time from it. |

### 15 · Observability & operator UX

| | |
|---|---|
| **Component** | the engine's dashboard and REST API |
| **Owner** | **engine** |
| **Surface** | Runs list · metadata filter · **DAG view** · per-task **logs** · input/output inspection · **replay** from a failure · worker and slot visibility · hand-triggered runs. |
| **Tracing** | The engine injects OpenTelemetry `traceparent` into every run's metadata — trace context is free here and absent on the library track. |
| **Metrics** | Prometheus and observability endpoints exist in the engine and are configuration flags. Track A has nothing equivalent to enable. |
| **Reading it correctly** | The dashboard's status chip and the run's verdict answer different questions — a unit skipped as non-dirty completes, so the per-unit verdict is what distinguishes "skipped" from "computed". |

### Track A equivalents, side by side

| dashboard view | Track A equivalent |
|---|---|
| Runs list | `SELECT` on `orchestration_run` |
| filter by metadata | `WHERE scenario_id = …` |
| **DAG view** | nothing — the shape is implicit in `wave.py` |
| per-task **logs** | process logs |
| input / output | `job_unit` row |
| **replay** | `fork_workflow`, CLI only |
| **workers** | `ps` + the heartbeat view |

---

## 6 · Deployment & configuration surface

| variable | default | what it controls |
|---|---|---|
| `HATCHET_CLIENT_TOKEN` | — | engine credential; required to register workflows or call the engine |
| `HATCHET_CLIENT_TLS_STRATEGY` | — | must match the engine's gRPC TLS mode |
| `DATABASE_URL_DIRECT` | — | the domain database the unit runners open |
| `REIMAGINA_UNIT_TIMEOUT_MIN` | 120 | per-unit execution deadline |
| `REIMAGINA_QUEUE_TIMEOUT_HOURS` | 6 | how long a unit may wait for a slot |
| `REIMAGINA_UNIT_RETRIES` | 2 | attempts, with backoff |
| `REIMAGINA_DEDUP_TTL_S` | 60 | the idempotency window |
| `REIMAGINA_UNIT_CONCURRENCY` | 2 | worker slots **= simultaneous domain DB connections** |
| `REIMAGINA_WORKER_NAME` | `reimagina-worker` | the worker's identity in the dashboard |
| `REIMAGINA_PLAN_RETENTION` | — | horizon for `make reclaim-run-plans`; **must exceed engine run retention** |

**Engine-side requirements Track A has no analogue for:** the engine's database must be UTC ·
an auth cookie domain must be configured even headless · the engine image is pinned, because
scheduling semantics and migrations move with it.

**Connection footprint.** The engine keeps several pools against the shared Postgres and is
bounded by `DATABASE_MAX_CONNS`; the working figure is **~47 idle connections**, plus one per
in-flight unit from the workers. This is the direct cost of co-locating the engine on the
domain database rather than giving it its own server.

**Client and DAG construction are lazy**, so pure imports, unit tests and preflight validation
need no engine credential; construction happens on first use.

**API surface.** `POST /jobs` accepts `wait` only as `false` — nothing can drain a run inside
the request, because execution requires a worker. Track A keeps `wait: true` → 200.
`GET /jobs/{run_id}` validates the id as a UUID at the boundary, keeping "unknown run" (404)
distinct from "engine unreachable" (503 + `Retry-After`).

**Migrations.** `r033` **drops** `orchestration_run` and `job_unit`. `r050` adds
`jobs_runtime.run_plan` — the claim check that makes a full-city submit possible at all.
`r051` indexes `run_plan.created_at` and turns the table from reclaimed into **retained**.

⚠ **`r051` exists because reclaiming the plan broke replay.** r050 deleted a plan at terminal
state on the reasoning that the table would be empty in the steady state. But the engine keeps
runs for 720 h by default and `runs.replay()` re-executes from the *original input* — which
here is a `plan_id` whose rows had already been deleted. Every replay older than the sweep's
horizon failed each unit with a missing-plan error, and the sweep ran on the **submit** path,
so a busy afternoon could delete a live run's plan. Plans now live at least as long as the
runs that reference them, reclaimed out-of-band by `scripts/reclaim_run_plans.sql`.
**The retention horizon must exceed `SERVER_LIMITS_DEFAULT_TENANT_RETENTION_PERIOD`** — not
"should": a plan that expires first turns a replayable run into one that cannot be replayed.

⚠ **The revision ranges are split across the fork, permanently.** Alembic identifies a
revision by its id alone, so a number minted on one branch and reused on the other yields two
migrations answering to one name. That already happened with `r033`: a migration from this
track applied to a database on the **DBOS** lineage and dropped its run tables while
`alembic current` still reported "head". **This track takes r050+; the DBOS track keeps
r03x/r04x.** Never continue the other branch's numbering, and use a separate database per
track.

---

## 7 · Comparative measurement

`slr` = makespan ÷ critical path. 1.00 means the scheduler achieved the DAG's lower bound.

| workload | slr A | slr **B** | head-of-line A | head-of-line **B** |
|---|---:|---:|---:|---:|
| `diamond` | 3.19 | **1.13** | 1868 ms | **78 ms** |
| `lattice` | 3.54 | **1.21** | 2480 ms | **103 ms** |
| `barrier_probe` | 6.80 | **1.44** | 2811 ms | **96 ms** |
| `layered_random` | 4.45 | **1.22** | 3023 ms | 1372 ms |
| `chain_deep` | 13.72 | **2.87** | 1956 ms | **80 ms** |

**The difference is structural, not incidental.** Track B evaluates readiness per task, where a
wave scheduler must clear a barrier; `slr` 1.13 on `diamond` is within 13 % of the DAG's
theoretical lower bound.

⚠ **Not every row carries the same weight**, per `tests/workloads/README.md`. Run-to-run
spread on two consecutive full runs of one track:

| stable to ±2 % | noisy |
|---|---|
| `lattice` 0.2 % · `diamond` 0.4 % · `barrier_probe` 1.8 % | `layered_random` 13 % · **`chain_deep` 35 %** |

**So `diamond`, `lattice` and `barrier_probe` are the comparison; `chain_deep` and
`layered_random` are order-of-magnitude only** and should not be read to three significant
figures. That applies to this track's favourable readings as much as to Track A's unfavourable
ones: `layered_random`'s speedup of **1.63×** versus `sync`'s 0.44× is a 13 %-noise workload,
and its head-of-line here is **1372 ms**, not the ~80–100 ms the other four rows show. Raise
`REIMAGINA_BENCH_REPS` before quoting either.

⚠ **Run conditions are not recorded for these figures** — machine, date, `REIMAGINA_BENCH_REPS`,
`REIMAGINA_BENCH_SCALE`, and the engine image and SDK versions.
`PHASE3_OPS_PLANE_MEASUREMENT.md` requires them: "A number without those is not comparable to
the other branch's." Fill them in or re-run before this table is used to decide anything.

> **Read in proportion.** The suite's units are `pg_sleep` and `sha256` loops of 10 ms–3 s,
> chosen to measure the scheduler rather than the geometry. **Real indicator units run for
> minutes**, where a 2 s wave barrier is under 1 % of wall clock. The `sync` control column
> agrees to within 0.5 % across both worktrees — that is evidence the *harness* is sound, and
> says nothing about the reproducibility of any individual workload above. At full scale the
> suite's median speedup is **0.86**, i.e. slower than in-process: these units are small enough
> that dispatch overhead dominates most shapes, and a sleeping unit never contends for the
> database the way a real one does.

### Ownership delta — state the counting rule, because it decides the sign

This is a spec'd metric and the headline this track exists to move. The two obvious rules give
**opposite answers**, so neither number means anything unlabelled:

| rule | Track A | Track B | who owns more |
|---|---:|---:|---|
| **executable code** — no blanks, comments or docstrings | 816 (6 modules) | **602** (4 modules) | A, by 36 % |
| statements, counted from the AST | 466 | **359** | A, by 30 % |
| raw lines of the owned ops-plane modules | 1 504 | 1 646 | B |
| insertions vs the fork point, `orchestration/` | +1 290 / −773 (15 files) | +1 793 / −1 029 (19 files) | B |

**This track owns roughly a quarter less executable code — and the raw-line column, which says
the opposite, is an artefact of prose.** `hatchet_app.py` is 629 lines of which **345 are
comment or docstring and 194 are code**: 55 % prose, because declaring a graph against an
engine's semantics needed more explaining than it needed machinery. `wc -l` measures that
explaining.

Report the code row. The raw and diffstat rows are kept only because they are what a casual
`wc -l` or `git diff --stat` returns, and they point the other way — anyone re-deriving this
number by the obvious method will get the wrong sign.

Where it comes from, and where it does not: `wave.py` (43 code lines) and `runstore.py` (139)
are gone outright. Against that, `hatchet_orchestrator.py` is the one module heavier than its
counterpart — 341 code lines to `dbos_orchestrator.py`'s 267 — for cancellation consistency,
status projection, the engine's not-found and unavailable mapping, plan lifecycle and
`cancel_scenario`. A library track gets most of that free by being in-process. The net is
still strongly in this track's favour, but it is bought in `hatchet_app.py` (194 code lines
for the whole DAG declaration, defaults and admission control), not in the adapter.

### What this track has that the other does not: real-data correctness

`scripts/verify_real_data.py` runs the shipped catalog over the real city at full scope —
**86 225 blocks × 3 indicators** — against both `sync` and `hatchet`, with four oracles that
exist independently of the code under test:

| check | what it proves | result |
|---|---|:--:|
| **A** neutral scenario reproduces the baseline | every value equals the committed baseline bit for bit | ✅ both |
| **B** an override changes exactly what it should | a `floors` override dirties `planned_floors` only, writes only the edited features, and the value reaches the result | ✅ both |
| **C** structural invariants | one `current` row per (feature, indicator, scenario); every result cites a real `job_run` | ✅ both |
| **D** determinism | two independent runs of the same input agree value for value | ✅ both |

Check **B** is the load-bearing one: a pipeline that ignored overrides entirely would still
pass A, because a what-if engine that silently returns the baseline returns exactly what a
neutral scenario expects. Before this run there were **zero** override rows in the database —
the what-if path had never been exercised on real data. Check A is also the only end-to-end
exercise of the claim check: 258 675 feature ids through `run_plan` rather than a 10 MB gRPC
message, values identical on the far side. Full write-up:
`tests/workloads/REAL_DATA_RESULT.md`.

⚠ **Track A has no equivalent.** It ships `bench_real_data.py` but no verifier and no result
document, so the two tracks are not equally evidenced and this row cannot be read as a
comparison.

### Metrics this document does not report

`slr` and `head_of_line_ms` are the workload suite's own metrics, not the six in
`PHASE3_OPS_PLANE_MEASUREMENT.md`. Of those six, **per-unit overhead, redundant compute and
recovery latency are not reported here on either track.** Idle footprint appears as §2 and §6;
ownership delta is above. Either the spec is amended or those three still have to be measured
before `PHASE3_OPS_PLANE_TRACKS.md` can be written.

---

## 8 · Pros

- **We do not own a scheduler.** Readiness, ordering, fan-out, propagation, retries and
  backoff are inherited; `wave.py` and `runstore.py` are deleted, not maintained. Measured,
  that is **36 % less executable code than Track A owns** — the saving is real, not just a
  shift in what we maintain.
- **Materially better scheduling** — on the three reproducible workloads, `slr` 1.13–1.44
  versus 3.19–6.80 and ~80–100 ms head-of-line versus 1.9–3.0 s, with no barrier to compound
  with DAG depth. The advantage holds on the noisy workloads too but at lower resolution
  (`chain_deep` 2.87 versus 13.72; `layered_random` head-of-line 1372 ms, not ~100 ms).
- **Failure propagation is exactly our F6 rule, for free.** The clearest single argument for
  the service track.
- **Admission control is declarative.** Dedup and per-scenario serialisation are ~6 lines;
  Track A would have to write both.
- **A real operator surface** — DAG view, per-task logs, input/output, replay, worker and slot
  visibility, metadata search, hand-triggered runs.
- **Retries distinguish transient from deterministic**, with backoff supplied by the engine.
- **Bulk operations by meaning, not by id** — cancel every run for a scenario in one call.
- **OpenTelemetry trace context is injected free**; Prometheus is a configuration flag.
- **No recovery code to write** — a dead worker's slot is reassigned by the engine.
- **No message broker.** Postgres is a first-class broker, so the engine is the only added
  infrastructure.

## 9 · Cons

- **There is a service to operate** — a container, a second database, an engine credential, a
  UTC requirement, and an image pin that couples scheduling semantics to an upgrade cycle.
- **A worker is a precondition.** There is no in-process mode, and a run is not observable
  until a worker has registered the graph.
- **~47 idle Postgres connections** on the shared instance, before any unit runs.
- **Run history is the engine's**, governed by a retention policy rather than by disk.
- **The status vocabulary is reachable one run at a time** — a lookup key, not an aggregate,
  and it does not survive the engine being unavailable.
- **Status is derived after the fact** from engine-held signals, rather than written by the
  component that knows it.
- **Run state is eventually consistent** through the engine's projection: a run is retrievable
  before it reports units, and a cancel is an accepted request rather than a terminal state.
- **Adding an indicator requires a redeploy** — the DAG is declared at import, where Track A
  reads the registry at runtime.
- **Semantics live in engine configuration**, which cannot be stepped through, and behaviour
  we depend on is tied to the engine version rather than to code in this repository.
- **Scaling is a connection budget** on two databases rather than one.

---

## 10 · Summary line

Track B buys a scheduler, an operator surface, admission control and retries, and pays with a
standing service: a container, a second database, ~47 idle connections, a worker that must
exist before a run is observable, and a run history governed by retention. It schedules
materially better — within 13 % of the DAG's lower bound where the library track is 3× off it
— and inherits our exact failure-propagation rule for free. The trade is **legibility**:
semantics live in engine configuration rather than steppable code, and status is derived
rather than written.
**The fit is a wide DAG, a large fleet, and an operator who will use the dashboard.**

---

## Appendix · Cross-references

Branch-local detail: `docs/architecture/OPS_PLANE_RUNBOOK.md` (operations, plus what to click
in the dashboard and in what order) and `docs/product/PHASE3_OPS_PLANE_HATCHET_SPIKES.md`
(engine behaviour established by experiment). Measurement definitions are shared:
`PHASE3_OPS_PLANE_MEASUREMENT.md`.

### Shared foundation — what is actually shared

`factory.py` is the single place either engine is named, on both branches. Below that, the
foundation is shared **in role**, and the two branches have edited most of it. Measured file
by file between the worktrees:

| file | state | what diverged |
|---|---|---|
| `impact.py` | **byte-identical** | — |
| `perf/workloads.py` | **byte-identical** | the fixed half of the benchmark, by design |
| `plan.py` | 8 lines | module docstring only; behaviour identical |
| `coordinator.py` | 17 lines | the `sync` path no longer touches `runstore` |
| `runners.py` | 17 lines | runners bound for the engine's task bodies |
| `graph.py` | 36 lines | this branch also emits parents-first declaration order |
| `frames.py` | 30 lines | `error_frame` plus surrounding changes — not "one function" |
| **`port.py`** | **47 lines** | **the contract itself moved — see below** |
| `tests/conformance/`, `tests/workloads/` | differ | `IMPLEMENTATIONS` is the branch-specific half by design; the READMEs and fixtures have also diverged |

⚠ **The port is no longer identical, and `JOB_LAYER.md` still calls it "the part that is
not [replaced]".** This branch made two changes Track A did not adopt:

- **`get_run(self, conn, run_id)` → `get_run(self, run_id)`** — capability 9.
- **`UNIT_UNSATISFIED_STATES` deleted.** It is the readiness rule's input; it has nothing left
  to describe on a branch whose engine owns readiness, and it survives on Track A with
  `wave.py`.

Both are defensible on this track's terms. Neither is reconcilable by picking one side: the
parameter is dead here and load-bearing there. **This is a finding of the fork, not a defect
to fix** — but it must be recorded, because "the port never changes" was a Phase 0 design
claim and it did not hold.

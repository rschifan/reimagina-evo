# Job Layer

How the platform executes indicator computation — the `Orchestrator` port, the run state it persists, and the engine behind it. The engine is the part that is about to be replaced twice; the port is the part that is not.

**Related documents:** `SCENARIOS_LAYER.md` — scenarios and overrides that trigger runs. `INDICATORS_LAYER.md` — indicator results that runs produce. `DATA_LAYER.md` — canonical city data that runs read from. `docs/product/PHASE3_OPS_PLANE_MEASUREMENT.md` — what the two ops-plane branches measure.

---

## What this layer does

A computation is a **run**, not a queue message. The application calls one method on one interface; what happens behind it — an in-process loop, a Postgres task queue, a durable-execution library, a workflow service — is the engine's business and is expected to change.

```
caller (apps/api/routers/jobs.py, apps/cli/indicators.py)
  │
  ├─ get_orchestrator(mode)             factory.py:48 — the only place an engine is named
  ├─ submit_run(conn, changed_attributes=…, edited_features=…, scenario_id=…)
  │     │
  │     ├─ plan_run              plan.py:54       resolve change-set → dirty-set, in dep order
  │     ├─ create_run            runstore.py:25   → jobs_runtime.orchestration_run
  │     ├─ record_units          runstore.py:129  → jobs_runtime.job_unit (one per dirty unit)
  │     └─ hand the run to the engine
  │
  ◄─ RunHandle(run_id)           port.py:76 — status populated only by a blocking engine
  │
  │   engine executes each unit, in wave order:
  │     ├─ runner(conn, features=…, scenario_id=…)      runners.py:41
  │     │     └─ job_run(...)    jobs/_lifecycle.py:46  → jobs_runtime.job_run (opened HERE,
  │     │            └─ writes curated_geo.indicator_result citing job_run_id   at compute time)
  │     ├─ set_unit_status       runstore.py:62 → True only if the write landed
  │     └─ events.emit           events.py:22   → pg_notify('job_events', …), gated on that True
  │
  ├─ get_run(conn, run_id)             → RunStatus + per-unit views
  └─ subscribe_progress(run_id)        → RunEvent stream, framed as SSE by the route
```

Two things follow from that shape and are stated plainly here because earlier drafts of this document assumed the opposite of both, and code was written against those assumptions:

**There is no job message and no job claim.** Nothing enqueues a per-model-family envelope, and no worker races to `UPDATE … WHERE status='queued'`. `JobRunRepository` says so in the interface itself — "Ops-plane queueing/claiming is owned by the Procrastinate task queue, not here — there is no claim_job" (`packages/py/reimagina/storage/interfaces.py:550-551`). Dedup is a queueing lock, not a claim (`orchestration/tasks.py:32`, `:46`).

**`job_run` is not opened at submit.** The API writes `orchestration_run` + `job_unit` and returns; the `job_run` row is opened later, by whatever runs the compute, straight to `running` (`jobs/_lifecycle.py:63-70`, `storage/postgres/repositories.py:1959`). A run that is queued but never executed leaves no `job_run` row at all.

---

## Permanent vs replaceable

The project is forking into two branches, each a complete reference implementation of a different ops-plane. They never merge; the deliverable is the side-by-side. Which half of this layer a module belongs to therefore decides whether it is a design commitment or an implementation detail.

| Fate | Modules |
|---|---|
| **Port-level — permanent, identical on both branches** | `orchestration/port.py`, `factory.py`, `plan.py`, `graph.py`, `impact.py`, `frames.py`, `coordinator.py`, `synchronous.py`, `memstore.py`; `indicators/*`; `jobs_runtime.job_run` + `jobs/_lifecycle.py` |
| **Engine-level — replaced on both branches** | `orchestration/tasks.py`, `queue_app.py`, `events.py`, `procrastinate_orchestrator.py`, `embedded.py`, `perf/procrastinate_bench.py` |
| **Split fate — this *is* the comparison** | `orchestration/wave.py` — survives on Track A, deleted on Track B. `orchestration/runstore.py` + `jobs_runtime.orchestration_run` + `jobs_runtime.job_unit` — survive on Track A, dropped on Track B (migration r033) |

The split-fate row is not a hedge. A branch that keeps our scheduler and one that deletes it are the two answers being compared, and the measurement spec names that difference as a reported metric — "one branch keeps the wave scheduler, the other deletes it" (`docs/product/PHASE3_OPS_PLANE_MEASUREMENT.md`, *Ownership delta*).

The module docstrings already carry their own fate, deliberately, so the split survives a file move: `wave.py:4-7` ("this is the half an engine can replace"), `events.py:4-6` ("specific to the Postgres-queue ops-plane and goes away with it"), `frames.py:3-7` ("every ops-plane implementation needs it"), `memstore.py:5-8` ("an ops-plane that drops those tables can then delete `runstore`").

---

## The port

`packages/py/reimagina/orchestration/port.py` — four methods, plain dataclasses, no engine types.

```python
class Orchestrator(Protocol):                                    # port.py:102
    def submit_run(self, conn, *, changed_attributes, edited_features,
                   scenario_id=None, aoi_id=None, code_version="dev") -> RunHandle: ...
    def get_run(self, conn, run_id: str) -> RunStatus | None: ...
    def cancel_run(self, conn, run_id: str) -> bool: ...
    def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]: ...
```

| Type | Shape | Note |
|---|---|---|
| `RunHandle` | `run_id`, `status: RunStatus \| None` | `port.py:76`. A blocking engine populates `status`; a detaching one leaves it `None` and the caller polls `get_run`. |
| `RunStatus` | `run_id`, `status`, `units: tuple[UnitStatusView, …]` | `port.py:69` |
| `UnitStatusView` | `indicator`, `status`, `detail` | `port.py:62` |
| `RunEvent` | `run_id`, `indicator \| None`, `status`, `detail` | `port.py:85`. Data, never a pre-formatted SSE string — framing is the route's job. |

Two signatures encode decisions worth keeping:

- **`submit_run` takes a connection, `subscribe_progress` does not.** Every engine has its own progress transport (LISTEN/NOTIFY here, workflow streams elsewhere), and none of them is the caller's pooled connection (`port.py:131-136`).
- **`cancel_run` promises to stop *scheduling*, not to stop *compute*.** "no engine can interrupt a thread blocked in a psycopg C call, so a unit already executing runs to its next boundary. Killing the backend is a separate mechanism" (`port.py:120-123`). See [Cancellation](#cancellation).

### The factory

`orchestration/factory.py` is the one place a concrete orchestrator is named. Callers ask for a mode; nothing outside that module imports an implementation class, and tests monkeypatch `get_orchestrator` rather than a class path only one engine has (`factory.py:1-11`).

| Mode | Implementation | Behaviour |
|---|---|---|
| `hatchet` (default) | `HatchetOrchestrator` | Submit to the engine and return. A worker is **mandatory** — nothing runs without one. |
| `sync` | `SynchronousOrchestrator` | Compute in-process, no engine, no worker. No production caller — it exists so the conformance suite has a target needing neither. |

⚠ Two modes, not three. `detach` and `embedded` named *orchestrators* on the queue rung; here they are CLI behaviours over the one engine — `--detach` submits and returns, the default blocks until the run is terminal. Both go through `hatchet`.

Mode resolution is `argument → REIMAGINA_ORCHESTRATOR → "hatchet"`, and an unknown mode raises rather than falling back.

**The environment may only select a durable mode.** `DURABLE_MODES = ("hatchet",)`; `REIMAGINA_ORCHESTRATOR=sync` is rejected with an explanation rather than honoured. `sync` keeps run state in memory *per orchestrator instance*, and the factory builds a fresh instance per call — so as a deployment default it would have `POST /jobs` writing to Postgres while `GET /jobs/{run_id}` and the SSE route read an empty dict. It stays available as an explicit argument, for a caller that holds on to the instance (`factory.py:37-45`, `:50-60`).

The API always submits to `hatchet` and returns `202`; it never executes a worker
inside a request. The CLI keeps the user-facing `embedded` behaviour as a
bounded wait over an already-running worker, with `--detach` and `--sync` as
explicit alternatives (`apps/cli/indicators.py`).

---

## The submit prelude

Everything between "a scenario changed" and "run it" is engine-independent, so it lives outside every engine: "an engine replaces the *execution* of a run, never the decision of what the run contains" (`orchestration/plan.py:5-7`).

**`plan_run`** (`plan.py:54`) resolves the effective change-set and derives the dirty-set, returning a `RunPlan(units, params)` where `params` is exactly `{scenario_id, aoi_id, code_version}` — kept as a dict because both `run_dirty_set` and `runstore.create_run` take them as keywords and an engine has to serialize them (`plan.py:43-47`).

The change-set is resolved by `scenarios.overrides.resolve_change_set` (`overrides.py:113`): explicit lists win; when only a `scenario_id` is given, the diff is derived from that scenario's `curated_geo.scenario_override` rows. An override that edits `attribute_key` *is* the `changed_attributes` token the impact engine keys off, so no translation layer is needed (`overrides.py:9-11`).

**Noop is a first-class answer.** An empty dirty-set means nothing was invalidated, which is not a run that succeeded at doing nothing. `noop_handle` (`plan.py:95`) is the one shape every implementation returns for it, and `noop` is deliberately **return-only** — `orchestration_run.status` has no such value and a noop persists no run (`plan.py:19-22`). The API suppresses the id rather than handing back one that 404s (`apps/api/routers/jobs.py:73-75`).

### Impact engine — what to recompute

`orchestration/graph.py` is the library-free graph contract shared by both tracks. It provides deterministic topological ordering, reverse edges, and downstream closure over plain `slug -> prerequisite slugs` mappings. `orchestration/impact.py` remains pure: it maps indicator metadata and feature scope onto those primitives, with no DB or engine dependency.

1. **Seed** — every indicator whose declared `reads` intersects the changed attributes (`impact.py:83`).
2. **Propagate** — anything depending transitively on a dirty indicator is dirty, via reverse edges (`impact.py:86-99`).
3. **Order** — topological over the dependency graph restricted to the dirty set, so inputs precede dependents; a cycle raises `graphlib.CycleError` (`impact.py:105-107`).

Feature scoping rides along: a seed unit gets `edited_features`, a dependent gets the union of its dirty inputs' feature sets, and each passes through an injectable `expand` hook that is the identity today (`impact.py:38-47`, `:110-119`). The hook exists because proximity and rollup indicators need the feature graph, which is not derivable from the registry.

The output is `list[DirtyUnit]` — `(indicator, features)`, where an empty feature set means "every feature this indicator applies to" (`impact.py:25-36`).

### Wave scheduler — what may run now

`orchestration/wave.py` is the DB-free readiness rule for a distributed engine. `plan_wave(units, registry)` takes `(slug, status)` pairs for one run and returns a `WavePlan`:

| Field | Meaning |
|---|---|
| `dispatch` | queued units whose dirty inputs all succeeded → run now (`wave.py:88`) |
| `skip` | queued units with a failed/skipped/cancelled input → mark skipped (`wave.py:86`) |
| `final_status` | set once every unit is terminal under the projected statuses, else `None` (`wave.py:91-99`) |

The readiness rule is stated twice on purpose, in two shapes: `plan_wave` answers "given these statuses, what moves now", while the in-process executor applies the same rule inline while walking a pre-sorted list (`wave.py:8-12`). Imports flow `wave → coordinator → port`, so there is no cycle: `coordinator` owns the graph derivation (`_dep_map`, `coordinator.py:97`) and `port` owns the status vocabulary.

### In-process executor

`coordinator.run_dirty_set` (`coordinator.py:102`) is the single-process executor and the `sync` path. Per unit, in dependency order:

- any *dirty* input that did not succeed → **skipped** (non-dirty inputs keep their current value and do not block) — `coordinator.py:134-146`;
- no runner registered → **skipped** — `coordinator.py:148-153`;
- otherwise run it; an exception isolates to that unit → **failed**, and its dependents skip on later iterations — `coordinator.py:155-168`.

Fault isolation is the point: independent units do not block each other, and a failure stops only what depends on it. The result is a `RunReport`, whose `status` rolls up through the shared `overall_status` — except for the empty case, which is `noop` (`coordinator.py:89-91`).

Runners are injected, never defined here. `orchestration/runners.py:76` binds slugs to compute functions: direct indicators to their bespoke function, composites to the generic `compute_composite` keyed by spec. A runner owns its own `job_run` row — provenance lives where the compute happens (`coordinator.py:17-19`).

---

## Run state — one table, one owner

| Table | Owner | Written by | Fate |
|---|---|---|---|
| `jobs_runtime.job_run` | domain | `jobs/_lifecycle.job_run` | survives on both branches |

⚠ `jobs_runtime.orchestration_run` and `jobs_runtime.job_unit` **are dropped on this branch** (migration r033), together with `orchestration/runstore.py`. That deletion is what the track is for: the engine owns run state end to end, and keeping a parallel copy would be re-adopting the model being given away.

`orchestration/memstore.py` survives — it backs the synchronous orchestrator, which needs somewhere to keep run state and must not need a database to do it.

### Where the run vocabulary lives now

Not in a table. The workflow's `on_failure_task` / `on_success_task` reconstruct a per-unit verdict and return it as the run's **output**, which `runs.get_result()` serves even for a run whose status is FAILED. `get_run` prefers that verdict and falls back to the status enum only while a run is still in flight.

| | Track A | Track B |
|---|---|---|
| `partial` / `skipped` expressible | yes | yes, in the run output |
| queryable across runs | `SELECT … GROUP BY` | one run at a time, via the engine |
| survives the engine being down | yes | no |
| expires | no | yes — engine retention, 720 h by default |

`additional_metadata` (scenario, AOI, code version) is stamped on every submit and is a filter in `runs.list()` and the dashboard, so "every run for this scenario" still works. It is a lookup key, not an aggregate.

### job_run (r016, extended by r031)

Domain provenance, not scheduler state. `curated_geo.indicator_result.job_run_id` is `NOT NULL` with an FK and an existence trigger (`r016_indicator_layer.py:175-176`, `:255-257`), so every result row cites the execution that produced it. That is why `job_run` survives an engine that owns run state natively — and why r031 hangs the cancellation seam off it rather than off `orchestration_run` (`r031:23-25`).

```sql
-- r016_indicator_layer.py:30-43, plus r028:67 and r031:43-44
CREATE TABLE jobs_runtime.job_run (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id   uuid,                    -- no FK: app_core.scenario is deferred (r016:7-8)
  model_family  text        NOT NULL,
  status        text        NOT NULL
                            CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  code_version  text        NOT NULL,
  queued_at     timestamptz NOT NULL DEFAULT now(),
  started_at    timestamptz,
  completed_at  timestamptz,
  error_message text,
  procrastinate_job_id bigint,           -- r028; never written
  run_id        uuid,                    -- r031; nullable, no FK, deliberately
  backend_pid   int,                     -- r031; pg_backend_pid() at insert
  CHECK (started_at   IS NULL OR started_at   >= queued_at),
  CHECK (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
);
CREATE INDEX job_run_run_idx ON jobs_runtime.job_run (run_id, status) WHERE run_id IS NOT NULL;
```

`run_id` is nullable with no FK on purpose: a direct CLI compute belongs to no run, and an ops-plane that owns run state natively drops `orchestration_run` entirely — which an FK would forbid (`r031:17-21`).

**The row is opened at compute time, straight to `running`.** `jobs/_lifecycle.job_run` (`_lifecycle.py:46`) is the context manager every compute function uses: `start_job(status="running")`, yield the id, then `complete_job('succeeded')` on clean exit or `complete_job('failed', "<ExcType>: <msg>")` on an exception before re-raising (`_lifecycle.py:63-81`). `started_at` is set by the repository when status is `running` (`repositories.py:1959`).

**`run_id` arrives ambiently, not as a parameter.** `current_run` is a `ContextVar` an executor sets around the call (`_lifecycle.py:25-29`), because threading the id through the `Runner` protocol, two adapter closures, four compute signatures and every test fake — so a standalone CLI compute could pass `None` through all of it — is worse than an ambient (`_lifecycle.py:6-15`). It must be set **inside** the thread that runs the compute; a ContextVar does not cross into a thread the setter did not enter (`_lifecycle.py:32-35`, applied at `tasks.py:115` and `coordinator.py:157`).

---

## Cancellation

`cancel_run` needs two mechanisms, and neither is sufficient alone (`procrastinate_orchestrator.py:110-153`).

**Half one — stop scheduling.** Every non-terminal unit is marked `cancelled` and the run is finalised. `plan_wave` treats `cancelled` as terminal, so the next `advance_run` finds nothing ready to dispatch (`wave.py:78`, `port.py:30`).

**Half two — stop the statement.** A worker already executing a unit holds its own connection; nothing in Python interrupts a blocking PostGIS call. `JobRunRepository.cancel_run_backends(run_id)` issues `pg_cancel_backend` against every live backend recorded for the run (`interfaces.py:574`, `repositories.py:1976-2001`):

```sql
SELECT count(*) FILTER (WHERE pg_cancel_backend(backend_pid))
FROM jobs_runtime.job_run
WHERE run_id = %s AND backend_pid IS NOT NULL
  AND status NOT IN ('succeeded', 'failed', 'cancelled');
```

`pg_cancel_backend` cancels the *statement*, not the session — the runner's psycopg call raises `QueryCanceled` and its own error handling takes over (`repositories.py:1985-1988`).

**Which creates the third problem the terminal guard solves.** That error handling then tries to write `failed` over a unit already marked `cancelled`, resurrecting a run that was closed. `runstore.set_unit_status` refuses to move a unit out of a terminal state (`runstore.py:92`), and that guard is what makes cancellation stick (`runstore.py:70-76`).

**And a fourth: a refused write must not be announced.** The guard protects the table, not the event stream. A worker that publishes the transition it *asked for* rather than the one that *happened* tells every SSE subscriber `failed` for a unit `GET /jobs/{run_id}` reports `cancelled`. So every `events.emit` in the engine is gated on the write landing — `compute_unit` (`tasks.py:140-147`), both loops in `advance_run` (`tasks.py:169-188`), and the cancel path itself (`procrastinate_orchestrator.py:134-143`, `:150-151`). `finalize_run` carries the same guard for the same reason: after a cancel, the worker still mid-unit finishes, advances, and finalises again, which would re-stamp `completed_at` and publish a second run-level terminal event. The first close is the real one — it is when the run stopped being able to change (`runstore.py:151-157`).

**Not dispatching a refused unit is the load-bearing half.** In `advance_run`, a `dispatch` entry whose `running` write is refused is skipped entirely rather than merely un-announced (`tasks.py:179-184`) — that is what stops a cancelled run from starting more compute.

Order matters: backends are cancelled first, then units are closed (`procrastinate_orchestrator.py:129-143`). A run that is unknown or already terminal returns `False` without side effects (`:125-127`). The final roll-up re-reads the units rather than trusting the snapshot it started from: a worker can commit `succeeded` in between, and `set_unit_status` then refuses our `cancelled`, so assuming the write landed would report a cancelled run whose units all say they finished (`procrastinate_orchestrator.py:145-149`).

The synchronous orchestrator always returns `False`: `submit_run` does not return until the last runner has, so no caller ever holds an id for something still in flight (`synchronous.py:121-127`).

---

## Status vocabulary

Python has one definition, in the port, because the port is the module no ops-plane deletes (`port.py:20-23`).

| Frozenset | Members | Meaning |
|---|---|---|
| `UNIT_TERMINAL_STATES` | `succeeded, failed, skipped, cancelled` | no further transition (`port.py:30`) |
| `UNIT_UNSATISFIED_STATES` | `failed, skipped, cancelled` | makes a dependent unrunnable (`port.py:33`) |
| `RUN_TERMINAL_STATES` | `succeeded, partial, failed, cancelled` | closes a progress stream (`port.py:36`) |

These are not advisory. `runstore` imports the first and third and interpolates them into the `UPDATE` guards (`runstore.py:14`, `:85`, `:166`), so the port's vocabulary is what the database enforces — and an engine that keeps `runstore` inherits the enforcement for free.

`overall_status(statuses)` (`port.py:40`) rolls unit statuses into a run status:

- all `succeeded` (or empty) → **succeeded**
- any `succeeded` alongside anything else → **partial**
- no failure, at least one cancellation → **cancelled**
- anything else, including an all-skipped run → **failed**

Two judgements are encoded there. `cancelled` only wins when nothing actually failed — a run that both failed and was cancelled is a failure, and calling it cancelled would hide the error. An all-skipped run folds into `failed` rather than gaining a fourth value, because nothing ran and `orchestration_run.status` has no `skipped` (`port.py:44-50`).

### The three database CHECKs disagree

Unifying Python did not unify the schema, and widening the CHECKs is a migration decision, not a refactor (`port.py:25-28`).

| Value | `job_run` (r016:35) | `orchestration_run` (r028:31) | `job_unit` (r028:51) |
|---|:---:|:---:|:---:|
| `queued` | ✓ | ✓ | ✓ |
| `running` | ✓ | ✓ | ✓ |
| `succeeded` | ✓ | ✓ | ✓ |
| `failed` | ✓ | ✓ | ✓ |
| `cancelled` | ✓ | ✓ | ✓ |
| `skipped` | — | — | **✓ only** |
| `partial` | — | **✓ only** | — |
| `cancelling` | — | ✓ (never written) | — |

`noop` appears in none of them, which is precisely why it is return-only (`plan.py:19-22`).

**Execution state lives on the run/unit tables, not on `indicator_result`.** `indicator_result.status` is `current | stale | failed` (r016) — lifecycle states of a *result*. Unit and run statuses are lifecycle states of an *execution*. Different concerns; still true.

---

## Progress

`subscribe_progress(run_id)` yields `RunEvent`s until the run-level terminal event, then stops. The first event is always a snapshot of current status, so a late subscriber is not left waiting on a run that already finished (`port.py:126-136`). `indicator=None` marks the run-level event carrying the overall status; that is the event that ends the subscription (`port.py:91-93`).

Three modules, three fates:

**`orchestration/frames.py` — permanent.** Payload construction and SSE framing, shared by every implementation. `notify_payload` emits `{run_id, indicator, status}`; `event_frame` wraps it as `data: …\n\n` (`frames.py:17-41`). `RunEvent.detail` is deliberately absent from the wire — no client reads it and `GET /jobs/{run_id}` already carries it per unit (`frames.py:23-25`).

**`orchestration/events.py` — engine-level.** The Postgres LISTEN/NOTIFY transport, one fixed channel `job_events`, filtered by `run_id` at the listener (`events.py:19-42`). `emit` must be called *before* the commit that persists the same status change, since NOTIFY is delivered on commit (`events.py:25-27`).

The subscriber (`procrastinate_orchestrator.py:155-211`) opens its own direct connection, `LISTEN`s, and only then takes the snapshot — so a finalise landing between the two is caught by one or the other rather than falling down the gap (`:174-175`). Two details there are cancellation-safety, not style, and are worth carrying to any engine with a socket-based transport: the connect is **synchronous**, because doing it in a thread leaves a window where the subscriber is cancelled mid-connect and the socket opens with nobody left to close it (`:162-166`); and the event loop watches the fd via `loop.add_reader` rather than a thread blocking in `select()`, because that thread would still be waiting on the fd after cleanup closed it — and Postgres hands the freed fd straight to the next connection in the process, so the stray wait lands on someone else's socket (`:182-190`).

**`apps/api/routers/jobs.py` — route policy.** `GET /jobs/{run_id}/events` returns a `StreamingResponse` over `_event_stream` (`jobs.py:169-178`). The route owns frame format, a 15 s keep-alive, and a 300 s lifetime cap; the orchestrator owns *what happened* (`jobs.py:127-134`). One subtlety worth preserving: a single pending `__anext__` is held across heartbeats rather than re-awaited, because wrapping the await in a timeout cancels it mid-flight and leaves the async generator unresumable — the keep-alive would break the stream it exists to keep open (`jobs.py:140-144`).

---

## The engine — Hatchet

Self-hosted, MIT, Postgres as the broker (`SERVER_MSGQUEUE_KIND=postgres`); no RabbitMQ. The queue-based Procrastinate plane that used to be documented here is **deleted on this branch** — `tasks.py`, `queue_app.py`, `events.py`, `procrastinate_orchestrator.py`, `embedded.py` and `perf/procrastinate_bench.py` are gone, along with `wave.py` and `runstore.py`. See `docs/product/PHASE3_ORCHESTRATION_EXPLORATION.md` for what it was.

**One workflow, one task per indicator.** `orchestration/hatchet_app.py::build_dag` declares the graph at import from `SHIPPED_INDICATORS`: a `plan` task publishes the dirty set, every indicator is a task parented on `plan` plus its own dependencies, and `skip_if` skips the ones that are not dirty. Readiness, ordering, fan-out, failure propagation and retries are the engine's. There is no wave loop to read.

⚠ `skip_if` means *skip when the expression is true*, so the condition is negated — `!('slug' in output.dirty)`. The un-negated form runs exactly the indicators it should have left alone while every task reports COMPLETED (spike B1).

**The declaration must be byte-identical across processes.** The API and the worker both call `build_dag()` at import. Two SDK defaults make that false unless overridden: `ParentCondition.readable_data_key` embeds `datetime.now(UTC).isoformat()`, and `BaseCondition.or_group_id` is `Field(default_factory=uuid4)`. Both are derived from the slug instead (spike B9).

**Timeouts are ours, because the defaults are wrong for this workload.** `execution_timeout` defaults to 60 *seconds* against units that run for minutes; `schedule_timeout` defaults to 5 minutes, and queued work that waits longer reports FAILED — indistinguishable from broken compute. Set via `task_defaults` from `REIMAGINA_UNIT_TIMEOUT_MIN` / `REIMAGINA_QUEUE_TIMEOUT_HOURS` (spike B5).

**Failure taxonomy.** Retries with exponential backoff are on by default (`REIMAGINA_UNIT_RETRIES`, default 2) because the engine, not us, distinguishes a dropped connection from a broken runner. A genuinely deterministic failure — a missing runner registration — raises `NonRetryableException` and is not retried.

**Admission control is declared, not coded.** Both were unimplemented on every previous rung:

| Concern | Mechanism |
|---|---|
| Two concurrent writers on one scenario's rows | `ConcurrencyExpression(input.concurrency_key, max_runs=1, GROUP_ROUND_ROBIN)` |
| A double-clicked Save starting two identical runs | `TTLBasedIdempotencyConfig(key_expression="input.dedup_key")` |

`GROUP_ROUND_ROBIN` keeps one writer active for a scope while retaining later
edits in the engine queue. Cancelling the in-flight run would be unsafe because
cancellation is cooperative and a blocking psycopg call can still be writing.

⚠ `dedup_key` is a hash of the scenario, AOI, code version, dirty set **and feature scope** — not just the scenario. Keying on scenario alone deduplicates two *different* edits inside the TTL, so the second edit's changes are never computed. A collision returns the existing run's id rather than raising, because a dedup hit is the feature working.

**Connection requirements.** `DATABASE_URL_DIRECT` is mandatory for submission
and for the worker: the submitter verifies that its planning connection and the
worker DSN name the same database, and unit runners open their own direct
connection. `HATCHET_CLIENT_TOKEN` is required when a client or worker is
actually constructed; importing the DAG module remains safe for pure tests.

**A worker is mandatory.** Not an optimisation: nothing executes without one, and a submitted run is not even *observable* until a worker has registered the workflow — `runs.get()` returns 404 on an id the engine just minted (spike B0).

---

## The two tracks

Both branches keep everything in the port-level row above and replace everything in the engine-level row. They are **opposite corners of the same row**: the same durability guarantee (a checkpointed workflow), the opposite deployment shape. Both are MIT with no gated features.

### Track A — `feat/ops-plane-dbos`

DBOS Transact: durable execution as a **library**. Zero standing services; the workflow is ordinary Python control flow and checkpoints land in the same Postgres as the domain data, in schema `dbos`.

- **We keep the scheduler.** `orchestration/wave.py` survives; only durability is borrowed.
- Run and unit status stay in `jobs_runtime.orchestration_run` + `job_unit`, so `skipped` and `partial` keep their fidelity and `runstore.py` stays.
- Progress via `DBOS.write_stream` — ordered and offset-resumable.
- An interactive "no worker" mode is natural, the way `embedded` is today.
- No dashboard. Cross-node recovery needs roughly twenty lines of our own sweep code.

### Track B — `feat/ops-plane-hatchet`

Hatchet, self-hosted: durable execution as a **service**. The engine owns scheduling *and* run state.

- One static DAG built at import from the indicator registry, using `parents=` / `skip_if=`.
- **We delete our scheduler.** `wave.py` and `runstore.py` both go, and migration r033 drops `orchestration_run` + `job_unit`. That deletion is the entire reason a service track exists.
- Postgres as the broker (`SERVER_MSGQUEUE_KIND=postgres`); no RabbitMQ.
- A standing worker is mandatory — there is no in-process mode.
- Dashboard included.
- **The vocabulary survives; the queryability does not.** Hatchet's status *enum* has no `partial` and no `skipped` — measured, spike B7. An earlier draft of this document concluded the words were therefore lost. They are not: the workflow's `on_failure_task` reconstructs a per-unit verdict from `task_run_errors`, `was_skipped` and `task_output`, and `get_run` reads that in preference to the enum (spike B8). What is actually lost is *querying across runs* — aggregates, joins and indexes over run history. `additional_metadata` narrows it to a lookup key, so "every run for this scenario" still works; "how many scenarios partially failed this week" does not.
- ⚠ Reconstructing a verdict from three partial signals has more corner cases than mapping an enum. A dependent the engine cancelled has no error *and* `was_skipped=False`, so it reads as a success unless absence of output is also checked (spike B10). The library track pays none of this, because it writes the status it means when it means it.

### The ladder this supersedes

`docs/product/PHASE3_ORCHESTRATION_EXPLORATION.md:452` framed the choice as a monotone ladder — `SKIP LOCKED` → procrastinate/PgQueuer → Hatchet. That framing is retired. It collapsed two orthogonal axes (durability substrate × deployment shape) into one line, and it was not monotone in operational cost: a durable-execution library demands *less* than a task queue plus a hand-rolled scheduler, not more. Track A sits below the current engine on operational cost while sitting above it on durability.

---

## What Phase 0 built

`phase0/ops-plane-seam` produced the shared core both branches consume, on `main`, before either branch exists:

- the port + factory seam (`orchestration/port.py`, `factory.py`) — so no caller names an engine;
- the shared submit prelude (`orchestration/plan.py`) — so both branches decide run contents identically;
- the module splits that make the fates separable: `wave.py` out of `coordinator.py`, `frames.py` out of `events.py`, `memstore.py` out of `synchronous.py`;
- the conformance suite (`tests/conformance/`), whose assertions are meant to read identically on both branches and whose `IMPLEMENTATIONS` table is the branch-specific half each branch edits (`tests/conformance/conftest.py:1-7`, `test_port_contract.py:1-12`);
- migration r031 — the cancellation seam (`job_run.run_id`, `job_run.backend_pid`), which works on any engine because it is addressed to Postgres, not to a scheduler;
- `docs/product/PHASE3_OPS_PLANE_MEASUREMENT.md` — the metric definitions, fixed before either branch exists so the numbers can be put side by side.

---

## Read/write boundary

Unchanged by any of this, and the reason an engine swap is safe:

- canonical baseline is written in `curated_geo`;
- scenario edits live in `curated_geo.scenario_override` — one row per `(scenario_id, feature_id, attribute_key)`, exactly one of `value_numeric` / `value_text` (r030);
- the effective value of an attribute is `override ⊕ current observation ⊕ base feature.attributes`, first non-null wins, resolved in SQL by `scenarios.overrides.effective_numeric` (`overrides.py:50`); baseline collapses to `observation ⊕ base`;
- runners apply that overlay on read and never write back to feature tables;
- computed outputs go to `curated_geo.indicator_result`, each citing its `job_run_id`.

`compute_planned_floors` (`indicators/planned_floors.py:30`) is the worked example: it reads the effective `floors` through `effective_numeric` and writes results inside a `job_run` context — the whole what-if mechanism, with no baseline mutation.

⚠ The previous version of this document described this boundary in terms of `app_core.intervention_feature.attribute_overrides` and `app_core.configuration`. Neither table exists — `app_core` contains only `project` (r001:27) — and the mechanism is `curated_geo.scenario_override`.

---

## Rollup — a direct call, not an orchestrated unit

`reimagina.jobs.rollup.run_rollup` (`jobs/rollup.py:38`) aggregates `indicator_result` rows from a fine grain to a coarse one via `curated_geo.feature_overlap`, applying the indicator's `aggregation_method` with math from the pure `reimagina.jobs.aggregation` module.

**It is not part of a run.** It is not in `SHIPPED_INDICATORS`, so `default_runners` never binds it (`runners.py:76-90`) and no `plan_wave` cascade can dispatch it. Callers invoke it directly from the CLI or an integration test (`jobs/rollup.py:10-12`). It opens its own `job_run` with `model_family="rollup"` (`rollup.py:75-79`), so its outputs carry provenance like any other compute.

Behaviour worth keeping on the record:

- Weights are `feature_overlap.overlap_measure` — the absolute intersection (m² for polygon rollups) — not `overlap_ratio`, which collapses to ~1.0 for fully-nested children and degrades a weighted mean into an arithmetic one (`interfaces.py:445-449`).
- Missing-data policy is shared with the AOI-summary worker in `aggregation.aggregate_children` (`aggregation.py:108`): sum weights, classify the missing share against a threshold defaulting to 0.5, drop missing children, aggregate the rest.
- Idempotency comes from the write path, not from the worker: `write_result` stales any existing `current` row for the same `(feature, scenario, indicator)` before inserting, and the partial unique indexes `ir_one_current_base` / `ir_one_current_scenario` (r016:193, `:198`) enforce it in the database.
- Stale-propagation from below is still not automatic: staling a child does not stale its parent rollup, and the caller that staled the child must re-run the rollup.

---

## Migration order

The job layer's tables are created in this order, and the dependency is real: `indicator_result.job_run_id` is `NOT NULL` with an FK, so `job_run` must exist first.

| Revision | What it adds |
|---|---|
| r001 | schemas `app_core`, `jobs_runtime`, `curated_geo`, `serving_geo`; `app_core.project` |
| r016 | `jobs_runtime.job_run`, `indicator_catalog`, `indicator_dependency`, `indicator_result`, `serving_geo.current_indicator_v` |
| r028 | `jobs_runtime.orchestration_run`, `jobs_runtime.job_unit`, `job_run.procrastinate_job_id` |
| r029 | schema `procrastinate` (engine-level) |
| r030 | `curated_geo.scenario_override` |
| r031 | `job_run.run_id`, `job_run.backend_pid`, `job_run_run_idx` — the cancellation seam |
| r033 (Track B only) | drops `orchestration_run` + `job_unit` |

---

## Decisions on the record

| Decision | Why |
|---|---|
| A narrow port, engine types never leaked | The engine is expected to change twice; callers must not (`port.py:1-12`) |
| One factory names the engine | An ops-plane swap is a rewrite of one file, and tests patch a seam rather than a class path (`factory.py:1-11`) |
| Submit prelude outside every engine | An engine replaces the *execution* of a run, never the decision of what it contains (`plan.py:5-7`) |
| Impact engine is pure | Dirty-set derivation is unit-testable with no DB and survives every engine (`impact.py:11-13`) |
| Partial success, not all-or-nothing | One unit's failure must not abort a run, only its dependents (`coordinator.py:166`) |
| `noop` is return-only | No status CHECK has it, and no run row exists to give it to (`plan.py:19-22`) |
| Execution state on run/unit, not on `indicator_result` | Execution lifecycle and result lifecycle are different concerns |
| `job_run` opened at compute time by the runner | Provenance belongs where the compute happens (`coordinator.py:17-19`) |
| Cancellation needs `pg_cancel_backend` too | No engine can interrupt a thread blocked in a psycopg C call (`r031:3-8`) |
| Terminal statuses are immutable, and guards return whether they fired | A cancelled unit must not be resurrected by its own worker's error handling, and a refused write must not be announced (`runstore.py:70-81`) |
| Never update a failed `job_run` row in place | Append-only audit trail; a re-run opens a new row. A convention, not a constraint — nothing in the schema enforces it |

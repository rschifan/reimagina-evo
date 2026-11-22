# Job Layer

How the platform executes indicator computation — the `Orchestrator` port, the run state it persists, and the engine behind it. The engine is the part that has now been replaced once and is replaced again on the other branch; the port is the part that is not.

**Related documents:** `SCENARIOS_LAYER.md` — scenarios and overrides that trigger runs. `INDICATORS_LAYER.md` — indicator results that runs produce. `DATA_LAYER.md` — canonical city data that runs read from. `ASSET_LAYER.md` — the asset graph whose freshness plans a run and whose versions deduplicate one. `OPS_PLANE_RUNBOOK.md` — operating it.

---

## What this layer does

A computation is a **run**, not a queue message. The application calls one method on one interface; what happens behind it — an in-process loop, a Postgres task queue, a durable-execution library, a workflow service — is the engine's business and is expected to change. It already did: this branch replaced a Postgres task queue with **DBOS**, durable execution as a library, and no caller changed.

```
caller (apps/api/routers/jobs.py, apps/api/routers/scenarios.py, apps/cli/indicators.py)
  │
  ├─ get_orchestrator(mode)          factory.py:54 — the only place an engine is named
  ├─ submit_run(conn, changed_attributes=…, edited_features=…, scenario_id=…, indicators=…)
  │     │
  │     ├─ plan_run                  plan.py:99  resolve change-set → dirty-set, in dep order
  │     ├─ _persist_plan             dbos_orchestrator.py:123 → jobs_runtime.run_plan (feature ids)
  │     ├─ project units to JSON     dbos_orchestrator.py:256  (slug, features=[], deps)
  │     └─ enqueue named run_workflow on DBOS `runs` queue   dbos_orchestrator.py:296-346
  │
  ◄─ RunHandle(run_id, status=None)  port.py:134 — durable already; no envelope yet, only run_plan rows
  │
  │   run_workflow(run_id, units, params)                   dbos_app.py:903
  │     ├─ s_open_run                dbos_app.py:691  → orchestration_run + job_unit
  │     └─ loop: plan_wave → enqueue newly-ready units, DBOS.wait_first, settle one
  │           unit_workflow          dbos_app.py:877
  │             ├─ s_set_status running          dbos_app.py:719 → runstore.set_unit_status
  │             ├─ s_compute                     dbos_app.py:780
  │             │     ├─ run_plan.features_for(plan_id, slug)
  │             │     └─ runner(conn, features=…, scenario_id=…)     runners.py:132
  │             │           └─ job_run(…)  jobs/_lifecycle.py:54 → jobs_runtime.job_run
  │             │                  └─ writes curated_geo.indicator_result citing job_run_id
  │             └─ s_set_status succeeded | failed
  │        ├─ _emit                  dbos_app.py:671 → DBOS.write_stream, from the body only
  │        └─ s_finalize, s_release_plan, close_stream   dbos_app.py:774, :755, :1036
  │
  ├─ get_run(conn, run_id)           → RunStatus + per-unit views
  ├─ cancel_run(conn, run_id)        → bool; see Cancellation
  ├─ list_enqueued_runs / forget_runs → the engine-side half of the run list and of deletion
  └─ subscribe_progress(run_id)      → RunEvent stream, framed as SSE by the route
```

Three things follow from that shape and are stated plainly here because earlier drafts of this document assumed the opposite of the first two, and code was written against those assumptions:

**There is no job message and no job claim.** Nothing in this repository enqueues a per-model-family envelope, and no worker races to `UPDATE … WHERE status='queued'`. `JobRunRepository` says so in the interface itself — "Ops-plane scheduling and claiming belong to the ops-plane, not here — there is no claim_job" (`packages/py/reimagina/storage/interfaces/jobs.py`). `job_unit` rows are a status projection the workflow writes; nothing ever claims one.

**`job_run` is not opened at submit.** `submit_run` returns before any compute exists; the `job_run` row is opened later, by whatever runs the compute, straight to `running` (`jobs/_lifecycle.py:71-78`, `storage/postgres/repositories.py:2125`). A run that is submitted but never executed leaves no `job_run` row at all.

**Neither is the run envelope.** `orchestration_run` and `job_unit` are written by the workflow's own first step (`dbos_app.py:942` → `:691`), not by the submitter. That is a deliberate inversion of the queue-era shape and the reason `get_run` needs a fallback — see [The engine is the existence oracle](#the-engine-is-the-existence-oracle). The submitter writes one thing of its own before the enqueue: the resolved feature ids, to `jobs_runtime.run_plan` — see [Run state](#run-state--four-tables-two-owners).

---

## Permanent vs replaceable

The project forked into two branches, each a complete reference implementation of a different ops-plane. They never merge; the deliverable is the side-by-side. **This is the library branch.** Which half of this layer a module belongs to therefore decides whether it is a design commitment or an implementation detail.

| Fate | Modules |
|---|---|
| **Port-level — permanent, kept on both branches** | `orchestration/port.py`, `factory.py`, `plan.py`, `graph.py`, `impact.py`, `frames.py`, `coordinator.py`, `synchronous.py`, `memstore.py`, `intake.py`, `runners.py`; `indicators/*`; `jobs_runtime.job_run` + `jobs/_lifecycle.py`; `jobs_runtime.run_plan` |
| **Engine-level — this branch's own, replaced on the other** | `orchestration/dbos_app.py`, `dbos_orchestrator.py`, `dedup.py`, `executor_health.py`, `reclaim.py`; `jobs_runtime.ops_executor_heartbeat` (r034); `apps/worker/` |
| **Split fate — this *is* the comparison** | `orchestration/wave.py` — **survives here**. `orchestration/runstore.py` + `jobs_runtime.orchestration_run` + `jobs_runtime.job_unit` — **survive here**. All of it is what a branch whose engine owns scheduling and run state deletes instead |

"Kept on both branches" is a statement about which files exist, not that their contents match. The other branch (`feat/ops-plane-hatchet`, last changed 2026-08-11) carries every port-level module, but its copies have diverged from these since: its `Orchestrator` port, for one, still declares four methods and no `list_enqueued_runs` or `forget_runs`.

The split-fate row is no longer a hedge about the future; it is this branch's answer. Keeping the wave scheduler and keeping our own run tables are two decisions, both taken here, and the measurement spec names the first as a reported metric — "one branch keeps the wave scheduler, the other deletes it" (*Ownership delta*).

The module docstrings carry their own fate, deliberately, so the split survives a file move: `wave.py:3-6` ("this is the half an engine can replace"), `runstore.py:4-6` ("this module, and both tables, are among the things an ops-plane branch may delete outright"), `frames.py:3-7` ("surviving that deletion untouched is the point"), `memstore.py:5-8` ("an ops-plane that drops those tables can delete `runstore`").

The queue engine's own modules — the task definitions, the queue app, the LISTEN/NOTIFY transport, the queue orchestrator, the embedded orchestrator and the queue bench — are gone from this branch, and migration r032 drops the schema they used. `frames.py` was split out of the notify module before the deletion and is untouched by it, which is what that split was for.

---

## The port

`packages/py/reimagina/orchestration/port.py` — six methods, plain dataclasses, no engine types.

```python
class Orchestrator(Protocol):  # port.py:167
    def submit_run(
        self,
        conn,
        *,
        changed_attributes,
        edited_features,
        scenario_id=None,
        aoi_id=None,
        code_version="dev",
        indicators=None,
    ) -> RunHandle: ...
    def get_run(self, conn, run_id: str) -> RunStatus | None: ...
    def list_enqueued_runs(self, limit: int = 50) -> list[RunStatus]: ...
    def forget_runs(self, run_ids: Sequence[str]) -> None: ...
    def cancel_run(self, conn, run_id: str) -> bool: ...
    def subscribe_progress(self, run_id: str) -> AsyncIterator[RunEvent]: ...
```

`indicators` names the units to compute directly and skips change-set
derivation — the "recompute exactly these" request, which neither a change-set
nor freshness can express. It is an optional keyword rather than a separate
port method on purpose: every implementation already resolves its plan through
`plan_run`, so honouring it is a pass-through, while a new method would be an
obligation each ops-plane has to implement (`port.py:179-186`).

`list_enqueued_runs` and `forget_runs` exist because the engine is the
existence oracle (see [below](#the-engine-is-the-existence-oracle)).
`list_enqueued_runs` returns runs the engine holds that no step has opened an
envelope for yet — without it a run nothing has dequeued, because no worker is
running, would be invisible everywhere but its own URL; `GET /jobs` folds them
into the run list as `queued` (`port.py:191-197`, `apps/api/routers/jobs.py:718-755`).
`forget_runs` drops runs from the engine after the caller has dropped our rows,
because a run deleted from our tables alone would come back as a ghost through
`get_run`'s fallback; `DELETE /jobs/{run_id}` and `DELETE /jobs` call it before
they commit (`port.py:199-205`, `jobs.py:622-682`). The DBOS implementation is
`DBOSClient.delete_workflows(..., delete_children=True)`
(`dbos_orchestrator.py:443-452`); the synchronous one has nothing to list or
forget (`synchronous.py:132-137`).

| Type | Shape | Note |
|---|---|---|
| `RunHandle` | `run_id`, `status: RunStatus \| None`, `deduplicated: bool = False` | `port.py:134`. A blocking implementation populates `status` (`synchronous.py:117`); the DBOS one leaves it `None` and the caller polls `get_run` (`dbos_orchestrator.py:366`). `deduplicated` is True when `run_id` names work that was already outstanding — see [Deduplication](#deduplication--one-identity-for-one-piece-of-outstanding-work). |
| `RunStatus` | `run_id`, `status`, `units: tuple[UnitStatusView, …]`, `submitted_at: datetime \| None` | `port.py:123`. `submitted_at` is the engine's own creation time, set only where the engine is the sole record (no envelope yet). |
| `UnitStatusView` | `indicator`, `status`, `detail` | `port.py:116` |
| `RunEvent` | `run_id`, `indicator \| None`, `status`, `detail` | `port.py:151`. Data, never a pre-formatted SSE string — framing is the route's job. |

The port also defines `OpsPlaneNotProvisioned` (`port.py:45`), the engine-neutral "the ops plane's storage does not exist in this database" error, and `DBConnection` (`port.py:95`), the structural Protocol (`cursor`, `commit`, `rollback`, `close`) every `conn` parameter is typed with.

Two signatures encode decisions worth keeping:

- **`submit_run` takes a connection, `subscribe_progress` does not.** Every engine has its own progress transport, and none of them is the caller's pooled connection (`port.py:223-226`). Here it is a DBOS durable stream read through a `DBOSClient` that opens its own (`dbos_orchestrator.py:54-106`, `:631`).
- **`cancel_run` promises to stop *scheduling*, not to stop *compute*.** "no engine can interrupt a thread blocked in a psycopg C call, so a unit already executing runs to its next boundary. Killing the backend is a separate mechanism" (`port.py:211-213`). See [Cancellation](#cancellation).

### The factory

`orchestration/factory.py` is the one place a concrete orchestrator is named. Callers ask for a mode; nothing outside that module imports an implementation class, and tests monkeypatch `get_orchestrator` rather than a class path only one engine has (`factory.py:1-6`). Its imports are deferred on purpose: the engine pulls its client library in at module scope, and `sync` must stay importable without it (`factory.py:30-31`).

| Mode | Implementation | Behaviour |
|---|---|---|
| `dbos` (default) | `DBOSOrchestrator` | Submit a durable workflow and return. The production default. `factory.py:68-71` |
| `sync` | `SynchronousOrchestrator` | Compute in-process, no engine. No production caller — it exists so the conformance suite has a target needing neither engine nor database, and it is the one path that still runs when the ops-plane is broken. `factory.py:73-76` |

**Two modes, not four** (`MODES`, `factory.py:41`). `detach` and `embedded` used to be *engines* — enqueue and return, versus enqueue and drain a bounded in-process worker. Under a durable-execution library that is not an engine choice at all, because one process can do both, so asking the factory for either now falls through to the unknown-mode raise (`factory.py:20-23`, `:78-80`).

⚠ **The CLI still uses both words one layer up, and that is not an oversight.** `embedded` explicitly creates a `DBOSOrchestrator(execution_mode="dev")`, which launches a local executor and waits. `detach` uses the normal `dbos` client mode and returns after enqueueing for a worker. They are caller-facing execution choices, not factory modes (`apps/cli/indicators.py:209-221`).

Mode resolution is `argument → REIMAGINA_ORCHESTRATOR → "dbos"`, and an unknown mode raises rather than falling back (`factory.py:56-57`, `:78-80`).

**The environment may only select a durable mode.** `DURABLE_MODES = ("dbos",)` (`factory.py:43`); `REIMAGINA_ORCHESTRATOR=sync` is rejected with an explanation rather than honoured. `sync` keeps run state in memory *per orchestrator instance*, the factory builds a fresh instance per call, and `POST /jobs`, `GET /jobs/{run_id}` and the SSE route each build their own — so as a deployment default a run would live in an instance no later request can reach. It stays available as an explicit argument, for a caller that holds on to the instance (`factory.py:44-51`, `:58-66`).

The API no longer picks a mode at all: every route calls `get_orchestrator()` with no argument. It is a DBOS **client** by default and queues named workflows for workers; `wait: true` only changes the response shape to 200 after polling the persisted run (every 0.25 s, for at most 300 s, then 504 — `apps/api/routers/jobs.py:252-276`). `dev` is the explicit local one-process exception: set by `REIMAGINA_DBOS_EXECUTION_MODE=dev`, or passed as `DBOSOrchestrator(execution_mode="dev")`, which is how the CLI's `embedded` mode gets it.

---

## The submit prelude

Everything between "a scenario changed" and "run it" is engine-independent, so it lives outside every engine: "an engine replaces the *execution* of a run, never the decision of what the run contains" (`orchestration/plan.py:5-6`). Swapping the ops-plane did not touch the dirty-set logic at all, which is the claim that split was made to be able to test.

**`plan_run`** (`plan.py:99`) resolves the effective change-set and derives the dirty-set, returning a `RunPlan(units, params)` where `params` is exactly `{scenario_id, aoi_id, code_version}` — kept as a dict because both `run_dirty_set` and `runstore.create_run` take them as keywords and an engine has to serialize them (`plan.py:44-47`). Here they are serialized into the workflow's input together with a `plan_id` (`dbos_orchestrator.py:268-274`).

It takes one of four paths, first match wins (`plan.py:153-211`):

| Request | Dirty set | Feature scope |
|---|---|---|
| `indicators` named | exactly those, in dependency order over the induced subgraph; an unregistered slug raises `UnknownIndicators` (a 422 at the API) | empty — every feature |
| an explicit change-set (`changed_attributes` or `edited_features`) | `impact.compute_dirty_set` over it | the edited features, propagated downstream |
| neither, asset graph present | freshness: the indicators whose materialized version is behind the version their inputs imply, at the scenario's partition (`assets.freshness.stale_units`); on the baseline only `stale` and `partial` count, never `absent` | empty — every feature |
| neither, no asset graph | `compute_dirty_set` over the diff `resolve_change_set` derives from the scenario's override rows | that diff's features |

`scenarios.overrides.resolve_change_set` (`overrides.py:232`) returns the explicit lists when given, else derives them from the scenario's `curated_geo.scenario_override` rows. An override that edits `attribute_key` *is* the `changed_attributes` token the impact engine keys off, so no translation layer is needed (`overrides.py:9-10`).

The callers pick their path. `POST /jobs` passes whatever the body names, and resolves a `changed_datasets` list to `indicators` first (`impact.indicators_for_datasets`, below). `POST /jobs/{run_id}/rerun` names the source run's non-succeeded indicators. `POST /scenarios/{id}/recompute` passes the scenario's **footprint** as an explicit change-set — every block a current rule overrides plus every block whose override that materialisation removed, with their attributes — so a scenario recompute is feature-scoped rather than whole-AOI (`apps/api/routers/scenarios.py:928-936`, `:972-979`). The CLI's ingest trigger passes no change-set by default and gets freshness (`apps/cli/indicators.py:92-147`). `POST /jobs/preview` runs `plan_run` as a dry run and persists nothing (`apps/api/routers/jobs.py:416-480`).

**Noop is a first-class answer.** An empty dirty-set means nothing was invalidated, which is not a run that succeeded at doing nothing. `noop_handle` (`plan.py:214`) is the one shape every implementation returns for it, and `noop` is deliberately **return-only** — `orchestration_run.status` has no such value and a noop persists no run (`plan.py:19-21`). No workflow is started either (`dbos_orchestrator.py:244-245`). The API suppresses the id rather than handing back one that 404s (`apps/api/routers/jobs.py:179-187`, and the scenario recompute at `scenarios.py:987`), and so does the CLI (`apps/cli/indicators.py:148-150`, `:231-240`).

### Impact engine — what to recompute

`orchestration/graph.py` is the library-free graph contract shared by both tracks. It provides deterministic topological ordering, reverse edges, and downstream closure over plain `slug -> prerequisite slugs` mappings. `orchestration/impact.py` remains pure: it maps indicator metadata and feature scope onto those primitives, with no DB or engine dependency.

`compute_dirty_set` (`impact.py:97`):

1. **Seed** — every indicator whose declared `reads` intersects the changed attributes (`impact.py:130`).
2. **Propagate** — anything depending transitively on a dirty indicator is dirty, via reverse edges (`impact.py:133`, over `graph.dependents_of` and `graph.downstream_closure`).
3. **Order** — topological over the dependency graph restricted to the dirty set, so inputs precede dependents; a cycle raises `graphlib.CycleError` (`impact.py:110-112`, `:158`).

**Step 3's sorting is load-bearing, and this engine is why.** Feeding the topological sorter a set made the output depend on `PYTHONHASHSEED` — measured at ten distinct orders across ten processes on a 20-indicator DAG, all topologically valid, which is exactly what made it dangerous. A durable-execution ops-plane replays a workflow by matching steps positionally, so a recovering process that re-derived a different order would resume against the wrong checkpoints. Both the nodes and the predecessor lists have to be sorted; sorting only the keys took it from ten orders to two, "the kind of partial fix that reads as success" (`impact.py:138-157`, and DBOS spike 3). The sorting itself now lives in `graph.topological_order`, so every caller gets it (`graph.py:41-72`).

Feature scoping rides along: a seed unit gets `edited_features`, a dependent gets the union of its dirty inputs' feature sets, and each passes through an injectable `expand` hook that is the identity today (`impact.py:42-51`, `:160-170`). The hook exists because proximity and rollup indicators need the feature graph, which is not derivable from the registry.

The output is `list[DirtyUnit]` — `(indicator, features)`, where an empty feature set means "every feature this indicator applies to" (`impact.py:29-39`).

`indicators_for_datasets` (`impact.py:54`) answers the question a change-set cannot: "this whole dataset changed". It seeds from each indicator's typed `inputs` edges rather than from `reads`, which drops the dataset, then propagates and orders the same way. `POST /jobs` and `POST /jobs/preview` use it for `changed_datasets` and submit the result as an `indicators` list.

### Wave scheduler — what may run now

`orchestration/wave.py` is the DB-free readiness rule for a distributed engine, and it **survives on this branch** — the library gives durability, not scheduling, so the waves are driven from application code (`wave.py:3-6`). `plan_wave(units, registry)` takes `(slug, status)` pairs for one run and returns a `WavePlan`:

| Field | Meaning |
|---|---|
| `dispatch` | queued units whose dirty inputs all succeeded → run now (`wave.py:64`) |
| `skip` | queued units with a failed/skipped/cancelled input → mark skipped (`wave.py:62`) |
| `final_status` | set once every unit is terminal under the projected statuses, else `None` (`wave.py:66-74`) |

The readiness rule is stated twice on purpose, in two shapes: `plan_wave` answers "given these statuses, what moves now", while the in-process executor applies the same rule inline while walking a pre-sorted list (`wave.py:8-11`). Imports flow `wave → coordinator → port`, so there is no cycle: `coordinator` owns the graph derivation (`_dep_map`, `coordinator.py:97`) and `port` owns the status vocabulary.

`plan_wave` is pure and was cleared for replay in the same spike that caught the ordering bug — given a fixed input it is byte-identical across processes, so it inherits order rather than generating it. That is what lets `run_workflow` call it inside a workflow body.

### In-process executor

`coordinator.run_dirty_set` (`coordinator.py:102`) is the single-process executor and the `sync` path. Per unit, in dependency order:

- any *dirty* input that did not succeed → **skipped** (non-dirty inputs keep their current value and do not block) — `coordinator.py:132-146`;
- no runner registered → **skipped** — `coordinator.py:148-153`;
- otherwise run it; an exception isolates to that unit → **failed**, and its dependents skip on later iterations — `coordinator.py:155-168`.

Fault isolation is the point: independent units do not block each other, and a failure stops only what depends on it. The result is a `RunReport`, whose `status` rolls up through the shared `overall_status` — except for the empty case, which is `noop` (`coordinator.py:89-91`).

Runners are injected, never defined here. `orchestration/runners.py:132` (`default_runners`) binds slugs to compute functions: direct indicators to their bespoke function, composites to the generic `compute_composite` keyed by spec. The adapter passes a unit's `features` on as `feature_ids` (empty means none, so the compute falls back to `aoi_id` scoping) and records an asset materialisation around the compute, complete only when the run was neither feature- nor AOI-scoped (`runners.py:58-109`). A runner owns its own `job_run` row — provenance lives where the compute happens (`coordinator.py:17-19`).

`orchestration/intake.py` (`evaluate_scenario`) is `plan_run` + `run_dirty_set` in one call. Its only caller is the CLI's `evaluate-scenario --sync`, which wants the coordinator's per-unit `RunReport` rather than the port's `RunStatus`.

---

## Run state — four tables, two owners

| Table | Owner | Written by | Fate |
|---|---|---|---|
| `jobs_runtime.orchestration_run` | ops-plane | `runstore.create_run` / `finalize_run` | kept here; dropped by an engine-owned ops-plane |
| `jobs_runtime.job_unit` | ops-plane | `runstore.record_units` / `set_unit_status` | kept here; dropped by an engine-owned ops-plane |
| `jobs_runtime.job_run` | domain | `jobs/_lifecycle.job_run`; `record_failed_job_run` for a unit whose transaction was rolled back | survives every branch |
| `jobs_runtime.run_plan` (r036) | ops-plane | `RunPlanRepository.write` at submit (`dbos_orchestrator._persist_plan`); read by `features_for`; removed by `delete` / `sweep` | the resolved feature set, one row per dirty indicator, `feature_ids uuid[]`. Written once and committed before the enqueue, read by each unit, deleted when the run is terminal (`s_release_plan`). Orphans — a submit that crashed, a plan abandoned by a deduplicated submit — are swept on the next submit once older than `REIMAGINA_RUN_PLAN_TTL_HOURS` (default 24), sparing any plan whose workflow is still `ENQUEUED`, `PENDING` or `DELAYED` (`dbos_orchestrator.py:123-162`, `storage/postgres/repositories.py:3100`). Exists on **both** branches: carrying the ids in the workflow input made a full-city run 258,675 uuids, which Hatchet's 4 MiB gRPC limit refused outright and DBOS merely persisted (`r036_run_plan.py:12-20`). Claim Check |

### orchestration_run + job_unit (r028, + `aoi_id` in r029)

One `orchestration_run` row per run — the envelope `get_run` reads. One `job_unit` row per `DirtyUnit`, unique on `(run_id, indicator_slug)`, with a `feature_scope` jsonb column (`r028_orchestration_run_and_job_unit.py:26-59`). `orchestration_run.aoi_id` arrives in r029 (`r029_procrastinate_schema.py:26`), which is the half of that mixed revision every branch keeps.

⚠ **`job_unit.feature_scope` is always `[]` on this branch.** `s_open_run` records each unit's `features` from the workflow input, and every current submitter sends that empty; the unit's real scope is `run_plan.feature_ids` (`dbos_orchestrator.py:259-263`, `dbos_app.py:708-715`). Only the synchronous orchestrator, given `store=runstore`, would write a real scope, and nothing does.

Both are written only through `orchestration/runstore.py`, which no-ops for a non-Postgres connection so unit tests need no database (`runstore.py:24-28`). `orchestration/memstore.py` mirrors the signatures the synchronous orchestrator calls (`create_run`, `record_units`, `finalize_run`, `get_run`) in memory so either can be injected without an adapter — which is what keeps the synchronous orchestrator from dragging `jobs_runtime` along with it (`memstore.py:1-8`, `synchronous.py:6-11`). The Ops dashboard reads them through `OpsRunReader` in `reimagina.storage`, deliberately not through `runstore`, so the dashboard outlives an ops-plane that deletes it; the same reader deletes finished runs (`delete_run`, `delete_terminal_runs`; `job_unit` goes by cascade) for `DELETE /jobs/{run_id}` and `DELETE /jobs` (`storage/interfaces/jobs.py:71-134`).

**The two insert paths are idempotent, and that is a requirement of durable execution, not a nicety.** A step only records its output once it *completes*, so a step interrupted partway through starts again from the top on recovery. A bare `INSERT` would then fail on the primary key, permanently — "a run that could not survive a crash during its own first step would defeat the point of durable execution entirely" (`runstore.py:42-51`). Both use `ON CONFLICT … DO NOTHING` rather than `DO UPDATE`, and for `record_units` the choice does more than avoid an error: units that already progressed keep the status they reached, so a re-run does not reset a succeeded unit to `queued` and schedule its minutes of PostGIS a second time (`runstore.py:59`, `:136-139`, `:149`).

**Both mutators are guarded and return whether the write landed**: `set_unit_status` refuses to move a terminal unit (`runstore.py:73-84`, `:95`) and `finalize_run` refuses to re-close a closed run (`runstore.py:161-167`, `:175`). The guards are not hand-written status lists — the SQL takes its terminal tuples straight from the port's frozensets (`runstore.py:19`, `:88`, `:176`), so the vocabulary in `port.py` is literally what the database enforces here.

One column exists but is never written by any code path: `job_unit.job_run_id` (r028:53). r031's docstring records why it matters — the missing unit→run edge is exactly what made cancellation impossible before `job_run.run_id` existed (`r031_job_run_cancellation.py:15-21`). Its companion fossil, `job_run.procrastinate_job_id` (r028:66-68), was never written either and was dropped by r035.

### job_run (r016, extended by r031, trimmed by r035)

Domain provenance, not scheduler state. `curated_geo.indicator_result.job_run_id` is `NOT NULL` with an FK and an existence trigger (`r016_indicator_layer.py:175-176`, `:255-257`), so every result row cites the execution that produced it. That is why `job_run` survives on every branch, including one whose engine owns run state natively — and why r031 hangs the cancellation seam off it rather than off `orchestration_run` (`r031:23-25`). It is also why the measurement spec can define per-run compute time as a single query on this one table.

```sql
-- r016_indicator_layer.py:30-43, plus r031:43-44
CREATE TABLE jobs_runtime.job_run (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id   uuid,                    -- no FK, though app_core.scenario exists since r048: r016:7-8 left it bare and no later migration ties it
  model_family  text        NOT NULL,
  status        text        NOT NULL
                            CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  code_version  text        NOT NULL,
  queued_at     timestamptz NOT NULL DEFAULT now(),
  started_at    timestamptz,
  completed_at  timestamptz,
  error_message text,
  run_id        uuid,                    -- r031; nullable, no FK, deliberately
  backend_pid   int,                     -- r031; pg_backend_pid() at insert
  CHECK (started_at   IS NULL OR started_at   >= queued_at),
  CHECK (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
);
CREATE INDEX job_run_run_idx ON jobs_runtime.job_run (run_id, status) WHERE run_id IS NOT NULL;
```

`run_id` is nullable with no FK on purpose: a direct CLI compute belongs to no run, and an ops-plane that owns run state natively drops `orchestration_run` entirely — which an FK would forbid (`r031:15-21`).

**The row is opened at compute time, straight to `running`.** `jobs/_lifecycle.job_run` (`_lifecycle.py:54`) is the context manager every compute function uses: `start_job(status="running")`, yield the id, then `complete_job('succeeded')` on clean exit or `complete_job('failed', "<ExcType>: <msg>")` on an exception before re-raising (`_lifecycle.py:71-85`). Marking the row failed is best-effort and never replaces the original exception: the compute usually failed by aborting the transaction, so that `UPDATE` can itself raise (`_lifecycle.py:88-120`). `started_at` is set by the repository — `clock_timestamp()`, not `now()`, so a unit's start and end are not the same transaction timestamp — when status is `running`, and `pg_backend_pid()` is evaluated on that same connection because it is the one that will run the compute — "capturing it anywhere else would record a pid nobody needs to cancel" (`repositories.py:2125`, `:2135-2164`).

**A failed unit's row is re-recorded on its own connection.** `s_compute` rolls its connection back when the runner raises, which takes the `failed` row the runner opened with it. It then calls `record_failed_job_run` with the DSN it connected with, the run id and the unit's start time, which writes a fresh `failed` row on a second connection the rollback cannot reach (`dbos_app.py:847-868`, `_lifecycle.py:123-203`). The in-process coordinator keeps its connection and commits, so it does not call it.

**`run_id` arrives ambiently, not as a parameter.** `current_run` is a `ContextVar` an executor sets around the call (`_lifecycle.py:31-50`), because threading the id through the `Runner` protocol, two adapter closures, four compute signatures and every test fake — so a standalone CLI compute could pass `None` through all of it — is worse than an ambient (`_lifecycle.py:7-14`). It must be set **inside** whatever executes the unit; a ContextVar does not cross into a thread the setter did not enter (`_lifecycle.py:40-42`, applied at `dbos_app.py:838` and `coordinator.py:157`).

---

## Cancellation

`cancel_run` needs **three** mechanisms, and none is sufficient alone (`dbos_orchestrator.py:456-542`).

**One — stop scheduling.** `cancel_workflow(run_id, cancel_children=True)`, through the shared `DBOSClient` so a process that is not an executor can cancel a run a worker is executing (`dbos_orchestrator.py:472`). `cancel_children` is not optional here: the units are child workflows, and leaving them schedulable would let the next wave start after the run was closed (`:470-471`). The parent's own loop reads its statuses from an in-memory dict seeded at entry, never re-read from the database — so nothing in the workflow body would notice a cancel on its own.

**Two — stop the statement.** A step already inside a blocking psycopg call keeps going; DBOS observes cancellation only at the next step boundary, which a minutes-long PostGIS statement does not reach. `JobRunRepository.cancel_run_backends(run_id)` issues `pg_cancel_backend` against every live backend of the run (`storage/interfaces/jobs.py`, `storage/postgres/repositories.py`). It finds them by name first: `s_compute` opens its connection with `application_name = 'reimagina-run:<run_id>'` (`dbos_app.compute_connection`), which `pg_stat_activity` shows from the moment it connects. The `job_run` row cannot be the only handle — `s_compute` writes it inside the unit's one transaction, so under READ COMMITTED the canceller cannot see it until the unit has already finished:

```sql
SELECT count(*) FILTER (WHERE pg_cancel_backend(pid))
FROM (
  SELECT pid FROM pg_stat_activity
  WHERE application_name = 'reimagina-run:' || run_id AND pid <> pg_backend_pid()
  UNION
  SELECT backend_pid FROM jobs_runtime.job_run
  WHERE run_id = %s AND backend_pid IS NOT NULL
    AND status NOT IN ('succeeded', 'failed', 'cancelled')
) AS live(pid);
```

`pg_cancel_backend` cancels the *statement*, not the session — the runner's psycopg call raises `QueryCanceled` and its own error handling takes over (`repositories.py:2192-2195`). This is the mechanism r031 exists to make possible, and it is addressed to Postgres rather than to a scheduler, which is why it works on any engine (`r031:1-13`).

**Three — close the run in our own vocabulary.** Every non-terminal unit is marked `cancelled` and the run is finalised (`dbos_orchestrator.py:475-491`). `plan_wave` treats `cancelled` as terminal and unsatisfying, so a unit closed this way neither dispatches nor unblocks its dependents (`wave.py:53`, `:61`, `port.py:35`, `:38`).

**Which creates the problem the terminal guard solves.** The statement-cancel error handling then tries to write `failed` over a unit already marked `cancelled`, resurrecting a run that was closed. `runstore.set_unit_status` refuses to move a unit out of a terminal state, and that guard is what makes cancellation stick (`runstore.py:73-79`, `:95`).

**And a rule that follows from it: announce only what the database accepted.** The guard protects the table, not the event stream. A worker that publishes the transition it *asked for* rather than the one that *happened* tells every SSE subscriber `failed` for a unit `GET /jobs/{run_id}` reports `cancelled`. That is why `set_unit_status` returns a bool at all — "the return value is not decoration" (`runstore.py:81-84`) — and why `finalize_run` carries the same guard: the first close is the real one, because it is when the run stopped being able to change (`runstore.py:161-167`).

**And `s_settle_unit` is how this engine honours it.** The parent does not announce the outcome it observed; it writes the unit terminal, re-reads what the row actually holds, and announces *that* (`dbos_app.py:728-751`, `:1028-1029`). "The store refuses to move a unit out of a terminal state, so what it holds wins over what we asked for — a cancel that landed first must survive" (`dbos_app.py:749-750`). `cancel_run` applies the same discipline at the run level, checking `finalize_run`'s answer and acting on the pre-envelope case rather than assuming (`dbos_orchestrator.py:491-540`).

The same step also **reconciles a unit its own child can no longer close.** Only the child writes a unit's status, so a child whose process died — and whose recovery attempts ran out — would leave `job_unit` stuck at `running` forever while the parent saw the failure and closed the run. `GET /jobs/{run_id}` would then report a terminal run containing a running unit, contradicting the progress stream, with nothing left to fix it (`dbos_app.py:734-738`).

The skip loop follows the same discipline: it announces `s_settle_unit`'s return rather than the `skipped` it asked for, so a cancel that already closed the unit is not overwritten on the wire.

Order matters: the workflow is cancelled and backends signalled first, then units are closed (`dbos_orchestrator.py:470-484`). A run that is unknown or already terminal returns `False` without side effects (`:466-468`). The final roll-up re-reads the units rather than trusting the snapshot it started from: a unit can reach a terminal state in between, and `set_unit_status` then refuses our `cancelled`, so assuming the write landed would report a cancelled run whose units all say they finished (`dbos_orchestrator.py:486-490`).

**A run cancelled before its first step needs the envelope written *by the canceller*.** `finalize_run` finds nothing to close, and doing nothing there is not safe: DBOS checks for cancellation when a step *begins*, so a step already past that check runs to completion — and `s_open_run` would then create the envelope as `running`, permanently, for a run that was cancelled. So the cancel path writes the envelope itself, already terminal. `create_run` is `ON CONFLICT DO NOTHING`, so the workflow's later attempt is a no-op, and `finalize_run` refuses to move a terminal run. The envelope takes its `scenario_id` and `aoi_id` from the workflow's persisted input, since it is the only envelope the run will ever have, and carries `code_version = 'cancelled-before-start'` to mark a run that never reached `s_open_run`; the cancel path also records every unit as `cancelled`, so the run does not read as zero units and `POST /jobs/{run_id}/rerun` can find them (`dbos_orchestrator.py:491-540`). This is the idempotency of [`create_run`](#orchestration_run--job_unit-r028--aoi_id-in-r029) being used for something other than crash recovery: two writers, either order, one outcome.

Two further defences live inside `s_compute` rather than in `cancel_run`, because they cover the window `cancel_run` cannot reach — a unit that has not yet opened its `job_run` row, and a result write caught mid-flight. Both are described under [Steps](#steps-every-database-touch-lives-in-one).

The synchronous orchestrator always returns `False`: `submit_run` does not return until the last runner has, so no caller ever holds an id for something still in flight (`synchronous.py:139-145`).

---

## Status vocabulary

Python has one definition, in the port, because the port is the module no ops-plane deletes (`port.py:26-28`).

| Frozenset | Members | Meaning |
|---|---|---|
| `UNIT_TERMINAL_STATES` | `succeeded, failed, skipped, cancelled` | no further transition (`port.py:35`) |
| `UNIT_UNSATISFIED_STATES` | `failed, skipped, cancelled` | makes a dependent unrunnable (`port.py:38`) |
| `RUN_TERMINAL_STATES` | `succeeded, partial, failed, cancelled` | closes a progress stream (`port.py:41`) |

These are not advisory. `runstore` imports the first and third and interpolates them into the `UPDATE` guards (`runstore.py:19`, `:88`, `:176`), so the port's vocabulary is what the database enforces — and an engine that keeps `runstore` inherits the enforcement for free. This branch keeps it, which is the whole reason `partial` and `skipped` survive here.

`overall_status(statuses)` (`port.py:73`) rolls unit statuses into a run status:

- all `succeeded` (or empty) → **succeeded**
- any `succeeded` alongside anything else → **partial**
- no failure, at least one cancellation → **cancelled**
- anything else, including an all-skipped run → **failed**

Two judgements are encoded there. `cancelled` only wins when nothing actually failed — a run that both failed and was cancelled is a failure, and calling it cancelled would hide the error. An all-skipped run folds into `failed` rather than gaining a fourth value, because nothing ran and `orchestration_run.status` has no `skipped` (`port.py:76-83`).

It is called from four places on this branch, all reaching the same answer: the workflow's final roll-up (`dbos_app.py:1031`), the cancel path's re-read (`dbos_orchestrator.py:490`), `RunReport.status` on the sync path (`coordinator.py:91`), and `plan_wave`'s `final_status` (`wave.py:70-74`), which `run_workflow` does not read.

### The three database CHECKs disagree

Unifying Python did not unify the schema, and widening the CHECKs is a migration decision, not a refactor (`port.py:30-33`).

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

`noop` appears in none of them, which is precisely why it is return-only (`plan.py:19-21`).

**Execution state lives on the run/unit tables, not on `indicator_result`.** `indicator_result.status` is `current | stale | failed` (`r016:187`) — lifecycle states of a *result*. Unit and run statuses are lifecycle states of an *execution*. Different concerns; still true.

---

## Progress

`subscribe_progress(run_id)` yields `RunEvent`s until the run-level terminal event, then stops. The first event is always a snapshot of current status, so a late subscriber is not left waiting on a run that already finished (`port.py:220-221`, `dbos_orchestrator.py:587-593`). `indicator=None` marks the run-level event carrying the overall status; that is the event that ends the subscription (`port.py:157-158`).

Three layers, three fates:

**`orchestration/frames.py` — permanent.** Payload construction and SSE framing, shared by every implementation. `notify_payload` emits `{run_id, indicator, status}`; `event_frame` wraps it as `data: …\n\n` (`frames.py:17-41`). `RunEvent.detail` is deliberately absent from the wire — no client reads it and `GET /jobs/{run_id}` already carries it per unit (`frames.py:23-25`). It was split out of the LISTEN/NOTIFY module that has since been deleted with the queue ops-plane, and surviving that deletion untouched is exactly the point (`frames.py:3-7`).

**DBOS durable streams — engine-level.** `_emit` writes one `RunEvent`, as a dict, to the workflow's `progress` stream (`dbos_app.py:671-680`, `:56`). Two properties are load-bearing:

- **It is called from the workflow body, never from a step.** A stream write from a workflow is exactly-once; from a step it is at-least-once, and a duplicated terminal event would close a subscriber's stream early (`dbos_app.py:674-676`).
- **The stream is closed explicitly** after the terminal event (`dbos_app.py:1036`), which is what lets a reader end rather than hang.

Measured before the design was fixed: full replay from offset 0 returns the five unit events plus the run-level `None`, a resume at the end returns nothing, no gap and no duplicate.

The subscriber (`dbos_orchestrator.py:546-673`) reads through a `DBOSClient` rather than the ambient `DBOS`, so a reader process need not host the workflow definitions — the API can follow a run a worker is executing (`:549-551`). Four details there are resource-safety, not style:

- **One shared client per system-database DSN, per process, built lazily under a lock.** A `DBOSClient` is not a handle: constructing one builds a SQLAlchemy engine with its own five-connection pool and opens a connection eagerly. Building one per call leaked an engine and a connection every time, on a path polled several times a second while a run is in flight. It is keyed by `DATABASE_URL_DIRECT` rather than held in one global, because a single global pinned the first DSN the process saw and then answered "no such run" for runs in another database. It is never destroyed, because there is no scope to tie it to (`dbos_orchestrator.py:54-106`).
- **`use_listen_notify=True`**, without which `read_stream` degrades to polling once a second — the latency the SSE endpoint exists to avoid (`dbos_orchestrator.py:101-104`).
- **The pump runs on a dedicated daemon thread, not the default executor.** `read_stream` is a *synchronous* generator that blocks until the stream closes, and a future returned by `run_in_executor` cannot be cancelled once its callable is running. An SSE client that disconnected one second into a twenty-minute run used to pin a shared-pool thread for the remaining nineteen; the default executor caps at `min(32, cpu+4)` and is shared with every `asyncio.to_thread` in the process, so roughly thirty such disconnects would deadlock the API's entire thread-offload path. The thread still cannot be interrupted — that is the generator's property, not the scheduler's — but the blast radius becomes one daemon thread that dies with the process instead of a permanent hole in a shared pool (`dbos_orchestrator.py:557-570`, `:629-656`, `:668-673`).
- **The pumps are bounded.** A process-wide semaphore of `REIMAGINA_PROGRESS_SUBSCRIBERS` slots (default 32) caps the daemon threads; past it `subscribe_progress` raises "progress subscriber capacity reached; reconnect shortly". The slot is released by the pump thread when it actually ends, not by the generator, because a disconnected client's generator ends at once while its pump stays blocked in `read_stream`. Events reach the subscriber through a queue of 128; a consumer that falls that far behind has its stream ended, so a slow client becomes a reconnect rather than unbounded memory (`dbos_orchestrator.py:46-51`, `:572-581`, `:607-627`).

The run-level snapshot is taken first, on its own connection, because the port hands `subscribe_progress` none (`dbos_orchestrator.py:675-696`).

**`apps/api/routers/jobs.py` — route policy.** `GET /jobs/{run_id}/events` returns a `StreamingResponse` over `_event_stream` after a 404 check on a short-lived connection of its own, closed before streaming starts, so a subscription does not hold a pooled connection for its whole life (`jobs.py:929-954`). The route owns frame format, a 15 s keep-alive, and a 300 s lifetime cap; the orchestrator owns *what happened*, and where the events come from is not this module's business (`jobs.py:882-888`, `:44-45`). One subtlety worth preserving: a single pending `__anext__` is held across heartbeats rather than re-awaited, because wrapping the await in a timeout cancels it mid-flight and leaves the async generator unresumable — the keep-alive would break the stream it exists to keep open (`jobs.py:895-899`).

---

## The engine — DBOS

Everything in this section is **engine-level**: it is this branch's answer, and the part the other branch replaces. Durable execution as a **library** — zero standing services, ordinary Python control flow, checkpoints in the same Postgres as the domain data under the `dbos` schema. Workers host workflow definitions and execute; API processes use `DBOSClient` to submit, inspect, cancel, and stream without becoming executors.

### The run id is the workflow id

`submit_run` mints a UUID and uses it as the DBOS workflow id. In production the API passes it to `DBOSClient.enqueue` with the named `run_workflow` and the `runs` queue; the worker executes it later. `dev` and executor submitters use `SetWorkflowID(run_id)` with the same identity. This makes a run addressable from any process **without a mapping table**, and DBOS treats the id as its idempotency key. Every cross-process operation is keyed only by `run_id`: workflow lookup, cancellation, stream read, and deletion. The units are child workflows under deterministic ids, `{run_id}-{slug}`.

### The dirty set is a workflow input, never derived inside the workflow

**The rule that shapes everything here.** DBOS matches replayed steps by `(position, function name)`. This design calls the *same* step function once per indicator, so the names always match and **position alone** decides which checkpointed output a replayed call receives (`dbos_app.py:12-18`).

A workflow body that rebuilt its unit list from the catalog would therefore not fail loudly on a catalog change mid-run. It would hand one indicator's checkpointed result to another — no exception, no warning, a plausible-looking result. This was **measured, not theorised**: spike 2 ran two workflows over the same units, one reading a module global and one taking its list as input, interrupted both, changed the registry, and resumed. The module-global workflow returned position 0's request for a new indicator answered with the old indicator's recorded output, and the new one never ran at all.

So the dirty set is derived in `submit_run`, projected to `{slug, features, deps}`, and passed in (`dbos_orchestrator.py:247-274`). `deps` travels with it so the body can rebuild the dependency graph for `plan_wave` without reading the catalog — `_Spec` is the slice of an `IndicatorSpec` the scheduler needs, reconstructed from the workflow's own input (`dbos_app.py:622-637`). `validate_workflow_input` rejects a malformed unit list — an empty or duplicate slug, non-string ids, a dependency cycle within the set — and an empty `code_version` before anything is persisted, because a cyclic set could never make progress and would stay queued forever (`dbos_app.py:640-668`).

`features` is sent empty. The feature ids are the one part of the plan that scales with the city, so they are written to `jobs_runtime.run_plan` and committed before the enqueue, and the input carries only the handle, `params["plan_id"]` (the run id). That keeps the same guarantee by other means: the plan is written once and never updated, so a replay reads the identical set. `s_compute` reads its own slice with `RunPlanRepository.features_for`, and falls back to `UnitInput.features` only for a run submitted before `plan_id` existed (`dbos_app.py:824-837`).

The rule generalises to one line: **the workflow body branches only on workflow inputs and step results** (`dbos_app.py:20-22`). Reading a global *inside a step* is fine, because a step's output is checkpointed; it is the body's control flow that must be reproducible.

### The engine is the existence oracle

`get_run` reads detail from our tables and *existence* from the engine (`dbos_orchestrator.py:370-395`).

A submitted run is durable when DBOS accepts the enqueue, but its envelope is written by the workflow's own first step. There is therefore a window where the workflow exists and `jobs_runtime` has nothing to say about it. Reporting `None` there would break the port's contract that a submitted id is immediately retrievable, so `get_run` falls back to DBOS workflow status in that window.

Nothing has to be reconstructed to do it: the unit list **is** the workflow's input, which DBOS persisted as part of making the run durable, so `_from_workflow` reads it straight back out and reports every unit as `queued`, with the run status mapped from DBOS's (`ENQUEUED`/`DELAYED` → `queued`, `PENDING` → `running`, …) and `submitted_at` from the workflow's creation time (`dbos_orchestrator.py:109-120`, `:165-183`, `:397-421`). On a database with no `dbos` schema the answer is `None`, not an `UndefinedTable` error: no workflow can exist there either (`:406-418`).

**Why writing the envelope at submit would be worse.** It would close that window and open a larger one: a crash between the commit and the enqueue leaves a run marked `running` that nothing will ever drive — a row that lies, permanently, with no process responsible for it. The pre-envelope window is bounded by the first step and self-heals; the orphaned-envelope window does not (`dbos_orchestrator.py:379-383`).

### Steps: every database touch lives in one

A workflow body that read Postgres directly would re-read on replay and could see different data than it decided with. DBOS enforces this — a bare DB call in a body raises `DBOSUnexpectedStepError` (`dbos_app.py:683-687`). Each status step opens its own **autocommit** connection to the direct DSN, so its write is committed when the step returns rather than deferred to a caller's transaction, and closes it on exit (`_pg`, `dbos_app.py:605-619`). `s_compute` is the deliberate exception and opens its own **non-autocommit** one (`dbos_app.py:812`) — see below.

| Step | Does | Definition |
|---|---|---|
| `s_open_run` | writes `orchestration_run` + one `job_unit` per unit | `dbos_app.py:691` |
| `s_set_status` | one guarded unit transition | `dbos_app.py:719` |
| `s_settle_unit` | forces a unit terminal, returns the status the row actually holds | `dbos_app.py:729` |
| `s_release_plan` | deletes the run's `run_plan` rows once it is terminal; best-effort, the TTL sweep catches what it misses. Appended after `s_finalize` so no earlier step position moves | `dbos_app.py:755` |
| `s_finalize` | one guarded run close | `dbos_app.py:774` |
| `s_compute` | one indicator's real compute — minutes of blocking PostGIS | `dbos_app.py:780` |

**`s_compute` is the one step with retries off** (`retries_allowed=False`, `dbos_app.py:779`). A runner exception is deterministic, so a retry would burn the same minutes to reach the same failure; infrastructure faults surface as a failed connect, which is retried by the step that owns it (`dbos_app.py:783-785`). It is also where `current_run` is set, which is what lets a cancel find the work: the runner opens its own `job_run` row, and the ambient run id plus `pg_backend_pid()` are how `cancel_run` locates the backend to signal (`dbos_app.py:787-789`, applied at `:838`).

Two further properties of that step are cancellation-safety, and both are stated in its own docstring:

- **One transaction, committed at the end** (`dbos_app.py:791-798`, `:812`, `:846`). The result write path is stale-then-insert: it flips the previous values to `status='stale'`, then inserts replacements. On an autocommit connection those are two transactions, so a `pg_cancel_backend` landing between them left those features with *zero* `current` rows — and because the workflow is cancelled, recovery never re-runs the step to repair it. `serving_geo` exposes only `current`, so that was a permanent hole in served values. In one transaction a cancel mid-write rolls the flip back with it.
- **Cancellation is re-checked at the top of the step** (`dbos_app.py:800-805`, `:815-823`). `cancel_run` cannot signal a unit that has not yet opened its `job_run` row — no row, no backend pid — and DBOS's own check already fired when the step began. Without this re-read the unit would run its full compute and commit results for a run already reported terminal. It narrows the window rather than closing it: a cancel arriving after the check still has only `pg_cancel_backend`, which is what the row exists for.

**Steps must be idempotent, and `s_open_run`'s are.** A step's output is recorded only once it completes, so one interrupted partway through re-runs from the top — which is exactly why `runstore.create_run` and `record_units` use `ON CONFLICT DO NOTHING` rather than a bare `INSERT`. That idempotency then earns a second keep: it is also what lets `cancel_run` write the envelope for a run cancelled before its first step, without racing the step that may still write it. See [orchestration_run + job_unit](#orchestration_run--job_unit-r028--aoi_id-in-r029) and [Cancellation](#cancellation).

### The workflows

Two, not seven. `run_workflow` drives the dirty-set as each unit unblocks; `unit_workflow` is one indicator.

`unit_workflow` (`dbos_app.py:876-895`) is three steps: mark `running`, compute, mark `succeeded` — or mark `failed` with `"<ExcType>: <msg>"` truncated to 500 characters and re-raise, so the parent sees the failure (`:891-893`, `:898-899`). Its arguments are append-only: DBOS stores them positionally, so inserting, reordering or removing one makes every in-flight child fail on replay (`:880-886`).

`run_workflow` opens the run, then loops: `plan_wave` over the in-memory status map, write and announce the skips, enqueue one `unit_workflow` per *newly* dispatchable unit, then `DBOS.wait_first` over everything still outstanding — settling **one** unit per iteration, through a step, announcing what that step returned. Repeat until nothing is outstanding and nothing was skipped; then roll up, finalise, release the run's plan rows, emit the run-level event and close the stream (`dbos_app.py:942-1037`).

**A unit starts the moment its last dependency clears**, rather than waiting for the slowest member of its level. Measured on the synthetic suite, head-of-line delay fell from 1600-1691 ms to 289-345 ms across every shape with dependencies, and makespan by 1.41-1.42x on the diverging ones. `lattice` and `diamond` are the designed controls: they reconverge, so the barrier never cost them makespan, and it did not move (1.03x).

⚠ **The loop must keep going while skips are still landing, even with nothing running.** `plan_wave` recomputes readiness from the statuses it is handed, so it marks exactly one layer of skips per call: with `a → b → c` and `a` failed, one pass skips `b` and only the next can see `b` as terminal and skip `c`. Stopping as soon as nothing is outstanding stranded `c` at `queued` — permanently, on a run the API reported as finished, since `overall_status` returns `failed` either way.

**`DBOS.wait_first` is what makes as-completed dispatch replayable.** It runs its poll through `call_function_as_step(..., "DBOS.waitFirst", ...)`, so the *completion order itself* becomes durable history — a replay returns the recorded winner instead of re-racing. Every value the loop branches on is therefore checkpointed: `plan_wave` is pure over `statuses`, `statuses` comes from `s_settle_unit`'s recorded return, and the outstanding set moves only on a dispatch or a recorded winner.

⚠ **Why this needed a durable primitive rather than care.** Child workflow ids are positional — parent id plus function id — and DBOS resolves a replayed child by `(parent_id, function_id)` alone. `DBOSUnexpectedStepError` fires only on a function-*name* mismatch, and every child here is `unit_workflow`, so a reordered replay does **not** raise: it hands back a handle to the wrong child and cross-attributes one indicator's result to another. That is spike 2's failure mode in its other guise, and nothing in DBOS detects it.

Children are enqueued under deterministic ids (`{run_id}-{slug}`) and the body asserts the returned handle carries the id it asked for. `SetWorkflowID` does not confer positional freedom — the counter increments regardless — but it makes a re-enqueue past recorded history dedup instead of minting a second child, and the assertion is the only available detector for the silent case above.

**Fault isolation is preserved across the boundary.** A child that raises is caught, recorded as `failed`, and the loop continues; `plan_wave` then skips whatever depended on it (`dbos_app.py:1022-1029`). Exception payloads cross the process boundary intact — spike 4 disproved the plan's assumption that psycopg2 errors carrying a live cursor and `.diag` would not pickle, which is why no `UnitFailed` wrapper exists.

**But the body branches on `s_settle_unit`'s return, not on `get_result`** (`dbos_app.py:1023-1028`). `get_result` *looks* checkpointed and is not: DBOS records it and never reads it back, so a replay re-queries the child's **live** status. If a child were resumed to `SUCCESS` after the parent had already skipped its dependents, the replayed body would take the dispatch branch where the original took the skip branch, the step positions would diverge, and the run would die with `DBOSUnexpectedStepError` — unrecoverable. A step's output *is* genuinely checkpointed, so routing the outcome through one keeps the body reproducible. This is the replay rule from the top of the module applied to the one value that arrives from outside it.

### Configuration and execution roles

`config()` configures DBOS — name `reimagina`, the direct DSN, system schema `dbos`, the executor id, and `application_version` when pinned (`dbos_app.py:311-332`); `launch()` is executor-only. Which a process is comes from `REIMAGINA_DBOS_EXECUTION_MODE`: `client` (the default), `executor`, or `dev`; anything else raises (`dbos_app.py:129-143`).

**A direct DSN is mandatory and fails loudly.** `DATABASE_URL_DIRECT` or a `RuntimeError` — because DBOS falls back to a local SQLite file when no database URL is set, so a missing environment variable in production would produce a *working* process with private, unrecoverable checkpoints instead of an error. LISTEN/NOTIFY backs the progress streams and cannot cross PgBouncer in transaction mode, so it must be the direct one (`dbos_app.py:146-163`). Every submit also checks, server-side, that the submitting connection and `DATABASE_URL_DIRECT` name the same database — different roles are fine, different databases would plan in one and execute in another (`assert_same_domain_database`, `dbos_app.py:279-308`).

**`executor_id` must be stable across restarts *and* unique between concurrent processes**, and getting either wrong is silent (`dbos_app.py:166-191`). Recovery is scoped by it: at launch a process reclaims the workflows recorded against its own id and no others. Not stable, and a restarted process has a new identity and reclaims *nothing* — its predecessor's in-flight runs orphaned with no error, durable execution quietly stopping being durable, which is what a pid-derived id does. Not unique, and a starting process reclaims work a live peer is still running — a double execution. DBOS 3 made duplicate *recovery requests* idempotent, which is a different thing and does not help here: recovering an executor that never stopped re-enqueues a workflow its original runner is still executing, and admits a second runner beside it. Nothing derivable satisfies both for every topology, so the default assumes the common one — at most one process per role per host — and derives `<hostname>:<role>` from `REIMAGINA_EXECUTOR_ROLE`, default `app` (`:187-191`). `python -m apps.worker` sets that role to `worker` itself, and `REIMAGINA_DBOS_EXECUTION_MODE` to `executor`, both only when unset — which is what distinguishes a worker from the API on the same host (`apps/worker/app.py:108-112`). Beyond one process per role per host, set `REIMAGINA_EXECUTOR_ID` per replica; launch logs a warning naming the derived id whenever it is unset (`dbos_app.py:446-454`).

**`application_version` gates recovery.** Unset, it is an MD5 of the registered workflow source, so any edit strands in-flight runs; pinned, they become recoverable onto possibly-incompatible code — which is why the deploy story is blue-green, with old-version processes kept alive to drain (`dbos_app.py:326-331`). Unpinned, each launch also records its own version as the latest (`DBOS.set_latest_application_version`), because a client enqueue carries no version and only an executor on the latest recorded version dequeues those; without it, going back to older source (a branch switch, a revert, a second worktree) left a newer version recorded as latest and the executor silently dequeued nothing. The most recent launch wins, so two unpinned executors on *different* source strand whichever launched first — pin `REIMAGINA_APP_VERSION` when running more than one (`dbos_app.py:459-470`). Spike 5 confirmed the isolation holds in both directions, and found a free operator signal: DBOS logs that the current version is not the latest while an old-version process is still draining.

**Use worker-local concurrency, not global concurrency.** The global limit also counts `PENDING` workflows from earlier application versions, which a blue-green deploy deliberately leaves lying around. `REIMAGINA_RUN_CONCURRENCY` limits parent runs per executor (default 2); `REIMAGINA_UNIT_CONCURRENCY` independently limits indicator units per executor (default 8, defined once in `reimagina/settings.py`). Both are applied where the queues are registered (`dbos_app.py:485-522`).

That default is measured, not guessed: on the real catalog, 2 slots gives 3614 ms and 8 gives 1691 ms. The ceiling is the graph rather than the engine — a DAG's average parallelism is `work/span`, here 4.03, and 16 slots measure exactly that for a further 3.7%. 8 is the latency choice at 0.49 efficiency; lower it toward 4 (0.76) where many runs execute concurrently.

**Dispatch is poll-only, and the interval is lowered.** DBOS's two 1.0 s defaults — the queue thread's dequeue loop and a parent's wait on a child — both sit on every wave's critical path, and lowering only one leaves ~1.18 s per wave. `REIMAGINA_POLL_INTERVAL_SEC` (default 0.1) sets both: the queues' `polling_interval_sec` and the `wait_first` / `get_result` polling in `run_workflow`. Measured at 1183 ms per wave at 1.0 s and 137 ms at 0.1 s, for 5x the idle load; on the real catalog makespan fell 1.97x, because polling had been serialising units that could overlap. The zero-contention result depends on the queues having only a `worker_concurrency`: a global `concurrency` switches the dequeue from `SKIP LOCKED` to `NOWAIT` (`dbos_app.py:60-100`).

`launch()` is idempotent and locked because an executor or `dev` CLI process can submit concurrently (`dbos_app.py:428-437`). The API does not call it in its default `client` mode. It checks the domain database, starts DBOS once, starts the executor heartbeat — deliberately before the queues are bound, so a process whose first beat fails is left with nothing to dequeue on — and then registers the `runs` and `units` queues (`dbos_app.py:438-522`).

Checkpoints live in schema `dbos` beside the application's own; `pg_dump` of the app schemas emits no reference to it, so application backups stay coherent without dragging checkpoint state along, and DBOS's own migration table does not collide with alembic (`dbos_app.py:315`).

**That schema is a bootstrap artifact, not a side effect of a worker starting.** It used to be the latter, and the consequence was a reachable 500. `DBOSClient` — what the API submits through in `client` mode — never runs DBOS's schema migrations; its docstring states the precondition outright, that the system database must already have been created by a DBOS application. Only `launch()` creates it, and only a worker launches. So a database migrated by Alembic alone served the whole read side correctly, listed every seeded indicator in the Ops dashboard, and answered every `POST /jobs` with `UndefinedTable: relation "dbos.workflow_status" does not exist` — next to a banner that said a worker was needed but not that submitting could not work without one.

`bootstrap_local_platform` now calls `dbos_app.ensure_system_database()`, so `make migrate` provisions it alongside the feature types, the indicator catalog and the asset registry (`packages/py/reimagina/db/bootstrap.py:95-138`, `dbos_app.py:335-420`). It provisions without launching: `launch()` would also start the dequeuer and startup recovery, so a bootstrap command would begin *executing* work as a side effect of creating a schema. Idempotent, advisory-locked, and guarded by the same `_assert_domain_database` check as `launch()` — pointed at a database that does not exist, DBOS creates one, and a bootstrap command is exactly where a DSN typo gets typed.

Two layers, because bootstrap is not the only way a database gets migrated. A submit that still finds the schema missing raises `port.OpsPlaneNotProvisioned`, which **every route that submits** answers as **503** with the remedy named — not a 500, because nothing is wrong with the request or the code; a step was skipped. The handler is registered app-wide in `create_app` (`apps/api/app.py:167-170`), not caught per route: three routes reach `submit_run` — `POST /jobs`, `POST /jobs/{id}/rerun` and `POST /scenarios/{id}/recompute` — and a per-route `except` covered the first while the other two answered 500 for the same condition with the same remedy.

**Both DDL steps in a bootstrap take the direct route.** `bootstrap-db` carries a `--direct-dsn` (defaulting to `$DATABASE_URL_DIRECT`, then to `--app-dsn`), and Alembic and the ops-plane schema both travel over it. Alembic would survive a pooler — every migration here avoids `CONCURRENTLY` so it runs inside one transaction, and a transaction-mode pooler pins a backend for a transaction's duration — so routing it direct is consistency rather than repair. The ops-plane schema is the one that genuinely needs it: its session advisory lock does not survive transaction pooling. The flag beats the environment variable, and a direct DSN naming a different database is refused, since a direct DSN is another route to the same database and only host and port may differ. With no direct DSN, the fallback to `--app-dsn` is checked rather than trusted: `ensure_system_database` opens two connections and refuses when their `pg_backend_pid()` answers show a pooler sharing backends between clients, the one condition no DSN string reveals.

### The worker

`python -m apps.worker` validates its timing settings, launches DBOS, starts the reclaim sweep (below) unless `REIMAGINA_RECLAIM_ENABLED=0`, and blocks; on shutdown it stops the heartbeat (`apps/worker/app.py:76-196`). Launching starts DBOS dequeue and startup recovery for the workflow definitions importing `dbos_app` registered, and registers two queues: `runs` for parent workflows and `units` for indicator work. Every process that launches — not only the worker — upserts a row in `jobs_runtime.ops_executor_heartbeat` (r034) every `REIMAGINA_EXECUTOR_HEARTBEAT_S` seconds (default 30), visible through `explore.ops_executor_health_v`. The first beat is unguarded, so a process that cannot record its liveness does not start; later beats are retried, because a dead heartbeat thread in a live process is exactly what the sweep must never see (`executor_health.py:35-105`). It belongs to `launch()` rather than to the worker entrypoint because the queues registered there carry a worker concurrency, so **any** process that launches can dequeue and execute units — the worker, and also `dev` mode, which is how the CLI's `evaluate-scenario` and the integration suite execute in the submitting process. A `client` launches nothing, which is the API's default and why it is not in that list. An executor that can own work and never announces itself is one whose work can never be reclaimed (`dbos_app.py:534-568`).

Two consequences, and they are the comparison:

- **A worker is required in production.** The API is client-only by default, keeping long-running PostGIS work outside request processes. `dev` is the explicit one-process mode for the CLI and integration tests; it is not a deployment default.
- **Launching reclaims this executor's interrupted workflows, and only its own.** Another executor's orphans are reclaimed by the sweep below, which is now built. Spike 1 verified both halves: a `SIGKILL` mid-wave left exactly one unit to recompute, and a live peer adopted nothing until swept.

### Deduplication — one identity for one piece of outstanding work

`submit_run` minted a fresh uuid per call, so a double-clicked button, a retried POST or an impatient operator started a second run recomputing what the first was already computing. Both wrote the same `indicator_result` rows, one after the other.

DBOS 3 deduplicates natively. An enqueue carrying a `deduplication_id` collides with any workflow already holding that id on the same queue; `duplication_policy="return-existing"` returns a handle to it and discards the arguments just submitted. The id is cleared on every terminal transition, so **the key is held only while the work is outstanding**. `RunHandle.deduplicated` and the `deduplicated` field on the API's submit, rerun and scenario-recompute responses carry the fact back, because a resubmission is otherwise answered exactly like a fresh submit. The adapter learns it from the handle: a collision returns a handle whose workflow id is not the run id it minted (`dbos_orchestrator.py:348-366`). The `run_plan` rows it wrote under that unused id are left to the TTL sweep.

⚠ **An identical request is not the same thing as identical work, and this is where a request-shaped id would be a correctness bug.** A scenario edit does not change the request — same attribute, same features, same scenario id. It changes the values those features resolve to. Deduplicate on request shape and an operator who edits an override mid-run and resubmits is handed back the run computing the values they just replaced, with nothing to say so.

So the id folds in the content fingerprint of everything the planned units read. `serving_geo.asset_expected_version_v` gives each indicator the hash of its whole upstream closure, which includes `scenario.override.<attribute>` for that scenario. Editing an override republishes that asset, the closure hash moves, the id changes, and the resubmission is a new run. `orchestration/dedup.py` builds it (`deduplication_id`, `dedup.py:92-139`): a `v1:`-prefixed SHA-256 over the scheme version, the changed attributes, a count and digest of the edited features, the scenario, AOI and code version, any named indicators, and each planned unit's expected version at the scenario's partition. Measured rather than assumed: editing one override value moves the indicator's expected version from `79b1aeffc19f2aef` to `fc302faf525037df`, and restoring it moves it back, because the version is content addressed rather than a counter.

**It fails open.** A planned unit with no expected version — an asset graph not yet backfilled, or a NULL hash — means input changes cannot be detected, so no id is produced and nothing is deduplicated. A redundant run costs CPU; a wrongly suppressed one costs an answer someone asked for.

**The queue decides, not a prior read.** Checking for an in-flight twin and then enqueuing would be a read followed by a write with nothing holding between them: two concurrent submits of the same work would both see nothing and both enqueue. Handing the id to DBOS makes the uniqueness check and the insert one operation: `EnqueueOptions` with `deduplication_id` and `duplication_policy="return-existing"` on the client path, `SetEnqueueOptions` on the `dev`/executor path (`dbos_orchestrator.py:282-346`).

Observed end to end on the live queue:

```
resubmit while outstanding          -> same run, deduplicated=True
resubmit after the run is terminal  -> new run
edit an override, same request      -> new run
then resubmit with nothing changed  -> same run, deduplicated=True
```

`tests/integration/test_run_deduplication.py` holds all four, in `client` mode so nothing drains the queue and the window is not a race. It also asserts that the asset graph is seeded — without that, every test in the file would pass by never deduplicating anything.

### Recovery — two mechanisms, and only one of them is DBOS's

They are easy to conflate and they cover different failures.

| | Covers | Who does it | Gate |
|---|---|---|---|
| **Startup recovery** | a process that comes back | `DBOS.launch()`, for its own `executor_id` only | none needed — a process is not running the workflows it is about to reclaim |
| **Cross-executor sweep** | a process that never comes back | `orchestration.reclaim.sweep`, from a peer | `jobs_runtime.ops_executor_heartbeat` |

Startup recovery is the library's, it is free, and it is enough for a restart,
a redeploy, or an OOM kill followed by a supervisor restart. It is **not**
enough for a death: a destroyed host or a scaled-away replica leaves PENDING
workflows recorded against an id no process will ever claim again, and the run
they belong to never reaches a terminal state. Nothing times it out, because
from the database's point of view the work is still in progress.

**The sweep.** A live executor periodically asks which executors have stopped
publishing liveness and calls DBOS recovery for each, which re-enqueues their
pending workflows onto itself. `reclaim.sweep` is one pass;
`apps/worker/app.py` runs it on a daemon thread every `REIMAGINA_RECLAIM_SWEEP_S`
seconds (default `max(stale/2, 60)`), the first pass one full interval after
start so a fleet restarting together does not sweep itself (`apps/worker/app.py:37-73`, `:137-177`). A reclaimed executor's heartbeat row is deleted **once it has nothing
left open**, so the same corpse is not swept forever and does not sit
permanently in `explore.ops_executor_health_v` as noise an operator learns to
ignore.

⚠ **"Once it has nothing left open" is the whole condition, because recovering
zero workflows is not evidence that there were none.** Recovery asks
`get_pending_workflows(executor_id, app_version)`, which filters on
`application_version = <the sweeper's own version>`. A sweeper on a different
version than the executor that died recovers nothing from it, every time,
however much work is stranded — and that is the *normal* state during the
blue-green deploy this version pinning exists to support, which deliberately
leaves old-version `PENDING` workflows lying around.

Deleting the row on that result destroyed the last pointer to the stranded
work: the workflows stay `PENDING` forever, no later sweep looks at that id
again, and `ops_executor_health_v` stops showing there was ever a worker
there. A permanent leak produced by the cleanup step, with no symptom
anywhere. So `reclaim.forget_executor` reads
`dbos.workflow_status` for `PENDING`/`ENQUEUED` rows first and declines to
forget an executor that still has any — other than the workflows this sweep
has just re-enqueued, which still carry the dead executor's id until a live
worker dequeues them (`reclaim.py:140-201`, `:240-261`). The sweep reports
those ids in `ReclaimReport.stranded_executors` and the worker logs them at
WARNING.
Draining one means starting a process pinned to the stranded
`REIMAGINA_APP_VERSION` — see `docs/architecture/OPS_PLANE_RUNBOOK.md`.

**Why it waited for DBOS 3.** Spike 1 measured the mechanism working on 2.28
— `_recover_pending_workflows(["exec-A"])` from B returned two handles, drove
the run to SUCCESS, and recomputed exactly one unit — and then recommended
building it last, because 2.28.0 shipped six hours before the
idempotent-recovery fix merged. Without that fix two peers sweeping the same
dead executor could each start a runner for the same workflow. DBOS 3's
`_recovery._recover_workflow` re-enqueues rather than executing directly,
"so that every recovered workflow starts through the queue's atomic
ENQUEUED->PENDING dequeue. That handoff admits exactly one runner, which makes
duplicate recovery requests idempotent." Racing sweepers are therefore safe.

⚠ **That fix does not make sweeping a *live* executor safe, which is why the
liveness gate is a correctness requirement rather than a courtesy.**
Re-enqueuing a workflow that a running process is still executing admits a
second runner alongside the first. The idempotency is about duplicate
*recovery requests*, not about recovering work that was never abandoned.

Four properties follow, and each is enforced rather than documented:

- **The threshold is many multiples of the beat.** `REIMAGINA_EXECUTOR_STALE_S`
  defaults to 300 s against a 30 s beat, and `reclaim.stale_after_seconds`
  **refuses to start** if the ratio falls below 4 (`reclaim.py:58-104`). The two are separate
  environment variables, so nothing else would notice an operator slowing the
  beat — the tempting edit, because beats look like chatter — without widening
  the window. The asymmetry is the argument: reclaiming a dead executor late
  costs latency; reclaiming a live one costs a duplicate execution.
- **A sweeper never sweeps itself.** The exclusion is in the SQL, not left to
  the caller (`reclaim.py:107-131`). A process busy in a long unit with a wedged heartbeat thread is
  exactly the case where its own row looks stale.
- **An executor with no heartbeat row is never swept.** Absence is not
  evidence of death. The one process that can be in that state is running code
  old enough not to publish liveness, which is when guessing is worst.
- **An executor is forgotten only when it is empty.** The liveness row is the
  last pointer to stranded work, so dropping it is gated on
  `dbos.workflow_status` holding nothing `PENDING` or `ENQUEUED` for that id
  beyond what this sweep just re-enqueued — not on how many workflows it
  happened to recover.

`tests/recovery/test_crash_recovery.py::test_a_peer_reclaims_a_dead_executors_run`
is the end-to-end proof, with a real `SIGKILL` and two distinct executor ids.
Observed on a 22,500-block territory: three units unfinished before the sweep,
three recovered, run `succeeded`, dead heartbeat row gone.

```
LAUNCHED_AS             recovery-probe-sweeper
UNFINISHED_BEFORE_SWEEP 3
DEAD                    ['recovery-probe-dead']
RECOVERED               3
STATUS                  succeeded
DEAD_HEARTBEAT_ROWS     0
```

Set `REIMAGINA_RECLAIM_ENABLED=0` to turn the sweep off. The worker logs a
warning when you do, naming the consequence: a dead executor's runs stay in
progress forever until a process with its id restarts.

---

## Where this branch stands in the comparison

Both branches keep everything in the port-level row and replace everything in the engine-level row. They are **opposite corners of the same table**: the same durability guarantee — a checkpointed workflow — and the opposite deployment shape. Both are MIT with no gated features.

This branch is the **library** corner, and its position is now decided rather than proposed:

- **We keep the scheduler.** `orchestration/wave.py` survives; only durability is borrowed. `dbos_app.py:4-6` states it in the engine's own docstring: "this module is what an engine that owned scheduling would let us delete."
- **We keep our run state.** `jobs_runtime.orchestration_run` + `job_unit` survive, `runstore.py` survives, and `skipped` and `partial` keep their fidelity rather than collapsing into an engine's status enum (`r032_drop_procrastinate_schema.py:21-23`). The conformance table records it as this implementation's declared `failed_run_status` (`tests/conformance/conftest.py:82-88`).
- **Progress is a durable stream** — ordered, offset-resumable, closes cleanly.
- **No standing service, and an interactive no-worker mode is natural**, because a library has no fleet to require.
- **No engine dashboard and no engine-side cross-node recovery**, so both are ours to write — and both are now written: the Ops page reads `GET /jobs` and `GET /jobs/{run_id}/detail`, and `orchestration/reclaim.py` sweeps dead executors.

A branch whose engine owns scheduling *and* run state is the other answer: it deletes `wave.py` and `runstore.py` and drops `orchestration_run` + `job_unit`, and that deletion is the entire reason such a track exists. The measurement spec is written so that neither branch's numbers depend on tables the other does not have — no metric may be defined in terms of the two tables this branch keeps.

### The ladder this supersedes

An earlier framing treated the choice as a monotone ladder — `SKIP LOCKED` → an at-least-once queue → a workflow engine. That framing is retired. It collapsed two orthogonal axes (durability substrate × deployment shape) into one line, and it was not monotone in operational cost: a durable-execution **library** demands *less* of an operator than a task queue plus a hand-rolled scheduler, not more. This branch sits below the engine it replaced on operational cost while sitting above it on durability, which a ladder cannot express.

---

## What Phase 0 built

`phase0/ops-plane-seam` produced the shared core both branches consume, on `main`, before either branch existed. This branch is the first proof it worked — the engine swap touched the factory, added two modules, deleted six, and changed no caller's logic:

- the port + factory seam (`orchestration/port.py`, `factory.py`) — so no caller names an engine;
- the shared submit prelude (`orchestration/plan.py`) — so both branches decide run contents identically;
- the module splits that make the fates separable: `wave.py` out of `coordinator.py`, `frames.py` out of the notify module, `memstore.py` out of `synchronous.py`. The second of those was tested by the deletion and held: `frames.py` outlived the transport it was split from, untouched;
- the conformance suite (`tests/conformance/`), whose assertions were written to read identically on both branches and whose `IMPLEMENTATIONS` table is the branch-specific half each branch edits (the two copies have since diverged along with the port); DBOS conformance sets `REIMAGINA_DBOS_EXECUTION_MODE=dev` explicitly so it can prove one-process durable execution without weakening the production client/worker process split;
- migration r031 — the cancellation seam (`job_run.run_id`, `job_run.backend_pid`), which works on any engine because it is addressed to Postgres, not to a scheduler;
- the ops-plane metric definitions, fixed before either branch existed so the numbers can be put side by side.

One thing Phase 0 did *not* anticipate and this branch's spikes caught: the dirty-set ordering bug. It was fixed at the source on `main` (`f6ff6ef`) because it gates both branches, not just this one — a positionally-replayed workflow and a statically-declared DAG are broken by non-determinism in the same way.

---

## Read/write boundary

Unchanged by any of this, and the reason an engine swap is safe:

- canonical baseline is written in `curated_geo`;
- scenario edits are authored as drawn rules in `curated_geo.scenario_edit` (r048) and materialised (`scenarios.authoring.materialize_scenario`) into `curated_geo.scenario_override` — one row per `(scenario_id, feature_id, attribute_key)`, exactly one of `value_numeric` / `value_text` (`r030_scenario_override.py:26-36`) — which is what runs read;
- the effective value of an attribute is `override ⊕ current observation ⊕ base feature.attributes`, first non-null wins, resolved in SQL by `scenarios.overrides.effective_numeric` (`overrides.py:169`); baseline collapses to `observation ⊕ base`;
- runners apply that overlay on read and never write back to feature tables;
- computed outputs go to `curated_geo.indicator_result`, each citing its `job_run_id`.

`compute_planned_floors` (`indicators/planned_floors.py:23`) is the worked example: it reads the effective `floors` through `effective_numeric` and writes results inside a `job_run` context (`planned_floors.py:39-45`, `:47-58`) — the whole what-if mechanism, with no baseline mutation.

⚠ An older version of this document described this boundary in terms of `app_core.intervention_feature.attribute_overrides` and `app_core.configuration`. Neither table exists — `app_core` contains `project` (`r001_schemas_and_extensions.py:27`) and, since r048, `scenario` — and the mechanism is `curated_geo.scenario_override`.

---

## Indicator rollup — removed, and what replaced each part of it

`reimagina.jobs.rollup.run_rollup` aggregated `indicator_result` rows from a
fine grain to a coarse one. It no longer exists. `39ab70a` ("block grain only")
deleted the module, the `indicators rollup` CLI command, and
`rollup_child_rows` on the overlap repository, which served only that worker.
Every shipped indicator now declares `applies_to=("block",)`, and r016's write
trigger rejects a result at a grain the indicator does not declare — so
block-only is enforced in the database rather than merely intended.

⚠ This section described the deleted worker as present for three months after it
went. The paragraphs below are what remains, because several mechanisms share
the word "rollup" and only one of them was removed:

| Still here | What it does |
|---|---|
| `curated_geo.feature_overlap` + `feature_overlap_type_pair` (r009) | the cross-grain bridge, read by the observation rollup |
| `reimagina.jobs.observation_rollup.run_observation_rollup` | observation → observation at a coarser grain, driven by `aggregation_recipe` |
| `reimagina.jobs.aggregation.aggregate_children` | the weighted-mean and missing-data policy, shared with `jobs/aoi_summary.py` |
| `aggregation_method` on `IndicatorSpec` | read by the AOI summary, which is how a scalar per AOI is still produced |

Behaviour worth keeping on the record, because it governs the surviving
aggregations:

- Weights are `overlap_measure` — the absolute intersection (m² for polygon
  aggregation), from `feature_overlap` in the observation rollup and from
  `aoi_feature` in the AOI summary — not `overlap_ratio`, which collapses to
  ~1.0 for fully-nested children and degrades a weighted mean into an
  arithmetic one (`storage/interfaces/indicators.py:247-250`,
  `storage/interfaces/features.py:306-308`).
- Missing-data policy lives in `aggregation.aggregate_children`
  (`aggregation.py:108`): sum weights, classify the missing share against a
  threshold defaulting to 0.5, drop missing children, aggregate the rest.
- Idempotency comes from the write path, not from any worker: the repository
  stales any existing `current` row for the same `(feature, scenario,
  indicator)` before inserting (`repositories.py:2568-2585`, `:2673-2683`), and the partial
  unique indexes `ir_one_current_base` / `ir_one_current_scenario`
  (`r016:193-196`, `:198-201`) enforce it in the database.
- Stale-propagation upward was never automatic and is now moot for indicators:
  there is no coarser indicator row to stale.

---

## Migration order

The job layer's tables are created in this order, and the dependency is real: `indicator_result.job_run_id` is `NOT NULL` with an FK, so `job_run` must exist first.

| Revision | What it adds |
|---|---|
| r001 | schemas `app_core`, `jobs_runtime`, `curated_geo`, `serving_geo`; `app_core.project` |
| r016 | `jobs_runtime.job_run`, `indicator_catalog`, `indicator_dependency`, `indicator_result`, `serving_geo.current_indicator_v` |
| r028 | `jobs_runtime.orchestration_run`, `jobs_runtime.job_unit`, `job_run.procrastinate_job_id` (dropped by r035) |
| r029 | `orchestration_run.aoi_id`; historically also the queue engine's schema |
| r030 | `curated_geo.scenario_override` |
| r031 | `job_run.run_id`, `job_run.backend_pid`, `job_run_run_idx` — the cancellation seam |
| r032 | drops the queue engine's schema (`DROP SCHEMA … CASCADE`) |
| r034 | `jobs_runtime.ops_executor_heartbeat`, `explore.ops_executor_health_v` — executor liveness |
| r035 | drops `job_run.procrastinate_job_id`, never written |
| r036 | `jobs_runtime.run_plan` — the claim check |
| r046 | `orchestration_run_submitted_at_idx`, for the run list's `ORDER BY submitted_at DESC` |
| r050 | `job_unit_success_by_slug_idx` on `job_unit (indicator_slug, completed_at DESC) WHERE status = 'succeeded'` |

There is no r033 on this branch. Both branches fork from r031 and both had minted an `r033`; Alembic identifies a revision by its id alone, so this branch's heartbeat migration was renumbered to r034 (`r034_executor_heartbeat.py:8-15`). r036's docstring assigns this track r03x/r04x and the other r050+; this track's own r050–r052 cross that line.

Two notes on the tail, because migrations are immutable and this branch removed an engine:

**r029 is mixed, and only its import is now conditional.** The `aoi_id` column every branch keeps sits in the same revision as the queue schema this branch does not. A revision cannot import a dependency the project no longer has, so the install is wrapped in a `try: from procrastinate.schema import SchemaManager / except ImportError: return` and is simply skipped here (`r029:28-45`). Keeping the library installed purely so a historical migration could run would misreport this branch's dependency footprint — one of the things the two ops-planes are being compared on.

**r032 drops the schema anyway**, for any database that already has it, and does so with `CASCADE` because the queue installed functions, triggers and types alongside its tables (`r032:44-48`). Between the two, a database that never creates the schema and one that creates then drops it reach the same state.

What r032 deliberately does *not* drop is recorded in its own docstring: `orchestration_run` and `job_unit`, because this track keeps its own run state, and `job_run`, because it is domain provenance (`r032:19-27`). Its docstring also records the one edit made to r029 — the conditional import — and why that edit is safe (`r032:8-14`).

---

## Decisions on the record

| Decision | Why |
|---|---|
| A narrow port, engine types never leaked | The engine changes; callers must not — and did not, through one swap (`port.py:1-11`) |
| One factory names the engine | An ops-plane swap is a rewrite of one file, and tests patch a seam rather than a class path (`factory.py:1-6`) |
| Two modes, `dbos` and `sync` | `detach`/`embedded` were an engine distinction a library does not have; the CLI keeps the words for caller behaviour (`factory.py:20-28`) |
| Submit prelude outside every engine | An engine replaces the *execution* of a run, never the decision of what it contains (`plan.py:5-6`) |
| Impact engine is pure, and deterministically ordered | Dirty-set derivation is unit-testable with no DB, and a positionally-replayed workflow needs the same order in every process (`impact.py:11-13`, `:138-157`) |
| The run id *is* the workflow id | A run is addressable across processes with no mapping table, and submit is idempotent (`dbos_orchestrator.py:7-11`) |
| The dirty set is a workflow input | Replay matches steps by position; a body that re-derived its units would misattribute one indicator's result to another, silently (`dbos_app.py:12-22`, spike 2) |
| The engine is the existence oracle, our tables hold detail | Writing the envelope at submit trades a self-healing window for an orphaned `running` row nothing will drive (`dbos_orchestrator.py:379-383`) |
| Steps are idempotent | An interrupted step re-runs from the top, so its writes must survive being applied twice (`runstore.py:42-51`, `:136-139`) |
| Progress is written from the body, never a step | Exactly-once from a workflow, at-least-once from a step; a duplicated terminal event closes a subscriber early (`dbos_app.py:674-676`) |
| Partial success, not all-or-nothing | One unit's failure must not abort a run, only its dependents (`coordinator.py:122-126`, `dbos_app.py:1022-1027`) |
| `noop` is return-only | No status CHECK has it, and no run row exists to give it to (`plan.py:19-21`) |
| Execution state on run/unit, not on `indicator_result` | Execution lifecycle and result lifecycle are different concerns |
| `job_run` opened at compute time by the runner | Provenance belongs where the compute happens (`coordinator.py:17-19`) |
| Cancellation needs three mechanisms | Stop scheduling, stop the statement, close the run — none of the three does another's job (`dbos_orchestrator.py:457-464`, `r031:1-13`) |
| Terminal statuses are immutable, and guards return whether they fired | A cancelled unit must not be resurrected by its own error handling, and a refused write must not be announced (`runstore.py:73-84`) |
| The parent settles each unit through a step and branches on its return | `get_result` is recorded but never read back, so a replay re-queries a child's live status and can diverge from the original control flow (`dbos_app.py:1023-1028`) |
| Feature ids travel by claim check | A workflow input that scaled with the city was 258,675 uuids on a full CDMX run; the plan is written once to `jobs_runtime.run_plan` and the input carries a handle (`r036_run_plan.py:12-20`, `dbos_orchestrator.py:123-162`) |
| Deduplicate on content, and fail open | An identical request after an override edit is different work, so the id folds in each unit's expected version; a unit with none is never deduplicated (`dedup.py:15-38`) |
| Never update a failed `job_run` row in place | Append-only audit trail; a re-run opens a new row. A convention, not a constraint — nothing in the schema enforces it |

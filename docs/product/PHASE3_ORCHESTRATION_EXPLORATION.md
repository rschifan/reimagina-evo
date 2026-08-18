# Phase 3 — Job Orchestration: Design Exploration

**Scope.** The job-orchestration subsystem: what runs when a scenario edit
invalidates part of the indicator dependency graph — selecting the outputs to
recompute, ordering them by dependency, running the heterogeneous units (SQL/PostGIS,
Python geometry, optimizers, ML), and handling failure, provenance, and
observability — across many scenarios and regions.

Two solutions are evaluated against one requirement set: an in-process orchestrator
and Dagster. Parentheticals marked `(today: …)` name the module that plays a role in
the current codebase; they are reference only and do not constrain the design.

## 1. Requirements

Solution-neutral. (Domain terms — feature, indicator, scenario, dirty-set — are
defined in §2.0; skim it first if the vocabulary is new.)

### Functional

| # | Requirement |
|---|---|
| **F1** | **Selective recompute** — recompute only outputs affected by a change, not the whole graph. |
| **F2** | **Dependency-ordered scheduling** — run in DAG order (inputs before dependents); a node starts when its inputs are ready. |
| **F3** | **Composite / derived outputs** — outputs computed from other outputs, with weights/formulas. |
| **F4** | **Heterogeneous units of work** — SQL/PostGIS, Python geometry, optimizers, ML inference/training, allocation models in one graph. |
| **F5** | **Run intake** — trigger a run by event (scenario edit), manual request, or schedule. |
| **F6** | **Failure handling** — per-unit retry with backoff, dead-letter, and partial-success (a non-critical unit fails without failing the run). |
| **F7** | **Cancellation** — cooperatively cancel an in-flight run. |
| **F8** | **Provenance** — record code version, input versions, and (for stochastic models) seed for every output. |
| **F9** | **Per-output validation** — post-conditions (bounds, quality) that fail the unit honestly. |

### Non-functional

| # | Requirement |
|---|---|
| **N1** | **Data lineage** — queryable input→output graph (what produced what), distinct from run status. |
| **N2** | **Run observability** — live status / progress / errors. |
| **N3** | **Concurrency control + dedup** — one in-flight compute per (scenario, unit); bounded pool concurrency. |
| **N4** | **Resource-aware execution** — route work to IO / CPU / GPU pools. |
| **N5** | **Redundant-work avoidance** — skip recompute when effective inputs are unchanged. |
| **N6** | **Backfills** — bulk recompute on code or data change. |
| **N7** | **Ops simplicity** — minimal standing services for a small team. |
| **N8** | **Scale path** — single-node now, multi-node later without rearchitecture. |
| **N9** | **Extensibility** — add a new domain/model without editing the engine. |

## 2. Solution A — In-process orchestrator

The orchestrator runs **inside the application process(es)** — no scheduler
daemon, no message broker required.

### 2.0 Domain primer

- **Feature** — a spatial unit (block, lot, road segment).
- **Attribute** — a property of a feature (e.g. `land_use`, `floors`).
- **Scenario** — a what-if that overrides attributes on chosen features; the
  baseline is never mutated.
- **Indicator** — a metric computed per feature (e.g. `walkability`).
- **Model family** — the kind of worker that computes a group of indicators
  (accessibility, housing, green_area, …).
- **Composite indicator** — an indicator computed from other indicators.
- **Rollup** — aggregating a fine-grain indicator to a coarser feature
  (block → AGEB). **AGEB** = *Área Geoestadística Básica*, the census block-group
  grain blocks roll up into.
- **Dirty-set** — the outputs a given change invalidates and forces to recompute.
- **Work unit** — the scheduling atom the whole engine moves around:
  `(scenario, indicator, feature_scope)`, where `feature_scope` is an explicit
  feature-id set or `ALL`. In code today: `DirtyUnit(indicator, features)` plus the
  run's `scenario_id`. Dedup (N3), the reuse-cache key (11), and result-store
  atomicity (9) are all keyed on this triple.
- **AOI** — area of interest, the geographic region a run is scoped to.

### 2.1 Components

**1. Catalog / registry**
Role: the declared set of indicators and model families plus their metadata —
which input attributes each indicator reads, which other indicators it depends
on, its valid value bounds, and which feature types it applies to. It is the
single source the rest of the system consults to know what can be computed and
what depends on what. Adding a domain or model is adding an entry here.

- Characteristics: declarative; versioned; per-indicator criticality (required
  vs optional inputs) that drives partial-success; the only extension point for
  new outputs.
- Satisfies: F3, F6, N9.
- (today: `indicator_catalog`, `indicator_dependency`, `IndicatorSpec`. Per-indicator
  criticality (required vs optional input) is **not yet modeled** on `IndicatorSpec`;
  the coordinator is currently fail-closed — a dependent skips if any *dirty* input
  fails, while non-dirty inputs keep their current value.)

**2. Intake / run API**
Role: the entry point that accepts a request to evaluate a scenario — or a
manual or scheduled or backfill trigger — and opens a *run* (one tracked
execution). It refuses a duplicate *run* for a scenario currently computing;
per-unit dedup (one in-flight compute per (scenario, unit)) is enforced
downstream at the queue (6).

- Characteristics: idempotent; run-level deduplicating; cheap (no compute in the
  request path).
- Satisfies: F5, N6.
- (today: **built** — `POST /jobs` (`apps/api/routers/jobs.py:78`) submits through
  the port and returns `202`; `orchestration/intake.py` `evaluate_scenario` is the
  in-process entry point. Run rows still come from `JobRunRepository.start_job`.
  Run-level dedup is **not** enforced — `submit_run` mints a fresh `run_id` every
  call (`orchestration/procrastinate_orchestrator.py:65`); per-unit dedup is the
  queue's queueing lock (`orchestration/tasks.py:32`).)

**3. Impact engine (dirty-set)**
Role: given the attributes a scenario changed *and the features they changed
them on*, decides the exact set of outputs to recompute. It matches changed
attributes to the indicators that read them, expands to the spatially affected
features (the edited features, their neighborhood for proximity indicators, and
the parent features they roll up into), then propagates downstream to the
composites and rollups computed from them. Everything else is left untouched —
this is what makes recompute *selective* instead of full.

- Characteristics: pure function of (changed attributes + features, registry);
  spatially scoped (feature set + affected parents/AOIs, not indicator-level
  only); downstream-propagating; no side effects.
- Spatial expansion is an injected `FeatureExpander` (modes: `identity` ·
  `neighbors` · `rollup_parents` · `full_grain`); it needs a feature graph
  (parent↔child + spatial neighbours), **not** derivable from the registry — the
  highest-risk correctness surface for F1, and an identity stub today.
- Input contract: consumes `(changed_attributes, edited_features)`. Producing that
  diff from a raw scenario edit (change detection) is upstream — intake (2) /
  scenario-diff — and **not yet specified**.
- Satisfies: F1.
- (today: **built** — `orchestration/impact.py` `compute_dirty_set`; the
  attribute→indicator mapping is declared as `IndicatorSpec.reads`. Feature-
  expansion is an identity stub — proximity/rollup widening not yet wired.)

**4. Dependency resolver**
Role: takes the dirty-set and orders it so every input is computed before the
output that needs it (direct indicators → composites → rollups), and rejects
cycles. Produces the execution order the coordinator follows.

- Characteristics: topological; cycle-rejecting; derived from the registry.
- Satisfies: F2, F3.
- (today: **built** — topo-ordering folded into `compute_dirty_set`
  (`graphlib.TopologicalSorter`); the graph is declared by `indicator_dependency`
  - its acyclicity trigger)

**5. Run / task state**
Role: the durable record of a run and each unit of work inside it, with status
(queued / running / succeeded / failed / skipped / cancelled). It is what the UI
reads and what lets a crashed run resume instead of restarting.

- Characteristics: durable; queryable; one row per unit.
- Satisfies: F6, N2.
- (today: **built** — `jobs_runtime.orchestration_run` (the run envelope) +
  `jobs_runtime.job_unit` (one row per unit, r028), read and written through
  `orchestration/runstore.py`. `jobs_runtime.job_run` stays *domain provenance*,
  not scheduler state — which is why r031 gave it `run_id` + `backend_pid`
  rather than folding it into the run envelope.)

**6. Work queue + claim**
Role: holds the units ready to run and hands each to exactly one worker, so two
workers never compute the same unit. Units that keep failing are moved aside
(dead-letter) instead of looping forever.

- Characteristics: at-least-once delivery; idempotent claim; dead-letter on
  retry-limit.
- Satisfies: F6, N3, N8.
- (today: **built** — a Procrastinate queue owns delivery and claiming
  (schema r029, app `orchestration/queue_app.py`, tasks `orchestration/tasks.py`),
  with per-unit dedup as a queueing lock (`unit:{run_id}:{slug}`,
  `orchestration/tasks.py:32`). `compute_unit` retries transient failures
  (`RetryStrategy(max_attempts=3, exponential_wait=2)`, `orchestration/tasks.py:92`);
  a deterministic runner exception is recorded terminal `failed` instead of
  retried. There is no `claim_job` — `storage/interfaces/jobs.py` says so in the
  protocol itself. Still missing: a dead-letter destination for retry exhaustion.)

**7. Worker pools**
Role: the processes that actually run one unit of work each — reading the
scenario's effective inputs, running the model, producing the output. Different
work needs different resources, so units are routed to an IO pool, a CPU pool
(heavy geometry), or a GPU pool. A running unit can be told to stop.

- Characteristics: resource-profiled (IO/CPU/GPU); bounded concurrency;
  cooperatively cancellable.
- Satisfies: F4, F7, N4.
- (today: **partial** — runner registry `orchestration/runners.py`
  `default_runners` binds slugs to compute fns; units execute in a real worker
  (`python -m apps.worker`, with `--queues` / `--concurrency`,
  `apps/worker/__main__.py:24-41`) under the `detach` mode, or in a bounded
  in-process worker under `embedded` (`orchestration/embedded.py`). Runners are
  feature-scoped: the dirty-set's `features` are forwarded as `feature_ids`
  (`orchestration/runners.py:48-55`), with `aoi_id` as the coarser fallback when
  the set is empty. Cancellation ships (F7) — `port.cancel_run` makes every
  non-terminal unit `cancelled` and `cancel_run_backends` issues
  `pg_cancel_backend` over the run's live backends (r031;
  `storage/postgres/repositories.py:1976`); a unit already inside a psycopg call
  still runs to its next boundary. Still missing: IO/CPU/GPU pools — one worker
  process per resource profile is the documented stand-in.)

**8. Coordinator**
Role: the scheduler — the brain of a run. It asks the impact engine for the
dirty-set, the resolver for the order, dispatches the units that are ready,
advances the next wave as each completes, and decides the run's final status,
including partial-success (a non-critical unit failed) and cancellation.

- Characteristics: event-driven; dependency-aware; owns partial-success +
  cancellation logic.
- Satisfies: F1, F2, F6, F7.
- (today: **built** — the same readiness rule in two shapes. In-process:
  `orchestration/coordinator.py` `run_dirty_set`/`RunReport` walks the ordered
  dirty-set, isolates per-unit failure, derives succeeded/partial/failed (F6).
  Distributed: `orchestration/wave.py` `plan_wave` is a pure, DB-free wave
  scheduler — dispatch / skip / final status — driven by
  `orchestration/tasks.py:152` `advance_run`, which defers a whole wave at once,
  so parallelism is the worker fleet's, not the coordinator's. Run state persists
  through `orchestration/runstore.py` rather than in memory, and cancellation
  ships (F7). Each runner still owns its own `job_run` row, so the coordinator
  opens none. Two wedges in this path are known and unfixed: `advance_run`
  commits units `running` and only then enqueues them, outside the transaction
  (`orchestration/tasks.py:189-192`), and a `compute_unit` whose retries are
  exhausted never writes its `job_unit` row (`orchestration/tasks.py:109` — only
  runner exceptions are caught). Either leaves a unit `running` forever, so this
  path is not deployable as it stands.)

**9. Result store**
Role: persists each computed output as the single *current* value for its
(feature, scenario, indicator), replacing the prior current value atomically so
a re-run never leaves duplicates or half-states.

- Characteristics: idempotent (replace-in-place); one current value per key;
  provenance-stamped.
- Satisfies: F1 (single-current invariant). Selectivity itself lives in components 3 and 11.
- (today: `indicator_result` with current/stale status + partial-unique indexes.
  **Decided: at write time.** A write marks the existing current row `stale` and
  inserts the new one `current` in the same transaction — stale-then-insert,
  `storage/postgres/repositories.py:2217-2234` — so a reader never sees a
  half-updated set, and the superseded row survives as the redundant-compute
  measure rather than being overwritten.)

**10. Validation**
Role: checks each output against its declared post-conditions (within bounds,
acceptable quality) and fails the unit honestly rather than storing a bad value.
The bounds are **declared once** (on the catalog entry) and enforced by a single
validator — never re-implemented per backend or per layer.

- Characteristics: per-output; **single declarative source** (no parallel
  re-implementations); fail-closed.
- Satisfies: F9.
- (today: **built** — `indicators/validation.py` is that single declarative
  validator: bounds are a Pandera schema (`bounds_schema`, also attachable as a
  Hamilton `@check_output`), and bounds + `applies_to` originate on
  `IndicatorSpec`, seeded into `indicator_catalog`. The Postgres write-trigger
  remains the DB-level backstop reading those same values — one declared source,
  two enforcement points, not two definitions to keep in sync.)

**11. Reuse cache**
Role: before running a unit, checks whether its effective inputs (and code
version) are unchanged from a prior successful run; if so, reuses that result
and skips the compute. Turns "recompute the dirty-set" into "recompute only what
actually changed."

- Characteristics: content-addressed (input fingerprint); safe to miss (a miss
  only costs recompute).
- Satisfies: N5.
- (today: **borrowed, opt-in** — Hamilton `.with_cache()` is wired in
  `indicators/_hamilton_driver.py:53`, storing under the data dir, gated by
  `REIMAGINA_HAMILTON_CACHE` and off by default so tests and CI stay
  deterministic. No run path enables it yet, so N5 is available rather than
  in force.)

Three mechanisms skip or store work — keep them distinct:

| Mechanism | Fires when | Keyed on |
|---|---|---|
| dirty-set (3, F1) | a scenario edit | changed attributes + edited features |
| reuse cache (11, N5) | a unit is about to run | content fingerprint of effective inputs + code version |
| result store (9) | a unit succeeds | authoritative current value per `(feature, scenario, indicator)` |

(Without this split, N5 gets re-implemented as "if `indicator_result` is current,
skip" — duplicating fingerprint logic in the store.)

**12. Lineage + observability** (two distinct facets)
Role: (a) **data lineage / provenance (N1, F8)** — records what produced what
(code version, input versions, seed) as a queryable input→output graph; and
(b) **run telemetry (N2)** — streams live status/progress so the UI and
operators can watch a run advance. Different axes, different tools (a lineage
event format vs. a status stream); bundled here as one component but realized
separately.

- Characteristics: vendor-neutral lineage events (a); live status stream (b);
  both queryable after the fact.
- Satisfies: F8, N1 (a); N2 (b).
- (today: split, as the two facets predicted. (b) **built** — `port.subscribe_progress`
  over Postgres LISTEN/NOTIFY (`orchestration/events.py`), framed as SSE by
  `orchestration/frames.py` (the route owns the wire shape, the engine does not)
  and served at `GET /jobs/{run_id}/events` (`apps/api/routers/jobs.py:169`).
  (a) **partial** — provenance columns `job_run.code_version` +
  `indicator_result.model_version`; OpenLineage emission is opt-in, attaching only
  when `OPENLINEAGE_URL` is set (`indicators/_hamilton_driver.py:31-44`), and there
  is still no queryable lineage graph.)

**13. Orchestrator interface (port)**
Role: the narrow interface the rest of the application depends on to start runs,
read status, and subscribe to progress — so the whole engine behind it can be
replaced (e.g. by Dagster) without touching the application.

- Characteristics: small (~10 methods — e.g. `submit_run` / `get_run` /
  `subscribe_progress`); stable; implementation-hiding; must **not** leak
  Hamilton/Dagster types.
- Satisfies: N8, N9.
- (today: **built** — `orchestration/port.py`: the `Orchestrator` protocol
  (`submit_run` / `get_run` / `cancel_run` / `subscribe_progress`) plus the shared
  status vocabulary, in plain dataclasses that leak no Hamilton/Procrastinate
  type. Implementations are named in exactly one place —
  `orchestration/factory.py`, modes `detach` / `embedded` / `sync` — which is the
  seam an ops-plane swap edits. The repository protocols in `storage/interfaces/`
  are the *storage* port, a different seam.)

Coverage: every F1–F9 + N1–N9 maps to ≥1 component above. **N7 (ops
simplicity)** is a whole-system property, judged in §4, not a component.

### 2.2 Realizations (both shown; not a forced choice)

- **stdlib realization** — resolver (4) = `graphlib.TopologicalSorter`; reuse
  cache (11) = hand-rolled input fingerprint; worker pools (7) = `asyncio` (IO) +
  `ProcessPoolExecutor` (CPU); queue (6) = Postgres `SKIP LOCKED`; lineage (12) =
  `openlineage-python`. No broker, no daemon.
- **SOTA (state-of-the-art) enhancement** — a micro-orchestrator (**Hamilton**)
  subsumes the **reuse cache (11)** (content-addressed `data_version` caching) and
  **lineage (12)** (OpenLineage — a vendor-neutral standard for lineage events —
  via its materializers), plus the *intra-unit*
  function graph of one indicator: outputs are declared as functions, and that
  graph, caching, and lineage are derived from the code in-process; it executes
  only the requested outputs. Trade: one dependency vs. owning caching + lineage
  code. (Lineage needs the materializer / `@datasaver` metadata.) **Scope:**
  Hamilton runs *within* one work-unit and is invoked per work-unit; the
  *cross-indicator* resolver (4) — ordering the dirty-set across indicators —
  stays ours (`graphlib`, in the impact engine), and the scenario×feature
  scheduling stays in 3 + 8 (why, in §4).
- **Validation realization** — declare bounds once (component 10) and enforce
  with a single declarative validator (e.g. **Pandera** for dataframe checks, or
  Hamilton `@check_output`), not a per-backend re-implementation.
- **Ops-plane realization** — pick a cell on two axes, not a step on a ladder
  (§4.1): durability substrate (`SKIP LOCKED` → an at-least-once queue such as
  procrastinate / PgQueuer → a checkpointed workflow engine) crossed with
  deployment shape (in-process · sidecar worker · standing cluster).

### 2.3 Reference architecture

```
 intake API ─► run/task state ◄─► coordinator ──► work queue ──► reuse cache ──► worker pools
   (2)             (5)              (8) │  ▲           (6)        (11, gate)      (7) IO/CPU/GPU
                                impact engine (3)                                       │
                                + resolver (4) ◄─ catalog/registry (1)                  ▼
        lineage + telemetry (12) ◄──── result store (9) ◄──────────────────── validation (10)
        orchestrator port (13) wraps the whole engine for the API.
   (reuse cache (11) gates the worker: a hit short-circuits compute before the pool.)
```

### 2.4 Shared worked example (used again in §3)

**The edit.** Convert three avenue blocks `b1, b2, b3` into a linear park: set
`land_use = green` and raise `tree_canopy`. So the change is
`changed_attributes = {land_use, tree_canopy}` on `edited_features = {b1, b2, b3}`.

**The registry (1)** — five indicators, declared once:

| indicator | grain | reads (attributes) | depends_on (indicators) |
|---|---|---|---|
| `green_coverage` | block | `land_use`, `tree_canopy` | — |
| `walkability` | block | `land_use` (+ neighbours' green) | — |
| `transit` | block | transit attrs (untouched) | — |
| `liveability_index` | block | — | `0.4·walkability + 0.3·transit + 0.3·green_coverage` |
| `liveability_rollup` | AGEB | — | `liveability_index` (block→AGEB) |

The dirty-set resolves on **two axes** — *which indicators* (attribute reads, then
downstream dependents) and *which features* each must recompute on (spatial
expansion). The impact engine returns both as an ordered list of
`DirtyUnit(indicator, features)`:

```python
# components 3 + 4 — impact engine: (changed attrs, edited features) → ordered dirty-set
units = compute_dirty_set(
    changed_attributes={"land_use", "tree_canopy"},
    edited_features={"b1", "b2", "b3"},
    registry=SHIPPED_INDICATORS,
    expand=feature_expander,            # identity · neighbours · rollup_parents
)
# units — DirtyUnit(indicator, features), already topo-ordered:
#   1. green_coverage      {b1,b2,b3}         seeded: reads land_use + tree_canopy
#   2. walkability         {b1,b2,b3,b4,b5}   seeded: reads land_use; + adjacent blocks (proximity)
#      (1 ∥ 2 are independent → one wave)
#   3. liveability_index   {b1..b5}           downstream of 1 + 2; reuses transit's CURRENT value
#   4. liveability_rollup  {AGEB_7}           rolls 3 up to the parent AGEB of b1..b5
#   transit: not seeded, not downstream → absent from the dirty-set

# component 8 — coordinator: run in dependency order, isolate per-unit failure
report = run_dirty_set(
    conn, units, registry=SHIPPED_INDICATORS,
    runners=default_runners(), scenario_id="linear_park",
)
# per unit: reuse-cache hit (11)? skip. else compute → validate (10)
#           → result store: put current value (9) → lineage.emit (12)
```

**Outcome.** Units 1 + 2 form one wave — dispatched together by `plan_wave`, so
whether they actually overlap is the worker fleet's concurrency, not the
scheduler's — then `liveability_index`, then `liveability_rollup`.
`transit` and every other indicator never run; `liveability_index` reuses
`transit`'s existing current value. Each result is bounds-checked (10), written as
the single current value (9), and provenance-stamped (12).

> The indicators here are illustrative. The shipped set today is four
> (`indicators/catalog.py:150`): `urban_form_block_compactness` and
> `urban_form_block_building_density`, both `reads=("geometry",)`;
> `urban_form_block_planned_floors`, `reads=("floors",)`; and
> `urban_form_block_composite`, a real composite reading no attribute and
> weighting the first two 0.5/0.5. So a `geometry` edit is the only one that
> fans out to two waves. Rollups still do not exist, and `feature_expander` is
> still the identity stub (`orchestration/impact.py:74`), so `features` passes
> through unwidened.

(Implementation state — what's built in code vs. still design — is the `(today: …)`
annotation on each component in §2.1; there is no separate status document.)

## 3. Solution B — Dagster

Platform built on **software-defined assets**: each output is an `@asset`
declaring its upstreams; Dagster owns the graph, a scheduling **daemon**, a
**web UI**, and a **metadata database**. The terms below (`@asset`, partition,
`eager()`, IO manager, run launcher, executor) are Dagster's own constructs; the
table maps each to the component (§2) it would play.

| Component / requirement (§1, §2) | Dagster construct |
|---|---|
| catalog/registry (1); composite + rollup (F3) | the asset graph + `deps=[…]` (rollup = a downstream `@asset`) |
| impact engine + selective recompute (3; F1) | Declarative Automation (`eager`) |
| redundant-work avoidance (11; N5) | data-version / observable-source-assets + a skip policy — `eager` alone re-runs on upstream materialization |
| dependency resolver (4; F2) | native asset dependency graph |
| run/task state + observability (5; N2) | runs + the Dagster UI |
| failure handling (F6) | op/asset `RetryPolicy` (backoff) + run-failure sensors (dead-letter); non-critical via separate assets |
| cancellation (F7) | run termination + cooperative cancel inside long PostGIS/ML ops |
| work queue + worker pools (6, 7; F4, N3, N4) | run launcher + per-asset executors / resources (GPU, MLflow) |
| coordinator (8) | the daemon |
| result store (9; F1) | IO managers |
| validation (10; F9) | `@asset_check` |
| lineage + provenance (12; F8, N1) | materialization metadata + lineage UI |
| intake / backfills (2; F5, N6) | sensors / schedules + native partition backfills |
| ops footprint (N7) | standing daemon + webserver + metadata DB |
| scale path (N8) | run launcher + executors (multiprocess / Celery / K8s / Dask) |
| orchestrator port + extensibility (13; N9) | new asset / registered Component |

Partitions = scenarios, so each scenario is an independent slice.

### Worked example (same linear park)

```python
@asset(partitions_def=scenarios, automation_condition=AutomationCondition.eager())
def green_coverage(ctx): ...
@asset(partitions_def=scenarios, automation_condition=AutomationCondition.eager())
def walkability(ctx): ...                                  # reads green/land-use
@asset(partitions_def=scenarios, deps=[walkability, "transit", green_coverage],
       automation_condition=AutomationCondition.eager())
def liveability_index(ctx): ...
@asset(partitions_def=scenarios, deps=[liveability_index])
def liveability_rollup(ctx): ...                           # block → AGEB
@asset_check(asset=walkability)
def in_range(v): return AssetCheckResult(passed=0 <= v <= 1)
```

Same five indicators, same edit (`S = linear_park`). Editing `S`'s inputs
re-materializes `green_coverage(S)` and `walkability(S)` → `liveability_index(S)`
→ `liveability_rollup(S)` — the same indicator chain as §2.4 units 1–4 — leaving
`transit(S)` and every other scenario untouched, scheduled by the engine, not by
application code.

**Caveat — F1 is not free here.** Partitioning by scenario gives *scenario*-grain
selectivity, not the *feature*-grain dirty-set of §2: `eager()` re-materializes the
whole `(S)` partition, so editing three blocks recomputes `walkability(S)` for the
entire scenario unless you add feature-grain modeling (dynamic per-feature outputs,
observable-source-asset diffs, or a custom automation condition). Dagster absorbs
ops, backfills, and observability; the spatial dirty-set logic of the impact engine
(3) has no free equivalent — it would be rebuilt as a sensor / upstream-diff asset.

## 4. Comparison + recommendation

A vs B against each requirement:

| Requirement | In-process (A) | Dagster (B) |
|---|---|---|
| F1 selective recompute | impact engine + reuse cache (feature-grain) | Declarative Automation (scenario-grain; feature-grain needs added modeling) |
| F2/F3 order + composite | resolver (graphlib, ours) + Hamilton intra-unit | native asset graph |
| F4 heterogeneous + ML | worker pools; bespoke GPU/ML | native executors + MLflow |
| F8/N1 provenance + lineage | OpenLineage (table-level, free) | lineage UI (column-level paid) |
| N2 observability | status stream to build | Dagster UI included |
| N6 backfills | build into intake | native |
| N7 ops simplicity | **no standing services** | daemon + webserver + metadata DB |
| N8 scale path | multi-node needs a durable substrate + worker-service extraction (§4.1), not "no rearchitecture" | native multi-node |
| N9 extensibility | registry entry + worker, behind port | new asset / Component |
| Cost to first run | **low** (no infra) | higher (adopt + operate platform) |
| Main risk | we own every component | platform ops + some paid features |

### 4.1 Final architecture

Three layers; each of the 13 components owned exactly once. Own the domain core,
borrow the compute-graph (one library — Hamilton) and the ops-plane (Postgres,
substrate chosen per the table below), keep Dagster behind the port as a later swap.

```
orchestration port (13) — ours; swaps the engine below (e.g. Dagster) without touching the app
 ├ DOMAIN CORE — ours (domain truth in curated_geo / jobs_runtime)
 │    catalog (1) · intake (2) · impact engine + cross-indicator resolver (3, 4) · result store (9) · validation bounds (10)
 ├ COMPUTE GRAPH — Hamilton (library; no daemon, no metadata DB)
 │    intra-unit function graph · reuse cache = data_version caching (11) · lineage (12, via materializers)
 │    runs only the requested outputs · validation engine = Pandera / @check_output (10)
 └ OPS PLANE — Postgres-backed; two orthogonal axes, not one ladder (table below)
      run/task state (5) · queue (6) · worker pools (7) · coordinator (8)
      durability substrate × deployment shape — Track A and Track B are opposite corners
```

**The ops-plane is two axes, not a ladder.** *What guarantees the substrate
gives* (none → at-least-once → checkpointed workflow) is independent of *where it
runs* (in this process → a sidecar worker → a standing cluster). Ordering them as
one line of increasing complexity is wrong on its own terms: it is non-monotone in
operational cost, because a durable-execution **library** demands *less* of an
operator than a task queue plus a hand-rolled scheduler, not more.

|  | in-process | sidecar worker | standing cluster |
|---|---|---|---|
| **no durability** | `SKIP LOCKED` | | |
| **at-least-once queue** | | Procrastinate, PgQueuer | |
| **checkpointed workflow** | **DBOS ← Track A** | | **Hatchet ← Track B**, Temporal |

The two tracks are the opposite corners of the bottom row: **the same durability
guarantee, the opposite deployment shape.** Both are MIT with no gated tier.

- **Track A — DBOS Transact**, durable execution as a *library*. No standing
  service; checkpoints land in the same Postgres as the domain data. We **keep**
  the wave scheduler (8) and borrow only durability, so the `skipped` / `partial`
  status fidelity of `port.overall_status` survives intact.
- **Track B — Hatchet**, self-hosted on a Postgres broker. One static DAG; the
  engine owns scheduling *and* run state, so we **delete** our scheduler — and,
  accepted deliberately, its run-status enum has no `partial` or `skipped`, so that
  vocabulary is lost even though the behaviour is not.

Neither is the winner. The branches are permanently non-mergeable and the
deliverable is a documented side-by-side, not a migration.

**Build vs borrow.** Pure-ours: **1, 2, 3, 4, 9, 13** (domain truth — incl. the
cross-indicator resolver, `graphlib` folded into the impact engine). Borrowed:
**11, 12 = Hamilton** (caching + lineage + the intra-unit graph, one dependency);
**5, 6, 7 = the Postgres ops-plane**. Hybrid: **8** — and this is precisely where
the two tracks diverge: Track A keeps our wave scheduler and borrows only
durability, Track B hands scheduling to the engine and deletes it. Also hybrid:
**10** (we declare the bounds once, a validator engine enforces them). None of
this is hand-written per-function code: retries, DLQ, cron, and durable execution
come from the substrate, whichever corner of the table it sits in.

**Two graph directions compose.** The impact engine (3) walks *downstream* —
changed attributes → the outputs to recompute. Hamilton walks *upstream* — the
inputs each requested output needs, skipping unchanged ones via data_version
caching. Different directions, they compose; neither replaces the other. Hamilton
has no partition abstraction, so scenario×feature scheduling, backfills, and
run-tracking stay in 3 + 8 — the exact machinery Dagster absorbs at maturity.

**Verdict.** *Now* — domain core + Hamilton + a Postgres ops-plane: no metadata
DB, no partition taxonomy, domain truth stays in our schema. *Later* — Dagster via
the port, on the same grounds as before: ML models needing versioning / GPU
scheduling · many regions with routine backfills · a lineage UI as a product
requirement.

**What distinguishes the two tracks** — not "how much scale", which is what the
ladder wrongly asked, but four properties that differ in kind:

| | Track A — DBOS (library) | Track B — Hatchet (service) |
|---|---|---|
| **Who schedules** | we do — `wave.plan_wave` survives | the engine does — we delete it |
| **Run state** | co-located with domain data, one Postgres | engine-owned, a second thing to migrate and back up |
| **To accept a submit** | nothing standing; runs in-process | a standing worker + broker, mandatory |
| **Progress stream** | ordered and resumable from the checkpoint log | best-effort, and `partial` / `skipped` are not in its vocabulary |

How each of these is *measured* — the metrics, and why they are defined only over
tables that survive both branches — is fixed in advance in
[`PHASE3_OPS_PLANE_MEASUREMENT.md`](./PHASE3_OPS_PLANE_MEASUREMENT.md). The
side-by-side result lands in `PHASE3_OPS_PLANE_TRACKS.md` (forthcoming, written on
`main` once both branches exist — neither branch can hold both halves).

## 5. Alternatives — positioning (2026)

Not whole-Dagster substitutes — each covers a different slice of the §2
components; the §4 design composes the best-fit ones.

| Tool                         | Covers (components)                                                                                                   | Best for                                      | Pick when                                                                                                                                                                                               |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Hamilton**                 | 11, 12 + intra-unit graph — `data_version` caching + OpenLineage + per-unit function DAG; runs only requested outputs | the compute-graph layer                       | **now** (top pick). Caveats: lineage needs materializer/`@datasaver` metadata; no partition abstraction and no cross-indicator resolver — that ordering (4) + scenario/feature scheduling stay in 3 + 8 |
| **procrastinate / PgQueuer** | 5, 6 (+ retries, scheduling, locks, DLQ)                                                                              | Postgres task-queue *library*, no new infra   | at-least-once delivery is enough and the scheduler stays ours — what ships on `main` today                                                                                                              |
| **DBOS Transact**            | 5, 6 — durable checkpoints in the domain Postgres; scheduling (8) stays ours                                         | durable-execution *library*, no standing service | **Track A**: checkpointed durability without a cluster; keeps `plan_wave` and the `partial`/`skipped` vocabulary                                                                                     |
| **Hatchet**                  | 5, 6, 7, 8 — durable runs/queue/workers/coordinator (Postgres)                                                        | durable-workflow *service*                    | **Track B**: engine owns scheduling + run state (we delete ours); dynamic concurrency + monitoring UI. Not a lineage platform; no `partial`/`skipped`                                                    |
| **SQLMesh**                  | SQL-expressible 1, 3, 4, 9, 10, 11, 12 (incremental, plan/apply, audits, free column lineage)                         | SQL-transform governance                      | **companion** for warehouse transforms — not central (PostGIS/geometry/ML/overrides aren't SQL)                                                                                                         |
| **Dagster**                  | all                                                                                                                   | full asset platform                           | mature scale (benchmark)                                                                                                                                                                                |
| **Prefect 3**                | 2, 5, 6, 7, 8 — ops/runtime, dynamic + Pythonic                                                                       | general Python workflows / API-triggered jobs | not over Dagster for a durable asset graph + backfills                                                                                                                                                  |

Not first choice: **Airflow** (task/schedule-centric, heavy), **Kestra** (YAML
platform, standing infra), **Temporal** (durable sagas — wrong axis), **Windmill**
(internal-tools lean). Adjacent: **Orchestra** (managed), **Bruin** (all-in-one OSS).

Ranking: compute graph = Hamilton · ops-plane = the two tracks side by side
(DBOS · Hatchet), not a sequence · SQL companion = SQLMesh · full platform
(later) = Dagster · general workflow = Prefect.

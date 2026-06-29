# Phase 3 — Job Orchestration: Design Exploration

Problem class: recompute a dependency graph of geospatial + analytical/ML models
when a scenario edits the world, across many scenarios and regions. Two
solutions evaluated against one requirement set: an in-process orchestrator and
Dagster. Parentheticals marked `(today: …)` name the module that plays a role in
the current codebase; they are reference only and do not constrain the design.

## 1. Requirements

Solution-neutral.

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
  (block → AGEB).
- **Dirty-set** — the outputs a given change invalidates and forces to recompute.

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
- (today: `indicator_catalog`, `indicator_dependency`, `IndicatorSpec`)

**2. Intake / run API**
Role: the entry point that accepts a request to evaluate a scenario — or a
manual or scheduled or backfill trigger — and opens a *run* (one tracked
execution). It refuses a duplicate *run* for a scenario currently computing;
per-unit dedup (one in-flight compute per (scenario, unit)) is enforced
downstream at the queue (6).
- Characteristics: idempotent; run-level deduplicating; cheap (no compute in the
  request path).
- Satisfies: F5, N6.
- (today: run rows via `JobRunRepository.start_job`; no intake endpoint)

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
- Satisfies: F1.
- (today: — ; the attribute→indicator mapping is not declared anywhere)

**4. Dependency resolver**
Role: takes the dirty-set and orders it so every input is computed before the
output that needs it (direct indicators → composites → rollups), and rejects
cycles. Produces the execution order the coordinator follows.
- Characteristics: topological; cycle-rejecting; derived from the registry.
- Satisfies: F2, F3.
- (today: `indicator_dependency` graph + its acyclicity trigger)

**5. Run / task state**
Role: the durable record of a run and each unit of work inside it, with status
(queued / running / succeeded / failed / skipped / cancelled). It is what the UI
reads and what lets a crashed run resume instead of restarting.
- Characteristics: durable; queryable; one row per unit.
- Satisfies: F6, N2.
- (today: `jobs_runtime.job_run`)

**6. Work queue + claim**
Role: holds the units ready to run and hands each to exactly one worker, so two
workers never compute the same unit. Units that keep failing are moved aside
(dead-letter) instead of looping forever.
- Characteristics: at-least-once delivery; idempotent claim; dead-letter on
  retry-limit.
- Satisfies: F6, N3, N8.
- (today: a `JobRunRepository.claim_job` contract method (`SELECT … FOR UPDATE
  SKIP LOCKED`) is defined but not yet wired into any worker; no queue or
  dead-letter)

**7. Worker pools**
Role: the processes that actually run one unit of work each — reading the
scenario's effective inputs, running the model, producing the output. Different
work needs different resources, so units are routed to an IO pool, a CPU pool
(heavy geometry), or a GPU pool. A running unit can be told to stop.
- Characteristics: resource-profiled (IO/CPU/GPU); bounded concurrency;
  cooperatively cancellable.
- Satisfies: F4, F7, N4.
- (today: pure compute modules in `reimagina/jobs/*`, run synchronously via the
  CLI; `apps/worker` is a scaffold — no pool runtime)

**8. Coordinator**
Role: the scheduler — the brain of a run. It asks the impact engine for the
dirty-set, the resolver for the order, dispatches the units that are ready,
advances the next wave as each completes, and decides the run's final status,
including partial-success (a non-critical unit failed) and cancellation.
- Characteristics: event-driven; dependency-aware; owns partial-success +
  cancellation logic.
- Satisfies: F1, F2, F6, F7.
- (today: —)

**9. Result store**
Role: persists each computed output as the single *current* value for its
(feature, scenario, indicator), replacing the prior current value atomically so
a re-run never leaves duplicates or half-states.
- Characteristics: idempotent (replace-in-place); one current value per key;
  provenance-stamped.
- Satisfies: F1 (single-current invariant). Selectivity itself is on 3/11.
- (today: `indicator_result` with current/stale status + partial-unique indexes)

**10. Validation**
Role: checks each output against its declared post-conditions (within bounds,
acceptable quality) and fails the unit honestly rather than storing a bad value.
The bounds are **declared once** (on the catalog entry) and enforced by a single
validator — never re-implemented per backend or per layer.
- Characteristics: per-output; **single declarative source** (no parallel
  re-implementations); fail-closed.
- Satisfies: F9.
- (today: enforced by a Postgres write-trigger and separately re-implemented in
  Python for the dev backend — two definitions to keep in sync; a single
  declarative validator would collapse them)

**11. Reuse cache**
Role: before running a unit, checks whether its effective inputs (and code
version) are unchanged from a prior successful run; if so, reuses that result
and skips the compute. Turns "recompute the dirty-set" into "recompute only what
actually changed."
- Characteristics: content-addressed (input fingerprint); safe to miss (a miss
  only costs recompute).
- Satisfies: N5.
- (today: —)

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
- (today: provenance columns `job_run.code_version` + `indicator_result.model_version`;
  no lineage graph and no progress stream)

**13. Orchestrator interface (port)**
Role: the narrow interface the rest of the application depends on to start runs,
read status, and subscribe to progress — so the whole engine behind it can be
replaced (e.g. by Dagster) without touching the application.
- Characteristics: small (~10 methods); stable; implementation-hiding.
- Satisfies: N8, N9.
- (today: the repository protocols in `storage/interfaces.py`; no orchestrator
  port)

Coverage: every F1–F9 + N1–N9 maps to ≥1 component above. **N7 (ops
simplicity)** is a whole-system property, judged in §4, not a component.

### 2.2 Realizations (both shown; not a forced choice)

- **stdlib realization** — resolver (4) = `graphlib.TopologicalSorter`; reuse
  cache (11) = hand-rolled input fingerprint; worker pools (7) = `asyncio` (IO) +
  `ProcessPoolExecutor` (CPU); queue (6) = Postgres `SKIP LOCKED`; lineage (12) =
  `openlineage-python`. No broker, no daemon.
- **SOTA enhancement** — a micro-orchestrator (**Hamilton**) subsumes the
  **resolver (4)**, **reuse cache (11)** (content-addressed `data_version`
  caching), and **lineage (12)** (OpenLineage, via its materializers): outputs
  are declared as functions, and the graph, caching, and lineage are derived
  from that code in-process; it executes only the requested outputs. Trade: one
  dependency vs. owning graph + caching + lineage code. (Lineage needs the
  materializer / `@datasaver` metadata.) **Scope:** Hamilton supplies the
  per-unit *graph + memoization* only — it has no partition abstraction, so the
  scenario×feature scheduling stays in the impact engine (3) + coordinator (8);
  Hamilton is invoked per work-unit.
- **Validation realization** — declare bounds once (component 10) and enforce
  with a single declarative validator (e.g. **Pandera** for dataframe checks, or
  Hamilton `@check_output`), not a per-backend re-implementation.
- **Ops-plane queue ladder** — `SKIP LOCKED` (hand-rolled) → procrastinate /
  PgQueuer (Postgres task-queue *library*: retries, scheduling, DLQ; no new
  infra) → Hatchet (durable-workflow *service*) as complexity grows.

### 2.3 Reference architecture

```
 intake API ─► run/task state ◄─► coordinator ──► work queue ──► worker pools
   (2)             (5)              (8) │  ▲            (6)         (7) IO/CPU/GPU
                                 impact engine (3)                      │
                                 + resolver (4) ◄─ catalog/registry (1) │
                                                                        ▼
        lineage + telemetry (12) ◄──────── result store (9) ◄── validation (10)
                                           + reuse cache (11)
        orchestrator port (13) wraps the whole engine for the API.
```

### 2.4 Shared worked example (used again in §3)

**Scenario edit:** convert three avenue blocks into a **linear park**
(`land_use → green`, raise green attributes). Indicators: `green_coverage`
(green_area family), `walkability` (accessibility; reads green/land-use),
composite `liveability_index = 0.4·walkability + 0.3·transit + 0.3·green_coverage`.
`transit` is untouched.

Registry (1): `liveability_index` depends on `walkability`, `transit`,
`green_coverage`; `liveability_rollup` rolls `liveability_index` block→AGEB.

```python
# impact engine (3): changed attributes + edited features → affected outputs
changed = {"land_use", "green_density"}              # on the 3 park blocks
seed = {o for o in registry if o.reads & changed}    # green_coverage, walkability
seed |= spatial_expand(edited_features)              # + walkability of adjacent blocks
dirty = downstream_dependents(seed, registry)        # + liveability_index + liveability_rollup
# transit / fiscal not reached → excluded

# resolver (4): order so inputs precede dependents
order = topo(dirty)   # [green_coverage ∥ walkability] → liveability_index → liveability_rollup

# coordinator (8): dispatch waves via queue (6) → worker pools (7)
for unit in ready(order):
    if reuse_cache(11).hit(unit): reuse(unit); continue
    run(unit)                                         # read overrides, compute
    validate(10); result_store(9).put_current(unit); lineage(12).emit(unit)
```

Outcome: `green_coverage` and `walkability` run in parallel, then
`liveability_index`, then `liveability_rollup` (block→AGEB); `transit`/`fiscal`
never run; each result is bounds-checked, stored as current, lineage-stamped.

## 3. Solution B — Dagster

Platform built on **software-defined assets**: each output is an `@asset`
declaring its upstreams; Dagster owns the graph, a scheduling **daemon**, a
**web UI**, and a **metadata database**.

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

Editing the park scenario's inputs re-materializes `green_coverage(S)` and
`walkability(S)` → `liveability_index(S)` → `liveability_rollup(S)`, leaving
`transit(S)` and every other scenario untouched — same dirty-set, wave, and
outputs as §2; the engine, not application code, schedules it.

## 4. Comparison + recommendation

| Requirement | In-process (A) | Dagster (B) |
|---|---|---|
| F1 selective recompute | impact engine + reuse cache | Declarative Automation |
| F2/F3 order + composite | resolver / Hamilton | native asset graph |
| F4 heterogeneous + ML | worker pools; bespoke GPU/ML | native executors + MLflow |
| F8/N1 provenance + lineage | OpenLineage (table-level, free) | lineage UI (column-level paid) |
| N2 observability | status stream to build | Dagster UI included |
| N6 backfills | build into intake | native |
| N7 ops simplicity | **no standing services** | daemon + webserver + metadata DB |
| N8 scale path | single-node; multi-node = build/swap | native multi-node |
| N9 extensibility | registry entry + worker, behind port | new asset / Component |
| Cost to first run | **low** (no infra) | higher (adopt + operate platform) |
| Main risk | we own every component | platform ops + some paid features |

**Recommendation (SOTA).** A three-layer split — each of the 13 components owned
exactly once: own the domain core, borrow the compute-graph and ops-plane from
libraries, keep Dagster as a later option behind the port.

```
orchestration port (13)                                            ← ours
 ├ DOMAIN CORE (ours — domain truth in curated_geo / jobs_runtime):
 │    catalog (1) · intake (2) · impact engine / dirty-set (3) ·
 │    result store (9) · validation (10)
 ├ COMPUTE GRAPH (Hamilton — a library, no daemon / metadata DB):
 │    resolver (4) · reuse cache = data-version caching (11) ·
 │    lineage (12, via materializers).  Runs only the requested outputs.
 └ OPS PLANE (Postgres-backed, escalate only as needed):
      run/task state (5) · queue (6) · worker pools (7) · coordinator (8)
      SKIP LOCKED (have a claim primitive) → procrastinate / PgQueuer (library)
      → Hatchet (durable-workflow service)
 (optional Dagster adapter later — behind the port, not a rewrite)
```

Composition: the impact engine (3) computes the *downstream* dirty-set from
changed attributes; Hamilton then resolves the *upstream* inputs of those
requested outputs and skips unchanged ones via data-version caching (11). The
two directions compose — neither replaces the other. **Hamilton supplies the
per-unit graph + memoization only**: it has no partition abstraction, so the
scenario×feature scheduling, backfills, and run-tracking stay in the impact
engine (3) + coordinator (8). That partition/backfill machinery is exactly what
Dagster would absorb at maturity — which is why partitioned backfills are a
switch trigger, not a Hamilton feature.

**Build vs borrow.** Custom code ≈ the domain core + a one-query claim; the
orchestration plumbing is borrowed.

| | Build (domain logic — no tool supplies it) | Borrow |
|---|---|---|
| Components | catalog (1) · intake (2) · impact engine (3) · result store (9) · validation (10) · port (13) | graph + caching + lineage (4, 11, 12) = **Hamilton**; run-state + queue + retries + workers (5, 6, 7, 8) = `SKIP LOCKED` → procrastinate/PgQueuer → Hatchet |

The ops-plane ladder is **not** per-function custom code: rung 1 is a ~10-line
claim query; retries, dead-letter, cron, and durable execution are not
hand-written — climbing a rung makes the library/service provide them.

**Dual verdict.** *Next step* — the domain core + Hamilton (resolver, caching,
lineage as one library) + a Postgres ops-plane is preferable to adopting
Dagster: no daemon, metadata DB, or partition taxonomy, and domain truth stays
in our schema. *Mature future* — many scenarios × regions, routine backfills, a
lineage UI, ML/GPU scheduling — Dagster is the benchmark, reached via the
adapter.

**Switch to Dagster when ≥2 hold:** trained ML models need versioning / GPU
scheduling; pipelines span many regions with routine backfills; a lineage UI
becomes a product requirement; execution must go multi-node.

## 5. Alternatives — positioning (2026)

Not whole-Dagster substitutes — each covers a different slice of the §2
components; the §4 design composes the best-fit ones.

| Tool | Covers (components) | Best for | Pick when |
|---|---|---|---|
| **Hamilton** | 4, 11, 12 — graph + `data_version` caching + OpenLineage; runs only requested outputs | the compute-graph layer | **now** (top pick). Caveats: lineage needs materializer/`@datasaver` metadata; no partition abstraction — scenario/feature scheduling stays in 3+8 |
| **procrastinate / PgQueuer** | 5, 6 (+ retries, scheduling, locks, DLQ) | Postgres task-queue *library*, no new infra | ops-plane rung above `SKIP LOCKED`, below Hatchet |
| **Hatchet** | 5, 6, 7, 8 — durable runs/queue/workers/coordinator (Postgres) | durable-workflow *service* | durable execution / dynamic concurrency / monitoring UI needed. Not a lineage platform |
| **SQLMesh** | SQL-expressible 1, 3, 4, 9, 10, 11, 12 (incremental, plan/apply, audits, free column lineage) | SQL-transform governance | **companion** for warehouse transforms — not central (PostGIS/geometry/ML/overrides aren't SQL) |
| **Dagster** | all | full asset platform | mature scale (benchmark) |
| **Prefect 3** | 2, 5, 6, 7, 8 — ops/runtime, dynamic + Pythonic | general Python workflows / API-triggered jobs | not over Dagster for a durable asset graph + backfills |

Not first choice: **Airflow** (task/schedule-centric, heavy), **Kestra** (YAML
platform, standing infra), **Temporal** (durable sagas — wrong axis), **Windmill**
(internal-tools lean). Adjacent: **Orchestra** (managed), **Bruin** (all-in-one OSS).

Ranking: compute graph = Hamilton · ops-plane library = procrastinate → Hatchet ·
SQL companion = SQLMesh · full platform (later) = Dagster · general workflow = Prefect.

## 6. Appendix — Dagster facts (2026)

| Claim | Status |
|---|---|
| Core `1.13.10` / libs `0.29.10` (Jun 2026) | current stable |
| Declarative Automation stable since 1.9; Components GA Sept 2025 | confirmed |
| Column-level lineage + owner-search are paid (Dagster+) | confirmed |
| No free managed tier | confirmed |
| Airflow 3.2 / Prefect 3 narrowing the asset/lineage gap | confirmed |
| Fit for a PostGIS-per-region + ML workload | inferred — no public benchmark covers it |

Sources: github.com/apache/hamilton · sqlmesh.readthedocs.io ·
kestra.io/resources/data/prefect-alternatives · windmill.dev/docs/compared_to/kestra ·
getorchestra.io/blog/top-dagster-alternatives-in-2026 ·
getbruin.com/blog/best-data-pipeline-tools-2026

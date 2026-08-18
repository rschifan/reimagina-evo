# DBOS Ops-Plane: Presentation Brief

Read this beside `PHASE3_OPS_PLANE_HATCHET_PRESENTATION.md`. For the single side-by-side ownership map, read `PHASE3_OPS_PLANE_COMPARISON_PRESENTATION.md`.

---

## 1. What This Option Is

DBOS is a Python library for durable workflows. It records workflow checkpoints in Postgres. When a process stops partway through a run, an executor can resume from the last completed checkpoint instead of starting the whole run again. An indicator unit is one calculation in that workflow.

It is not a separate orchestration server. The API submits and reads progress; workers execute units; and both use the same Postgres database for domain data, run history, and checkpoints.

---

## 2. How A Run Works

1. A scenario edit or explicit request identifies the indicators that may be stale.
2. The system calculates the affected indicator set and its dependency order.
3. The unit list becomes durable workflow input. The list of *features* each unit must recompute is written to a table first and referenced by a short handle, because on a full-city edit that list is a quarter of a million ids and does not belong in a message.
4. A worker executes ready units. Units in the same dependency wave may run in parallel.
5. A unit writes its result in one database transaction: either the old value remains current, or its replacement is fully committed.
6. The run records a final business outcome: succeeded, partial, failed, or cancelled.

Independent work continues after a failure. A unit whose required dirty input failed is skipped; a non-dirty input does not block because its existing value is still valid.

---

## 3. Strengths

- **Smallest operational footprint.** One database, no broker, no orchestration container and no engine credential.
- **SQL-native history.** Runs and units are retained with the domain data, can be joined to business entities, and can be aggregated without calling another service.
- **Business status is first-class.** `partial`, `skipped`, reasons, and terminal-state rules are stored exactly when they occur.
- **Good fit for long PostGIS units.** A few seconds of scheduling delay is insignificant when a real unit runs for minutes.
- **Runtime-extensible indicator catalog.** Adding an indicator does not require a workflow redeploy; the registry is read when work is planned.
- **Straightforward local development.** One process can submit and execute locally.
- **Direct diagnosis.** The authoritative state is ordinary Postgres data rather than an engine projection.

---

## 4. Costs And Limits

- **We own the scheduler.** Dependency waves, failure propagation, status roll-up, and their tests are application code — about a quarter more executable code than the Hatchet option carries, not less.
- **Wave barriers reduce parallelism.** The next layer waits until every unit in the current layer settles. This is structurally slower than per-task readiness for wide DAGs.
- **No operator console by default.** There is no ready-made DAG view, task log viewer, replay button, metrics or tracing surface.
- **No built-in admission control.** Repeated submits and overlapping writes need an explicit product policy.
- **Recovery is scoped to an executor identity.** A live peer does not automatically adopt work abandoned by another executor.
- **Deploys require discipline.** In-flight work must remain associated with compatible code until it drains.

---

## 5. Evidence From The Comparison

The workload suite measures scheduling separately from real spatial computation. A score of 1.0 is ideal: ready work starts with no avoidable scheduling delay.

| Measure | DBOS | Hatchet |
|---|---:|---:|
| Diamond DAG delay relative to ideal | 3.19 | 1.13 |
| Lattice DAG delay relative to ideal | 3.54 | 1.21 |
| Typical scheduling delay before a ready task starts | 1.9-3.0 s | 80-100 ms |

The two DAGs quoted here are the reproducible ones — repeat runs agree to within 0.4%. Other shapes in the suite vary by 13-35% between runs and are order-of-magnitude only; they are not quoted. The Hatchet delay figure is typical rather than universal: one shape in the suite measured 1372 ms.

The run conditions behind these numbers — machine, date, repetitions, engine versions — are not on record. They should be before this table decides anything.

This matters most for many short units. Real indicator units run for minutes, so the measured DBOS scheduling overhead is usually a small part of total wall-clock time.

---

## 6. Production Readiness Gates

Do not deploy this option for concurrent scenario editing until these are closed:

1. **Admission control:** define and implement deduplication plus one-writer-at-a-time serialization for each scenario or write scope.
2. **Cross-executor recovery:** establish a supported recovery procedure for a worker that disappears permanently, and prove it does not double-execute units.
3. **Observability:** add metrics, alerts and a practical operator view for stuck runs.
4. **Real-data correctness:** run the shipped catalog over the real city and check the answers against independent oracles. Hatchet has done this at full scope; this option has a timing script but no verifier, so its results are unproven on real data.
5. **Load validation:** test realistic PostGIS units, multiple workers, restarts and cancellation under saturation.

Provenance is **not** on this list. It is already closed on both options and enforced by the database: every result row carries the id of the execution that produced it, `NOT NULL`, with a foreign key and an existence trigger.

---

## 7. Choose DBOS When

Choose DBOS when the team values the simplest deployable system, SQL-native operational history, and full control of business semantics. It is the stronger fit for a small worker fleet and long-running spatial computations, provided the readiness gates above are treated as product work rather than operational details.

Do not choose it solely because it has fewer components if the product requires wide, short-task DAGs, rich operator workflows, or fully managed admission control from day one.

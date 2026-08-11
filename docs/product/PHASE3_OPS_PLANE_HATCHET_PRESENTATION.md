# Hatchet Ops-Plane: Presentation Brief

Read this beside `PHASE3_OPS_PLANE_DBOS_PRESENTATION.md`. For the single side-by-side ownership map, read `PHASE3_OPS_PLANE_COMPARISON_PRESENTATION.md`.

---

## 1. What This Option Is

Hatchet is a self-hosted workflow engine. The application declares an indicator DAG; the engine decides when each ready task can run, assigns it to workers, retains run state, applies retry and concurrency rules, and exposes a dashboard. An indicator unit is one calculation in that workflow.

This is not external SaaS, but it is an additional stateful service: it has its own container, database, credentials, upgrades, backups and capacity budget. The API submits to the engine; workers receive tasks from it and write results to the domain database; the engine stores its own queue and workflow state separately.

---

## 2. How A Run Works

Hatchet runs work that we define; it does not decide what the product should recompute or how a spatial result is safely written. The map above defines the boundary. A run follows this sequence:

1. **We plan:** turn a scenario edit or explicit request into a precise list of indicators and features to recompute.
2. **We submit:** send that list, plus correct deduplication and concurrency keys, to Hatchet. The features themselves are written to a table first and referenced by a short handle — inline they exceed the engine's message limit, and a full-city edit could not be submitted at all.
3. **Hatchet schedules:** create one durable run and dispatch each declared indicator as soon as its prerequisites are ready.
4. **We execute:** run each delivered indicator in one domain-database transaction and record provenance.
5. **Hatchet propagates:** cancel dependent tasks while independent branches continue.
6. **We protect and present:** cancel an active database statement when needed, then project engine state into the business verdict exposed by the API.

The engine's raw status and the product verdict intentionally answer different questions. For example, a run may be technically failed while the product verdict is `partial` because other indicators completed successfully.

---

## 3. Strengths

- **Native DAG scheduling.** Each task starts when ready; there is no application-managed wave barrier.
- **Fewer orchestration responsibilities.** Dependency scheduling, propagation, queueing, retry backoff and admission controls become engine capabilities rather than code to design and test. Note this is a shift in *what* is maintained, not a reduction in *how much*: the service adapter is larger than the scheduler it replaces, so total application code does not go down.
- **Built-in admission control.** A short deduplication window absorbs repeated clicks, and a concurrency key prevents two runs from writing the same scope together.
- **Operator experience.** Dashboard, DAG view, task logs, input/output inspection, replay, worker visibility and metadata search are available without building them.
- **Better behavior for wide DAGs.** The measured scheduler starts ready work in roughly 80-100 ms rather than waiting for a whole dependency wave.
- **Retries with policy.** Transient database failures can retry with backoff while deterministic errors and intentional cancellations do not.
- **Useful bulk operations.** Runs can be located and cancelled by scenario metadata rather than requiring every caller to know individual run identifiers.
- **Telemetry foundation.** Trace context and metrics are available from the engine.

---

## 4. Costs And Limits

- **A service to operate.** The engine adds a container, a separate state database, credentials, configuration, upgrades and backups.
- **A worker is mandatory.** Submission can succeed before the graph is registered by a worker; local in-process execution is not part of this model.
- **Connection footprint.** The engine keeps a meaningful number of idle Postgres connections before application work begins.
- **History is engine-governed.** Retention is configuration, not simply available disk; broad SQL aggregation is not the natural interface.
- **Business status is derived.** The product reconstructs its richer vocabulary from engine signals rather than writing it at each transition.
- **The graph is deployed code.** Adding an indicator changes the declared workflow and requires a worker deployment.
- **Configuration is behavior.** Retry, timeout, deduplication and concurrency settings can produce incorrect behavior if their keys or limits are wrong.
- **Engine upgrades are semantic upgrades.** The application depends on documented and tested engine behavior, not only an API surface.

---

## 5. Evidence From The Comparison

The workload suite measures scheduling separately from real spatial computation. A score of 1.0 is ideal: ready work starts with no avoidable scheduling delay.

| Measure | DBOS | Hatchet |
|---|---:|---:|
| Diamond DAG delay relative to ideal | 3.19 | 1.13 |
| Lattice DAG delay relative to ideal | 3.54 | 1.21 |
| Typical scheduling delay before a ready task starts | 1.9-3.0 s | 80-100 ms |

The two DAGs quoted here are the reproducible ones — repeat runs agree to within 0.4%. Other shapes in the suite vary by 13-35% between runs and are order-of-magnitude only; they are not quoted, in either direction. The Hatchet delay figure is typical rather than universal: one shape measured 1372 ms.

The run conditions behind these numbers — machine, date, repetitions, engine versions — are not on record. They should be before this table decides anything.

Separately, this option has been checked for **correctness** on real data, which the scheduling suite does not test: the shipped catalog over the full city, 86,225 blocks, against four independent oracles, all passing. The DBOS option has no equivalent result yet.

This advantage matters most for wide DAGs or shorter tasks. Real indicator units run for minutes, so it should be weighed against the extra service rather than treated as an automatic winner.

---

## 6. Production Readiness Gates

Do not deploy this option until these are closed:

1. **Worker recovery:** prove that clean exits, crashes, engine restarts and saturation remove dead workers and reassign their work without leaving runs permanently stuck.
2. **Capacity:** establish a connection budget for the engine and workers, then load-test it against realistic PostGIS work.
3. **Admission safety:** prove that deduplication keys distinguish different edits and that concurrency keys serialize precisely the rows that may conflict.
4. **Status contract:** test the derived product verdict under partial failure, skipped work, cancellation and engine unavailability.
5. **Retention and export:** set the required run-history retention and decide how historical reporting will work after engine retention expires. The stored plan each run is submitted with must outlive the engine's own retention, or a run the dashboard still offers to replay cannot actually be replayed.
6. **Security and operations:** configure non-default credentials, TLS, health checks, backup, metrics and alerting before exposing the dashboard or API.

Provenance is **not** on this list. It is already closed on both options and enforced by the database: every result row carries the id of the execution that produced it, `NOT NULL`, with a foreign key and an existence trigger. It survived deleting this option's run tables, which is the point of keeping it in the domain rather than in the scheduler.

---

## 7. Choose Hatchet When

Choose Hatchet when the product benefits from a wide DAG, frequent parallel work, retries and admission control, and when operators will genuinely use the dashboard, logs and replay tools. It is the stronger choice when the team is willing to operate an orchestration service as a product dependency.

Do not choose it merely for a dashboard. It is not appropriate until worker recovery and capacity behavior have been proven, and it is the wrong fit when the non-negotiable constraint is “no additional stateful service.”

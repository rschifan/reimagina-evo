# DBOS And Hatchet: Capability Comparison

This is the decision document for the two ops-plane options. The two companion briefs explain each option in more detail:

- `PHASE3_OPS_PLANE_DBOS_PRESENTATION.md`
- `PHASE3_OPS_PLANE_HATCHET_PRESENTATION.md`

The central question is not whether either engine can run Python. Both can. The decision is **which orchestration responsibilities the engine takes over, and which remain product code**.

> **Describes:** `feat/ops-plane-dbos` at `8034f04` and `feat/ops-plane-hatchet` at `26527fc`, both forked from `3d0a2cc`. Both branches are still moving; check the commits before relying on a row.

## Who Provides What

### Plan, run, and retry

| Capability | DBOS: engine and our code | Hatchet: engine and our code | Decision impact |
|---|---|---|---|
| 1. Decide what changed | **Engine:** nothing.<br>**We build:** translate an edit into affected indicators and features. | **Engine:** nothing.<br>**We build:** the same planning logic. | Same: this is domain knowledge, not orchestration. |
| 2. Submit and persist a run | **Engine:** durable workflow input and queueing in our Postgres.<br>**We build:** request validation and workflow input. | **Engine:** durable run state and queueing.<br>**We build:** request validation and run input. | DBOS: one database. Hatchet: a complete run-control service. |
| 3. Schedule dependencies | **Engine:** nothing beyond durable workflow execution.<br>**We build:** wave scheduler, readiness checks, and dependency order. | **Engine:** per-task readiness, fan-out, and dispatch.<br>**We build:** task graph and skip conditions. | Hatchet: native scheduling for wide DAGs. DBOS: no service to operate. |
| 4. Execute an indicator | **Engine:** checkpointed workflow steps.<br>**We build:** runner and domain-database transaction. | **Engine:** task delivery, deadlines, retry machinery, and logs.<br>**We build:** runner and domain-database transaction. | Hatchet: richer task controls. DBOS: simpler execution path. |
| 5. Isolate failures | **Engine:** nothing.<br>**We build:** skip dependents and continue independent work. | **Engine:** dependency failure propagation.<br>**We build:** define and test the domain rule. | Hatchet: less custom scheduler code. |
| 6. Retry work | **Engine:** step retry mechanism.<br>**We build:** conservative retry policy and error classification. | **Engine:** backoff and retry execution.<br>**We build:** retry classification and safe limits. | Hatchet: native backoff and per-task policy. |

### Cancel, describe, and observe a run

| Capability | DBOS: engine and our code | Hatchet: engine and our code | Decision impact |
|---|---|---|---|
| 7. Cancel work | **Engine:** stop future workflow scheduling.<br>**We build:** cancel active Postgres work and preserve consistent results. | **Engine:** accept cancellation and stop future scheduling.<br>**We build:** cancel active Postgres work and preserve consistent results. | Same: both need domain-level database cancellation. |
| 8. Define business status | **Engine:** nothing.<br>**We build:** write first-class business status and reasons. | **Engine:** task and run state.<br>**We build:** derive the business verdict from engine state. | DBOS: status is direct, queryable business data. |
| 9. Read status | **Engine:** workflow existence.<br>**We build:** business run and unit detail. | **Engine:** run and task state.<br>**We build:** map it to the product API contract. | DBOS: SQL-native detail. Hatchet: dashboard-native detail. |
| 10. Keep run history | **Engine:** durable checkpoints.<br>**We build:** SQL history, retention, and reporting. | **Engine:** retained run history and metadata lookup.<br>**We build:** retention policy, export, and broader reporting. | DBOS: unbounded SQL reporting. Hatchet: ready-made operational history. |
| 11. Stream progress | **Engine:** ordered, resumable progress transport.<br>**We build:** client event format. | **Engine:** immediate task lifecycle notifications.<br>**We build:** client event format and status projection. | DBOS: resumable event stream. Hatchet: immediate engine notifications. |

### Recover and operate

| Capability | DBOS: engine and our code | Hatchet: engine and our code | Decision impact |
|---|---|---|---|
| 12. Recover after a crash | **Engine:** resume work for the same executor identity.<br>**We build:** a stable, unique executor identity per process. Reclaiming another executor's work is **out of scope by design**, not a planned deliverable — an operator procedure, if one is wanted. | **Engine:** worker assignment and task recovery.<br>**We build:** prove recovery behavior and operate worker lifecycle. | Hatchet potentially removes recovery code, but it must be proven under failure. |
| 13. Control admission | **Engine:** nothing.<br>**We build:** deduplicate repeated work and allow one active writer per scope. | **Engine:** idempotency and per-scope concurrency limits.<br>**We build:** correct work and write-scope keys. | Hatchet: native admission controls; DBOS requires product code. |
| 14. Record provenance | **Engine:** nothing.<br>**Already built:** every result row cites its database execution, enforced by a foreign key and a trigger. | **Engine:** nothing.<br>**Already built:** the same model, unchanged by deleting the run tables. | Same, and already closed on both. Not a gate. |
| 15. Operate the system | **Engine:** nothing.<br>**We build:** inspection, alerts, metrics, and runbook. | **Engine:** dashboard, logs, replay, tracing, and metrics endpoints.<br>**We build:** configure, secure, monitor, and operate the service. | Hatchet: much richer operator surface. DBOS: much smaller footprint. |

## What The Table Means

### DBOS shifts less responsibility out of the application

DBOS gives the application durable execution, queueing, checkpoints, and progress transport. The application still owns the scheduler, failure propagation, business status, run history, admission control, and operations.

This is an advantage when those behaviors must be business-specific, SQL-queryable, and fully understood by the team. It is a cost when the team does not want to maintain scheduler and operations code.

### Hatchet shifts more responsibility into the engine

Hatchet takes over task scheduling, dependency propagation, retries, admission control, task state, and the operator surface. The application still owns planning, safe database writes, cancellation of active SQL, provenance, status interpretation, and all configuration choices that encode product policy.

This is an advantage when the team wants native DAG scheduling and operational tooling. It is a cost because the engine becomes a production dependency, and configuration mistakes can change business behavior.

## The Real Trade-Off

| If the priority is... | Favor | Why |
|---|---|---|
| Fewest production components | DBOS | It runs in the existing application and Postgres database. |
| Wide DAGs and high parallelism | Hatchet | It schedules every ready task instead of waiting for a wave barrier. |
| Long-running spatial units and a small worker fleet | DBOS | Its scheduling overhead is small relative to minutes of computation. |
| Deduplication and one-writer-at-a-time safety | Hatchet | The engine supplies the primitives; DBOS needs them designed and built. |
| SQL reporting and durable business history | DBOS | Run and unit state are ordinary application data. |
| Dashboard, logs, replay, and worker visibility | Hatchet | These are engine features rather than new product work. |
| Complete control of business status semantics | DBOS | Status is written directly rather than derived from engine state. |
| A team ready to operate a stateful orchestration service | Hatchet | The extra service buys scheduler and operator capability. |

## Before Choosing Either

DBOS needs admission control, an operator procedure for a permanently dead executor, observability, real-data correctness verification, and load validation.

Hatchet needs proven worker recovery, capacity validation, correct admission keys, a tested status contract, retention/export policy, and security hardening.

Provenance is on neither list: it is already built and database-enforced on both, and it survived Hatchet deleting its run tables.

Neither option removes the need for careful domain code around planning, transactional result writes, and active database cancellation.

## Two Things The Table Does Not Show

**Less responsibility is also less code, but only if you measure it correctly.** Hatchet owns about a quarter less executable code than DBOS — 602 lines against 816, or 359 statements against 466. A raw line count says the opposite, because Hatchet's modules are up to 55% comment and docstring; `wc -l` on this codebase measures explanation, not machinery. If anyone re-derives this number, they will get the wrong sign unless docstrings are excluded.

**The two options are not equally evidenced yet.** Hatchet has been verified for correctness on the real city at full scope against independent oracles. DBOS has not. Until that gap closes, the scheduling numbers compare the two options but the correctness evidence does not.

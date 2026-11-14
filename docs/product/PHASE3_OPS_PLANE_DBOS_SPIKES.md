# Track A — DBOS spike results

Executed against `dbos 2.28.0` and PostgreSQL 17.10 before writing any Branch A
code, as the ops-plane plan requires. Every claim below was produced by running
something, not by reading documentation. Where a spike contradicted the plan,
the plan is wrong and this document is right.

Harnesses live outside the repo (they are throwaway), but each is described
precisely enough to rebuild.

---

## Summary

| # | Spike | Result |
|---|---|---|
| 1 | Kill -9 mid-wave, cross-executor recovery | **pass** — exactly one unit recomputed |
| 2 | Registry drift under replay | **fail, worse than predicted** — silent wrong data, no error |
| 3 | Dirty-set / wave order stability | **failed, then fixed** — real bug on `main` |
| 4 | Exception picklability | **pass — plan was wrong**, psycopg2 errors pickle fine |
| 5 | Blue-green with in-flight runs | **pass** — version isolation holds, plus a free operator signal |
| 6 | Progress streams, offset resume | **pass** — ordered, resumable, closes cleanly |
| 7 | Connection budget | **pass** — DBOS costs ~3 connections idle |
| 8 | Schema co-location | **pass** — `pg_dump -n app` stays self-contained |

Two spikes changed the design. Three corrected the plan.

---

## A0 — dependency facts, re-derived

The plan says to re-derive these at build time rather than inherit them. Doing so
found the pin unusable.

| Claim in the plan | Verified state (2026-07-28) |
|---|---|
| Pin `dbos>=2.29,<3` | **Unsatisfiable.** Latest stable is **2.28.0** (released 2026-07-21). Pinned `>=2.28,<3`. |
| PR #786 (idempotent recovery) missed 2.28.0 | **Correct.** Merged `2026-07-21T21:26:34Z`; 2.28.0 published `15:20:16Z`, six hours earlier. Its release body lists #773–#788 but not #786. |
| `DBOS._recover_pending_workflows` is private | **Correct.** The only public-looking entry point lives in the private `_recovery` module; `dbos`'s top level exports no recovery API. |
| MIT, ungated | **Correct.** `Copyright (c) 2024 DBOS, Inc.`, no additional clauses, no field-of-use restriction. |
| `executor_id` defaults to `"local"` | **Correct**, and confirmed before launch. Overriding it is mandatory. |

Consequence of shipping without #786: recovery is not idempotent, so a sweep
that fires against an executor which is not actually dead can double-execute.
That is an argument about *when to build the sweep*, not about the pin — see
"What this changes".

---

## Spike 1 — kill -9 mid-wave (the one that mattered)

Two processes with distinct `executor_id`s, a three-unit run (two fast, one
30 s), `SIGKILL` to executor A during the long unit. A `compute_log` table counts
every execution, so redundant compute is measured rather than assumed.

**After the kill:** `run_workflow` PENDING on `exec-A`, units 0 and 1 SUCCESS,
unit 2 PENDING. Executions: one each.

**A live peer does not adopt orphans.** Executor B ran for 25 s alongside and
changed nothing — confirming the library recovers only its own `executor_id`, and
that the cross-executor sweep is genuinely required rather than belt-and-braces.

**The sweep works.** `DBOS._recover_pending_workflows(["exec-A"])` from B returned
two handles and drove the run to `SUCCESS` with `executor_id` flipped to `exec-B`:

```
compute_log   u0 runs=1  [exec-A]          <- not recomputed
              u1 runs=1  [exec-A]          <- not recomputed
              u2 runs=2  [exec-A, exec-B]  <- the interrupted one, re-run
app.result    u0=1  u1=1  u2=1             <- replace-in-place, no duplicates
```

Redundant compute is exactly one unit — the minimum possible. This is the
headline operational property of the track, and it holds.

## Spike 2 — registry drift under replay (the one that changed the design)

The plan predicted a loud failure: `DBOSUnexpectedStepError`. **That is not what
happens, and the truth is worse.**

Two workflows over the same units — one reading its registry from a module
global (the natural way to write it), one taking it as a workflow input. Both
interrupted, the registry changed, both resumed.

```
registry at resume : ["vNEW", "v0", "v1"]      (vNEW inserted at the front)
module-global wf   -> "G:v0, G:v0, G:v1"       no error raised
input-passing wf   -> "I:v0, I:v1"             unaffected
compute_log        -> G:vNEW never ran at all
```

Position 0 asked for `vNEW` and received **`v0`'s recorded output**. No
exception, no warning, a plausible-looking result, and one indicator's value
silently attributed to another.

The cause is structural, not incidental: DBOS matches replayed steps by
`(function_id, function name)`. Our design calls *the same* step function once
per indicator, so the names always match and position alone decides which
checkpointed output you get. Any change that shifts positions re-attributes
results.

**This makes DAG-as-workflow-input mandatory, for a sharper reason than the plan
gave.** A crash is safe; this is data corruption. It also means the same hazard
applies to any workflow whose steps are one function called N times — which is
precisely the shape of `run_workflow`.

## Spike 3 — order stability (a real bug, fixed on `main`)

`compute_dirty_set` returned a different order in every process: ten processes,
ten distinct orders on a 20-indicator DAG, all topologically valid. Fixed at the
source in `f6ff6ef`; see that commit for why it gates both branches.

Worth recording: sorting the sorter's *keys* took it from ten orders to two,
which reads as success. The predecessors are sets too. Both halves must be
sorted.

`plan_wave` was cleared in the same spike — given a fixed input it is
byte-identical across processes, so it inherits order rather than generating it.

## Spike 4 — exception picklability (plan was wrong)

The plan states a psycopg2 error "carrying a live cursor/diag will not" pickle,
and proposes a `UnitFailed` domain exception to work around it.

Measured, on 2.28.0:

| case | pickles? |
|---|---|
| `psycopg2.errors.UndefinedTable`, cursor still **open**, `.diag` attached | **yes** |
| same error after the connection is closed | yes |
| `sqlalchemy.exc.ProgrammingError` wrapping it | yes |
| an exception holding a closure | no |

Only a genuinely unpicklable payload fails, which is not what our runners raise.
**`UnitFailed` is not required for serialization.** It may still be worth having
as a stable domain error, but that is a design preference, not a constraint —
and 2.28.0 additionally exposes a `serializer` config key if it ever becomes one.

## Spike 5 — blue-green

A run left PENDING by a process at `application_version=blue`:

- a **green** process sweeping the same `executor_id` recovered **0** — it does
  not steal in-flight work;
- a **blue** process recovered **1**.

Version isolation holds, so the documented blue-green drain is real. Unlooked-for
bonus: DBOS logs `Current version 'blue' is not the latest version. Latest
version is 'green'.` — an operator signal that an old-version process is still
draining, which the plan did not know existed.

## Spike 6 — progress streams

`write_stream` from the workflow body, five unit events plus a run-level
terminal event, then `close_stream`:

```
full replay from offset 0 : ['u0','u1','u2','u3','u4', None]
resume at offset=6        : []          (no gap, no duplicate)
```

Ordered, offset-resumable, closes cleanly. Suitable to replace the LISTEN/NOTIFY
SSE plumbing, and the run-level `indicator=None` event maps directly onto the
port's existing terminal-event convention.

## Spike 7 — connection budget

One launched, idle DBOS process holds **3** Postgres connections; four concurrent
workflows took it to **5**. DBOS's own pool is not the constraint — the runners'
own psycopg2 connections are, and they are additive at
`worker_concurrency × 1`. Against `max_connections = 100` there is ample room.

## Spike 8 — schema co-location

`dbos_system_schema="dbos"` creates 11 tables in a `dbos` schema beside the
application's own. `pg_dump -n app --schema-only` emits the app DDL with **no
reference to the dbos schema**, so application backups stay coherent without
dragging checkpoint state along. DBOS tracks its own migrations in
`dbos.dbos_migrations`, which does not collide with alembic.

---

## What this changes for Branch A

1. **DAG-as-input is a correctness requirement, not a style rule.** Spike 2
   promotes it from "reduces risk" to "prevents silent misattribution of
   indicator results". Any reviewer tempted to simplify it back to a module
   global should read that spike first.
2. **`UnitFailed` can be dropped** unless wanted for its own sake. Spike 4
   removes its stated justification.
3. **Build the cross-executor sweep last, and gate it on a conservative liveness
   signal.** It works (spike 1), but 2.28.0 lacks the idempotent-recovery fix, so
   a sweep that fires against a live executor can double-execute. Single-executor
   startup recovery is unaffected — a process only ever recovers its own id, at a
   moment when by definition it is not running them.
4. **Pin `dbos>=2.28,<3`** and revisit when 2.29 ships.

> **Status, 2026-11-09 — 3 and 4 are done.** The pin is `dbos>=3.4,<4`. DBOS 3's
> `_recovery._recover_workflow` re-enqueues rather than executing directly, so a
> recovered workflow starts through the queue's atomic ENQUEUED→PENDING dequeue
> and duplicate recovery requests admit exactly one runner. The sweep is built
> (`reimagina.orchestration.reclaim`), the gate is kept — the fix makes racing
> *sweepers* safe, not sweeping a live executor — and
> `tests/recovery/test_crash_recovery.py::test_a_peer_reclaims_a_dead_executors_run`
> proves it end to end with a real `SIGKILL` and two distinct executor ids.
> Recommendations 1 and 2 are unchanged. See `docs/architecture/JOB_LAYER.md`
> — *Recovery — two mechanisms*.

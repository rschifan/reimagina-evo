# Track B — Hatchet spike results

Run against `hatchet-sdk 1.35.1` and a self-hosted `hatchet-lite` engine on
PostgreSQL 17.10, before writing Branch B code. Where a spike contradicted the
plan, the plan is wrong and this document is right.

## B0 — dependency and engine facts

| Claim in the plan | Verified state (2026-07-28) |
|---|---|
| `hatchet-sdk>=1.35,<2` | **1.35.1 is current.** Pin holds. |
| MIT | **Confirmed** from wheel metadata. |
| `ParentCondition`, `ConcurrencyExpression`, `ConcurrencyLimitStrategy` exist | **All present.** |
| `ctx.was_skipped` is how a skip is detected | **Present.** |
| Run status has no `partial`/`skipped` | **Confirmed from the SDK enum**: `QUEUED, RUNNING, COMPLETED, CANCELLED, FAILED`. This is the decided fidelity loss, now measured rather than asserted. |

### Two engine constraints the plan did not have

- **The engine's database must be UTC.** It refuses to start otherwise:
  `primary database instance timezone is set to 'Europe/Rome' but must be
  'UTC'`. Fixed per-database with `ALTER DATABASE hatchet SET TIMEZONE='UTC'`.
  Worth recording for the comparison: the same local PostgreSQL served the DBOS
  track with no such requirement, because that track has no engine to satisfy.
- **`SERVER_AUTH_COOKIE_DOMAIN` is mandatory**, even headless with no dashboard
  user. Absent, the API exits with `cookie domain is required`.

## B1 — `skip_if` CEL set membership (the gating spike)

The plan calls this "the one unverified API… everything else depends on it".

**It works.** CEL evaluates set membership against a parent task's output, so a
`plan` task can return the dirty slug list and every indicator task can decide
for itself whether to run. The static-DAG design is viable.

**But the polarity is inverted from the plan's sketch.** The plan writes:

```python
skip_if=[ParentCondition(parent=plan, expression="<slug not in output.dirty>")]
```

reading as *"skip if the slug is not dirty"*, which is the intent — but the plan
elsewhere describes the expression as selecting what runs. Measured, with
`dirty = ["alpha", "gamma"]` over tasks alpha/beta/gamma:

| expression | alpha | beta | gamma |
|---|---|---|---|
| `'{slug}' in output.dirty` | **SKIPPED** | QUEUED | **SKIPPED** |
| `!('{slug}' in output.dirty)` | QUEUED | **SKIPPED** | QUEUED |

`skip_if` means *skip when this is true*. The second row is the dirty set, so
the correct expression is the negated one. Getting this backwards produces a
run that computes exactly the indicators it should have left alone — and every
task reports `COMPLETED`, so nothing looks wrong.

This is the shape of bug that would survive review: the DAG builds, the run
succeeds, the dashboard is green, and the wrong values are served.

## B2 — skip inheritance

A composite is dirty; one of its two inputs is **not**. Our rule says the
composite must still run — a non-dirty input kept its current value and does not
block (`coordinator.run_dirty_set`).

Measured: `in_clean` **SKIPPED**, and `composite` **ran anyway**, reporting
`ctx.was_skipped(in_clean) == True`.

So Hatchet agrees with our rule. The plan predicted this and warned it is
load-bearing and undocumented: declaring `skip_if` anywhere disables the
engine's default skip-inheritance graph-wide. Confirmed behaviourally, but it
remains an emergent property rather than a stated contract — **pin the engine
image and cover it with a test**, because an upgrade could flip it silently and
the symptom would be composites quietly not recomputing.

## B3 — failure propagation

`doomed` fails; `dependent` depends on it; `independent` does not.

| task | outcome |
|---|---|
| `doomed` | FAILED |
| `dependent` | **CANCELLED** |
| `independent` | COMPLETED — *after* the failure |

Exactly our F6 rule, inherited rather than implemented: the dependent does not
run, the independent branch is untouched, and the run stays alive until the
healthy branch finishes. This is the strongest argument for the service track —
behaviour we hand-wrote in `wave.py` comes free.

## B6 — payload headroom

Every child receives every parent's full output, and `plan` parents every node,
so `plan`'s return value is the one that scales.

| catalog size | plan output | % of the 4 MiB gRPC frame |
|---|---|---|
| 4 (shipped today) | 145 B | 0.003% |
| 200 | 7.2 KB | 0.17% |
| 2 000 | 72 KB | 1.72% |

Returning slugs only — not features, not values — keeps this a non-issue for any
plausible catalog. The 3 MiB dispatcher threshold above which results are
*silently dropped* is never approached.

## B7 — status reconstruction: what is actually lost

The decided fidelity loss, measured rather than argued.

| our vocabulary | Track A (DBOS) | Track B (Hatchet) |
|---|---|---|
| unit succeeded | `succeeded` | `COMPLETED` |
| unit failed | `failed` | `FAILED` |
| unit **skipped — not dirty** | never created | **`COMPLETED`** |
| unit **skipped — input failed** | `skipped` | `CANCELLED` |
| run **partial** (some units worked) | `partial` | **`FAILED`** |

Two losses, and they are not the same size.

**A skipped indicator is indistinguishable from a computed one.** `in_clean` had
`initial_state = SKIPPED` and `readable_status = COMPLETED`. Anyone reading run
status — a dashboard, an alert, an operator — sees a green task that never ran.
Recoverable only via the internal `initial_state` column or the
`{"skipped": true}` output sentinel through `ctx.was_skipped`.

**A run that half-worked reports `FAILED`.** One failed indicator out of three
takes the whole run's status down, with no way to say two succeeded. Callers
that today distinguish "retry everything" from "retry one unit" cannot.

Neither is a defect — it is what adopting an engine's status model costs, and it
is the row the comparison exists to show.

## B4 — cancelling a live statement

A task holding `SELECT pg_sleep(45)` on a real connection, cancelled ten seconds
in. Measured:

- the task reports **`CANCELLED`** immediately;
- the statement **keeps running** — `pg_stat_activity` still shows `pg_sleep` on
  that backend;
- the worker logs, verbatim: *"thread … is still running after cancellation.
  This could cause the thread pool to get blocked and prevent new tasks from
  running."*

So the slot is **not** released, and Hatchet says so itself.
`enable_force_kill_sync_threads` defaults false and is not a fix worth having —
killing a thread mid-transaction is worse than waiting.

This is the same conclusion the DBOS track reached: **neither engine can
interrupt a thread blocked in a psycopg C call.** It is not a difference between
the tracks, and the comparison should say so rather than implying either
solves it.

What actually stops the statement is `pg_cancel_backend`, which needs
`job_run.backend_pid` (migration r031). That column survives on every branch, so
this track can use the identical mechanism — the ops-plane is not where the
answer lives.

## B5 — `schedule_timeout` under saturation

Defaults, read from the SDK:

| | default | our reality |
|---|---|---|
| `schedule_timeout` | **5 minutes** | queued work is failed after waiting this long |
| `execution_timeout` | **60 seconds** | our PostGIS units run for *minutes* |

Both must be overridden on every task. The execution default is the sharper
one: left alone it kills a real unit a minute in.

Measured with `schedule_timeout=10s`, three tasks, one slot, twenty seconds
each:

| task | outcome |
|---|---|
| 1 | COMPLETED |
| 2 | **FAILED** |
| 3 | **FAILED** |

The two that merely *waited too long* report **`FAILED`** — the same status as a
unit whose compute genuinely broke. Combined with B7, a saturated pool therefore
produces a run reporting `FAILED`, full of units reporting `FAILED`, none of
which ever executed.

Sizing follows directly: `schedule_timeout` must exceed
`slowest_unit × queued_depth ÷ slots`, or a busy period is indistinguishable
from an outage.


## Port implementation — status

`orchestration/hatchet_orchestrator.py` implements the port; `factory.py` has
modes `hatchet` and `sync` only. Conformance currently reads:

```
12 passed, 3 skipped, 3 failed
```

All three failures are the **same measured property**, not defects:
`test_a_submitted_run_is_retrievable_by_its_id`,
`test_cancelling_a_live_run_closes_it`, and
`test_subscribing_to_a_terminal_run_yields_then_stops` — each needs a run the
engine can answer for.

**A worker is mandatory, and this is where that stops being a sentence in a
plan.** `run_no_wait` returns a workflow-run id, but `runs.get()` on it returns
**404** until a worker has registered the workflow definition with the engine.
So on this track a run id can exist and be unqueryable, and the port's contract
that a submitted run is immediately retrievable cannot be honoured by the
submitting process alone.

The library track has no equivalent: there, the process that submits can also
drive and answer for the run, and `get_run` falls back to the engine's own
record during the pre-envelope window.

Closing this means the conformance suite must start a worker for the `hatchet`
implementation — which is itself the finding. The comparison row is not "one
engine is slower to answer"; it is **one track requires a second process to
exist before a run is observable at all**.

## Spike B8 — is `partial` actually inexpressible?

**The answer is no, and it invalidates part of what this document said.**

Spikes B7 and the first port implementation both recorded the status enum's
limits as an "accepted fidelity loss": no `partial`, no `skipped`, therefore the
vocabulary is gone. The first half is true. The conclusion was wrong, because it
only ever looked at the run's *status*.

A run's **output** is arbitrary JSON, and `on_failure_task` is a shipped hook
that runs when a workflow did not fully succeed — including when it half
succeeded. Three tasks, one failing, one skipped as non-dirty, one succeeding,
submitted with `additional_metadata`. The finalizer returned:

```json
{"overall": "partial",
 "verdict": {"ok_unit":    {"skipped": false, "error": null},
             "clean_unit": {"skipped": true,  "error": null},
             "doomed":     {"skipped": false,
                            "error": "RuntimeError: boom: this unit fails..."}},
 "error_keys": ["doomed"],
 "additional_metadata": {"scenario_id": "s-42", "aoi_id": "cuauhtemoc",
                         "traceparent": "00-4629d223...-01"}}
```

Everything the enum could not say is in there:

| claim | verdict |
|---|---|
| `on_failure_task` fires on *partial* failure | yes — and reports COMPLETED itself |
| `ctx.was_skipped(task)` separates skipped from computed | yes — `clean_unit` true, `ok_unit` false |
| `ctx.task_run_errors` is keyed by task name | yes — `["doomed"]`, with the traceback |
| `runs.get_result()` works on a **FAILED** run | yes — serves the finalizer's output |
| `additional_metadata` round-trips and is queryable | yes — `runs.list(additional_metadata=...)` matched |

⚠ The run's status is still `FAILED`. Nothing here changes the enum; the
verdict lives beside it. A dashboard, an alert or an operator reading status
alone still sees a red run with no way to tell which units survived.

### What the comparison row actually is

Not "the engine is lossy". It is **where the vocabulary lives**:

| | Track A | Track B |
|---|---|---|
| `partial` / `skipped` expressible | yes | yes, in the run output |
| queryable across runs | `SELECT ... GROUP BY` | one run at a time, via the engine |
| survives the engine being down | yes, it is our table | no |
| costs a table to maintain | yes | no |

"How many scenarios partially failed this week" is a `GROUP BY` on one track and
N+1 round-trips on the other. `additional_metadata` narrows the gap — scenario
and AOI are filters in `runs.list()` and in the dashboard's search box — but it
is a lookup key, not an aggregate.

**A bonus nobody asked for:** the engine injects `traceparent` into every run's
metadata. OTel trace context is free here and absent on the library track.

## Spike B9 — the DAG declaration is not deterministic

Two `build_dag()` calls in one process, a second apart, serialise to **different
bytes**. Two independent causes, both defaults:

```
identical: False
timestamps embedded in proto: ['2026-07-28T14:51:55.281644+00:00', ...]
or_group_id: "11ae4d08-..."  vs  "49153afa-..."
```

* `ParentCondition.__init__` defaults `readable_data_key` to
  `parent.name + ":" + expression + ":" + datetime.now(UTC).isoformat()`
  (`hatchet_sdk/conditions.py:118-128`) — a microsecond timestamp that reaches
  the wire.
* `BaseCondition.or_group_id` is `Field(default_factory=uuid4)`
  (`conditions.py:41-44`) — a fresh random UUID per condition, per build.

This matters here specifically because **the API and the worker both call
`build_dag()` at import**. Left alone they describe the same workflow
differently every time either process starts.

Fixed by deriving both from the slug (`readable_data_key=f"dirty:{slug}"`,
`or_group_id=uuid5(ns, ...)`). Verified: identical bytes across a second
boundary.

⚠ Not established: what the engine *does* with a changed definition under an
existing name — whether it versions, overwrites or rejects. The fix is cheap and
obviously more correct, so it was applied without settling that; if a
measurement ever depends on version churn, spike it then.

## Also measured while here

* **A valid `HATCHET_CLIENT_TOKEN` is needed to import the module at all.**
  `hatchet_app` constructs `Hatchet()` at module scope, so unit tests that touch
  only the verdict-reading logic still need a credential. Track A's modules
  import with no engine credential of any kind. This is a CI constraint, not
  just an inconvenience.
* **Unused capability, deliberately.** `rate_limits` (engine-enforced, global,
  cross-worker) is the right answer to N workers saturating one Postgres, and
  `slot_cost` weights a heavy unit against light ones. Neither is wired up
  because neither has been measured, and the library track would need a
  hand-rolled semaphore for the first. Worth a spike before the comparison
  claims a footprint difference.

### Remaining on this branch

1. Start a worker in the conformance fixture, then re-run; the three failures
   should resolve and the two partial-success assertions (currently skipped for
   lack of a worker) should run and confirm `failed` / `cancelled`.
2. Delete `wave.py`, `runstore.py` and migration r033 dropping
   `orchestration_run` + `job_unit` — the deletions this track exists to make.
3. Repurpose `apps/worker` for Hatchet.

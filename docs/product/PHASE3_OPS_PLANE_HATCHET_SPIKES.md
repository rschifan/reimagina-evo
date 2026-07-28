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

## Not yet run

- **B4 — cancel a live PostGIS unit.** Needs a long-running real compute against
  the domain database; the engine-side question (does the slot release, does
  `runner.py` log "still running after cancellation") is unanswered.
- **B5 — `schedule_timeout` under saturation.** ⚠ The default is **5 minutes**,
  and queued work is cancelled when it expires. With units that run for minutes
  and a small slot count this is the most likely production surprise on this
  track, and it is untested.

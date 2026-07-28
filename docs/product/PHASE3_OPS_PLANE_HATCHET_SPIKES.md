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

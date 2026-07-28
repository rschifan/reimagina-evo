# Port conformance suite

What every `Orchestrator` implementation must do, asserted through
`factory.get_orchestrator` and the port's four methods only — never through a
queue, an engine client, or a run table.

## Why it is not in `tests/integration/`

Integration tests prove *this* implementation works. These prove *any*
implementation satisfies the same contract, which is a different job: they are
the instrument for comparing ops-planes, and they are written to survive being
carried onto a branch where the engine underneath them has been replaced
wholesale.

## How the cross-branch comparison works

The two ops-plane branches never merge — they take conflicting migrations off
the same head — so no shared fixture can parametrise across both. The comparison
is a diff of two runs of the same file:

```bash
pytest tests/conformance -v | tee conformance-$(git branch --show-current).txt
```

Run that on each branch and diff the results. Every line is
`test_name[mode] PASSED|SKIPPED`, so the diff shows exactly which guarantees
each ops-plane keeps, which it cannot express, and which it never gets asked.

## What each branch edits

`conftest.IMPLEMENTATIONS` — and nothing else. One row per orchestrator the
branch ships:

| field | meaning |
|---|---|
| `mode` | what `get_orchestrator` is asked for |
| `blocking` | `submit_run` returns only once the run is terminal. `False` means a run can be caught in flight, which is the only way to test a cancel that cancels something |
| `failed_run_status` | what a run with one failed and one succeeded unit reports |

`failed_run_status` is the field to watch. We own run state here, so a
half-successful run is `partial`. An engine that owns run state natively may
have no such value — its run-status enum may offer only
`QUEUED|RUNNING|CANCELLED|FAILED|COMPLETED`. That branch declares whatever it
actually says, and **the difference between the two tables is the finding**, not
a test failure. A conformance suite that forced both branches to answer
`partial` would be asserting the comparison away.

Assertions in `test_port_contract.py` should stay identical across branches. If
a branch needs to change one, that change is itself a result worth recording in
the comparison document.

## Preconditions

Postgres (`TEST_DATABASE_URL`, default `postgresql://localhost/reimagina_test`).
Everything skips cleanly without it. The suite seeds on a committed connection
and tears down surgically, because a queue worker opens its own connection and
cannot see uncommitted rows.

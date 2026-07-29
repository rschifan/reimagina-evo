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
pytest tests/conformance -q --no-header -rA \
  | grep -E '^(PASSED|SKIPPED|FAILED)' \
  | sort > conformance-$(git branch --show-current).txt
```

Run that on each branch and diff the two files. Each line is
`STATUS path::test_name[mode]` and nothing else.

⚠ Not `-v`. Its lines carry a trailing `[ 42%]` progress column, so if the two
branches have different test or skip counts — which is the whole point — every
percentage shifts and `diff` reports every line as changed. `-rA` also prints
skip reasons as a separate summary rather than wrapping them mid-line.

## What each branch edits

Primarily `conftest.IMPLEMENTATIONS` — one row per orchestrator the branch
ships:

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

The other thing a branch must edit is `conftest._teardown`. It deletes from
`procrastinate.procrastinate_jobs`, `jobs_runtime.job_unit` and
`jobs_runtime.orchestration_run` — all of which an engine-owned ops-plane drops.
Only `test_port_contract.py` is genuinely engine-agnostic; cleaning up after a
run means knowing where that engine put it.

## Preconditions

Postgres (`TEST_DATABASE_URL`, default `postgresql://localhost/reimagina_test`).
Everything skips cleanly without it. The suite seeds on a committed connection
and tears down surgically, because a queue worker opens its own connection and
cannot see uncommitted rows.

## Running it

```bash
export HATCHET_CLIENT_TOKEN=...            # a running engine is required
export TEST_DATABASE_URL=postgresql://localhost/reimagina_test_conf
pytest tests/conformance -p no:randomly
```

⚠ **Give this suite its own database.** `migrated_db` tears down with
`alembic downgrade base`, which drops every tracked schema. Two pytest sessions
sharing one database will pull the schema out from under each other — observed
as 13 fixture errors reading
`relation "curated_geo.indicator_result" does not exist`, in a run whose own
code was fine. The name must contain `test` or the fixture refuses to run.

⚠ **A stale worker will make this suite lie.** Worker registrations live in the
engine's database, survive the process that made them, survive an engine
restart, and are *not* removed by `Worker.exit_gracefully()`. A leaked one stays
eligible for `reimagina-run` tasks, so the engine hands work to a process that
no longer exists and runs sit at `running` with no units until the timeouts
fire. The fixture pauses stale `conformance` workers on setup for this reason.

If results look wrong, check the registry before the code:

```bash
python -c "from hatchet_sdk import Hatchet; print([(w.name, str(w.status)) for w in Hatchet().workers.list().rows])"
```

The tell is **suite wall clock**, not the failures: 62s → 322s across unchanged
code is a leak, not a regression.

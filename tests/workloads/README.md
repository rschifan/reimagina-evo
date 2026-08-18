# Workload suite — how each ops plane schedules

The conformance suite proves the `Orchestrator` port *contract* holds. It says
nothing about **how** an engine schedules, because the shipped catalog is four
indicators wide and only a `geometry` edit produces more than a single wave.

This suite supplies the DAG shapes the catalog cannot: seven synthetic
workloads varying topology (chain, diamond, lattice, seeded random), unit size
(10 ms to 3 s) and failure behaviour.
It **asserts** only what must hold whatever an engine does, and **records**
everything the two tracks are allowed to differ on.

```bash
make test-workloads
```

Reports land in `artifacts/workloads-<track>.json`. Diff two of them:

```bash
python -m reimagina.perf.workload_report artifacts/workloads-dbos.json artifacts/workloads-hatchet.json
```

## Running it

| variable | default | what it does |
|---|---|---|
| `REIMAGINA_BENCH` | unset | **must be `1`** — otherwise every test skips. `testpaths = ["tests"]` means a bare `pytest` would otherwise spend minutes here. |
| `REIMAGINA_BENCH_SCALE` | `full` | `smoke` (÷10, ~1 min, a shape check **not** a measurement), `full` (~3–4 min), or a bare float for a longer run |
| `REIMAGINA_BENCH_REPS` | 3 at full, 1 at smoke | repetitions per workload; **the first is always discarded as warm-up** |
| `REIMAGINA_UNIT_CONCURRENCY` | 4 | slots on both tracks — the number is the same, what it means is not |
| `TEST_DATABASE_URL` | `reimagina_test_bench_<track>` | see below |

### Each track needs its own database

Not a preference. The migration histories fork — `r032` on the library track,
`r033` here, both off `r031` and mutually unresolvable — so a database migrated
by one worktree makes the other's alembic fail outright with
`Can't locate revision`. Sharing one is not slow, it is impossible.

### Track B additionally needs the engine

`HATCHET_CLIENT_TOKEN` and a reachable engine, or the whole suite skips cleanly.
Track A needs neither, which is itself one of the findings.

## Traps worth knowing before you debug something else

Each of these cost real time and presents as a symptom that points somewhere
else entirely.

- **Mint the token against the `default` tenant, not `internal`.** A fresh
  engine database has two tenants. A token for `internal` authenticates
  perfectly, registers the worker, registers all its actions — and then every
  run sits in `queued` forever, because nothing services that tenant. The
  symptom looks exactly like a broken worker.
  ```bash
  psql -d hatchet -tAc "select id from \"Tenant\" where slug='default'"
  ```
- **`HATCHET_CLIENT_TLS_STRATEGY=none`.** The compose sets
  `SERVER_GRPC_INSECURE=t`, so the client must be told too, or the worker dies
  on a TLS handshake with `WRONG_VERSION_NUMBER`.
- **The engine will eat the connection pool.** Left at its default it held
  **94 of the host's 100 connections** at idle, and the next thing to connect
  got `sorry, too many clients already` — which this suite reports as
  *"bench database unavailable"*, i.e. as a missing database rather than a full
  one. `DATABASE_MAX_CONNS` in the compose halves it; see the comment there.
- **`worker.exit_gracefully()` is `async def`.** Calling it from sync code
  returns a coroutine that is never awaited and does nothing — Python only
  warns. Worse, the action listener is a **spawned child process** and not a
  daemon, so Python joins it at interpreter exit and the whole pytest session
  hangs after the last test passes, with no output and no failing test. See
  `_stop_worker`.
- **On the library track, launch DBOS before anything reads its tables.** It
  creates its own system schema during `launch()`; anything earlier fails with
  `relation "dbos.workflow_status" does not exist`. `launch()` also reads the
  queue's `worker_concurrency` exactly once, so setting it late leaves the queue
  at the default 2 while the report claims 4 — a silently unfair comparison.

## Reading the numbers

`slr` (Schedule Length Ratio) = makespan ÷ critical path, using **measured**
unit durations. It is ≥ 1 and dimensionless, so it compares across DAG shapes
and machines in a way raw milliseconds do not. Compare `slr` across tracks;
compare `makespan` only within one.

`head_of_line_ms` is the diagnostic, not a standard metric: the worst gap
between a unit becoming runnable and actually starting. ⚠ It conflates three
causes — a wave barrier, no free slot, and the engine's own dispatch latency —
so read it beside `slr` rather than alone. On `sync` it is large and meaningless
by construction, because serial execution finishes a unit's parents long before
it gets a turn.

`speedup` = sync makespan ÷ this mode's makespan, with `sync` as T₁.
**Omitted for `partial_failure`**, where Track B retries the failing unit and
`sync` does not: a number there would compare retry policies while looking like
it compared schedulers.

The `sync` columns are the harness's own control. Being the same code on both
worktrees, they must agree across tracks — if they do not, the suite is
measuring itself and every other column is void. Measured: they agree to within
0.5% (e.g. 3137 vs 3133 ms), which is the strongest evidence the harness is
sound.

### ⚠ Not every workload is equally reproducible

Two consecutive full runs on one track, same machine, same everything:

| stable to ±2% | noisy |
|---|---|
| `lattice` 0.2%, `diamond` 0.4%, `barrier_probe` 1.8% | `partial_failure` 8.8%, `layered_random` 13%, `many_tiny` 15%, **`chain_deep` 35%** |

The split is not random: the stable ones have real work to do, and the noisy
ones are dominated by the engine's per-dispatch latency. `chain_deep` is the
extreme — six *serial* 100 ms units, so there is no parallelism to absorb
per-hop jitter and the makespan is mostly overhead being measured six times in
a row.

So: **the scheduling comparison (`barrier_probe`, `lattice`, `diamond`) is solid
at ±2%; the overhead-dominated numbers are order-of-magnitude only.** Raise
`REIMAGINA_BENCH_REPS` before quoting the latter. Three repetitions was chosen
for a ~4 minute suite, not because it is enough for the noisy tail.

## What this suite is not

Long-running units. Even at `full` the slowest unit is 3 s, so nothing here
approaches `UNIT_EXECUTION_TIMEOUT` (120 min) or `schedule_timeout` (6 h).
`REIMAGINA_BENCH_SCALE` takes a multiplier when that matters.

Disruption drills. Cancelling mid-run and killing a worker stay manual, per the
ops-plane verification plan.

Real PostGIS. Units are `pg_sleep` and `sha256` loops, chosen so the measurement
is of the scheduler rather than of geometry. Real indicator computation —
including the shipped composite — is covered by `tests/conformance` and
`tests/integration`.

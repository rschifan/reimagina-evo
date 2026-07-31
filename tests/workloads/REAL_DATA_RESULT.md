# Real-data runs — correctness first, then what the synthetic suite cannot show

> **Correctness is in [the first section](#correctness-does-the-pipeline-compute-the-right-answers).**
> The timing section below it is a separate question and a weaker result; read
> the caveat on `sync` before quoting any of its numbers.

## Correctness — does the pipeline compute the right answers?

`scripts/verify_real_data.py`, run at **full city scope (86,225 blocks)** against
both implementations. Every check has an oracle that exists independently of the
code producing the answer.

| check | what it proves | `sync` | `hatchet` |
|---|---|:--:|:--:|
| **A** neutral scenario reproduces the baseline | a scenario with no overrides sees baseline inputs, so all 86,225 × 3 values must equal the committed baseline bit for bit | ✅ | ✅ |
| **B** an override changes exactly what it should | a `floors` override dirties `planned_floors` **only**, writes rows for **only** the edited features, and the overridden value reaches the result | ✅ | ✅ |
| **C** structural invariants | one `current` row per (feature, indicator, scenario); every result cites a real `job_run` | ✅ | ✅ |
| **D** determinism | two independent runs of the same input agree value for value | ✅ | ✅ |

Both implementations match the *same* baseline exactly, so they match each other
— 86,225 features × 3 indicators, zero differences, on both.

Check A alone would be worthless. **A pipeline that ignored scenario overrides
entirely would pass it**, because a what-if engine that silently returns the
baseline returns exactly the baseline a neutral scenario expects. That is what
check B is for, and it is the check that actually exercises
`scenario_override → resolve_change_set → dirty set → run_plan → effective_numeric
→ result`. Before this run there were **zero** override rows in the database:
the what-if path had never been exercised on real data at all.

Check A is also the only end-to-end exercise of the claim check: a full-city run
carries 258,675 feature ids through `jobs_runtime.run_plan` rather than through
a 10 MB gRPC message, and the values that come out the far side are identical to
the baseline.

### `planned_floors` computes nothing, and that is the data

It writes zero rows on this dataset and still reports `succeeded`. Not a
pipeline bug: **no block has a `floors` attribute** — not in
`feature.attributes`, not in `observation` — so there is nothing to compute.
Check B proves the unit works the moment an override supplies the input.

Worth knowing anyway, because a unit that legitimately produces nothing is
indistinguishable at the run level from one that was supposed to produce
something and did not. `run_plan` raises `LookupError` for a *missing plan* for
this reason; an *empty result set* has no equivalent guard.

---

# Performance — what the synthetic suite cannot show

Measured 2026-07-31 on `reimagina_real_b`, the real CDMX dataset: **86,225
blocks**, 2,053,044 buildings, the four shipped indicators, 4 worker slots.
Reproduce with `scripts/bench_real_data.py`, which writes the raw numbers to
`artifacts/workloads-real-hatchet.json` — `artifacts/` is gitignored, so the
table below is the committed record of this run.

Edit: `geometry` **and** `floors`, over every block. That dirties all four
indicators — three of them independent, the composite downstream of two — so the
DAG offers three-way parallelism against a serial tail.

⚠ This is deliberately **not** the measurement spec's baseline edit (`geometry`
on one block in Cuauhtémoc, `PHASE3_OPS_PLANE_MEASUREMENT.md`). That one sizes
the *ops-plane's* overhead against a trivial compute. This one sizes the
*engine's scheduling benefit* against a compute large enough to have one.

## Serial cost of each unit, measured directly

No orchestrator, one connection, full city:

| unit | duration |
|---|---|
| `urban_form_block_building_density` | 9.91 s |
| `urban_form_block_compactness` | 6.01 s |
| `urban_form_block_planned_floors` | 0.18 s |
| `urban_form_block_composite` | downstream of the first two |

## The result

Median of two repetitions after a discarded warm-up:

| | `sync` | `hatchet` |
|---|---:|---:|
| **makespan** | 22,977 ms | **20,511 ms** |
| busy — Σ of unit durations | 21,480 ms | **27,002 ms** |
| span | 22,872 ms | 19,641 ms |
| critical path | 15,369 ms | 18,293 ms |
| observed parallelism | 0.94 | **1.38** |
| schedule length ratio | 1.50 | 1.12 |
| dispatch lead | 11 ms | 684 ms |
| settle lag | 94 ms | 186 ms |
| **speedup vs `sync`** | — | **1.12×** |
| **efficiency** (speedup ÷ 4 slots) | — | **0.28** |

## What it says

**Four slots and three independent units bought 1.12×.** Not the ~2.5× the DAG's
shape allows, and not close to the 4 slots paid for. Observed parallelism reached
1.38, not 3.

**The mechanism is visible in one row.** `busy` is the sum of each unit's own
duration, so it should be *invariant* to how the units are scheduled — the same
work either way. It is not: 21.5 s serially, **27.0 s concurrently, +26%**. Three
real PostGIS statements against one Postgres slow each other down, and the
critical path itself gets *longer* under concurrency (15.4 s → 18.3 s) because
the units on it are contending with the ones beside them.

So the engine scheduled correctly and the database refused to go faster. Nothing
about the ops-plane fixes that; the ceiling is Postgres, and it is reached long
before the slots are.

**Dispatch is not free either.** 684 ms of lead versus 11 ms in-process. Against
23-second units that is noise, which is the point of measuring at this scale —
at the 2,000-block scope the same run is *slower* on the engine (2,070 ms vs
664 ms) because dispatch is most of it. There is a workload size below which a
service ops-plane costs more than it returns, and on this hardware it is not a
small one.

## Against the synthetic suite, at full scale

Same machine, same engine, same 4 slots, 3 reps, `REIMAGINA_BENCH_SCALE=full`:

| workload | speedup | observed parallelism |
|---|---:|---:|
| `layered_random` | 1.33 | 1.43 |
| `barrier_probe` | 1.28 | 1.41 |
| `lattice` | 1.13 | 1.23 |
| `diamond` | 0.91 | 0.98 |
| `cpu_vs_io` | 0.81 | 0.93 |
| `fanout_wide` | 0.80 | 0.86 |
| `chain_deep` | 0.48 | 0.69 |
| `many_tiny` | 0.18 | 0.17 |
| **median** | **0.86** | |
| *real data, this run* | *1.12* | *1.38* |

⚠ **The synthetic numbers are not an upper bound on the real one, and an earlier
draft of this document said they were.** They are mostly *below* it, and below
1.0 — on this hardware the engine is **slower** than running in-process for most
synthetic shapes. The reason is the other ceiling: synthetic units run 10 ms to
3 s, against 684 ms of measured dispatch lead. `many_tiny` (0.18) and
`chain_deep` (0.48) are that effect undiluted — forty tiny units, or a serial
chain with no parallelism to win, are pure overhead.

So the two suites bracket the answer from opposite sides:

- **from below — dispatch overhead.** Units must be large enough to amortise
  ~0.7 s of scheduling. Below that the service ops-plane loses outright.
- **from above — database contention.** Once units are large enough, they are
  large because they are hammering Postgres, and then they slow each other down
  (+26 % unit-time here) and the DAG's shape stops being the constraint.

The window where an engine wins on wall-clock is the gap between those, and on
this machine it is narrow: 1.12× at the full city, against a shape that offers
about 2.5×.

Neither suite alone shows this. The synthetic one holds topology fixed and the
database out; this one holds the real database in and takes whatever topology the
catalog has.

## Caveats, so the number is not overread

- ⚠ **The `sync` column was measured without committing.** `sync` runs every
  unit inside the caller's transaction and never commits on its own, and the
  bench script did not either — so its results were rolled back at close, and
  the engine-backed modes each paid a real commit per unit on their own
  connections while `sync` paid none. The compute time is real; the comparison
  flatters `sync` by whatever those commits cost. Fixed in the script after the
  fact, and **the numbers above predate the fix**.
- One machine, one Postgres, everything co-located — engine, worker, database.
  Contention is therefore at its worst here; a deployment that separates them
  would land somewhere better, and that number is not measured.
- Two measured repetitions. Enough to see a 26 % effect, not enough for a
  confidence interval.
- `shared_buffers` and the rest of the server's configuration are whatever this
  machine has; no tuning was attempted, and tuning is the obvious next lever
  before more slots.
- Track A has not been run against `reimagina_real_a` for the same comparison.
  Until it is, this is a Track B result, not a side-by-side.

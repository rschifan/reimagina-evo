# Phase 3 — Ops-plane measurement spec

**Scope.** The quantities the two ops-plane branches report, and exactly how each
is obtained. Fixed here, on `main`, before either branch exists — so both measure
the same thing and the numbers can be put side by side without arguing about
definitions after the fact.

This is a specification, not a harness. Each branch measures these natively:
The queue branch's own timing harness (`perf/procrastinate_bench.py`, deleted
here with the engine it measured) could not be generalised behind the port: it
timed enqueue and drain of a **no-op job**, while the port's granularity is a
**run**, and the two engines share no transport. A shared harness would either
measure nothing real or privilege one engine's shape.

Every metric below is therefore obtained from the port, from `jobs_runtime`, or
from the process — never from an engine-specific probe. That is what makes the
two branches' numbers comparable at all.

---

## The baseline edit

The dirty set depends entirely on which attribute changed, so it must be named.

**Baseline: a `geometry` edit on one block in the CDMX-Cuauhtémoc AOI.**

| edit | dirty units | waves |
|---|---|---|
| **`geometry`** | `urban_form_block_building_density`, `urban_form_block_compactness` → `urban_form_block_composite` | **2** |
| `floors` | `urban_form_block_planned_floors` | 1 |

`geometry` is not an arbitrary pick: it is the **only** edit the shipped catalog
answers with more than one wave. `compactness` and `building_density` both read
`geometry`; the composite depends on those two and is pulled in downstream.
`planned_floors` reads `floors` and nothing depends on it, so a `floors` edit is
a single unit with no fan-out and would measure scheduling by not exercising it.

> An earlier draft of the plan stated that a `floors` edit dirties
> `planned_floors + composite`. It does not — the composite depends on
> compactness and density, not planned_floors. Verified against
> `compute_dirty_set` over `SHIPPED_INDICATORS`.

**AOI.** Cuauhtémoc — the prototype's real data, and already the prerequisite for
the overlap benchmark (`perf/runner.py:212-246`):

```bash
python -m apps.cli.app cdmx bootstrap
```

Existing baseline numbers for this AOI are in
[`docs/architecture/PERFORMANCE.md`](../architecture/PERFORMANCE.md) — "Baseline
numbers (Cuauhtémoc, Apr 2026)" — which is where the domain compute times come
from. This spec adds the ops-plane's cost *on top of* those; it does not restate
them.

⚠ One stale reference not to inherit: `perf/runner.py:246` tells you to run
`reimagina-cli cdmx bootstrap`, but `pyproject.toml` has no `[project.scripts]`
entry and no such console script exists — the invocation above is the real one.

`perf/manifest.py`'s tiers (`smoke` / `representative` / `stress`) drive
synthetic AOIs for query-plan work, which is a different job from this one; they
are not the baseline here.

Both branches must run against the same AOI on a freshly migrated database.

---

## Where the numbers come from

One constraint governs every timing definition:

> **`jobs_runtime.orchestration_run` and `jobs_runtime.job_unit` are dropped by an
> engine-owned ops-plane.** No metric may be defined in terms of them, or Track B
> cannot report it.

What survives on both branches is **`jobs_runtime.job_run`** — it is domain
provenance, not scheduler state (`curated_geo.indicator_result.job_run_id` is NOT
NULL with an existence trigger, r016), and migration r031 gave it `run_id`, so
one run's compute is now a single query on a table neither branch deletes:

```sql
SELECT sum(completed_at - started_at) AS runner_time,
       count(*)                       AS units
FROM jobs_runtime.job_run
WHERE run_id = :run_id;
```

Everything else is measured at the port or at the process, both of which are
engine-independent by construction.

---

## Metrics

| Metric | Definition | Source |
|---|---|---|
| **Wave-cascade wall clock** | `submit_run` returns → `get_run` first reports a terminal status | the harness, around the port |
| **Per-unit overhead** | (wall clock − runner time) ÷ unit count | wall clock above; runner time from `job_run` |
| **Redundant compute** | `count(*) WHERE status='stale'` on `curated_geo.indicator_result` for the run's features, after the crash drill | `indicator_result` |
| **Recovery latency** | worker `SIGKILL` → run reports terminal, with no operator action | the harness, around the port |
| **Idle footprint** | RSS across all ops-plane processes + container count + database count, at rest | `ps` / `docker compose ps` |
| **Ownership delta** | LOC and module count we own vs delegated to the engine | `git diff --stat` against the fork point |

### Wave-cascade wall clock

Measured by the caller, not read from a table, and deliberately so: the
scheduling latency between submit and the first unit starting is **part of what
is being compared**, and no engine's own tables record the moment a client asked.
The port is the only vantage point both branches share.

Poll `get_run` until `status in RUN_TERMINAL_STATES`. State the poll interval
alongside the number; it is a floor on the resolution.

### Per-unit overhead

The headline number: what the ops-plane costs on top of the compute. Runner time
is a `sum` over `job_run` rows and therefore excludes queueing, dispatch,
checkpointing and status writes by construction — those are exactly the residue
this is measuring.

⚠ Report the unit count as measured, not as expected. A run where a unit was
skipped has fewer `job_run` rows than dirty units, and dividing by the dirty
count would silently understate the overhead.

### Redundant compute

Writes are stale-then-insert (`repositories.py:2173-2185`), so a recomputed value
leaves its predecessor behind as `status='stale'`. Counting those measures work
done twice — directly, in rows.

⚠ Do **not** assert "no duplicate current rows" instead. `ir_one_current_base`
(r016:193-196) and `ir_one_current_scenario` (r016:198-201) are partial unique
indexes that already guarantee it, so that assertion tests Postgres, not the
ops-plane.

### Recovery latency

`SIGKILL` a worker mid-run and start the clock. It stops when the run reports
terminal **without an operator doing anything** — a recovery that needs a manual
sweep is a different product, and that difference is a result worth recording
rather than a footnote.

### Idle footprint

At rest means: everything an operator must keep running to accept a submit, with
no run in flight. Count *databases*, not just containers — an engine with its own
Postgres is a second thing to back up, migrate and monitor, which no RSS figure
shows.

### Ownership delta

LOC and modules, split into what we own and what the engine owns. The headline
this exists to quantify: one branch keeps the wave scheduler, the other deletes
it. Measure from each branch's fork point, and count deletions as well as
additions — deleting the scheduler is the point of a service track, not a
rounding error.

---

## Deliberately excluded

**Submit-latency percentiles.** p50/p95/p99 on `submit_run` are noise against a
multi-minute PostGIS unit, and reporting them to three digits invites a
conclusion the measurement cannot support. An engine's own queue benchmark
answers that question in isolation, which is legitimate but different.

**Throughput at varying worker concurrency.** The dirty set is three units wide.
Concurrency numbers here would describe the catalog, not the engine.

**Anything OpenTelemetry-derived.** Nothing in the repo emits OTel today. Adding
it to Track A only to match Track B's built-in would erase a real difference
between the two rather than measure it.

---

## Reporting

One table per branch, plus the conditions that produced it: AOI, dataset state,
poll interval, machine, and the engine image or library version pinned at the
time. A number without those is not comparable to the other branch's.

Numbers land in `docs/product/PHASE3_OPS_PLANE_TRACKS.md`, written on `main`
after both branches are complete — neither branch can hold both halves, because
they never merge.

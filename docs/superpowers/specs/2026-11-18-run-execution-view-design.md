# Run execution view — design

Date: 2026-11-18 · Status: draft for approval

> **Amended 2026-11-19 — no prediction.** The timeline no longer forecasts
> anything. A2 is the only axis rule: a live run starts on a 10 s axis that
> doubles each time the run reaches 95 % of it, so every bar shrinks at once
> and the run continues left to right; a finished run fits its axis to its
> length. Removed with it: A1 and `forecastMs`, the dashed "expected (last
> run)" outline and its legend entry, the "· 40%" / "+1.0s over" running
> numbers (a running unit shows its elapsed time), `UnitDetail.expected_ms`
> and `OpsRunReader.expected_durations`. Sections below that describe those
> are kept as history and superseded by this note. The `job_unit_success_by_slug_idx`
> index (r050) now serves no query; dropping it needs its own migration.

## Intent

The run page's execution panel must answer two questions at a glance, live
and after the fact:

1. **How far along is each indicator?**
2. **How are they interleaved?** Which run in parallel now, which is next,
   which is waiting on what.

Today it fails both. Reproduced on four composite runs (4, 5, 5 and 12
indicators): rows reorder mid-run, colours contradict the legend (finished
bars read as running), bars shrink and slide left as the axis rescales, bars
step once a second and can snap back, nothing is drawn for the first second,
status badges are cut to four letters, the last axis label is clipped,
dependencies are truncated text, and a units table plus a Progress card
repeat the same facts in different colours.

## Goals

- One view, layout **A1** (approved from mockups): one row per indicator, a
  shared time axis, a "now" line, a parallelism strip.
- Per-indicator progress as **elapsed / expected**, expected = this
  indicator's duration on its last successful run.
- A time axis that never shrinks a bar while the run is live.
- "Next" and "waiting on" stated in words on each row that hasn't started.
- Mechanics that are correct by construction: stable order, monotonic clock,
  one colour map feeding bars, dots and legend.

## Non-goals

- Intra-indicator progress reported by the indicator itself (none exists).
- Changing how the worker schedules units.
- The run list page (its stacked progress bar stays as is).
- Drawing dependency edges between rows (the "after …" text carries it).

## Anti-goals (failure even if it "works")

- Any bar moving left or getting shorter while the run is live, except the
  one announced overrun rescale.
- Rows changing position during a run.
- A colour meaning two things, or a legend that disagrees with the bars.
- The same fact shown twice in two places.

## Approaches considered

| | Axis rule | Trade-off |
|---|---|---|
| A0 | `max(10 s, now × 1.25)` (old mockup; today is the 10 s-step version) | bars shrink continuously — rejected |
| A1 | fixed at forecast finish, ×1.25 only when exceeded | needs expected durations; never rescales in a normal run — chosen, then removed 2026-11-19 |
| **A2** | doubling 10/20/40 s | no data needed; visible jumps — **the only rule since 2026-11-19** |
| A3 | fixed px/s, 20 s scrolling window | never rescales; early bars scroll away, no overview — rejected |

Layouts B (slot lanes) and C (dependency stages) were shown and rejected in
favour of A: B hides the per-indicator row, C has no time.

## Design

### Backend (one field, one index)

- `UnitDetail.expected_ms: int | None` — duration (`completed_at −
  started_at`) of the most recent **succeeded** unit with the same
  `indicator_slug` in another run **on the same `aoi_id`**
  (`IS NOT DISTINCT FROM`, so whole-dataset runs, `aoi_id` NULL, match each
  other) that **completed before this run was submitted**. The bound makes
  the estimate a fixed fact about the run: it does not move while the run is
  live, and a finished run opened later shows the forecast it actually had.
  None when there is no such unit (new indicator, new AOI, or that history
  was deleted). One query per detail request: `DISTINCT ON (indicator_slug)
  … WHERE indicator_slug = ANY(%s) AND status = 'succeeded' AND completed_at
  < %(submitted_at)s ORDER BY indicator_slug, completed_at DESC`, joined to
  `orchestration_run` for `aoi_id`. Lives in `PostgresOpsRunReader` beside `list_units_detailed`
  and on the `OpsRunReader` Protocol.
- Migration `r050`: partial index `job_unit (indicator_slug, completed_at
  DESC) WHERE status = 'succeeded'`. Without it the lookup scans every unit
  ever written, on a 1 s poll.
- `RunDetailResponse.unit_slots: int` — `settings.unit_concurrency()`, the
  per-worker unit concurrency. Deliberately not multiplied by live workers:
  heartbeats are current-only, so a worker-count factor would change mid-run
  and could not be reconstructed for a finished run. With more workers the
  run simply beats its forecast. Drives the forecast and the strip's cap line.
- `list_units_detailed` orders by `indicator_slug` only. Display order is
  the client's job and must not depend on start times.
- `_detail_before_envelope` sends `expected_ms: None` and `unit_slots`. It
  has no `aoi_id` (the port's `RunStatus` carries none) and no
  `depends_on`, and widening the port is out of scope. Harmless: no unit has
  started before the envelope exists, so there is no bar for the A2→A1 axis
  switch to move.

### Frontend — module split

- `apps/web/src/ops/timeline.ts` (new, pure, no Svelte): everything the view
  computes. Replaces the timeline section of `derive.ts`
  (`TimelineRow`, `Timeline`, `PX_PER_SECOND`, `liveWindowMs`,
  `fitPxPerSecond`, `tickStepSeconds`, `axisTicks`, `timelineOf`,
  `peakParallelism` — all deleted, with their tests).
  - `rowState(unit, units)` → `done | failed | cancelled | skipped | running |
    ready | blocked`. `ready` = queued and every prerequisite succeeded;
    `blocked` = queued with a prerequisite not yet finished.
  - `rowOrder(units)` → dependency depth, then slug. Stable for the whole run.
  - `forecastMs(units, slots)` → list-scheduling simulation over
    `expected_ms` and `depends_on` with `slots` workers; null if any unit
    lacks `expected_ms`. Inputs never change during a run, so neither does
    the forecast.
  - `axisMs(elapsed, forecast, live)` → A1 (`forecast`, ×1.25 while
    `elapsed > 0.98 × axis`) when forecast is non-null, else A2 (10 s
    doubling); finished runs use `max(elapsed, forecast ?? 0)`.
  - `ticks(axisMs)` → nice steps; last label right-aligned, never clipped.
  - `parallelism(units, now)` → step series of running count for the strip.
  - `progress(unit, now)` → `{ elapsedMs, pct | null, overMs | null }`.
  - `summary(units, now, slots)` → counts for the header chips.
- `UNIT_VISUAL` (in `timeline.ts`): the single state → colour/label map. Bars,
  row dots and the legend all read it; nothing else names a colour.
- `RunTimeline.svelte` rewritten as a thin renderer of the model above:
  header chips, parallelism strip (inline SVG), rows, axis. Rows are a CSS
  grid (`label | track | numbers`), one fixed row height, label and bar
  vertically centred. Positions are percentages of the track, so no width
  measurement and no horizontal scroller.
- `RunDetail.svelte`: delete the units table and the Progress card. The Run
  card keeps metadata (submitted, completed, code version, scenario, AOI).
  A failed row shows its `detail` in a `<details>` under the row (keyboard
  reachable), replacing the table's error column.

### Row content

| State | Bar | Right column | Track text |
|---|---|---|---|
| running | solid from start to now, dashed outline to start + expected | `12 s · 42%`, or `+3 s over` (warning colour) past expected, or `12 s` with no expected | — |
| done / failed / cancelled | solid start→completed in state colour | duration | — |
| ready | none | – | `next · waits for a free slot` |
| blocked | none | – | `after building density, compactness` (unfinished prerequisites only) |
| skipped | none | – | `skipped` + reason from `detail` |

Running bars have a 2 px minimum width so a just-started unit is visible.

Header chips are about **this run only**: run status and elapsed · `6
running` · `4 of 12 done` ·
`2 next in line` · `2 waiting on prerequisites` · `finished in 30 s` once
done. The forecast is not shown (no chip, no line): it only sets the axis, so
bars keep their scale for the whole run. (Amended 2026-11-18: the forecast
chip and dashed line were removed as noise.)

### Clock

- `clockOffset` uses the request midpoint: `server_now − (sent + received) / 2`.
- The displayed `now` is monotonic: `now = max(previousNow, serverNow(...))`
  within one run page, so bars never step backward on a jittery sample.
- The timeline is drawn at the **response's own `server_now`**, not the wall
  clock (`timelineAt`), and a one-second linear CSS transition glides bars
  between polls. A unit's finish reaches the page one poll late; drawing to
  the wall clock grew running bars past `completed_at` and snapped them back
  when it arrived. At the data's time a finish always lands at or after the
  edge already drawn. (Revised after review; this replaced a 100 ms tick.)

Slots are shared by every live run (one global units queue), so the chips
never claim how many slots are busy, and "waits for a free slot" is true
whichever run holds them. The forecast assumes this run has all
`unit_slots`; contention from another run shows up as an announced overrun.

## Constraints

- Svelte 5 runes, Tailwind tokens already in `ops.css`; no new dependency.
- Postgres and DuckDB parity is not involved (ops tables are Postgres-only).
- Gate: `make check` (ruff + format + mypy, unit + contract, `build-web`,
  `test-web`), `make test-integration` for the reader query.

## Verification

- `timeline.test.ts`: state derivation, stable order, forecast on the
  recorded 12-indicator run, A1/A2 switch, and a monotonicity check — for a
  live run sampled every 100 ms, no finished bar's right edge and no running
  bar's width ever decreases in percentage terms except at the one overrun
  rescale.
- Contract test: `expected_ms` and `unit_slots` present; null with no history.
- Test fixture: record a fresh 12-indicator run into
  `apps/web/src/ops/fixtures/` (none is in the repo yet).
- Integration test: reader returns the latest same-AOI succeeded duration,
  ignores failed units, other AOIs and anything completed after this run's
  submission; matches NULL AOI to NULL AOI; excludes the run itself.
- Live: submit the four composite mixes again, headless screenshots every
  second (scratch `shoot.js`), check frames for the anti-goals.

## Calls made by the designer (check these)

1. Expected duration = **last** succeeded run, **same AOI only** (NULL
   matches NULL), completed **before this run was submitted**. Not a median,
   not cross-AOI (an AOI's size dominates duration).
2. Forecast only when **every** unit has an expected duration; otherwise the
   whole axis uses A2, while units that do have one still show a %.
3. `unit_slots` = configured per-worker concurrency only, not × workers.
   Header chips count this run's units only, never global slot use.
4. No forecast before the run's envelope row exists (A2 until then; no bars
   exist yet, so nothing moves).
5. Units table and Progress card removed; error text moves under the row.
6. Display order = dependency depth, then slug.

## Known limits

- A run scoped to some features can be much faster than a whole-indicator
  run, so its duration can make a later estimate too short. Overrun handling
  covers it; filtering by scope is left out until it bites.
- Deleting runs deletes their units (cascade), so it also deletes the
  history estimates come from.

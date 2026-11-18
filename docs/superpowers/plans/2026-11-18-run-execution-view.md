# Run execution view — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the run page's execution panel with a stable timeline (layout A1) that shows each indicator's progress against its last duration and how the run's indicators interleave.

**Architecture:** The API adds one field per unit (`expected_ms`, from the last same-AOI success before this run) and one per run (`unit_slots`). A new pure module `apps/web/src/ops/timeline.ts` turns units + clock into a render model (states, order, forecast, axis, positions, text); `RunTimeline.svelte` only renders it. The clock gets latency-corrected, monotonic and a 100 ms tick while a live run is open.

**Tech Stack:** FastAPI + psycopg2 + Alembic (Postgres), Svelte 5 runes + TypeScript + Tailwind, vitest, pytest.

**Spec:** `docs/superpowers/specs/2026-11-18-run-execution-view-design.md`

## Global Constraints

- No new dependency, Python or npm.
- Colours only through `UNIT_VISUAL` (Task 3). Mapping: running `bg-state-partial`; succeeded `bg-state-fresh`; failed `bg-state-error`; cancelled and skipped `bg-line`; ready = no bar, dot `border-state-stale`; blocked = no bar, dot dashed `border-line`.
- Axis: forecast present → start at forecast, ×1.25 while `elapsed > 0.98 × axis`; absent → start 10 s, ×2 while `elapsed > 0.95 × axis`; finished run → `max(elapsed, forecast ?? 0)`.
- Copy, verbatim: `next · waits for a free slot`; `after <slugs, comma-separated>`; `won't start: <slug> <status>`; skipped row `skipped` + ` · <detail>` when detail is set; `<n> running`; `<done> of <total> done`; `<n> failed`; `<n> next in line`; `<n> waiting on prerequisites`; `expected finish in ~<dur>`; `<dur> past forecast`; `finished in <dur> · forecast was <dur>`; running numbers `<elapsed> · <pct>%` or `+<dur> over`.
- Durations rendered with the existing `formatDuration` (`derive.ts`).
- Commits as the repo's configured user, no AI attribution anywhere; never commit `CLAUDE.md`.
- Gate before the PR: `make check` and `make test-integration`.

## Review Focus

1. **A run whose prerequisite failed** — the dependent unit is queued forever or skipped; the row must say `won't start: <slug> failed`, never `after …` indefinitely. Test in Task 3.
2. **Clock skew and a slow response** — a client clock minutes off, or a 2 s response, must not make bars jump backward or show negative elapsed. Test in Task 4.
3. **History deleted / first run on an AOI** — `expected_ms` null for some units: no forecast, A2 axis, % only where known, no `NaN`. Test in Task 3.
4. **Unit with `started_at` but a `completed_at` earlier than the displayed now** (finish lands between polls) — the bar ends at `completed_at`; it must not be drawn shorter than its own duration. Test in Task 3 (monotonic replay).
5. **Run with zero units / pre-envelope response** (no `depends_on`, no timings) — renders "This run has no units." or all rows ready, no exception. Test in Task 3.

---

### Task 1: Expected-duration lookup in the reader

**Files:**
- Create: `infra/db/migrations/versions/r050_job_unit_success_index.py`
- Modify: `packages/py/reimagina/storage/interfaces/jobs.py` (`OpsRunReader`)
- Modify: `packages/py/reimagina/storage/postgres/repositories.py` (`PostgresOpsRunReader`, `list_units_detailed` at ~3208)
- Test: `tests/integration/test_ops_run_reader.py`

**Interfaces:**
- Produces: `OpsRunReader.expected_durations(self, run_id: str) -> dict[str, int]` — slug → milliseconds, only slugs that have history; `{}` for a non-uuid or unknown id.
- Produces: `list_units_detailed` ordered by `indicator_slug` only.

- [ ] **Step 0: Branch and commit the spec**

```bash
git switch -c feat/run-execution-view
git add docs/superpowers/specs/2026-11-18-run-execution-view-design.md docs/superpowers/plans/2026-11-18-run-execution-view.md
git commit -m "docs: run execution view design and plan"
```

- [ ] **Step 1: Write the failing tests** (in `test_ops_run_reader.py`; add a helper `_time_unit(conn, run_id, slug, started, completed)` that `UPDATE`s `jobs_runtime.job_unit` timestamps, and pass `aoi_id` through `_make_run`)

```python
def test_expected_duration_is_the_last_same_aoi_success_before_this_run(conn):
    old = _make_run(conn, status="succeeded", units=[("a", "succeeded")], aoi_id="x")
    _time_unit(conn, old, "a", "2026-01-01T00:00:00Z", "2026-01-01T00:00:07Z")
    newer = _make_run(conn, status="succeeded", units=[("a", "succeeded")], aoi_id="x")
    _time_unit(conn, newer, "a", "2026-01-02T00:00:00Z", "2026-01-02T00:00:05Z")
    this = _make_run(conn, units=[("a", "queued")], aoi_id="x")
    assert _reader(conn).expected_durations(this) == {"a": 5000}

def test_expected_duration_ignores_failures_other_aois_and_later_runs(conn):
    # failed unit on same AOI, succeeded unit on AOI "y",
    # and a same-AOI success whose completed_at is after this run's submitted_at
    ...
    assert _reader(conn).expected_durations(this) == {}

def test_whole_dataset_runs_match_each_other(conn):
    # aoi_id=None on both runs → {"a": <ms>}

def test_expected_durations_guards_the_id(conn):
    assert _reader(conn).expected_durations("not-a-uuid") == {}
```

Update `test_units_carry_their_timings_in_dispatch_order` → rename `test_units_are_ordered_by_slug` and assert `[u["indicator"] for u in units] == ["alpha", "beta"]` after `beta` starts first.

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/integration/test_ops_run_reader.py -q`
Expected: FAIL — `AttributeError: ... 'expected_durations'`.

- [ ] **Step 3: Migration `r050`** (`revision = "r050"`, `down_revision = "r049"`)

```sql
CREATE INDEX job_unit_success_by_slug_idx
  ON jobs_runtime.job_unit (indicator_slug, completed_at DESC)
  WHERE status = 'succeeded'
```
Downgrade drops it.

- [ ] **Step 4: Implement `expected_durations`** (Protocol method with a one-paragraph docstring stating the three rules; Postgres implementation)

```sql
SELECT DISTINCT ON (u.indicator_slug)
       u.indicator_slug AS slug,
       (EXTRACT(EPOCH FROM u.completed_at - u.started_at) * 1000)::bigint AS ms
FROM jobs_runtime.orchestration_run this
JOIN jobs_runtime.job_unit mine ON mine.run_id = this.id
JOIN jobs_runtime.job_unit u
  ON u.indicator_slug = mine.indicator_slug
 AND u.status = 'succeeded'
 AND u.started_at IS NOT NULL
 AND u.completed_at < this.submitted_at
JOIN jobs_runtime.orchestration_run r
  ON r.id = u.run_id AND r.aoi_id IS NOT DISTINCT FROM this.aoi_id
WHERE this.id = %(run_id)s::uuid
ORDER BY u.indicator_slug, u.completed_at DESC
```
Change `list_units_detailed`'s `ORDER BY` to `indicator_slug` and its docstring to say display order is the client's.

- [ ] **Step 5: Migrate and run tests**

Run: `make migrate && uv run pytest tests/integration/test_ops_run_reader.py -q`
Expected: all pass.

- [ ] **Step 6: Commit** — `feat(ops): look up each unit's last same-AOI duration`

---

### Task 2: `expected_ms` and `unit_slots` on the detail route

**Files:**
- Modify: `apps/api/routers/jobs.py` (`UnitDetail` ~106, `RunDetailResponse` ~120, `get_run_detail` ~770, `_detail_before_envelope` ~813)
- Test: `tests/contract/test_jobs_api.py` (`_FakeOpsReader` ~256 gains `expected_durations`, default `{}`)

**Interfaces:**
- Consumes: `OpsRunReader.expected_durations` (Task 1).
- Produces (JSON): `units[].expected_ms: int | null`, `unit_slots: int`.

- [ ] **Step 1: Failing contract tests**

```python
def test_detail_carries_expected_ms_and_unit_slots(monkeypatch):
    monkeypatch.setenv("REIMAGINA_UNIT_CONCURRENCY", "8")
    # reader: one run, units a (history 5000) and b (none)
    body = client.get(f"/jobs/{run_id}/detail").json()
    assert body["unit_slots"] == 8
    assert {u["indicator"]: u["expected_ms"] for u in body["units"]} == {"a": 5000, "b": None}

def test_detail_before_envelope_has_no_expected_ms(monkeypatch):
    # existing pre-envelope fake path
    assert all(u["expected_ms"] is None for u in body["units"])
    assert body["unit_slots"] == 8
```

- [ ] **Step 2: Run, expect failure** — `uv run pytest tests/contract/test_jobs_api.py -q -k "expected_ms"` → KeyError.

- [ ] **Step 3: Implement** — `UnitDetail.expected_ms: int | None = None`; `RunDetailResponse.unit_slots: int = 1`; `get_run_detail` calls `reader.expected_durations(run_id)` once and sets `unit_slots=settings.unit_concurrency()` in both responses. Comment on `unit_slots`: per-worker concurrency, not × workers, and why (spec §Backend).

- [ ] **Step 4: Run** — `uv run pytest tests/contract -q` → pass; `make lint` → clean.

- [ ] **Step 5: Commit** — `feat(api): expected unit duration and slot count on run detail`

---

### Task 3: `timeline.ts` — the render model

**Files:**
- Modify: `apps/web/src/ops/api.ts` (`UnitDetail.expected_ms: number | null`, `RunDetailResponse.unit_slots: number`)
- Create: `apps/web/src/ops/timeline.ts`
- Create: `apps/web/src/ops/fixtures/run-12-units.json` — the final frame of the recorded 12-indicator run (`run`, `units`, `server_now`), `expected_ms: null` on every unit, `unit_slots: 8`. Source: scratch recording `runs/everything.json`, last element of `snaps`.
- Test: `apps/web/src/ops/timeline.test.ts`

**Interfaces:**
- Consumes: `UnitDetail`, `formatDuration` from `derive.ts`.
- Produces:

```ts
export type RowState = "succeeded" | "failed" | "cancelled" | "skipped" | "running" | "ready" | "blocked";
export const UNIT_VISUAL: Record<RowState, { label: string; bar: string | null; dot: string }>;
export function rowState(unit: UnitDetail, byIndicator: Map<string, UnitDetail>): RowState;
export function rowOrder(units: UnitDetail[]): UnitDetail[];           // depth, then slug
export function forecastMs(units: UnitDetail[], slots: number): number | null;
export function axisMs(elapsedMs: number, forecast: number | null, live: boolean): number;
export function ticks(axis: number): { pct: number; label: string }[]; // last tick pct === 100
export interface Row {
  unit: UnitDetail; state: RowState;
  leftPct: number; widthPct: number;   // 0 width when no bar
  expectedPct: number | null;          // running only: expected_ms / axis
  numbers: string; note: string | null; over: boolean;
}
export interface TimelineModel {
  rows: Row[]; axis: number; elapsedMs: number; forecast: number | null;
  forecastPct: number | null; nowPct: number | null;   // null when finished
  ticks: { pct: number; label: string }[];
  strip: { pct: number; running: number }[];           // step points, for the SVG
  chips: { text: string; tone: Tone }[];   // Tone from lib/components/ui/tone.ts; past forecast = "stale", failed = "error", status chip = runTone[status]
}
export function timelineModel(units: UnitDetail[], slots: number, now: number, live: boolean): TimelineModel;
```

Origin `t0` = min `queued_at`. Elapsed: live → `now − t0`; finished → max `completed_at` − `t0`. A unit's bar end = `completed_at ?? now`.

- [ ] **Step 1: Failing tests** (`timeline.test.ts`, fixture-driven where marked)

```ts
test("ready vs blocked vs won't start", ...)
  // queued, prereq succeeded → "ready", note "next · waits for a free slot"
  // queued, prereq running   → "blocked", note "after <slug>"
  // queued, prereq failed    → "blocked", note "won't start: <slug> failed"
test("order is depth then slug and does not change during the run [fixture]", ...)
  // rowOrder at every 100 ms frame equals rowOrder of the final frame
test("forecast reproduces the recorded run within 1 s when expected = actual [fixture]", ...)
  // expect(Math.abs(forecastMs(u, 8)! - 29_700)).toBeLessThan(1_000)
test("forecast is null when any unit lacks expected_ms", ...)
test("axis: A1 holds then grows ×1.25 past 98 %", () => {
  expect(axisMs(20_000, 26_500, true)).toBe(26_500);
  expect(axisMs(26_000, 26_500, true)).toBe(33_125);
});
test("axis: A2 doubling", () => {
  expect(axisMs(9_000, null, true)).toBe(10_000);
  expect(axisMs(9_600, null, true)).toBe(20_000);
});
test("axis: finished run", () => expect(axisMs(29_800, 26_500, false)).toBe(29_800));
test("nothing drawn moves left or shrinks while live [fixture replay]", ...)
  // helper frameAt(units, t): blank started_at/completed_at later than t and set
  // status from what remains (queued/running/final); now = t0 + t.
  // expected_ms = 0.9 × actual duration (forces one overrun); frames every 100 ms;
  // between consecutive frames with equal axis: every row's leftPct is equal and
  // leftPct + widthPct is >= its previous value; axis never decreases while live.
test("running numbers: pct, over, no estimate", ...)
  // "4.0 s · 40%" | "+1.0 s over" (over === true) | "4.0 s"
test("chips: live with forecast, past forecast, finished", ...)
  // exact copy from Global Constraints
test("zero units and pre-envelope units do not throw", ...)
  // [] → rows [], chips []; units with only queued_at and empty depends_on → all "ready"
test("last tick sits at 100 %", () => expect(ticks(30_000).at(-1)!.pct).toBe(100))
```

- [ ] **Step 2: Run, expect failure** — `cd apps/web && npx vitest run src/ops/timeline.test.ts` → module not found.

- [ ] **Step 3: Implement `timeline.ts`.** `forecastMs` is list scheduling; the one algorithm the signature does not fix:

```ts
// Units in rowOrder; at each event start every pending unit whose prerequisites
// have finished (in the simulation) while fewer than `slots` run; then advance
// to the earliest simulated finish. Nothing running and units pending → null
// (cycle guard). Returns the last finish.
```
`ticks`: step = smallest of 1, 2, 5, 10, 15, 30, 60, 120, 300 s giving ≤ 6 intervals; last tick replaced by the axis end so it sits at 100 %.

- [ ] **Step 4: Run** — `npx vitest run src/ops/timeline.test.ts` → pass.

- [ ] **Step 5: Commit** — `feat(ops): timeline render model with forecast axis`

---

### Task 4: Clock — midpoint offset, monotonic now, fast tick while live

**Files:**
- Modify: `apps/web/src/ops/derive.ts` (`clockOffset`, add `monotonicNow`)
- Modify: `apps/web/src/ops/store.svelte.ts` (`loadList` ~170, `loadDetail` ~200)
- Modify: `apps/web/src/ops/App.svelte` (`clientNow`/`now` ~35, interval ~105)
- Test: `apps/web/src/ops/derive.test.ts` (`clock correction` block ~239)

**Interfaces:**
- Produces: `clockOffset(serverIso: string, sentAt: number, receivedAt: number): number` — `server − (sentAt + receivedAt) / 2`; `0` when unparseable.
- Produces: `monotonicNow(prev: number, next: number, toleranceMs = 1_000): number` — `prev` when `next` is behind by less than the tolerance, else `next` (a real correction is still accepted).

- [ ] **Step 1: Failing tests**

```ts
expect(clockOffset("2026-08-20T10:00:20Z", t(18), t(20))).toBe(1_000);  // midpoint 19 s
expect(monotonicNow(10_000, 9_600)).toBe(10_000);
expect(monotonicNow(10_000, 5_000)).toBe(5_000);
expect(monotonicNow(10_000, 10_100)).toBe(10_100);
```
Update the two existing `clockOffset` tests to the three-argument form.

- [ ] **Step 2: Run, expect failure** — `npx vitest run src/ops/derive.test.ts`.

- [ ] **Step 3: Implement.** Store: take `const sentAt = Date.now()` before each `await`, pass `Date.now()` after. App: `now` becomes `$state`, set in the tick as `now = monotonicNow(now, serverNow(Date.now(), ops.offsetMs))`; interval is 100 ms when `ops.openRun` exists and `!isRunTerminal(ops.openRun.run.status)`, else 1 000 ms (the `$effect` re-runs when that boolean flips). Keep the `document.hidden` guard.

- [ ] **Step 4: Run** — `npx vitest run` → all pass.

- [ ] **Step 5: Commit** — `fix(ops): latency-corrected monotonic clock, smooth tick while live`

---

### Task 5: Render it — `RunTimeline.svelte` and `RunDetail.svelte`

**Files:**
- Rewrite: `apps/web/src/ops/RunTimeline.svelte`
- Modify: `apps/web/src/ops/RunDetail.svelte` (delete the Progress card ~193-220 and the units table ~225-274; pass `slots` and `live`)
- Modify: `apps/web/src/ops/derive.ts` (delete `TimelineRow`, `Timeline`, `PX_PER_SECOND`, `liveWindowMs`, `fitPxPerSecond`, `tickStepSeconds`, `axisTicks`, `timelineOf`, `peakParallelism`)
- Modify: `apps/web/src/ops/derive.test.ts` (delete the `timelineOf`, `nothing already drawn ever moves`, `axis ticks`, `fitPxPerSecond`, `liveWindowMs` blocks and their imports)

**Interfaces:**
- Consumes: `timelineModel`, `UNIT_VISUAL` (Task 3); `now` from App (Task 4).
- Props: `RunTimeline({ units: UnitDetail[]; slots: number; now: number; live: boolean })`.

- [ ] **Step 1: Rewrite `RunTimeline.svelte`** as a renderer of `timelineModel(units, slots, now, live)`:
  - Header: chips row (`Badge` with the chip tone); legend built by iterating `UNIT_VISUAL` (no colour literal in the component).
  - Strip: inline SVG, `height 32`, step path from `strip`, dashed cap line at `slots`, now line at `nowPct`.
  - Rows: CSS grid `grid-cols-[16rem_minmax(0,1fr)_7rem]`, every row `h-7 items-center`; label = dot + slug (`truncate`, `title` = slug); track `relative h-3`; expected outline `border border-dashed` at `expectedPct`; bar at `leftPct`/`widthPct` with `min-w-[2px]` when running; `note` text placed at `nowPct` (or left 0 when finished) for ready/blocked; numbers right-aligned `font-mono tabular-nums`, warning colour when `over`.
  - Failed row: `<details>` under the row with `unit.detail` (same markup the old table used).
  - Forecast: dashed vertical at `forecastPct` across the track column.
  - Axis: ticks at `pct`; last label `-translate-x-full` so it never clips.
  - Empty: `This run has no units.`
- [ ] **Step 2: Trim `RunDetail.svelte`** — delete the Progress card and the table, drop now-unused imports (`progressOf`, `unitTone`), render `<RunTimeline units={detail.units} slots={detail.unit_slots} {now} live={!isRunTerminal(run.status)} />`. Run card stays.
- [ ] **Step 3: Delete the old timeline code and tests** listed above.
- [ ] **Step 4: Gate** — `make build-web && make test-web` → typecheck clean, all vitest pass; `grep -rn "timelineOf\|liveWindowMs\|PX_PER_SECOND" apps/web/src` → no hits.
- [ ] **Step 5: Commit** — `feat(ops): stable execution timeline with forecast axis`

---

### Task 6: Live verification and PR

**Files:** none in the repo (scratch scripts `record.py`, `shoot.js` from the investigation; recreate if `/tmp` was cleaned).

- [ ] **Step 1:** With `make dev` running, submit the four mixes (urban_form_dag, livability_dag, mixed, everything) **twice** — the first round seeds history, the second has `expected_ms` on every unit. Also one run on an AOI with no history.
- [ ] **Step 2:** Headless frames every 1 s of each second-round run (`NODE_PATH=…/ghsci/svelte/node_modules node shoot.js live '<json>'`). Check every frame against the anti-goals: no row reorders, no bar moves left or shrinks while live, legend colours equal bar colours, nothing clipped, no blank first second. The no-history run must use the doubling axis and show no `%`.
- [ ] **Step 3:** `make check` and `make test-integration` → green.
- [ ] **Step 4:** Report frames to the user; push and open the PR only on their go-ahead.

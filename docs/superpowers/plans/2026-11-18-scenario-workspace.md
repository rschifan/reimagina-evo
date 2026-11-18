# Scenario Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Svelte `scenarios.html` workspace that shows only the indicators a scenario rule can change, a table of every scenario on the pilot, a map of one, and full rule authoring — with the map page returned to the baseline city.

**Architecture:** Three small API additions (`GET /scenarios/indicators`, `sum` on stats, `edit_count` + `latest_run` on scenario rows). A new Svelte 5 entry whose logic lives in a runes store and pure modules under `src/scenarios/`, calling the existing vanilla-TS helpers (`scenario.ts` pure exports, `choropleth.ts`, `selection.ts`, `aoi.ts`, `api.ts`) rather than re-implementing them. Then the map page's scenario half is deleted and the delivery documents are regenerated.

**Tech Stack:** FastAPI + psycopg2 + PostGIS; Svelte 5 runes, Tailwind 4, MapLibre 6, terra-draw, vitest 5 (jsdom per file), Playwright e2e.

**Spec:** `docs/superpowers/specs/2026-11-18-scenario-workspace-design.md`

## Global Constraints

- Work only in `/Users/rschifan/Desktop/projects/reimagina/.claude/worktrees/scenario-workspace` on `feat/scenario-workspace`. Never touch the shared checkout.
- Commit with `git -c user.name='Rossano' -c user.email='rossano.schifanella@gmail.com' commit`. No Co-Authored-By, no AI attribution in commits, code, comments or PR text.
- Stage paths explicitly — `git add -A` sweeps an npm-written `engines` block into `apps/web/package-lock.json`.
- No new dependencies (npm or Python).
- Python DB tests: `TEST_DATABASE_URL=REIMAGINA_APP_DSN=DATABASE_URL_DIRECT=postgresql://localhost/reimagina_test_ws` (all three set), `uv run pytest`.
- Web: `cd apps/web && npx vitest run <file>`; full web gate `npm run build && npx vitest run`.
- Python gate: `uv run ruff check . && uv run ruff format --check . && uv run mypy packages/py/reimagina apps scripts`.
- The reachable set is exactly, in this order: `urban_form_block_planned_floors`, `edificability_block_housing_units`, `edificability_block_population`, `edificability_block_density`, `livability_block_score` — wherever `compute_dirty_set` orders them; tests assert livability after density, not a fixed full order unless the function's sorted output is stable (it is: `topological_order` sorts).
- `statistic` is `"sum"` for units `units` and `people`, else `"median"`.
- The baseline tile URL must stay byte-identical: build every tile URL with `tileUrlFor`.
- Every delivery document keeps the same front-matter `date:`; a change is recorded in its revision table only (`delivery_manifest.py` refuses mixed dates).

## Review Focus

1. A pilot with no scenarios, or no active pilot → table shows only the Baseline row, "New scenario" disabled with a reason; no request with `aoi_id=null`. (Task 4 test `statusFor`/store boot test in Task 6.)
2. One table cell's stats request fails → that cell shows "?" with the error in its title; the rest of the table renders. (Task 6 test.)
3. The URL hash names a scenario that is deleted or belongs to another pilot → selection falls back to none, with a message; no 404 loop. (Task 6 test.)
4. Rules written but never recomputed → cells "—" (not 0) and status "Not computed"; rules changed after a recompute → "Rules changed — recompute". (Task 4 tests.)
5. Fast switching between scenarios → a slower response for the previous selection (rules, cells, run follow, comparison) never lands on the current one. (Task 5 + Task 6 tests.)

---

### Task 1: `GET /scenarios/indicators`

**Files:**
- Modify: `packages/py/reimagina/scenarios/proposal.py` (append)
- Modify: `apps/api/routers/scenarios.py` (new model + route, declared **above** `@router.get("/{scenario_id}")`)
- Test: `tests/unit/test_scenario_proposal.py`, `tests/contract/test_scenarios_api.py`

**Interfaces:**
- Produces: `proposal.reachable_indicators(registry: Sequence[IndicatorSpec] = SHIPPED_INDICATORS) -> list[str]`; `proposal.table_statistic(unit: str | None) -> Literal["sum", "median"]`; route `GET /scenarios/indicators` → `list[ReachableIndicator]` where `class ReachableIndicator(IndicatorSummary): statistic: Literal["sum", "median"]` (`IndicatorSummary` imported from `apps.api.routers.indicators`).

- [ ] **Step 1: Write the failing unit tests**

```python
def test_a_rule_reaches_exactly_the_five_indicators():
    assert set(reachable_indicators()) == {
        "urban_form_block_planned_floors", "edificability_block_housing_units",
        "edificability_block_population", "edificability_block_density",
        "livability_block_score",
    }

def test_a_composite_comes_after_the_input_it_reads():
    order = reachable_indicators()
    assert order.index("livability_block_score") > order.index("edificability_block_density")

def test_counts_are_summed_and_rates_are_medians():
    assert table_statistic("units") == "sum" and table_statistic("people") == "sum"
    for unit in ("units_per_ha", "floors", "score_0_1", None):
        assert table_statistic(unit) == "median"
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_scenario_proposal.py -q` — Expected: FAIL, `ImportError: cannot import name 'reachable_indicators'`.
- [ ] **Step 3: Implement** both functions in `proposal.py`: changed attributes = `{e["attribute_key"] for e in edits_for(1, 1, 1)}`; `[u.indicator for u in compute_dirty_set(changed, (), registry)]`. Docstring says why it is derived, not listed.
- [ ] **Step 4: Run** the same command — Expected: PASS.
- [ ] **Step 5: Write the failing contract tests** (fake connection, as the file's other tests do; the route reads catalog rows through `get_backend(conn).indicator_catalog_repo().list_indicators()` and keeps reachable slugs in reachable order):

```python
def test_the_indicator_list_is_not_read_as_a_scenario_id(client, events, monkeypatch):
    # stub the catalog repo to return all twelve rows; the route must answer 200
    # with the five, ordered, each carrying `statistic` — not 404 "no scenario".
    response = client.get("/scenarios/indicators")
    assert response.status_code == 200
    body = response.json()
    assert [r["slug"] for r in body] == reachable_indicators()
    assert {r["slug"]: r["statistic"] for r in body}["edificability_block_housing_units"] == "sum"
```

- [ ] **Step 6: Run** `uv run pytest tests/contract/test_scenarios_api.py -q -k indicator_list` — Expected: FAIL (404 or 422 from the `/{scenario_id}` route).
- [ ] **Step 7: Implement** the route and model in `scenarios.py`, above `get_scenario_route`. Connection handling as the module's other read routes (`contextlib.closing(connect(api_dsn()))`), so `test_no_handler_uses_the_pooled_connection` stays green.
- [ ] **Step 8: Run** `uv run pytest tests/unit/test_scenario_proposal.py tests/contract/test_scenarios_api.py -q` — Expected: all PASS.
- [ ] **Step 9: Commit** — `feat(scenarios): GET /scenarios/indicators lists what a rule can change`.

### Task 2: `sum` on indicator stats

**Files:**
- Modify: `packages/py/reimagina/storage/postgres/repositories.py` (`value_stats` SELECT + dict), `apps/api/routers/indicators.py` (`IndicatorValueStats.sum: float | None = None`), `apps/web/src/api.ts` (`IndicatorValueStats.sum: number | null`)
- Test: `tests/integration/test_scenario_read_overlay.py`

**Interfaces:**
- Produces: `value_stats(...)["sum"]: float | None` over exactly the rows `count` counts; JSON field `sum` on `GET /indicators/{slug}/stats`.

- [ ] **Step 1: Write the failing integration test** beside `test_value_stats_counts_each_feature_once`, same fixtures:

```python
def test_value_stats_sums_the_effective_values_once_each(blocks, conn):
    # baseline rows on every block, a scenario row on one block with a new value:
    # sum == baseline total - that block's baseline value + its scenario value
    stats = backend_for_connection(conn).indicator_result_repo().value_stats(
        indicator_slug=slug, scenario_id=SCENARIO)
    assert stats["sum"] == pytest.approx(expected)
```

and in the same file `test_value_stats_sum_is_none_with_no_rows` (unknown scenario on an indicator with no rows → `sum is None`, `count == 0`).

- [ ] **Step 2: Run** with the DB env — Expected: FAIL, `KeyError: 'sum'`.
- [ ] **Step 3: Implement** `SUM(ir.value_numeric) AS sum` in the SELECT, unpack it, `"sum": float(s) if s is not None else None`; add the model field and the TS field.
- [ ] **Step 4: Run** the file — Expected: PASS. Run `uv run pytest tests/unit tests/contract -q` — Expected: PASS (`ValueHistogram`/`extra` settings unchanged).
- [ ] **Step 5: Commit** — `feat(indicators): stats report the sum of the values they count`.

### Task 3: `edit_count` and `latest_run` on scenario rows

**Files:**
- Modify: `packages/py/reimagina/scenarios/authoring.py` (`list_scenarios` query), `apps/api/routers/scenarios.py` (`ScenarioRow`: `edit_count: int = 0`, `latest_run: LatestRun | None = None`, `class LatestRun(BaseModel): id: str; status: str`), `apps/web/src/api.ts` (`ScenarioRow.edit_count`, `ScenarioRow.latest_run: { id: string; status: string } | null`)
- Test: `tests/integration/test_scenario_authoring.py`, `tests/contract/test_scenarios_api.py`

**Interfaces:**
- Produces: each `list_scenarios` row carries `edit_count: int` and `latest_run: {"id": str, "status": str} | None` — newest `jobs_runtime.job_run` for that `scenario_id` by `queued_at DESC, id DESC`. `get_scenario` is unchanged (`ScenarioDetail` inherits the defaults).

- [ ] **Step 1: Write the failing integration tests**

```python
def test_the_list_counts_each_scenarios_edits(conn, ...):
    # one scenario with one proposal (4 edits), one with none
    rows = {r["name"]: r for r in list_scenarios(conn, aoi_id=aoi)}
    assert rows["with"]["edit_count"] == 4 and rows["without"]["edit_count"] == 0

def test_the_list_names_each_scenarios_newest_run(conn, ...):
    # two job_run rows for scenario A (failed older, succeeded newer), none for B
    rows = {r["name"]: r for r in list_scenarios(conn, aoi_id=aoi)}
    assert rows["A"]["latest_run"]["status"] == "succeeded"
    assert rows["B"]["latest_run"] is None
```

Use `tests/integration/_helpers.start_run` (or a direct `INSERT INTO jobs_runtime.job_run`) for the runs.

- [ ] **Step 2: Run** — Expected: FAIL, `KeyError: 'edit_count'`.
- [ ] **Step 3: Implement** in the one query: a correlated `count(*)` over `curated_geo.scenario_edit` and a `LEFT JOIN LATERAL (SELECT id, status FROM jobs_runtime.job_run r WHERE r.scenario_id = s.id ORDER BY queued_at DESC, id DESC LIMIT 1)`; fold the two run columns into `latest_run` (None when the id is null). Docstring: the queued-window limit, quoted from the spec.
- [ ] **Step 4: Run** the file — Expected: PASS. Add a contract test that a row with `latest_run=None` and `edit_count=0` serialises, run `tests/contract/test_scenarios_api.py` — Expected: PASS.
- [ ] **Step 5: Commit** — `feat(scenarios): list rows carry their edit count and newest run`.

### Task 4: Pure derivations for the workspace

**Files:**
- Create: `apps/web/src/scenarios/derive.ts`, `apps/web/src/scenarios/derive.test.ts`
- Modify: `apps/web/src/api.ts` — `export interface ReachableIndicator extends IndicatorSummary { statistic: "sum" | "median" }`, `export function listReachableIndicators(): Promise<ReachableIndicator[]>` (`GET /scenarios/indicators`)

**Interfaces:**
- Produces:
  - `type ScenarioStatus = "no-rules" | "not-computed" | "stale" | "queued" | "computing" | "failed" | "up-to-date"`
  - `statusFor(row: ScenarioRow, followed: { status: string } | null): ScenarioStatus`
  - `statusLabel(status: ScenarioStatus): string` — "No rules yet", "Not computed", "Rules changed — recompute", "Queued", "Computing", "Failed", "Up to date"
  - `cellValue(stats: IndicatorValueStats | null, statistic: "sum" | "median"): number | null` — null when `stats` is null or `count === 0`
  - `formatCell(value: number | null, indicator: ReachableIndicator): string` — "—" for null; sums with thousands separators via `format.ts`; medians to the precision `format.ts` uses for the unit
  - `parseHash(hash: string): { scenarioId: string | null; slug: string | null }` and `formatHash(scenarioId: string | null, slug: string | null): string` — `#/<scenario>/<slug>`, both optional, round-trip

- [ ] **Step 1: Write the failing tests**

```ts
const row = (o: Partial<ScenarioRow> = {}): ScenarioRow => ({ id: "A", project_id: "p", aoi_id: "a",
  name: "A", description: null, created_at: "2026-11-18T10:00:00Z", updated_at: "2026-11-18T10:00:00Z",
  materialized_at: null, edit_count: 4, latest_run: null, ...o });

it("no rules beats everything", () => expect(statusFor(row({ edit_count: 0 }), null)).toBe("no-rules"));
it("never materialised is not computed", () => expect(statusFor(row(), null)).toBe("not-computed"));
it("rules edited after materialising are stale", () =>
  expect(statusFor(row({ materialized_at: "2026-11-18T09:00:00Z" }), null)).toBe("stale"));
it("a failed latest run is failed even though materialised", () =>
  expect(statusFor(row({ materialized_at: "2026-11-18T11:00:00Z", latest_run: { id: "r", status: "failed" } }), null)).toBe("failed"));
it("the followed run wins over the row's latest run", () =>
  expect(statusFor(row({ materialized_at: "2026-11-18T11:00:00Z", latest_run: { id: "r", status: "failed" } }), { status: "running" })).toBe("computing"));
it("queued is queued", () => expect(statusFor(row({ materialized_at: "2026-11-18T11:00:00Z" }), { status: "queued" })).toBe("queued"));
it("a cell with no values is null, not zero", () =>
  expect(cellValue({ count: 0, min: null, p05: null, p50: null, p95: null, max: null, sum: null, histogram: null }, "sum")).toBeNull());
it("sum and median read different fields", () => { /* count 3, sum 30, p50 7 → 30 / 7 */ });
it("formatCell shows a dash for null", () => expect(formatCell(null, densityIndicator)).toBe("—"));
it("the hash round-trips", () => expect(parseHash(formatHash("A", "edificability_block_density"))).toEqual({ scenarioId: "A", slug: "edificability_block_density" }));
it("an empty hash selects nothing", () => expect(parseHash("")).toEqual({ scenarioId: null, slug: null }));
```

Status precedence: no-rules → followed run (queued/running→computing/failed) → stale (`updated_at > materialized_at`) → not-computed (`materialized_at` null) → latest_run failed → queued/computing from latest_run → up-to-date. Write the ordering as the implementation; the tests above pin it.

- [ ] **Step 2: Run** `npx vitest run src/scenarios/derive.test.ts` — Expected: FAIL, module not found.
- [ ] **Step 3: Implement** `derive.ts` and the `api.ts` additions.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `feat(web): status, cell and route derivations for the scenario workspace`.

### Task 5: Run follower and rule removal, out of the DOM

**Files:**
- Create: `apps/web/src/scenarios/follow.ts`, `apps/web/src/scenarios/follow.test.ts`

**Interfaces:**
- Consumes: `getRun`, `removeScenarioEdit` from `api.ts`; `isTerminalRunStatus`, `runProducedResults` from `scenario.ts`.
- Produces:
  - `const RUN_POLL_MS = 2_000, MAX_RUN_POLLS = 60, MAX_RUN_READ_FAILURES = 3` (moved from `scenario.ts`, which re-imports them until Task 11 deletes the panel)
  - `followRun(runId: string, on: FollowEvents): () => void` — returns `cancel`. `interface FollowEvents { progress(status: string): void; landed(run: RunView): void /* succeeded|partial */; failed(run: RunView): void /* failed|cancelled */; unreadable(): void /* read failures hit the cap, or polls hit MAX_RUN_POLLS while unreadable */; handedOver(status: string): void /* MAX_RUN_POLLS reached while running */ }`. After `cancel()`, no event fires, including from a tick already awaiting `getRun`.
  - `removeRule(scenarioId: string, editIds: string[]): Promise<{ removed: number; error: unknown | null }>` — sequential, stops at the first failure, every DELETE to `scenarioId`.

- [ ] **Step 1: Write the failing tests** (fake timers, `vi.mock("../api")`), porting `scenario-panel.test.ts` behaviours:

```ts
it("fires nothing after cancel, even from a tick in flight", ...)       // was: does not follow a run whose scenario is no longer active
it("keeps polling while the run is running", ...)                       // progress("running") then landed
it("survives one unreadable poll and lands on the next", ...)           // getRun rejects once, then succeeded → landed, no unreadable
it("hands over only after MAX_RUN_READ_FAILURES consecutive failures", ...) // 3 rejections → unreadable once
it("reports a failed run as failed", ...)                               // failed → failed(), never landed()
it("hands over a run still running after MAX_RUN_POLLS", ...)           // handedOver("running")
it("sends every DELETE to the scenario it was given", ...)              // removeRule("A", [e1,e2,e3]) → three calls with "A"
it("stops at the first failed DELETE and reports how many went", ...)   // second rejects → { removed: 1, error }
```

- [ ] **Step 2: Run** `npx vitest run src/scenarios/follow.test.ts` — Expected: FAIL, module not found.
- [ ] **Step 3: Implement** `follow.ts` with the token-guard pattern of `scenario.ts`'s `pollRun` (a cancelled flag checked after every await). Point `scenario.ts` at the moved constants.
- [ ] **Step 4: Run** `npx vitest run src/scenarios/follow.test.ts src/scenario-panel.test.ts src/scenario.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** — `refactor(web): follow a run and remove a rule without a DOM`.

### Task 6: The workspace store

**Files:**
- Create: `apps/web/src/scenarios/store.svelte.ts`, `apps/web/src/scenarios/store.test.ts`

**Interfaces:**
- Consumes: Tasks 4–5; `api.ts` (`listScenarios`, `listReachableIndicators`, `getIndicatorStats`, `createScenario`, `getScenario`, `addScenarioEdits`, `recomputeScenario`, `deleteScenario`, `getComparison`, `getIndicator`); `aoi.ts` (`subscribe`, `getActiveAoiId`); `scenario.ts` (`refusalText`, `groupEditsByArea`, `densityBoundOf`, `validateEdit`, `comparisonHeadline`, `DENSITY_INDICATOR_SLUG`, `FLOORS_INDICATOR_SLUG`).
- Produces: `export const ws` — a `$state` object `{ aoiId, projectId, indicators: ReachableIndicator[], scenarios: ScenarioRow[], cells: Record<string /* scenarioId | "baseline" */, Record<string /* slug */, { value: number | null; error: string | null }>>, selectedId: string | null, slug: string | null, mode: "value" | "delta", rules: EditGroup[], followed: { runId: string; status: string } | null, comparison: ComparisonHeadline | null, message: { text: string; tone: "ok" | "bad" } | null, densityValidMax: number | null, floorsValidMax: number | null }`, and actions: `boot(): Promise<void>`, `select(scenarioId: string | null): void`, `setIndicator(slug: string): void`, `setMode(mode): void`, `create(name: string): Promise<void>`, `applyRule(area: Feature<Polygon>, values: { levels: number; unitSize: number; peoplePerHousehold: number }): Promise<void>`, `removeRuleGroup(editIds: string[]): Promise<void>`, `recompute(): Promise<void>`, `compare(): Promise<void>`, `removeScenario(): Promise<void>`, `refreshTable(): Promise<void>`, and `onMapRefresh(listener: () => void): () => void` (fired when a followed run lands, so the map rebuilds its source).

- [ ] **Step 1: Write the failing tests** (mock `../api` and `../aoi`; fake timers):

```ts
it("boots on the active pilot: indicators, scenarios, then one stats call per cell", ...)
it("with no active pilot it lists nothing and asks for no stats", ...)            // Review Focus 1
it("a failed cell keeps its error and the rest of the table", ...)                // Review Focus 2
it("a hash naming an unknown scenario selects nothing and says so", ...)          // Review Focus 3
it("switching scenario cancels the followed run and drops late rule loads", ...)  // Review Focus 5
it("a landed run refreshes that row's cells, clears the comparison and fires onMapRefresh", ...)
it("a failed run leaves the overrides standing and says so", ...)
it("removing a rule sends every DELETE to the scenario it belongs to, even after a switch", ...)
it("changing pilot clears the selection", ...)
it("applyRule refuses locally with validateEdit's text before any request", ...)
it("cells are fetched with sum or median per the indicator's statistic", ...)
```

- [ ] **Step 2: Run** `npx vitest run src/scenarios/store.test.ts` — Expected: FAIL, module not found.
- [ ] **Step 3: Implement** the store. Selection changes bump a token; every async action captures the token and the scenario id and drops its result if either moved. Cells: one `getIndicatorStats(slug, { aoi_id, scenario_id })` per (row × indicator) through a 6-way concurrency cap — `// ponytail: one request per cell; a batch endpoint when a pilot holds dozens of scenarios`. Messages reuse `refusalText` and the current panel's copy.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `feat(web): the scenario workspace store`.

### Task 7: Page entry, navigation and build wiring

**Files:**
- Create: `apps/web/scenarios.html` (copy of `ops.html`'s shape: `#scenarios-root`, title `reimagina · scenarios`), `apps/web/src/scenarios/main.ts` (as `ops/main.ts`), `apps/web/src/scenarios/App.svelte` (TopBar + placeholders), `apps/web/src/scenarios/scenarios.css` (as `ops/ops.css`), `apps/web/tsconfig.scenarios.json` (as `tsconfig.ops.json` + `src/scenario.ts`, `src/choropleth.ts`, `src/selection*.ts`, `src/aoi.ts`, `src/api.ts`, `src/format.ts`, `src/download.ts` and whatever they import)
- Modify: `apps/web/src/lib/TopBar.svelte` (`current: "map" | "scenarios" | "catalog" | "ops"`; link `{ key: "scenarios", href: "/scenarios.html", label: "Scenarios" }` after Map), `apps/web/vite.config.ts` (`scenarios` in `rollupOptions.input`; header comment says four entries), `apps/web/package.json` (`check:scenarios`, chained into `build`), `tests/e2e/test_web_app_loads.py` (`PAGES` and the parametrised list gain `"scenarios"`) and `tests/e2e/conftest.py` / `_browser_probe.js` if they map page names to paths
- Test: `apps/web/src/vite-config.test.ts` if it enumerates entries

- [ ] **Step 1: Write the failing test** — extend `vite-config.test.ts` (or add one) asserting `scenarios` is in `build.rollupOptions.input` and points at `scenarios.html`.
- [ ] **Step 2: Run** `npx vitest run src/vite-config.test.ts` — Expected: FAIL.
- [ ] **Step 3: Implement** the files above.
- [ ] **Step 4: Run** `npm run build` — Expected: tsc, every svelte-check (including `check:scenarios`) and vite build succeed, `dist/scenarios.html` exists. `npx vitest run` — Expected: PASS.
- [ ] **Step 5: Commit** — `feat(web): a Scenarios page in the top bar`.

### Task 8: Scenario table

**Files:**
- Create: `apps/web/src/scenarios/ScenarioTable.svelte`; modify `App.svelte`

**Interfaces:**
- Consumes: `ws`, `select`, `refreshTable`, `create` (Task 6); `formatCell`, `statusLabel`, `statusFor` (Task 4); `Badge`, `Card` from `src/lib/components/ui`.

- [ ] **Step 1:** Table per the spec layout: Baseline row first, then scenarios by name; one column per `ws.indicators` in order, header = name + unit, tooltip "Total, potential in this scenario" (sum) or "Median across blocks" (median); "?" cells carry the error as `title`; status as a `Badge`; the selected row marked with `aria-selected` and a border, not colour alone; rows are buttons (keyboard reachable). "New scenario" (name input + button) in the TopBar `actions`, disabled with a `title` reason when `ws.projectId` is null; the pilot picker uses `populateAoiSelect`-equivalent Svelte `<select>` bound through `aoi.ts`.
- [ ] **Step 2: Run** `npm run check:scenarios && npx vitest run` — Expected: PASS.
- [ ] **Step 3: Verify** in the browser preview (`.claude/launch.json`, API on :8001 from this worktree): the table renders for the demo pilot with Baseline + scenarios, five columns. Record what was seen in the commit body.
- [ ] **Step 4: Commit** — `feat(web): scenario table with status and per-indicator totals`.

### Task 9: Scenario map

**Files:**
- Create: `apps/web/src/scenarios/ScenarioMap.svelte`; modify `App.svelte`

**Interfaces:**
- Consumes: `ws`, `setIndicator`, `setMode`, `onMapRefresh` (Task 6); `choroplethConfig`, `activateIndicator`, `removeIndicator`, `rampDomainFromStats`, `deltaDomainFromComparison`, `renderLegend`, `renderDeltaLegend`, `HISTOGRAM_BINS` (from `choropleth.ts`/`distribution.ts`); `enableSelection` + the toolbar wiring pattern of `main.ts::_wireSelectionToolbar`; `installInputLayers` with only the block layer (or a minimal equivalent from the same tile source) and the AOI boundary; `listTileSources`, `listAttributions`; `wireDownloadButtons`/`downloadUrl`.
- Produces: `export function drawnArea(): Feature<Polygon> | null` via a bindable prop the authoring panel reads.

- [ ] **Step 1:** MapLibre map in an `$effect`-managed container: empty style like `main.ts::emptyStyle`, `NavigationControl({ showCompass: true })`, `ScaleControl`, compact `AttributionControl` from `listAttributions()`; fly to the active AOI. Indicator `<select>` lists only `ws.indicators`; Values/Difference toggle shown only with a selected scenario; legends rendered into a bound element with the existing render functions; draw toolbar (circle/rectangle/polygon/clear) bound to `enableSelection(map)`; downloads (CSV/GeoJSON/GeoParquet) with `scenario_id`. Re-render on `ws.selectedId`, `ws.slug`, `ws.mode`, and on `onMapRefresh`, with a render token so a slow comparison/stats response for a previous selection is dropped.
- [ ] **Step 2: Run** `npm run check:scenarios && npx vitest run` — Expected: PASS.
- [ ] **Step 3: Verify** with `read_network_requests` in the preview: tile requests carry `scenario_id` (and `delta=true` only in Difference); the baseline view's tile URL is identical to the map page's. (MapLibre may not paint in a hidden pane — verify through requests, not pixels.)
- [ ] **Step 4: Commit** — `feat(web): scenario map with draw tools, legend and difference view`.

### Task 10: Rule panel

**Files:**
- Create: `apps/web/src/scenarios/RulePanel.svelte`; modify `App.svelte` (two-column under the table ≥ 1024 px, stacked below)

**Interfaces:**
- Consumes: `ws`, `applyRule`, `removeRuleGroup`, `recompute`, `compare`, `removeScenario` (Task 6); `CLIENT_PRECHECK_NOTE`, `maxLevelsFor`, `describeGroup` (export it from `scenario.ts` if not exported); the map's drawn area (Task 9).

- [ ] **Step 1:** Form (Storeys 6, m² per dwelling 60, People per household 3.6 defaults; storeys `max` clamped as `scenario.ts::clampLevels` does), the precheck note, "Apply to drawn area" disabled with a title until an area is drawn; rules list with × per area; Recompute; Compare to baseline (headline lines `counts`/`denominator`/`shift`); Delete behind a confirm naming the scenario; the message line with `role="status"`. Nothing shown but a prompt when no scenario is selected.
- [ ] **Step 2: Run** `npm run check:scenarios && npx vitest run` — Expected: PASS.
- [ ] **Step 3: Verify** end to end against the demo pilot through the preview: create → draw → apply → recompute → the row goes Queued/Computing → Up to date and its cells fill → Difference shows the change → delete. Note results in the commit body.
- [ ] **Step 4: Commit** — `feat(web): author rules from the scenario workspace`.

### Task 11: Return the map page to the baseline city

**Files:**
- Modify: `apps/web/index.html` (remove `data-section="scenario"` and `#mode-toggle` with their comments), `apps/web/src/main.ts` (remove `enableScenarioPanel` wiring, `activeScenarioId`, `activeMode`, the delta branch of `renderIndicator`, every `scenario_id` passed), `apps/web/src/scenario.ts` (delete `enableScenarioPanel`, `ScenarioPanelOptions`, `ScenarioController`, `PANEL_HTML`, `renderScenarioOptions`, `renderEditGroups`; keep every export Tasks 4–10 import; update the module docstring), `apps/web/src/style.css` (scenario-panel rules), `apps/web/src/commands.ts`/`shortcuts.ts` if they reference the panel
- Delete: `apps/web/src/scenario-panel.test.ts` (its behaviours are now in `follow.test.ts` and `store.test.ts`)

- [ ] **Step 1:** Before deleting, map each of the 7 `scenario-panel.test.ts` tests to its replacement in Tasks 5–6 in the commit body; a test without a replacement blocks the deletion.
- [ ] **Step 2:** Make the removals.
- [ ] **Step 3: Run** `grep -rn -E "enableScenarioPanel|scenario-panel|mode-toggle|activeScenarioId|activeMode" apps/web/src apps/web/*.html docs` — Expected: no hits outside the new workspace and history files.
- [ ] **Step 4: Run** `npm run build && npx vitest run` — Expected: PASS; `scenario.test.ts`'s baseline-URL test still passes.
- [ ] **Step 5: Commit** — `refactor(web): the map page shows the baseline city; scenarios have their own page`.

### Task 12: Repository documentation

**Files:**
- Modify: `docs/architecture/SCENARIOS_LAYER.md` ("Three surfaces author an edit" → the workspace; "The map carries the scenario in one place and four reads" rewritten for `scenarios.html`), `docs/architecture/API.md` (route, `sum`, `edit_count`, `latest_run`), `apps/web/README.md` (the fourth page)

- [ ] **Step 1:** Edit; every claim names the file that makes it true.
- [ ] **Step 2: Run** `grep -rn "scenario-panel\|#scenario-panel" docs apps/web/README.md` — Expected: no hits.
- [ ] **Step 3: Commit** — `docs: the scenario workspace`.

### Task 13: Delivery documents

**Files:**
- Modify: `docs/delivery/04_technical_documentation.md` (§1 page list; §10 re-measured; a revision-table row), `docs/delivery/03_database_and_api_reference.md` (regenerated), `docs/delivery/05_capability_proof.md` only if it describes the map-page panel, `docs/delivery/MANIFEST.md`, `docs/delivery/SHA256SUMS`

- [ ] **Step 1:** `uv run python scripts/build_schema_reference.py` per its usage line; confirm the diff adds exactly the new route row, `sum`, `edit_count`, `latest_run`.
- [ ] **Step 2: Re-run §10** in a fresh clone of this branch (`git clone` the worktree into the scratchpad), steps 1–11 exactly as the table lists, with `REIMAGINA_REQUIRE_DB=1` for DB suites, API/web on free ports if :8001/:5173 are taken (the e2e conftest's port settings). Record each actual result; step 5 lists `/`, `/scenarios.html`, `/catalog.html`, `/ops.html`.
  ⚠ `.env.example` names the databases `reimagina` and `reimagina_test` on the shared local server, which other sessions use, and the integration suite drops schemas (§9.5). After `cp .env.example .env`, rewrite only the database names in the clone's `.env` to `reimagina_install` / `reimagina_install_test` (same server, same roles), drop both when done, and say so in §10's prose — the one deviation from a fresh machine, stated rather than hidden.
- [ ] **Step 3:** Update §10's table and counts from that run only; §1's page list; add revision row `0.8 | <date> | Rossano Schifanella | Scenario workspace page; §10 re-measured`. Keep the front-matter `date:` equal across all four documents.
- [ ] **Step 4:** `NODE_PATH=~/Desktop/projects/ghsci/svelte/node_modules uv run python scripts/render_delivery_pdfs.py`, then `uv run python scripts/delivery_manifest.py` per its usage.
- [ ] **Step 5: Run** `uv run pytest tests/contract/test_delivery_manifest_instructions.py tests/contract -q && (cd docs/delivery && sha256sum -c SHA256SUMS)` — Expected: PASS, every line OK.
- [ ] **Step 6: Commit** — `docs(delivery): the scenario workspace, §10 re-measured, manifest regenerated`.

### Task 14: Full gate

- [ ] **Step 1: Run** the Python gate, `uv run pytest tests/unit tests/contract`, `tests/integration`, `tests/conformance` (DB env), `cd apps/web && npm run build && npx vitest run`, `REIMAGINA_E2E=1 uv run pytest tests/e2e`. Expected: all green; e2e includes `scenarios` in every parametrised page test.
- [ ] **Step 2:** Final whole-branch review per the execution skill; fix Critical/Important with a failing test first.

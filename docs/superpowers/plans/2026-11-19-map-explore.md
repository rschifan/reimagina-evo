# Map page for exploration — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the map page around exploring one indicator (colours on the
opening view, legend and block inspector on the map, a stable selection summary)
and carry the shared fixes to the scenario workspace.

**Architecture:** Three small API additions (a catalog `cap_value`, cap counts
and exact ranks, polygon-scoped stats) feed a reworked vanilla-TS map page.
Shared modules (`choropleth.ts`, `layers.ts`, `map-base.ts`, `tooltip.ts`)
change once and both pages pick the changes up. Map-only parts (panel, floating
cards, inspector, selection summary) live in `main.ts`, `index.html` and
`style.css`.

**Tech Stack:** FastAPI + psycopg2 + PostGIS, Alembic migrations, MapLibre GL 6,
vanilla TS (map page), Svelte 5 (scenario workspace), vitest, pytest, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-19-map-explore-design.md`

## Global Constraints

- Commit as the configured git user; no AI attribution anywhere; never commit `CLAUDE.md`.
- Opening-view zoom floor: **8** (overlay tile function serves only z ≥ 8).
- Cap: only indicators with a non-null `cap_value`; today the four `accessibility_block_*_network_m` at `ACCESSIBILITY_CUTOFF_M` = 1500.
- Cap colour: not teal / red / amber (`DELTA_GOOD`, `DELTA_BAD`, `DELTA_NEW`); validated against every ramp stop and the map background.
- Delta mode on the scenario workspace keeps its palette and legend unchanged.
- Downloads send `aoi_id`, `feature_type`, `limit=500000`.
- No rank on hover; exact rank only in the inspector, from the server.
- Copy: sentence case, no raw identifiers (`CVE_ENT=…`, `units_per_ha`, `p05–p95`).
- Gates before any push: `make check`, `make test-integration`, `make test-conformance` (migration touches the data layer).

## Review Focus

1. Indicator with no cap (livability, compactness): no cap swatch, no "% at the cap", selection shows the p05–p95 range — covered in Task 5 and Task 10 tests.
2. AOI switch with a pinned block and a drawn selection: inspector closes, selection clears, nothing from the old AOI stays on screen — Task 9 and Task 10.
3. Scenario workspace in delta mode with a capped indicator: delta colours and legend unchanged — Task 5 test plus Task 12 Playwright.
4. Selection with no blocks inside, or with no indicator active: card says so instead of showing zeros as data — Task 10.
5. Fast drawing: a slow response for an older shape must not overwrite a newer one — Task 10 (abort + token test).

---

### Task 1: `cap_value` in the catalog

**Files:**
- Create: `infra/db/migrations/versions/r051_indicator_cap_value.py`
- Modify: `packages/py/reimagina/indicators/catalog.py` (`IndicatorSpec`, the four accessibility specs, `seed_catalog`)
- Modify: `packages/py/reimagina/storage/postgres/repositories.py` (`upsert_indicator`, catalog row reads)
- Modify: `packages/py/reimagina/storage/interfaces/indicators.py` (`upsert_indicator` signature)
- Modify: `apps/api/routers/indicators.py` (`IndicatorSummary`, row mapping near line 499)
- Test: `tests/integration/test_api_routes.py`, `tests/unit/` catalog test (whichever already asserts spec fields)

**Interfaces:**
- Produces: `IndicatorSpec.cap_value: float | None = None`; column `curated_geo.indicator_catalog.cap_value double precision NULL`; `IndicatorSummary.cap_value: float | None`.

- [ ] **Step 1: Write failing tests**
  - `test_indicator_list_exposes_cap_value`: after `seed_catalog(conn)`, `GET /indicators` has `cap_value == 1500.0` for `accessibility_block_park_network_m` and `None` for `urban_form_block_compactness`.
  - Unit: every `SHIPPED_INDICATORS` spec whose slug starts `accessibility_block_` and ends `_network_m` has `cap_value == ACCESSIBILITY_CUTOFF_M`; every other spec has `None`.
- [ ] **Step 2: Run** `uv run pytest tests/integration/test_api_routes.py -k cap_value -q` → FAIL (`KeyError`/missing field).
- [ ] **Step 3: Implement.** Migration `r051` (revises `r050`): `ALTER TABLE curated_geo.indicator_catalog ADD COLUMN cap_value double precision`, downgrade drops it. Thread `cap_value` through spec → `seed_catalog` → `upsert_indicator` (insert + `ON CONFLICT` update) → reads → `IndicatorSummary`.
- [ ] **Step 4: Run** the two tests → PASS; `uv run alembic upgrade head` on the dev DB, then reseed (`make` target or CLI the repo uses for `seed_catalog`) so the running app sees the column.
- [ ] **Step 5: Commit** `feat(catalog): cap_value marks a bound that means beyond measurement`

### Task 2: `count_at_cap` on `/stats`

**Files:**
- Modify: `repositories.py` `value_stats` (~line 2805), `interfaces/indicators.py` `value_stats` docstring
- Modify: `apps/api/routers/indicators.py` `IndicatorValueStats`
- Test: `tests/integration/test_api_routes.py`

**Interfaces:**
- Consumes: Task 1 `cap_value`.
- Produces: `IndicatorValueStats.count_at_cap: int | None` — rows `>= cap_value` over the same filtered rows as `count`; `None` when the indicator has no cap.

- [ ] **Step 1: Failing tests** (reuse `two_aois_one_block_each`):
  - `test_stats_count_at_cap_is_null_without_a_cap`: compactness → `count_at_cap is None`.
  - `test_stats_count_at_cap_counts_rows_on_the_cap`: `UPDATE curated_geo.indicator_catalog SET cap_value = <block_a's value>` for compactness; `?aoi_id=<aoi_a>` → `1`; `?aoi_id=<aoi_b>` → `0` when block_b's value is lower, else adjust the cap to the smaller value and assert the AOI split.
- [ ] **Step 2: Run** `-k count_at_cap` → FAIL.
- [ ] **Step 3: Implement**: one extra aggregate in the existing query, `COUNT(*) FILTER (WHERE ir.value_numeric >= c.cap_value)` with a scalar subquery or join on `indicator_catalog` by slug; return `None` when `cap_value` is null.
- [ ] **Step 4: Run** `-k "stats"` → PASS (existing stats tests still green).
- [ ] **Step 5: Commit** `feat(api): stats report how many values sit on the cap`

### Task 3: `GET /features/{feature_id}/indicators`

**Files:**
- Modify: `apps/api/routers/features.py` (new route), `repositories.py` + `interfaces` (new reader method)
- Test: `tests/integration/test_api_routes.py`, `tests/contract/test_malformed_ids.py`

**Interfaces:**
- Produces: response `list[FeatureIndicatorValue]` with `slug, name, unit, cap_value, value, count_below, count_equal, count`; query `aoi_id: UUID | None`, `scenario_id: UUID | None`. Counts are over current rows of the same indicator and the same `feature_type` as this feature, scoped to `aoi_id` exactly as `value_stats` scopes (`aoi_feature` EXISTS). 404 when the feature does not exist; empty list when it has no values.
- Repo method: `feature_indicator_ranks(*, feature_id: str, aoi_id: str | None, scenario_id: str | None) -> list[dict]`.

- [ ] **Step 1: Failing tests**
  - `test_feature_indicators_rank_within_the_aoi`: block_a with `?aoi_id=<aoi_a>` → one row for compactness, `count == 1`, `count_below == 0`, `count_equal == 1`; without `aoi_id` → `count == 2`.
  - `test_feature_indicators_404_on_unknown_feature`.
  - Contract: `("GET", f"/features/{BAD}/indicators", None)` and `?aoi_id={BAD}` → 422.
- [ ] **Step 2: Run** → FAIL (404 route missing).
- [ ] **Step 3: Implement** one SQL statement: the feature's current values (`_scenario_overlay` for scenario), then per indicator `COUNT(*) FILTER (WHERE v < mine)`, `FILTER (WHERE v = mine)`, `COUNT(*)` over the scoped population; join catalog for `name, unit, cap_value`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(api): a feature's indicator values with their exact rank in the AOI`

### Task 4: `POST /metrics/indicator-within-polygon`

**Files:**
- Modify: `apps/api/routers/metrics.py`, `repositories.py` (`value_stats` gains `polygon_geojson: str | None = None`), interface
- Test: `tests/integration/` metrics tests (the file covering `/metrics/within-polygon`)

**Interfaces:**
- Consumes: Task 2 stats shape.
- Produces: request `{aoi_id: UUID, polygon: GeoJSON Polygon, indicator_slug: str, feature_type: str | None}`; response = `IndicatorValueStats` (with `count_at_cap`, no histogram). Polygon predicate identical to `count_features_within_polygon`: `ST_Covers(ST_SetSRID(ST_GeomFromGeoJSON(polygon), 4326), geometry_centroid)`. Same 422s as `/within-polygon` for non-Polygon and non-finite coordinates.

- [ ] **Step 1: Failing tests**: polygon around block_a → `count == 1`; polygon in empty desert → `count == 0` and percentiles `None`; a `Point` → 422; unknown slug → 404.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** by adding the optional polygon clause to `value_stats` (EXISTS on the feature's centroid) and a thin route that reuses the `/within-polygon` validators.
- [ ] **Step 4: Run** → PASS, and the existing `/within-polygon` tests.
- [ ] **Step 5: Commit** `feat(api): indicator stats inside a drawn polygon`

### Task 5: Shared colour, cap and legend (both pages)

**Files:**
- Modify: `apps/web/src/api.ts` (types), `choropleth.ts` (`valueRamp`, `activateIndicator` fill expression, `renderLegend`, `markLegendValue`, tooltip text), `distribution.ts` (drop the rank readout path), `format.ts`
- Modify: `apps/web/src/catalog/derive.ts` + `IndexView.svelte`/`DetailView.svelte`/`LineageGraph.svelte` imports (move `unitLabel`)
- Test: `choropleth` tests (existing `*.test.ts` covering `valueRamp`/legend), `format.test.ts`, `distribution.test.ts`, `catalog/derive.test.ts`

**Interfaces:**
- Consumes: `IndicatorSummary.cap_value`, `description`, `model_family`; `IndicatorValueStats.count_at_cap`.
- Produces: `unitLabel(unit: string | null): string` in `format.ts`; `CAP_COLOR` exported from `choropleth.ts`; `valueRamp(domain, capValue: number | null)`; `renderLegend(container, indicator, domain, histogram, countAtCap: number | null, total: number)`; `formatValue(value: number, indicator): string` returning `"1,500 m+ (cap)"` at the cap.

- [ ] **Step 1: Failing tests**
  - `valueRamp` with `capValue = 1500` → expression is a `case` whose `>= 1500` branch is `CAP_COLOR`; with `null` → no `CAP_COLOR` anywhere in the expression.
  - Ramp stops exclude the darkest current stop (`#08306b`).
  - `CAP_COLOR` ∉ {`DELTA_GOOD`, `DELTA_BAD`, `DELTA_NEW`, `DELTA_GOOD_MID`, `DELTA_BAD_MID`}.
  - `renderLegend(..., countAtCap: 11781, total: 19058)` contains `"62% of blocks at the 1,500 m cap"`; with `cap_value: null` contains no `"cap"`.
  - `formatValue(1500, culture)` → `"1,500 m+ (cap)"`; `formatValue(0.41, livability)` → `"0.41"`.
  - `markLegendValue` output no longer contains `"higher than"`.
  - `unitLabel` tests move from `catalog/derive.test.ts` to `format.test.ts` unchanged.
- [ ] **Step 2: Run** `npx vitest run` in `apps/web` → FAIL on the new cases.
- [ ] **Step 3: Implement.** Pick the cap colour with the dataviz validator against the new ramp stops and `#1a1a19`; record the choice in a comment. Delta paths (`deltaRamp`, `deltaFill`, `renderDeltaLegend`) are not touched.
- [ ] **Step 4: Run** vitest + `npm run check:catalog` + `npm run check:scenarios` → PASS.
- [ ] **Step 5: Commit** `feat(web): cap colour and cap share in the legend; values read as words`

### Task 6: Shared layers and opening view (both pages)

**Files:**
- Modify: `apps/web/src/layers.ts` (`INPUT_LAYERS` `visible`, labels, `groupRastersAndOverlays`, `wireRasterToggles` heading)
- Modify: `apps/web/src/choropleth.ts` (`choroplethConfig`/`activateIndicator` minzoom), `map-base.ts` (`flyToAoi`)
- Test: `layers` tests, new `map-base.test.ts` for the pure clamp

**Interfaces:**
- Produces: `OVERLAY_MIN_ZOOM = 8` (exported from `map-base.ts`); `clampZoom(camera: {zoom: number}, floor: number)` pure helper used by `flyToAoi`; indicator overlay `minzoom = baseline minzoom for its grain` (reuse `_baselineMinzoomForGrain`, exported).

- [ ] **Step 1: Failing tests**
  - Every `INPUT_LAYERS` entry has `visible === false`.
  - Municipalities label is `"Municipalities"`.
  - `groupRastersAndOverlays` returns orphan overlays with no group label (render test: no element with text `"Derived overlays (no parent raster)"`).
  - `clampZoom({zoom: 7.2}, 8).zoom === 8`; `clampZoom({zoom: 11}, 8).zoom === 11`.
  - Indicator overlay config for grain `block` has `minzoom === 8`.
- [ ] **Step 2: Run** vitest → FAIL.
- [ ] **Step 3: Implement.** `flyToAoi`: `map.cameraForBounds(bbox, {padding: 60, maxZoom: 14})`, clamp, `map.easeTo(camera)`. The `f` command and AOI switch already call `flyToAoi`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(web): layers start off; the first view lands where the indicator draws`

### Task 7: Delete `selection-filter.ts`

**Files:**
- Delete: `apps/web/src/selection-filter.ts`
- Modify: `choropleth.ts` (~line 548), `layers.ts` (~line 682), `main.ts` (`wireAggregationOverlayClipping`, `clearAggregationOverlayClipping` and their calls), `selection-geom.ts` (drop exports only it used, if any)

**Interfaces:**
- Produces: nothing; `activateIndicator` no longer takes `selectionController` (update both callers: `main.ts`, `ScenarioMap.svelte`).

- [ ] **Step 1: Failing check**: a vitest that greps the source tree for `registerValueAwareLayer` and `feature-state", "inSelection"` expects none — FAIL now.
- [ ] **Step 2: Implement** the deletion; remove every comment that mentions value clipping.
- [ ] **Step 3: Run** vitest + all three svelte-checks + `npx tsc --noEmit` → PASS.
- [ ] **Step 4: Commit** `refactor(web): drop selection clipping, which MapLibre never accepted`

### Task 8: Map page layout and scoped downloads

**Files:**
- Modify: `apps/web/index.html`, `apps/web/src/style.css`, `main.ts` (status → map banner, legend container moved), `api.ts` `downloadUrl`, `download.ts`, `map-base.ts` (single attribution control)
- Test: `download.test.ts` (new or existing), Playwright in Task 12

**Interfaces:**
- Consumes: Task 5 legend, Task 6 layers.
- Produces: `downloadUrl(slug, format, scope: {aoiId: string | null; grain: string | null; scenarioId?: string | null}): string`; DOM ids `#map-legend` (card on the map), `#map-banner`, `#indicator-about` (catalog link), `#layers-summary` ("N on").

- [ ] **Step 1: Failing test**: `downloadUrl("x","csv",{aoiId:"a",grain:"block"})` contains `aoi_id=a`, `feature_type=block`, `limit=500000`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the panel order from the spec (AOI, indicator + description + "About this indicator", grain only when > 1, Layers collapsed with count, download chips + "N blocks · <AOI> only"); legend card bottom-left of `.map-panel`; banner top-centre for loading/errors only; AOI raw description no longer rendered; one attribution control.
- [ ] **Step 4: Run** vitest → PASS; build `npm run build` → OK.
- [ ] **Step 5: Commit** `feat(web): map-first layout — indicator on top, legend on the map`

### Task 9: Block inspector

**Files:**
- Create: `apps/web/src/inspector.ts`, `apps/web/src/inspector.test.ts`
- Modify: `api.ts` (`getFeatureIndicators`), `main.ts` (click wiring, AOI-change close, `Esc`), `style.css`

**Interfaces:**
- Consumes: Task 3 endpoint, Task 5 `formatValue`.
- Produces: `rankText(row: FeatureIndicatorValue): string`; `renderInspector(container, feature, rows, activeSlug, onPick: (slug) => void, onClose: () => void)`; map click on the overlay layer pins, click on empty map closes.

- [ ] **Step 1: Failing tests** for `rankText`: at cap with `count_equal 11781 / count 19058` → `"At the cap, with 62% of blocks"`; unit `m`, `count_below 5908` → `"Farther than 31% of blocks"`; unit not `m` → `"Higher than 31% of blocks"`; `count 1` → `""` (no comparison).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; one request per click, aborted when another click or an AOI change arrives; the pinned feature gets an outline via the existing hover-highlight feature-state on the overlay source; clicking a row in the card dispatches the indicator `<select>` change.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(web): click a block to pin its values and rank`

### Task 10: Selection summary without flicker

**Files:**
- Create: `apps/web/src/selection-card.ts`, `selection-card.test.ts`
- Modify: `main.ts` (`_wireSelectionPanel` replaced), `api.ts` (`indicatorWithinPolygon`), `index.html`, `style.css`
- Delete: `selection-metrics.ts` and its tests if nothing else imports them

**Interfaces:**
- Consumes: Task 4 endpoint, Task 2 `count_at_cap`, AOI-wide stats already fetched for the legend.
- Produces: pure reducer `nextCardState(state, event): CardState` with events `selection-changed`, `response(token, stats)`, `cleared`; `CardState = {visible, stale: boolean, token: number, stats | null}`; `renderSelectionCard(container, state, indicator, aoiStats)`.

- [ ] **Step 1: Failing tests**
  - `selection-changed` keeps the previous `stats` and sets `stale: true` (never a placeholder).
  - `response` with an older token is ignored; with the current token replaces `stats` and clears `stale`.
  - Render: capped indicator → "Median 1,500 m+", "N% at the cap", "Whole AOI: median …"; uncapped → p05–p95 range, no "cap"; `count 0` → "No blocks inside"; no active indicator → block count only.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**: debounced (150 ms) single request per settled change, previous request aborted; stale numbers dimmed via a class; `font-variant-numeric: tabular-nums`; card cleared on AOI change.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(web): selection shows the indicator inside it, without flicker`

### Task 11: Phone layout

**Files:**
- Modify: `apps/web/src/style.css`, `main.ts` (bottom-sheet switching)

**Interfaces:**
- Consumes: Tasks 8–10 cards.

- [ ] **Step 1:** Below 768 px: legend becomes a one-line strip; inspector and selection card render as one bottom sheet (newest wins); tap pins (the click handler already does).
- [ ] **Step 2:** Verified in Task 12 at 390×844 (no horizontal overflow, overlay visible on first view, sheet does not cover the draw tools).
- [ ] **Step 3: Commit** `feat(web): phone layout for legend and cards`

### Task 12: Verification and gates

- [ ] **Step 1:** Playwright, map page, 1440×900 and 390×844 (scripts in the session scratchpad, `NODE_PATH` to an installed `playwright`): opening view has overlay tiles requested and painted; all layer checkboxes unchecked; no `Derived overlays` text; draw a rectangle → no `#map-banner` error, the card's number element never contains `…` during the drag; click a block → inspector lists other indicators with ranks; download links carry `aoi_id`; no console errors.
- [ ] **Step 2:** Playwright, scenario workspace: value mode shows the cap legend for a distance indicator; delta mode legend and colours unchanged (compare against a screenshot taken on `main` before Task 5); layers off; no console errors.
- [ ] **Step 3:** `make check`, `make test-integration`, `make test-conformance` → all green (the local delivery-PDF manifest test is red on `main` too; report it, do not "fix" it here).
- [ ] **Step 4:** Update `docs/architecture/API.md` for the three API additions; regenerate nothing under `docs/delivery/` without asking.
- [ ] **Step 5: Commit** `docs(api): document cap_value, feature indicators and polygon stats`

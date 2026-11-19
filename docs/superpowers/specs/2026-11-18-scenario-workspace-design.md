# Scenario workspace — design

**Date:** 2026-11-18 · **Ships in:** the Schedule A delivery due 20 November 2026
**Branch:** `feat/scenario-workspace` off `main` @ `fec878e`

## Intent

Planners need a workspace of their own for scenarios. Today scenario authoring
is one section of the map page's sidebar (`index.html` → `#scenario-panel`,
rendered by `src/scenario.ts`), squeezed between the pilot picker and the
spatial layers, in a sidebar that already scrolls to ~1,200 px.

The more important problem is what the map page *offers* while a scenario is
active: every indicator, of which a scenario rule can change only five. A
planner who picks building density or a walking distance under a scenario sees
a map identical to the baseline and has no way to tell "the scenario did
nothing here" from "the scenario cannot reach this". The workspace shows only
what a scenario can change, and lets a planner hold several scenarios side by
side.

## What a scenario can change today (the facts this design rests on)

A rule is a drawn area plus three numbers — storeys, m² per dwelling, people
per household. `scenarios/proposal.edits_for` turns it into four block
attributes: `floors`, `num_levels`, `unit_size`, `people_per_household`. The
catalog's declared reads and dependencies decide what that reaches;
`orchestration/impact.compute_dirty_set` over those four keys returns:

| Indicator | How it is reached | Unit |
|---|---|---|
| `urban_form_block_planned_floors` | reads `floors` | floors |
| `edificability_block_housing_units` | reads `num_levels`, `unit_size` | units |
| `edificability_block_population` | reads all three edificability inputs | people |
| `edificability_block_density` | reads all three edificability inputs | units_per_ha |
| `livability_block_score` | composite, 40 % housing density | score_0_1 |

The other seven (building density, compactness, the urban-form composite, four
network walking distances) cannot move under any scenario.

⚠ **The real pilots have no baseline for these five.** INEGI blocks carry
identifiers only and no cadastre or zoning table is loaded, so the four
attributes exist only where `scripts/seed_demo_attributes.py` wrote fixture
values. Under a real scenario these indicators hold values only inside rule
areas, and a comparison with the baseline reads mostly `new`. This is why the
workspace compares scenarios with each other in a table, not only with the
baseline.

## Goals

1. **A new page, `scenarios.html`**, in the Svelte 5 + Tailwind style of the
   Catalog and Runs pages, with a "Scenarios" link in the shared `TopBar`
   between Map and Catalog.
2. **Only the indicators a scenario can change**, everywhere on the page. The
   list comes from the server, derived from the catalog — never typed into the
   frontend — so a sixth reachable indicator appears without a frontend change.
3. **A table of every scenario on the active pilot**, one row each plus a
   *Baseline city* row, one column per reachable indicator, each cell one
   number: the **total** for additive indicators (housing units, population)
   and the **median** otherwise. Each row carries its status.
4. **A map of one scenario at a time**: the selected row's scenario, one of
   the reachable indicators, as values or as the difference from the baseline,
   with the draw tools for authoring.
5. **Authoring parity** with today's panel: create, add a rule over a drawn
   area, remove a rule, recompute and follow the run, compare with the
   baseline, delete — with every refusal and guard the current panel has.
6. **The map page returns to the baseline city**: its Scenario section, its
   Values/Difference toggle and its scenario plumbing go.
7. **Delivery documents stay true**: the page list, the API reference and the
   architecture docs describe what ships.

## Non-goals

- A map of scenario A against scenario B (needs a new comparison and a change
  to the `indicator_overlay_tile` SQL function; out of reach before delivery).
- New rule types — any attribute beyond the three numbers.
- Editing a rule's numbers in place (remove and re-add, as today).
- Choosing blocks by clicking, or a whole-pilot rule without drawing.
- Any change to how a recompute is materialised, queued or computed.
- Loading real baseline attributes (cadastre, zoning).

## Anti-goals — failures even if it technically works

- The page offers an indicator a scenario cannot change.
- A table total that reads as a measured fact about the territory when it is
  a scenario's potential. Header and tooltip say "potential, in this
  scenario", and the Baseline row shows "—" where it has no values rather
  than 0.
- Losing any behaviour the current panel already fixed: following only the
  run of the scenario still selected, surviving one unreadable poll, handing
  over to the Runs page after repeated failures, reporting a failed run as
  failed with its overrides standing, clearing a stale comparison when a new
  run lands, sending every rule DELETE to the scenario the rule belongs to,
  dropping the selected scenario when the pilot changes.
- The baseline tile URL changing by a byte (MapLibre caches tiles by URL;
  `tileUrlFor` owns and asserts that contract).
- An OpenStreetMap-derived map shown without its attribution (licence
  obligation; see `main.ts`).

## Constraints

- Delivery on 20 November; lands as one PR to `main`, merged by the user.
- Every commit authored and committed as `Rossano <rossano.schifanella@gmail.com>`;
  no AI attribution anywhere (AGENTS.md).
- No new dependencies. Available: `svelte` 5 (runes), `tailwindcss` 4,
  `bits-ui`, `@lucide/svelte`, `maplibre-gl`, `terra-draw`, the shared
  `src/lib/components/ui` (`Badge`, `Card`, `Chip`) and `src/lib/TopBar.svelte`.
- The shared checkout is used by other sessions; all work happens in the
  worktree `.claude/worktrees/scenario-workspace`.
- Gate = CI: `ruff check`, `ruff format --check`, `mypy packages/py/reimagina
  apps scripts`; `pytest tests/unit tests/contract`, `tests/integration`,
  `tests/conformance`; web `npm run build` (tsc + every svelte-check + vite
  build) and `vitest run`.

## Approaches

| | Approach | Trade-off |
|---|---|---|
| A | New page in the map page's vanilla-TS style, reusing `scenario.ts` whole | Least work and risk; looks unlike Catalog/Runs |
| **B** | **New Svelte page like Catalog/Runs (chosen)** | Consistent with the other workspaces; the panel UI is rewritten |
| C | "Scenario mode" toggle on the map page | Least code; not a dedicated workspace |

**Chosen: B, by the user.** The rewrite is confined to the *panel UI*. Pure
logic stays in the TypeScript modules that already hold it and are already
tested — `scenario.ts`'s exported helpers (`validateEdit`, `densityBoundOf`,
`refusalText`, `groupEditsByArea`, `comparisonHeadline`, `tileUrlFor`,
`isTerminalRunStatus`, `runProducedResults`, …), `choropleth.ts`
(`activateIndicator`, ramps, legends, `deltaDomainFromComparison`),
`selection.ts` (`enableSelection`, terra-draw), `aoi.ts` (the pilot choice,
already persisted in `localStorage` and so shared with the map page) and
`api.ts`. Svelte components call them; they are not re-implemented.

## Decided design

### Server

1. **`GET /scenarios/indicators`** — the indicators a scenario rule can
   change, in dependency order. Each row: the existing `IndicatorSummary`
   model of `apps/api/routers/indicators.py`, unchanged, plus
   `statistic: "sum" | "median"` — the number the
   table shows. Derived as `compute_dirty_set({e["attribute_key"] for e in
   edits_for(1, 1, 1)}, (), registry)`; `statistic` is `"sum"` for units
   `units` and `people`, `"median"` otherwise (*my call*: one rule in one
   place on the server, next to the catalog it reads).
   ⚠ Declared **before** the `/{scenario_id}` routes, or FastAPI matches
   `indicators` as a scenario id.
2. **`sum` on `GET /indicators/{slug}/stats`** — added to `IndicatorValueStats`
   and to `value_stats`'s SQL, over exactly the rows `count` counts (scenario
   overlay included). Nothing else about the endpoint changes.
3. **`edit_count` and `latest_run` on the scenario list rows** (`GET
   /scenarios`), computed in `authoring.list_scenarios`'s own query — not by
   reading `explore.scenario_v`, which application code does not use.
   `edit_count` lets the table say "no rules yet" without one request per
   scenario. `latest_run` is `{id, status} | null`: the scenario's newest
   run envelope in `jobs_runtime.orchestration_run` (corrected after review:
   `job_run` holds one row per indicator unit, not per run). `GET /jobs` cannot stand in for it — it has no
   scenario filter and pages at 200 — and a failed recompute still sets
   `materialized_at`, so without this a reload would show a failed scenario
   as up to date (*my call*).
   `# ponytail:` a run still waiting in the queue has no envelope until a
   worker opens it (`dbos_app.s_open_run`), so a reload during that window
   shows the previous run's status; the page shows "Queued" for the run it is
   itself following. Reading the queue's workflow input closes the window if
   it ever matters.

### Page layout

```
┌ reimagina   Map · Scenarios · Catalog · Runs        Pilot [Mexicali ▾] [+ New] ┐
│ Scenario        Dwellings  People   Density   Floors  Livability   Status       │
│   Baseline city     —         —        —        —        —                      │
│ ▸ Upzone A      12,400    44,600   167 /ha    6.0     0.52       ✓ up to date   │
│   Corridor       3,100    11,200   125 /ha    4.0     0.48       ⚠ recompute    │
├───────────────────────────┬─────────────────────────────────────────────────────┤
│ Upzone A                  │ Indicator [Housing density ▾]  [Values|Difference]  │
│ Storeys / m² / people     │                                                     │
│ [Apply to drawn area]     │  map · draw tools ● ▭ ⬠ × · legend                  │
│ Area 1 (12 storeys…)  ×   │                                                     │
│ [Recompute] [Delete]      │                                                     │
│ comparison headline       │  Download CSV · GeoJSON · GeoParquet                │
└───────────────────────────┴─────────────────────────────────────────────────────┘
```

Below 1024 px the authoring panel stacks under the map (*my call*).

- **Table.** Rows: Baseline, then the pilot's scenarios by name. Cells from
  `getIndicatorStats(slug, {aoi_id, scenario_id})`, `sum` or `p50` per the
  column's `statistic`, formatted with `format.ts`. A cell with `count == 0`
  shows "—". Column headers carry the unit and a tooltip naming the statistic.
  `# ponytail:` one request per cell (scenarios × 5), run concurrently with a
  small cap — a batch endpoint when a pilot carries dozens of scenarios.
- **Status**, from the row's timestamps plus the latest run: *No rules yet*
  (`edit_count == 0`), *Not computed* (`materialized_at` null), *Rules changed
  — recompute* (`updated_at > materialized_at`), *Queued* / *Computing* /
  *Failed* (the run the page is following, else the row's `latest_run`),
  else *Up to date*.
- **Selection** lives in the URL hash (`#/<scenario_id>/<indicator_slug>`), as
  the Runs page keeps its open run, so a reload or a shared link keeps the
  place (*my call*).
- **New scenario** from the top bar: a name, created on the active pilot,
  selected on creation.
- **Map** shows the AOI boundary and block outlines for orientation, the
  selected indicator under the selected scenario via `activateIndicator`, the
  legend, the draw toolbar, navigation, scale and the API-derived attribution.
  No other spatial layers (*my call*: focus is the point of the page).
- **Values / Difference** as today (`renderModeToggle` semantics; Difference
  uses `getComparison` → `deltaDomainFromComparison`), with the comparison
  headline (`comparisonHeadline`) under the authoring panel.
- **Downloads** for the selected indicator under the selected scenario, as the
  map page offers today (`download.ts`).
- **After a recompute lands**, the table row and the map refresh.

### State and tests

- A runes store (`src/scenarios/store.svelte.ts`, after `ops/store.svelte.ts`)
  owns the selected scenario, the followed run and the table cells.
- **The run-following logic moves out of the DOM** into a function the store
  calls and vitest drives with fake timers, so each behaviour that
  `scenario-panel.test.ts` pins today (7 tests: follow only the active
  scenario's run, keep following, survive one bad poll, hand over after
  repeated failures, failed run reported as failed, stale comparison cleared,
  DELETE goes to the rule's scenario) has an equivalent test before the old
  panel is deleted.
- Python: a unit test that the reachable set is exactly the five above in
  dependency order (livability after density), a contract test that
  `/scenarios/indicators` is not captured by `/{scenario_id}`, an integration
  test that `sum` under a scenario equals the sum of the effective values.

### Map page cleanup

Remove from `index.html` the `scenario` section and `#mode-toggle`; from
`main.ts` the `enableScenarioPanel` wiring, `activeScenarioId`, `activeMode`
and every `scenario_id` it passes (indicator list, tiles, ramp stats,
downloads); from `scenario.ts` the DOM half (`enableScenarioPanel`,
`renderScenarioOptions`, `renderEditGroups`, `PANEL_HTML`) and its CSS in
`style.css`; delete `scenario-panel.test.ts` once its behaviours are pinned in
the new store tests. Keep every pure export the new page uses, `tileUrlFor`
included. Remove each stale mention of what was removed.

### Build wiring

`scenarios.html` entry in `vite.config.ts` `rollupOptions.input` (and its
header comment, which still says "two entries"), `tsconfig.scenarios.json`,
`check:scenarios` in `package.json` `build`, `src/scenarios/main.ts` mounting
`App.svelte` like `ops/main.ts`, and `"scenarios"` in `TopBar`'s `current`
union and links.

### Documents

- Repo: `docs/architecture/SCENARIOS_LAYER.md` (the three authoring surfaces;
  the map paragraph moves to the workspace), `docs/architecture/API.md` (new
  route, `sum`, `edit_count`, `latest_run`), `apps/web/README.md`.
- Delivery, **all in this PR** (decided by the user, as #94 and #101 did):
  - `04_technical_documentation.md`: the page list (line ~54), and §10's
    clean-install record **re-run and re-measured** — the sequence executed
    again from a clean install, its test counts updated, `/scenarios.html`
    added to the `make dev-web` page check. Nothing in §10 is edited without
    the run that measured it.
  - `03_database_and_api_reference.md` regenerated with
    `scripts/build_schema_reference.py`.
  - PDFs rendered with `scripts/render_delivery_pdfs.py`, then MANIFEST and
    SHA256SUMS regenerated with `scripts/delivery_manifest.py`, so
    `test_the_delivery_manifest_matches_the_artefacts_it_names` stays green.
  - `01_architecture_map.md` unchanged (*my call*): its T3 line — authored
    "through the API, the CLI and the map" — stays true of a workspace built
    around a map, and its revision rows are history.
  - Tagging and sending the delivery stay a separate step the user authorises.

## Left to the builder

- Component split inside `src/scenarios/`, Tailwind classes, icons, the
  concurrency cap on table requests.
- How the block-outline layer is installed (reuse `layers.ts` if it can add
  one layer alone; otherwise a minimal layer from the same tile source).
- Exact wording of statuses, tooltips and empty states, in the plain style of
  the current panel's messages.

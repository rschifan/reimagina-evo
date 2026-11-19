# Scenario editing on the map — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the scenarios page's panel-driven rule flow with editing on the map (draw → box → Apply recomputes → Change view; click a rule to edit, drag, resize or delete it in place), add lot coverage to the housing formula, and prove the whole flow in a real browser.

**Architecture:** Lot coverage is folded into the edificability read as a CUS multiplier (`num_levels × lot_coverage`), so kernels and the Hamilton dataflow keep their shapes. A new `PATCH /scenarios/{id}/rules` updates a rule's edits in place (ordinals kept). The web store gains one-call actions (apply / save / delete, each ending in a followed recompute, with compensating undo on refusal); `ScenarioMap` draws rules from a GeoJSON source and hosts a `RuleBox` popover; terra-draw stays the only drawing/editing engine.

**Tech Stack:** Python 3.13, FastAPI, psycopg2, PostGIS; Svelte 5, MapLibre GL, terra-draw, vitest; Playwright (node, `--no-save`) for the browser suite.

**Spec:** `docs/superpowers/specs/2026-11-19-scenario-map-editing-design.md`

## Global Constraints

- Commits: `git -c user.name=Rossano -c user.email=rossano.schifanella@gmail.com commit`; no AI/Claude attribution anywhere. Stage paths explicitly.
- Work only in this worktree and only on reimagina; never touch another project's processes, databases or files. Never write test data into `reimagina_demo`.
- Lot coverage: share `0 < c ≤ 1`; UI default **0.6**; API/CLI default **1.0**; a block with no `lot_coverage` uses **1.0**; baseline totals unchanged (Mexicali 340,169 homes).
- First-rule defaults: 6 storeys, 60 m², 3.6 people per home, 60 %. Later boxes prefill the last values used on the page.
- Scenario tiles stay `Cache-Control: no-cache` (`c9a7cd9`).
- Delivery docs: front-matter date stays 13 Nov; changes go in revision rows; regenerate PDFs + `scripts/delivery_manifest.py` after edits.
- Browser-suite Playwright is installed `npm install --no-save playwright` in `apps/web` (no lockfile change: the licence inventory is derived from the installed tree).
- CI gates: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy packages/py/reimagina apps scripts`, `uv run pytest tests/unit tests/contract`, integration modules touched (`TEST_DATABASE_URL` naming a DB containing "test"), `cd apps/web && npm run build && npm test`.

## Review Focus

1. A rule drawn where no block's centre falls (outside the city) → the planner is told "No blocks fall inside this area" and no rule is kept (Task 4 test `a rule that catches no block is undone and said so`).
2. Switching scenario or pilot while a box is open or a recompute runs → the box closes and nothing is written to or reported on the other scenario (Task 4 test `switching scenario mid-apply writes nothing to the new one`).
3. A box opened for a shape near the map's edge → stays fully inside the map (Task 6 test `anchorBox keeps the box inside the map`).
4. Double-clicking Apply → exactly one rule (Task 4 test `a second apply while busy is ignored`).
5. Coverage typed as a percentage (60 → 0.6; 0 and 120 refused with the field named) (Task 6 test `coverage is entered in percent`).

---

### Task 1: Lot coverage in the model

**Files:**
- Modify: `packages/py/reimagina/indicators/catalog.py` (`_EDIFICABILITY_READS`, descriptions of the three edificability specs)
- Modify: `packages/py/reimagina/indicators/edificability.py` (`_ATTRIBUTES`, `_edificability_rows`, module docstring formula)
- Modify: `packages/py/reimagina/scenarios/ceilings.py` (`BlockInputs`, `check_ceilings`)
- Modify: `packages/py/reimagina/scenarios/authoring.py` (`_refuse_ceiling_breaches`)
- Modify: `packages/py/reimagina/scenarios/proposal.py` (`edits_for`, `require_positive` caller contract, `precheck_ceilings`, new coverage check)
- Modify: `apps/cli/scenario.py` (pass `--lot-coverage`, default 1.0)
- Test: `tests/unit/test_lot_coverage.py`, `tests/integration/test_lot_coverage_compute.py`

**Interfaces:**
- Produces: `edits_for(levels: float, unit_size: float, people_per_household: float, lot_coverage: float = 1.0) -> list[dict]` (five edits, `lot_coverage` last); `require_coverage(lot_coverage: float) -> None` raising `ProposalValueError` with field `lot_coverage` unless `0 < c ≤ 1` and finite; `precheck_ceilings(levels, unit_size, people_per_household, lot_coverage: float = 1.0)`; `BlockInputs.lot_coverage: float = 1.0`.

- [ ] **Step 1: Write the failing unit tests** in `tests/unit/test_lot_coverage.py`:
  - `test_edits_for_writes_coverage_last`: `[e["attribute_key"] for e in edits_for(3, 66, 3.6, 0.6)] == ["floors","num_levels","unit_size","people_per_household","lot_coverage"]`, last value `0.6`; `edits_for(3,66,3.6)[-1]["value_numeric"] == 1.0`.
  - `test_coverage_must_be_a_share`: `require_coverage(x)` raises with `.field == "lot_coverage"` for `0, -0.1, 1.2, nan, inf`; passes for `0.01, 0.6, 1.0`.
  - `test_ceilings_scale_by_coverage`: one `BlockInputs(levels=10, unit_size=60, people_per_household=3, area_m2=10_000, lot_coverage=0.5)` → no density breach at bound 2000 although `10×10000/60 = 1666` would pass anyway; use `levels=20, unit_size=60` (3333 > 2000 at c=1 → breach; c=0.5 → 1666 → none).
  - `test_precheck_uses_coverage`: `precheck_ceilings(20, 60, 3)` raises `CeilingPrecheckFailed`; `precheck_ceilings(20, 60, 3, 0.5)` does not.
- [ ] **Step 2:** `uv run pytest tests/unit/test_lot_coverage.py -q` → FAIL (signature/attribute errors).
- [ ] **Step 3: Implement.** `check_ceilings`: density and units use `b.levels * b.lot_coverage` (floors bound unchanged). `edits_for` appends `{"attribute_key": "lot_coverage", "value_numeric": float(lot_coverage)}`. `require_coverage` beside `require_positive`. `precheck_ceilings` forwards coverage into `BlockInputs`.
- [ ] **Step 4:** unit tests PASS.
- [ ] **Step 5: Write the failing integration test** `tests/integration/test_lot_coverage_compute.py` (pattern: existing edificability integration tests' fixtures for a migrated DB with two blocks carrying `num_levels`, `unit_size`, `people_per_household`):
  - `test_absent_coverage_is_one`: housing units of a block without `lot_coverage` equal `area × levels / unit_size` (unchanged).
  - `test_rule_coverage_scales_units`: scenario rule with coverage 0.6 over block A → A's scenario housing units `== pytest.approx(0.6 × baseline units)`; density likewise ×0.6; population ×0.6.
  - `test_materialisation_ceiling_counts_coverage`: a rule whose units breach at c=1 but not at c=0.1 materialises without `CeilingExceeded`.
- [ ] **Step 6:** run it → FAIL.
- [ ] **Step 7: Implement.** Add `"lot_coverage"` to `_EDIFICABILITY_READS` and `_ATTRIBUTES` (this makes it authorable). In `_edificability_rows` read it like the others but **optional**: `coverage = attributes["lot_coverage"].get(fid, 1.0)`, and emit `levels * coverage` in the row's levels slot (comment: CUS = COS × levels; kernels unchanged). `_refuse_ceiling_breaches`: add `lot_coverage` to `checked`, default 1.0 instead of 0.0 for that key, pass into `BlockInputs`. Update the three catalog descriptions and the module docstring to `area × lot_coverage × num_levels / unit_size` and "blocks without lot_coverage use 1.0". CLI: `--lot-coverage` (default 1.0) through `require_coverage` and `edits_for`.
- [ ] **Step 8:** integration + unit + contract PASS (`uv run pytest tests/unit tests/contract -q`; `TEST_DATABASE_URL=postgresql://localhost/reimagina_test uv run pytest tests/integration/test_lot_coverage_compute.py tests/integration -k "edificab or scenario or ceiling" -q`). If a contract test snapshots indicator `reads`/the asset graph, update the snapshot and say so in the commit.
- [ ] **Step 9: Commit** `feat(scenarios): lot coverage share in the housing formula`.

### Task 2: API — coverage on POST, rule update in place

**Files:**
- Modify: `apps/api/routers/scenarios.py` (`ProposalBody.lot_coverage: float = 1.0`; `add_edits_route`; new `UpdateRuleBody`, `PATCH /{scenario_id}/rules`)
- Modify: `packages/py/reimagina/scenarios/authoring.py` (new `update_rule`)
- Test: `tests/integration/test_scenario_rule_update.py`; extend the existing edits-route test module with one coverage case

**Interfaces:**
- Produces: `update_rule(conn, *, scenario_id: str, edit_ids: list[str], geometry_wkt: str | None, values: dict[str, float], type_id: str) -> list[str]` (ids of the rule's edits after the update, including an inserted `lot_coverage` edit); route `PATCH /scenarios/{scenario_id}/rules` body `{edit_ids, area?, levels, unit_size, people_per_household, lot_coverage}` → `200 EditsWritten`.

- [ ] **Step 1: Failing tests** (real DB, existing scenario-route fixtures):
  - `test_values_update_in_place`: POST a rule, PATCH with new numbers → same edit ids, same ordinals, new `value_numeric`s.
  - `test_area_moves_every_edit`: PATCH with `area` → every edit's `geometry_wkt` equals the new polygon; ordinals unchanged.
  - `test_foreign_or_mixed_ids_are_422`: ids from another scenario, or ids of two different rules → 422, nothing changed.
  - `test_pre_coverage_rule_gets_a_coverage_edit`: a rule written with four edits (insert directly via `add_edit`) → PATCH returns five ids, the new one is `lot_coverage`.
  - `test_refusals_match_post`: `levels=0` → 400 naming `levels`; `lot_coverage=1.5` → 400 naming `lot_coverage`; ceiling breach → same status/body shape as POST.
  - POST module: `test_post_writes_coverage` → five edits, coverage 1.0 when omitted, 0.6 when sent.
- [ ] **Step 2:** run → FAIL (404/405 on PATCH).
- [ ] **Step 3: Implement** `update_rule` under the scenario row lock (`_lock_scenario`): verify all ids belong to `scenario_id` and share one `geometry`; `UPDATE … SET value_numeric` per attribute key; when `geometry_wkt` is given, `UPDATE … SET geometry = <same ST_MakeValid/polygonal reduction add_edit uses>` (reuse its SQL, do not duplicate the repair logic — extract a helper if needed); insert `lot_coverage` via `add_edit` when absent. Route: same refusal order as POST (id → values incl. `require_coverage` → ceiling precheck → geometry), one transaction.
- [ ] **Step 4:** PASS. `uv run python scripts/build_schema_reference.py` to regenerate 03; confirm the new route row appears.
- [ ] **Step 5: Commit** `feat(api): update a scenario rule in place; coverage on proposals`.

### Task 3: Web model — types, parsing, labels

**Files:**
- Modify: `apps/web/src/api.ts` (`ProposalBody.lot_coverage`, `updateRule()`)
- Modify: `apps/web/src/scenario.ts` (`ProposalValues.lotCoverage`, `validateEdit`, `maxLevelsFor`, `EditGroup.lotCoverage`, `groupEditsByArea` completeness, `describeGroup`)
- Create: `apps/web/src/scenarios/rule-geometry.ts` (`wktToGeometry`, `inferShape`, `rulesToFeatureCollection`)
- Test: `apps/web/src/scenario.test.ts`, `apps/web/src/scenarios/rule-geometry.test.ts`

**Interfaces:**
- Produces: `updateRule(scenarioId: string, body: { edit_ids: string[]; area?: Feature<Polygon>; levels: number; unit_size: number; people_per_household: number; lot_coverage: number }): Promise<EditsWritten>`; `wktToGeometry(wkt: string): Polygon | MultiPolygon` (throws on anything else); `inferShape(geometry: Polygon | MultiPolygon): SelectionShape | null` (null = MultiPolygon, not draggable); `rulesToFeatureCollection(groups: EditGroup[]): FeatureCollection` with properties `{index: number /*1-based*/, label: string, editIds: string /*comma-joined*/}`; `EditGroup.lotCoverage: number | null`; `describeGroup(group, index)` → `"Rule 2 · 3 st · 66 m² · 60 %"` (coverage omitted when null).

- [ ] **Step 1: Failing tests:** `wktToGeometry("POLYGON((0 0,1 0,1 1,0 0))")` → Polygon ring; `MULTIPOLYGON(((…)),((…)))` → 2 polygons; `"POINT(0 0)"` throws. `inferShape`: axis-aligned 5-vertex ring → `"rectangle"`; a 64-vertex circle ring → `"circle"`; a pentagon → `"polygon"`; MultiPolygon → `null`. `validateEdit({… lotCoverage: 1.2})` → message containing `lot_coverage`; `maxLevelsFor(60, 2000, 0.5)` → `Math.floor(2000*60/10000/0.5)` = 24. `groupEditsByArea` on four edits (no coverage) → `complete: true, lotCoverage: null`; on five → `lotCoverage: 0.6`. `describeGroup` strings above. `rulesToFeatureCollection` → later rules after earlier (ascending `ordinal`), `index` 1-based.
- [ ] **Step 2:** `cd apps/web && npx vitest run src/scenario.test.ts src/scenarios/rule-geometry.test.ts` → FAIL.
- [ ] **Step 3: Implement.** WKT parser: a small hand-written parser for `POLYGON`/`MULTIPOLYGON` only (server always emits these). Circle test: ≥ 32 vertices, every vertex's distance to the ring centroid within 2 % of the mean.
- [ ] **Step 4:** PASS; `npm test` green.
- [ ] **Step 5: Commit** `feat(web): rule geometry, coverage and labels in the scenario model`.

### Task 4: Store — one-call apply, save, delete

**Files:**
- Modify: `apps/web/src/scenarios/store.svelte.ts`
- Test: `apps/web/src/scenarios/store.test.ts`

**Interfaces:**
- Consumes: Task 3 (`updateRule`, `EditGroup`, `validateEdit`).
- Produces (exported): `RuleValues = { levels: number; unitSize: number; peoplePerHousehold: number; lotCoverage: number }`; `ws.busy: boolean`; `ws.lastValues: RuleValues` (initial 6/60/3.6/0.6); `ws.boxError: { field: string | null; text: string } | null`; `applyNewRule(area: Feature<Polygon>, values: RuleValues, scenarioName?: string): Promise<boolean>`; `saveRule(group: EditGroup, area: Feature<Polygon> | null, values: RuleValues): Promise<boolean>`; `deleteRule(group: EditGroup): Promise<boolean>`; `headline(scenarioId: string | null): { homes: { base: number|null; now: number|null }; people: { base: number|null; now: number|null } }` (from `ws.cells`, units/population slugs). Removes `applyRule`, `recompute`, `compare` exports once Task 6 no longer uses them (keep `removeScenario`).

- [ ] **Step 1: Failing tests** (mocked `../api` as the file already does):
  - `apply on the baseline creates the scenario, then the rule, then recomputes`: call order `createScenario → addScenarioEdits → recomputeScenario`; on landed run `ws.mode === "delta"`, `ws.selectedId` = created id, returns true.
  - `a refused recompute undoes the new rule` (recomputeScenario rejects 422 ceiling) → `removeScenarioEdit` called for every written id; `ws.boxError.text` names the ceiling; returns false.
  - `a refused recompute after creating the scenario deletes the scenario`.
  - `a rule that catches no block is undone and said so`: recompute result `matched_per_edit` all 0 → edits removed, `boxError.text` contains "No blocks fall inside this area".
  - `save keeps the rule's edit ids` (`updateRule` called with `group.editIds`, then recompute); `a refused save writes the old values back` (second `updateRule` with the group's previous numbers and previous area).
  - `delete removes every edit then recomputes`.
  - `a second apply while busy is ignored` (two concurrent calls → one `addScenarioEdits`).
  - `switching scenario mid-apply writes nothing to the new one` (select B while A's add is pending → recompute only ever called with A; no `ws.message` about A while B selected).
  - `values are remembered`: after a successful apply `ws.lastValues` equals the applied values.
- [ ] **Step 2:** `npx vitest run src/scenarios/store.test.ts` → FAIL.
- [ ] **Step 3: Implement** around one private `recomputeAndWait(scenarioId): Promise<"landed" | "failed" | "refused" | "empty">` built from the existing `recomputeScenario` + `followRun` follower (resolve on `landed`/`failed`/`unreadable`/`handedOver`). `busy` set for the whole action. Refusal text via the existing `refusalText`. On landed: `loadScenarios`, `refreshRow`, `loadRules`, `refreshMap`, `ws.mode = "delta"`.
- [ ] **Step 4:** PASS; full `npm test` green.
- [ ] **Step 5: Commit** `feat(web): apply, save and delete a rule in one call`.

### Task 5: Selection — edit an existing shape

**Files:**
- Modify: `apps/web/src/selection.ts` (`SelectionController.edit`)
- Modify: `apps/web/src/scenarios/ScenarioMap.svelte` (install `installSelectionHandle` so circles resize, as `main.ts` does)

**Interfaces:**
- Produces: `SelectionController.edit(feature: Feature<Polygon>, shape: SelectionShape): void` — clears, adds the feature to terra-draw in `shape`'s mode, switches to select mode and selects it, and publishes it through `subscribe` exactly as a finished drawing would (same `SelectionGeometry`), so drags/resizes arrive as `change` events.

- [ ] **Step 1:** No unit harness exists for terra-draw on a map (it needs WebGL); this task is covered by Task 7 steps 2b and 6. Write the method and the handle install.
- [ ] **Step 2:** `npm run build` (tsc) and `npm test` green.
- [ ] **Step 3: Commit** `feat(web): load a saved rule into the selection for editing`.

### Task 6: The map editor — rules layer, rule box, top bar

**Files:**
- Create: `apps/web/src/scenarios/RuleBox.svelte`, `apps/web/src/scenarios/box-anchor.ts`
- Create: `apps/web/src/scenarios/ScenarioBar.svelte` (top bar: picker, New, headline, run state, delete scenario)
- Modify: `apps/web/src/scenarios/ScenarioMap.svelte` (rules source/layers, click-to-edit, box hosting, Values domain from baseline, view labels "Change"/"Values", default Change when the scenario has rules)
- Modify: `apps/web/src/scenarios/App.svelte` (drop `RulePanel`, add `ScenarioBar`; keep `ScenarioTable` below the map)
- Delete: `apps/web/src/scenarios/RulePanel.svelte`, `apps/web/src/scenarios/rule-panel.test.ts`
- Test: `apps/web/src/scenarios/rule-box.test.ts`, `apps/web/src/scenarios/box-anchor.test.ts`

**Interfaces:**
- Consumes: Tasks 3–5.
- Produces: `RuleBox` props `{ mode: "new" | "rule"; values: RuleValues; needsScenarioName: boolean; overriddenBy: number | null; busy: boolean; error: { field: string | null; text: string } | null; draggable: boolean; onapply(values, name?): void; onsave(values): void; ondelete(): void; oncancel(): void }`; `anchorBox(shapePx: {minX,minY,maxX,maxY}, map: {width,height}, box: {width,height}, margin = 8): {left: number; top: number}` — right of the shape if it fits, else left, else clamped inside.

- [ ] **Step 1: Failing tests.** `box-anchor.test.ts`: `anchorBox keeps the box inside the map` for shapes at each edge/corner and a shape larger than the map; prefers the right side. `rule-box.test.ts` (jsdom mount like the old rule-panel test): `prefills values and shows coverage in percent` (0.6 → "60"); `coverage is entered in percent` (typing 60 → `onapply` gets `lotCoverage: 0.6`; 0 and 120 → error naming lot coverage, no callback); `asks for a scenario name only on the baseline`; `a rule box offers Save, Delete and Cancel`; `says which rule overrides this one` (`overriddenBy: 3` → "Rule 3 overrides this one where they overlap"); `undraggable rule says why` (`draggable: false` → text about a shape in several pieces); `Escape cancels`; `buttons are disabled while busy`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3: Implement.**
  - Rules layers: GeoJSON source `scenario-rules` from `rulesToFeatureCollection(ws.rules)`; fill (opacity 0.12) + line (2 px) + symbol label; inserted below the lowest `td-*` layer (same rule as `choropleth.ts`), so above the indicator fill. While a rule is being edited, filter it out (`["!=", ["get","index"], n]`).
  - Click on `scenario-rules` fill (not while drawing) → `selection.edit(feature, inferShape(geometry) ?? "polygon")` unless MultiPolygon (then numbers only, `draggable: false`); open box in `"rule"` mode with the group's values.
  - Keys `r` `p` `c` start rectangle / polygon / circle drawing when focus is not in an input; `Esc` cancels drawing or editing.
  - Draw finish → box in `"new"` mode with `ws.lastValues`; `needsScenarioName = ws.selectedId === null`, name prefilled `Scenario ${ws.scenarios.length + 1}`.
  - Box position from `anchorBox` using the shape's bbox projected with `map.project`, recomputed on `move`.
  - Values view: domain from `getIndicatorStats(slug, {feature_type, aoi_id, bins})` **without** `scenario_id`; legend notes clamping above the baseline p95.
  - `ScenarioBar`: picker (select of scenarios + "Baseline city"), "New" inline name → `create`, headline `Homes 340,169 → 372,410 (+32,241) · People +…` from `headline()`, run state from `ws.busy`/`ws.following`, Delete scenario (confirm) in a small menu.
- [ ] **Step 4:** PASS; `npm run build` and `npm test` green.
- [ ] **Step 5: Commit** `feat(web): edit scenario rules on the map`.

### Task 7: Browser suite — the whole flow, on a scratch stack

**Files:**
- Create: `tests/e2e/_scenario_flow.js`, `tests/e2e/test_scenario_flow.py`
- Modify: `tests/e2e/conftest.py` (skip message: `npm install --no-save playwright`; opt-in `REIMAGINA_E2E_WRITES=1` for tests that write)
- Modify: `tests/e2e/README.md` (how to run the flow test on a scratch stack)
- Modify: `apps/web/src/scenarios/ScenarioMap.svelte` (dev-only hook `if (import.meta.env.DEV) window.__scenarioMap = map`)

**Interfaces:**
- Consumes: everything above. The driver reads the map through `window.__scenarioMap.queryRenderedFeatures(point, { layers: [overlay fill id] })` and the API directly.

- [ ] **Step 1: Write the driver and the test** asserting, on a scratch DB (synthetic territory from `scripts/acceptance_run.sh` into a database whose name contains `e2e`; API, worker and vite on free ports, never `:8001/:5173`):
  1. draw rectangle on blocks → box opens → Apply → within 60 s the top bar shows a positive homes change, view is Change, the rendered feature at the rectangle's centre has `state: "changed"`;
  2. click the rule → storeys +2 → Save → rendered value at the centre equals the API comparison's scenario value for that feature (no stale tile);
  2b. click the rule → drag it ≥ 200 px → Save → still one rule (same edit ids), centre of the old place `unchanged`, new place `changed`;
  3. draw an overlapping rule with different storeys → in the overlap the API value matches rule 2's numbers; clicking rule 1 says "Rule 2 overrides this one";
  4. delete both → every rendered feature `unchanged`; comparison `changed == 0`;
  5. select baseline → draw → Apply with a name → a new scenario exists and is selected; clean up by deleting it.
  The test deletes every scenario it created in teardown.
- [ ] **Step 2:** Bring up the scratch stack; run `REIMAGINA_E2E=1 REIMAGINA_E2E_WRITES=1 REIMAGINA_E2E_API=… REIMAGINA_E2E_WEB=… uv run pytest tests/e2e -q -rs` → all pass. Fix any failure at its root (systematic-debugging), not in the test.
- [ ] **Step 3:** Run the existing `tests/e2e/test_web_app_loads.py` on the same stack → pass (or the known street-tiles failure only on `reimagina_demo`, not here).
- [ ] **Step 4: Commit** `test(e2e): drive the scenario editor end to end`.

### Task 8: Docs and delivery

**Files:**
- Modify: `docs/architecture/SCENARIOS_LAYER.md` (lot coverage, PATCH rules, in-place semantics)
- Modify: `docs/delivery/04_technical_documentation.md` (scenario workspace: the map flow; revision row 0.11)
- Modify: `docs/delivery/05_capability_proof.md` (formula with coverage where it appears; revision row)
- Regenerate: `docs/delivery/03_database_and_api_reference.md` (Task 2), PDFs, `MANIFEST.md`, `SHA256SUMS`

- [ ] **Step 1:** Edit the docs (plain language, as the existing sections).
- [ ] **Step 2:** `NODE_PATH=<apps/web node_modules with --no-save playwright> uv run python scripts/render_delivery_pdfs.py` then `uv run python scripts/delivery_manifest.py` and `--check` → clean.
- [ ] **Step 3: Commit** `docs: scenario editing on the map and lot coverage`.

### Task 9: Finish

- [ ] All CI gates (Global Constraints) on the branch tip; e2e on the scratch stack once more.
- [ ] Final whole-branch review (fresh reviewer); fix Critical/Important with a failing test first.
- [ ] Tear down the scratch stack and database; push, PR, merge (user: "ship everything"); update local `main` ref; remove worktree.

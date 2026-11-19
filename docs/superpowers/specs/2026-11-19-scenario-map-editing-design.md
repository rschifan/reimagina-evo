# Scenario editing on the map — design

Status: draft for approval, 2026-11-19. Ships in the 20 Nov delivery.

## Why

The scenarios page (`/scenarios.html`, #108–#113) works, but a planner using it
for the first time could not make a scenario change anything, and when they did,
the map contradicted the table:

- Six separate steps (create, select, draw, Apply, Recompute, switch to
  Difference) before anything visible happens; skip one and the scenario
  silently equals the baseline.
- Rules are invisible on the map. Moving the drawn shape and pressing Apply
  writes a *new* rule; the old one stays. Overlapping rules resolve "later
  wins" with nothing on screen saying so.
- After a recompute the map kept painting the previous run's tiles for up to
  six minutes (`Cache-Control: max-age=60, stale-while-revalidate=300` on
  scenario tiles). Fixed in `c9a7cd9` on this branch.
- In Values view the colour ramp stretches to the scenario's own p95, so one
  dense rule turns every baseline block the same pale colour.
- Housing units assume the building covers the whole block
  (`area × storeys / m²`), so three storeys over ~3,500 blocks gives 2.04 M
  homes against a census of 340 k.

The planner wants to draw an area, say what could be built there, and see the
impact at once — and trust what the map shows.

## Goals

1. One gesture to a visible result: draw → type numbers next to the shape →
   Apply. Apply writes the rule **and** recomputes, and the map switches to
   the difference from the baseline.
2. Rules live on the map: every rule of the selected scenario is drawn,
   labelled, and clickable to change its numbers, **drag or resize it**, or
   delete it — all in place, keeping its position in the "later wins" order.
3. The map always shows the latest run (the tile cache fix).
4. Colours stay comparable: Values view uses the **baseline's** colour domain
   for every scenario.
5. Housing totals stop assuming full block coverage: a rule carries a **lot
   coverage** share (COS), default 60 %.
6. The flow is covered by a browser test that drives it end to end on a real
   stack, plus unit and integration tests for each new piece.

## Non-goals (this delivery)

- Undo, rule reordering, per-rule colours, rule names.
- Any change to the baseline data or to the explore page (`/`).

## Anti-goals

- A map that shows something the table does not (stale tiles, a half-written
  rule, a rule whose recompute failed silently).
- A rule that changes meaning when edited: editing a rule's numbers must not
  move it in the "later wins" order.

## Approaches considered

| | Approach | Trade-off |
|---|---|---|
| **A** | **Edit on the map** — rules are shapes; a box next to the shape holds the numbers; Apply recomputes | Fastest for the planner; biggest change |
| B | Step-by-step side panel (1 draw, 2 numbers, 3 see impact) | Easy to learn; every change goes through the panel |
| C | Keep today's layout, merge Apply + Recompute, outline rules | Cheapest; moving a rule still means redraw, flow still panel-driven |

**Chosen: A** (user, 2026-11-19, after mockups). Ships whole on 20 Nov,
including dragging and resizing a rule (user: "ship everything").

## The design

### Layout

```
┌ Scenario: [Downtown densify ▾] [+ New]   Homes 340,169 → 372,410 (+32,241)   People +116,068   ● Recomputing… ┐
├────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Indicator [Housing density ▾]  [Change | Values]                                     Download CSV · GeoJSON · … │
│ ┌ map ───────────────────────────────────────────────────────────────────────────────────────────────────────┐ │
│ │ [Draw ▭ ⬠ ●]   ┏━━━━━━━━━━┓ Rule 1 · 4 st · 60 m² · 60 %                                                    │ │
│ │                ┃          ┃        ┌ New rule ────────────┐                                                │ │
│ │                ┗━━━━━━━━━━┛        │ Storeys        [3  ] │  ← box opens next to a just-drawn shape         │ │
│ │                                    │ m² per home    [66 ] │                                                │ │
│ │                                    │ People / home  [3.6] │                                                │ │
│ │                                    │ Lot coverage   [60%] │                                                │ │
│ │                                    │ [Apply]  Cancel      │                                                │ │
│ │                                    └──────────────────────┘                                     legend     │ │
│ └────────────────────────────────────────────────────────────────────────────────────────────────────────────┘ │
│ All scenarios (existing comparison table, unchanged)                                                            │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Top bar** replaces the left "Rules" card: scenario picker, "New"
  (inline name field), the selected scenario's headline change against the
  baseline (total homes and total people, from the same stats the table uses),
  and the run state (`Recomputing…`, `Up to date`, or the failure with a link
  to the Runs page). With the baseline selected the bar shows baseline totals
  and "Draw an area to start a scenario".
- **The scenarios table stays** below the map, as the side-by-side comparison.
- **Removed:** the rule form card, the separate Recompute and Compare buttons.
  Recompute happens on every Apply / Save / Delete. Delete scenario moves to
  the picker's menu.

### Drawing a new rule

1. Pick ▭ ⬠ ● (toolbar, or `r` `p` `c`); drag or click to draw (as #113).
2. On finish, a **rule box** opens anchored next to the shape (inside the map
   bounds), prefilled with the **last numbers used on this page** (first time:
   6 storeys, 60 m², 3.6 people, 60 %).
3. If no scenario is selected, the box also asks for a **scenario name**
   (prefilled "Scenario N"); Apply creates the scenario first, then the rule.
4. **Apply** = validate locally (existing `validateEdit`, now with coverage) →
   `POST /scenarios/{id}/edits` → `POST /scenarios/{id}/recompute` → follow the
   run (existing follower) → when it lands: refresh table row, top bar and map,
   and switch the view to **Change**. The box closes on Apply; the drawn
   shape is replaced by the saved rule's outline.
5. **Cancel** / `Esc` discards the shape.
6. Refusals (ceilings, 4xx) show inside the box next to the field they name.
   The precheck catches the area-independent ceilings before anything is
   written. The per-block ceilings (housing units, population) are only known
   at recompute, after the edits exist, so the page **undoes its own write**
   when the recompute is refused: a new rule's edits are deleted, a saved
   rule's previous shape and numbers are written back (the page holds them),
   and a scenario created by that Apply is deleted. If the undo itself fails,
   the box says exactly what is left and offers Delete rule. The map and
   table never keep a rule the recompute refused without saying so.

### Rules on the map

- One GeoJSON source built from the scenario's edits (`GET /scenarios/{id}`,
  grouped as today by `groupEditsByArea`), parsed from `geometry_wkt` on the
  client (polygons only; `wellknown`-style parsing of `POLYGON`/`MULTIPOLYGON`
  is enough — no new dependency if a 20-line parser suffices, otherwise an
  established one).
- Drawn as an outline plus a light fill, **above** the indicator fill and
  below terra-draw's shapes (same stacking rule as #113). Later rules are
  drawn on top of earlier ones, matching "later wins".
- A label at each rule's interior point: `Rule N · 3 st · 66 m² · 60 %`.
- **Click a rule** → the rule becomes **editable on the map**: its shape is
  loaded into terra-draw's select mode (drag to move; corner/edge handles to
  resize a rectangle, vertex handles for a polygon, the rim handle for a
  circle — the explore page's existing select-mode behaviour), and the same
  box opens prefilled with its numbers, with **Save**, **Delete rule** and
  **Cancel**. Save writes the new shape and/or numbers **in place** (new route,
  below) and recomputes; Delete removes all its edits and recomputes; Cancel /
  `Esc` restores the shape. While a rule is being edited its static outline is
  hidden so only the editable copy shows.
- **Which handles a saved rule gets.** The database stores only the polygon,
  not the tool it was drawn with, so the shape kind is **inferred on the
  client** from the stored ring: 5 vertices with axis-aligned edges →
  rectangle; ≥ 32 vertices all within 2 % of one radius from their centroid →
  circle; anything else → polygon (vertex handles). No schema change. A rule
  stored as a **MULTIPOLYGON** (CLI-written, or a self-crossing shape that
  `ST_MakeValid` split) can have its numbers edited and be deleted, but not be
  dragged; the box says why.
- **One edit at a time.** A recompute on the Mexicali demo lands in about 5 s
  (measured 2026-11-19: 444 blocks, five indicators). While one is running,
  Apply / Save / Delete are disabled and the top bar shows `Recomputing…`; the
  map stays pannable. No concurrent runs per scenario from this page. When the clicked
  rule overlaps a later rule, the box says "Rule M overrides this one where
  they overlap".

### Colours and views

- **Change** (was "Difference") is the default view whenever the selected
  scenario has at least one rule; **Values** otherwise.
- **Values** for a scenario uses the **baseline's** ramp domain (the same
  stats call with no `scenario_id`), so a block's colour means the same value
  on every scenario; values above the baseline's p95 clamp to the darkest
  colour, and the legend says so.

### Lot coverage

- New attribute key **`lot_coverage`** (share 0–1). A rule writes it as a
  fifth edit (`edits_for` gains it).
- Formula: `housing_units = area × lot_coverage × num_levels / unit_size`;
  population and density follow from units unchanged (density becomes
  `lot_coverage × num_levels × 10 000 / unit_size`).
- A block with no `lot_coverage` (every baseline block) uses **1.0**, so the
  census-fitted baseline is unchanged to the unit (340,169). `lot_coverage` is
  **optional** in the edificability read — the "skip a feature missing any
  attribute" rule still applies to the other three.
- API: `ProposalBody.lot_coverage` optional, default **1.0** (existing API and
  CLI callers keep today's meaning); the UI sends 0.6 by default. Validation:
  `0 < lot_coverage ≤ 1`.
- The area-independent ceiling precheck (`precheck_ceilings`, client
  `validateEdit` / `maxLevelsFor`) includes coverage.
- `urban_form_block_planned_floors` is unaffected (it reads `floors`).
- Catalog descriptions, `SCENARIOS_LAYER.md`, and the delivery docs (04 §
  scenario workspace, 05 capability proof where the formula appears, 03 API
  reference) state the factor and that the baseline uses 1.0.

### Editing a rule in place — new route

`PATCH /scenarios/{scenario_id}/rules` with body
`{edit_ids: [uuid…], area?: GeoJSON polygon, levels, unit_size,
people_per_household, lot_coverage}`:

- All `edit_ids` must belong to the scenario and share one geometry (one
  rule); otherwise 422.
- Updates `value_numeric` of each existing edit — and, when `area` is given,
  the geometry of every one of them — in place (ordinals unchanged, so the rule
  keeps its place in "later wins"). Same geometry validation as POST
  (`polygon_wkt`). A rule written before lot
  coverage existed has no `lot_coverage` edit: the route inserts one with the
  same geometry (it takes the next ordinal; acceptable — coverage only competes
  with another rule's coverage).
- Same refusals as `POST …/edits` (positive values, ceilings, coverage range).
- Does **not** recompute; the client calls recompute after, as for POST.
- Mirrored in the typed client (`api.ts`) and documented in 03.

### The tile cache (done)

`c9a7cd9`: any tile request with `scenario_id` is `Cache-Control: no-cache`
(revalidated each time, ETag keeps unchanged tiles at 304). Baseline tiles keep
their lifetime.

## Testing (the bar for "it works")

- **Python:** kernel with coverage (incl. absent → 1.0); `edits_for` five
  edits; precheck with coverage; PATCH route (in-place values, ordinals
  unchanged, cross-scenario / mixed-geometry ids → 422, missing coverage
  inserted; `area` moves every edit of the rule, ordinals unchanged);
  materialised scenario with coverage 0.6 gives 0.6× the units of
  coverage 1.0 on the same blocks (integration, real DB).
- **Web unit (vitest):** store actions (apply on baseline creates scenario
  then rule; apply → recompute → mode Change; save keeps edit ids; delete;
  refusals land in the box); WKT → GeoJSON parser; rule box component
  (prefill, coverage %, validation messages); Values view asks stats without
  `scenario_id` for the domain.
- **Browser (tests/e2e), run against a local stack on a scratch database:**
  1. draw a rectangle → box opens → Apply → run succeeds → the scenario tile at
     the shape's centre has changed values, the top bar shows a positive
     change, the view is Change;
  2. click the rule → change storeys → Save → the tile at the same point
     carries the new value (proves no stale tile);
  2b. click the rule → drag it to another place → Save → blocks at the new
     place changed, blocks at the old place back to baseline, still one rule,
     same position in the order;
  3. draw a second, overlapping rule → its values win in the overlap;
  4. delete both → the scenario tile equals the baseline tile;
  5. start from the baseline (no scenario) → Apply creates the scenario.
  The suite gets its own Playwright dependency (`apps/web` devDependency)
  instead of borrowing another project's `node_modules`.
- Gates: `npm run build`, `npm test`, ruff, ruff format, mypy, unit +
  contract, the touched integration modules, the new e2e module.

## Left to the builder

- Component split (one `RuleBox.svelte` used for new and existing rules is the
  expectation), label placement, exact copy within the existing plain-language
  style, how the box anchors near map edges.
- Whether rule outlines use a MapLibre GeoJSON source or terra-draw features
  (MapLibre source expected: terra-draw stays the drawing tool only).
- Scratch-database recipe for the e2e run (the acceptance synthetic territory
  is the expected base).

## Builder calls (marked for review)

- Default numbers for a first rule: 6 storeys, 60 m², 3.6, 60 % (today's
  defaults plus coverage).
- API default coverage 1.0 (back-compat) vs UI default 0.6.
- A pre-coverage rule's missing coverage edit is inserted at the end of the
  order rather than renumbering ordinals.
- Shape kind inferred from the stored ring (no new column); MULTIPOLYGON rules
  editable by numbers only.
- A refused recompute is undone by the page (compensating writes), not by a
  new server transaction, so `POST …/edits` and `PATCH …/rules` stay
  write-only and recompute stays its own call.
- Editing is locked while a recompute runs (~5 s measured), rather than
  queueing concurrent runs.

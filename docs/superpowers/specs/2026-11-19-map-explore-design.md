# Map page for exploration — design

Date: 2026-11-19 · Status: draft for approval · Prototype: the "option A" working
mockup reviewed in the session that produced this document.

## Intent

The map page (`apps/web/index.html`, `src/main.ts`) is used mainly to **explore**
indicator results: pick an indicator, see where values are high and low, look at
one block, draw an area and ask "what is it like here compared with the whole
AOI?". Today the page is built around layer management: the indicator and its
legend sit below the fold of a 1,349 px sidebar, the opening view shows no
colours, and the selection panel counts geometries instead of describing the
indicator.

Success: a person opening the map sees the active indicator coloured over the
city straight away, can read any block's value and how it ranks without
scrolling, and can draw an area and get a stable summary of the indicator inside
it.

## Decisions taken with the user

- Audience: exploration (not operators, unlike the catalog).
- Layout: option A, "map first" (see Approaches).
- Spatial layers are **off** by default.
- The "Derived overlays (no parent raster)" group label goes.
- The selection box must stop flickering while drawing.
- **Both pages:** the shared behaviour changes below also apply to the scenario
  workspace (`scenarios.html`, `ScenarioMap.svelte`), which imports the same
  `choropleth.ts`, `layers.ts`, `map-base.ts`, tooltip and selection code. See
  "Scenario workspace".
- The tile 500s under load are fixed already by #111 (pool waits for a free
  connection; 32 concurrent tile requests now return 0 errors, 102 before).

## Goals

1. The opening view shows the active indicator coloured.
2. The indicator, its description, legend and a block's details are readable
   without scrolling at 1440×900, and without leaving the map.
3. Values at a declared cap (e.g. 1,500 m = unreachable) are visibly distinct
   and named, everywhere a value is shown.
4. Ranks are correct when many blocks tie (today a cap block reads "higher than
   69%" while 62% of blocks share its value).
5. Drawing a selection gives a summary of the active indicator inside it that
   updates without flicker.
6. Downloads hold the rows the page describes: active AOI, drawn grain.

## Non-goals

- Comparing two indicators side by side, bivariate maps, swipe.
- A basemap (licence hygiene decision stands).
- Scenario overlays on this page (the scenario workspace owns them).
- Server-side clipping of tiles to the selection.

## Anti-goals

- No number on screen that disagrees with another number on screen (the legend
  median vs the hover rank today).
- No raw identifiers as copy (`CVE_ENT=02`, `units_per_ha`, `p05–p95`,
  `ramp 425.17–1500.00`).
- No control that resets to a placeholder while the user is still drawing.

## Approaches considered

- **A. Map first (chosen).** Slim explore panel; legend, block inspector and
  selection summary float on the map. Everything an explorer reads sits next to
  what it explains. Costs a layout rework of `index.html` / `style.css` and two
  small API additions.
- **B. Reorder the sidebar.** Indicator first, sticky legend, layers collapsed.
  Smaller change, but the eye still travels between panel and map, and no
  inspector.
- **C. Compare mode.** Two indicators side by side. Larger feature; deferred
  until the basic exploration loop works.

## Design

### Layout (desktop ≥ 768 px)

Left panel, 200–240 px, top to bottom:

1. **AOI** picker (compact `<select>`), name only. The AOI's raw description
   (`AOI derived from MEX:municipality CVE_ENT=…`) is no longer shown.
2. **Indicator** picker, full name visible (wraps, no truncation), then the
   catalog description (from `IndicatorSummary.description`, clamped to 4 lines
   with the full text on hover) and an "About this indicator" link to
   `/catalog.html#/i/<slug>`.
3. **Grain** toggle only when the indicator has more than one grain.
4. **Layers**, a collapsed group showing "N on". Inside: Streets, Buildings,
   Blocks, Municipalities, then the observation overlays. Overlays that belong
   to a raster stay grouped under the raster's name; overlays without a parent
   raster are listed directly, with no group heading.
5. **Downloads** (CSV · GeoJSON · Parquet as compact chips) and a line
   "19,058 blocks · Mexicali only".

On the map:

- **Draw tools** top-left (unchanged behaviour, Tabler-style icons).
- **Inspector** top-right, only when a block is pinned.
- **Legend** bottom-left, always while an indicator is active.
- **Selection summary** bottom-right, only while a selection exists.
- **Status/errors** as a small banner top-centre on the map, shown only for
  loading and errors, never below the fold.
- Navigation and attribution controls stay; the duplicated attribution button
  is removed.

### Opening view

- The indicator overlay's `minzoom` follows its grain's baseline layer
  (blocks: 8), the same rule `layers.ts` already applies to aggregation
  overlays. Measured: an overlay tile over the Mexicali core at z8 is ~1 s and
  346 KB, well inside the 5 s tile timeout. Below z8 the tile function returns
  nothing.
- `flyToAoi` computes the camera for the AOI's features bbox and clamps the
  zoom to at least 8, so a wide AOI (or a phone screen) still lands where the
  overlay draws. The `f` / "Fit map to the active AOI" command uses the same
  clamp.

### Colour, cap and legend

- The fill ramp keeps the observed p05–p95 domain but drops the darkest stop,
  so the high end stays visible on the dark map background.
- **What counts as a cap.** `valid_min` / `valid_max` are validation ranges
  (every indicator declares `valid_min=0`; scores declare `valid_max=1`) and
  must not trigger cap treatment. A cap is declared explicitly: a new nullable
  catalog field `cap_value`, set only where a value at the bound means "beyond
  measurement" — today the four `accessibility_block_*_network_m` indicators
  (`ACCESSIBILITY_CUTOFF_M` = 1500, "also what an unreachable block reports").
  It is a catalog column like the others (migration + seed from
  `indicators/catalog.py`) and is exposed on `IndicatorSummary`.
  *(Builder-check ruling: a data-driven guess would paint zero-density blocks
  as capped.)*
- When `cap_value` is set, features at it get a separate **cap colour**: a hue
  the scenario delta palette does not use (not its teal "better", red "worse"
  or amber `DELTA_NEW` "new in this scenario"), contrasting with every ramp
  stop and the map background — checked with the dataviz validator, and the legend shows a cap swatch labelled "1,500 m+ (cap)" plus
  "62% of blocks at the 1,500 m cap". The share comes from a new `count_at_cap`
  in the stats response (exact, not from histogram bins; `null` without a cap).
- The legend shows the hovered value as a marker on the ramp.
- Units use one shared formatter (the catalog's `unitLabel`, moved to a shared
  module): `units / ha`, `ratio 0–1`, `m`.

### Hover, rank and inspector

- **Hover** tooltip: value (cap values as "1,500 m+ (cap)") and "Click to pin".
  No rank on hover: the client only has 24 histogram bins, and a bin-level rank
  next to an exact one is two numbers that disagree. The legend marker shows
  where the value sits. The existing tooltip controller stays; only its text
  changes. The legend's "higher than N%" readout is removed for the same reason.
- **Rank** appears only in the inspector and is exact, computed by the server
  over the AOI's values: values at the cap read "At the cap, with 62% of
  blocks"; other values read "Farther than 31% of blocks" (distances, unit `m`)
  or "Higher than 31% of blocks" (everything else), counting strictly-lower
  values. *(Builder-check ruling.)*
- **Click** pins an **inspector card**: block id (copyable), the active
  indicator's value and rank, and the block's other current indicator values
  in this AOI. Clicking one of those switches the map to it. The card closes on
  ✕, `Esc`, clicking empty map, or an AOI change. The pinned block keeps an
  outline. The other values come from a new endpoint (below), one request per
  click.

### Selection summary

- Replaces the geometry counts. Shows "Selection · N blocks", the active
  indicator's median, the share at the cap (or the p05–p95 range when there is
  no cap), and the whole-AOI median for comparison. With no indicator active it
  shows the block count only.
- One request per settled selection change (existing 150 ms debounce), not one
  per layer. While a new request is in flight the previous numbers stay on
  screen, dimmed; they are replaced in one paint when the response arrives.
  Numbers use tabular figures so widths do not jump. This removes the flicker.
- The value-clipping module (`selection-filter.ts`) is **deleted**: MapLibre
  rejects `feature-state` in layer filters, so it has never worked (every draw
  logs "feature-state data expressions are not supported with filters" into the
  status box), and the spotlight mask already dims outside the selection, which
  is the behaviour users have actually seen.

### Downloads

`downloadUrl` sends `aoi_id`, `feature_type` (the drawn grain) and
`limit=500000`, as the catalog already does. The row count shown is the stats
`count`.

### Scenario workspace

Shared, so it changes on both pages: layers off by default, the overlay
`minzoom` and zoom-8 floor, the ramp without its darkest stop, the cap colour
and cap legend (value mode only — delta mode keeps its palette, and a capped
block in delta mode keeps the delta colour), unit wording, the hover tooltip
text, and the deletion of `selection-filter.ts`. The tooltip's "Click to pin"
line appears only where pinning exists (the map page). The workspace keeps its
own layout: no inspector, no floating selection summary, no panel rework.

### Phone (< 768 px)

- Panel is the existing drawer, closed by default.
- Legend becomes a one-line strip at the bottom; inspector and selection
  summary open as a bottom sheet, one at a time (the newest wins).
- Tap on a block = pin (there is no hover on touch).

### API additions

1. Catalog field `cap_value` (nullable float) on `indicator_catalog`, seeded
   from `indicators/catalog.py`, returned on `IndicatorSummary`.
2. `GET /indicators/{slug}/stats` gains `count_at_cap`: values `>= cap_value`
   over the same scoped rows; `null` when the indicator has no cap.
3. `GET /features/{feature_id}/indicators?aoi_id=&scenario_id=` returns the
   feature's current value for every indicator that has one, with its exact
   rank in the AOI:
   `[{slug, name, unit, cap_value, value, count_below, count_equal, count}]`
   (`count_*` over the same AOI-scoped, same-grain rows as `/stats`), base city
   when `scenario_id` is omitted. 404 for an unknown feature.
4. `POST /metrics/indicator-within-polygon` with
   `{aoi_id, polygon, indicator_slug, feature_type}` returns the same shape as
   `/stats` (count, percentiles, `count_at_cap`) over features whose centroid the
   polygon covers — the predicate `count_features_within_polygon` already uses —
   so the selection numbers and the block counts agree.

## Left to the builder

- Exact sizes, spacing and icon choices, within the existing `theme.css`
  tokens.
- Whether the inspector's "other indicators" list is sorted by family or name.
- The exact cap colour, within the constraint in "Colour, cap and legend".
- How the banner times out for non-error messages.

## Verification

- Unit tests (vitest) for the pure parts: rank wording, cap expression,
  zoom clamp, unit formatter, selection card state (stale-while-loading).
- Integration tests for the three API changes, including an AOI-scoped and a
  polygon-scoped case.
- Playwright on the scenario workspace too: value mode and delta mode render,
  the delta legend is unchanged, no console errors, layers off by default.
- Playwright on the live app at 1440×900 and 390×844: opening view coloured,
  no console errors, draw a rectangle (no status error, numbers never reset to a
  placeholder during the drag), click a block (inspector with other values),
  layers off by default, downloads carry `aoi_id`.

# Spec: the web application — review, and what to change

A full review of `apps/web`, and the five changes that move it from "a
careful prototype" to something an urban planner would recognise as a
professional tool.

**Status:** spec. Written before the code, from a browser, against a running
stack.

**Scope:** `apps/web` throughout, plus one read-side API addition
(`/indicators/{slug}/stats` gains a histogram). No migration, no schema
change, no change to `curated_geo`, `serving_geo` or `explore`, no new tile
function.

**Anti-scope, stated so it cannot drift:**

- **No basemap ingest.** The map has no street context and that is the
  loudest visual gap, but closing it properly means writing roads as features
  and re-running ingest — a data-layer change, not a web-app one. Reviewed
  below, recommended, and deliberately not done here.
- **No framework migration.** The map page stays vanilla TS; the catalog and
  ops pages stay Svelte. Two idioms is a cost already paid.
- **No redesign of the catalog or ops tables** beyond the overflow defect.
  They are good.
- **No new analytical computation.** The histogram summarises values that
  already exist; it does not derive anything new.

**Honest scale:** roughly 1,200 lines across the web app and about 60 in the
API, plus tests. Two of the five items are defect fixes measured in lines,
not hundreds. The expensive items are the command palette and the
distribution legend, and they are expensive because they are the two that
change what the application *is* rather than how it looks.

**The test for whether this is worth doing properly:** *if a planner cannot
answer "is this block unusual?" without exporting to a spreadsheet, the map
is a picture rather than a tool.* The legend today gives min, max, median and
a colour ramp. That renders a pattern; it does not let anyone place a
specific block inside a distribution. Everything else in this spec is layout
and interaction — real, but recoverable. That one is the difference between
looking and analysing.

---

## What was reviewed, and how

Three pages (`/`, `/catalog.html`, `/ops.html`) at four viewports (1440,
1280, 834, 390), in a real Chromium, against a live API, a worker and the
synthetic Mexicali territory. Screenshots and console output for all twelve
combinations; a production build for bundle sizes; and the source of every
module the pages load.

Console errors across all twelve: **zero**. Uncaught exceptions: **zero**.
This is not a broken application and the review should not read as if it is.

---

## What is already good, and must survive

Stated first because a review that lists only faults will get the faults
fixed and the strengths refactored away.

- **The tile wiring is careful.** AOI scoping is pushed into the tile query
  rather than filtered client-side; `promoteId` is set correctly so feature
  state attaches; minzoom is matched between a baseline layer and its
  choropleth so the two never disagree about what is visible.
- **Selection is a real feature, not a demo.** Draw → spotlight mask →
  server-authoritative metrics, with an abort token per selection.
- **`selection-metrics` already refused the easy wrong answer.** It
  deliberately abandoned `queryRenderedFeatures` because it "silently
  undercounted whenever the renderer hadn't loaded the relevant tiles". That
  decision constrains this spec — see §4.
- **The legend ramp is honest.** `p05–p95` rather than min–max, and it says
  so, so one outlier cannot flatten the whole colour range.
- **The catalog page is close to right already**: facet counts, monospace
  slugs, declared units and ranges, search.
- **The ops page knows about failure modes a dashboard usually hides** —
  there is a `stalled` badge for "every unit finished but the run never
  closed", and an executor-health strip.

---

## Findings

### 1. A failed run renders a full green progress bar — defect

`RunList.svelte` renders `<progress value={p.done} max={p.total}>`, and
`progressOf` sets `done = run.units_terminal`. Terminal, not succeeded. So a
run with one unit that failed renders a **full, green** bar, next to a red
`failed` badge, with "1/1 · 1 failed" beside it.

Observed on 25 of 27 runs in the dashboard: solid green bars, every one a
failure.

The bar is the largest and most saturated element in the row. Where a glance
and a read disagree, the glance wins — and green means success in every
convention a dashboard inherits. This is not a styling preference; the
component's strongest signal contradicts the status it sits next to.

**Change:** a stacked bar — succeeded, failed, still running, remainder — so
the fill encodes outcome rather than completion. One bar, four segments, each
labelled for assistive technology.

### 2. The Clear button is permanently highlighted as the active tool — defect

`_wireSelectionToolbar` marks the active draw mode with `aria-pressed`:

```js
const target = btn.dataset.shape ?? null;
btn.setAttribute("aria-pressed", target === shape ? "true" : "false");
```

The clear button carries `data-action="clear"` and **no** `data-shape`, so
`target` is `null`. At rest `shape` is also `null`, so `null === null` is
true and clear is pressed. `.selection-toolbar__btn[aria-pressed="true"]`
paints it solid cyan.

So the toolbar's resting state shows "clear" as the selected tool — in a
palette whose other three entries are drawing modes. It is both a visual lie
and a false `aria-pressed` for a screen reader.

**Change:** only buttons that name a shape participate in pressed state.

### 3. The map is unusable below ~1000 px, and the catalog clips on a phone

At 390×844 the sidebar takes **82%** of the width and the map is a 140 px
strip. The draw toolbar overlaps MapLibre's zoom control. There is no
breakpoint anywhere in `style.css` for the map layout: the sidebar is a fixed
column, always open, at every size.

At the same width the catalog page scrolls sideways by **320 px** —
measured as `window.scrollX`, not as `scrollWidth`. (The cause turned out
not to be the table; see "What the implementation found".)

Desktop has the inverse problem: the sidebar is **1,173 px of content in an
853 px column**, one unbroken scroll of four stacked panels. A user cannot
see the layer list and the legend at the same time on a laptop, and there is
no way to put the sidebar away to get the map back.

**Change:** a sidebar that collapses on demand at any width and is a drawer
below the breakpoint; tables that scroll horizontally with a visible edge
affordance; a map that is usable at 390 px.

### 4. The legend renders a pattern but cannot place a value in it

The legend gives a ramp, min, max, median, units, and the ramp's percentile
basis. That is more than most prototypes.

What it cannot do is answer the question the map is for: *this block reads
dark — is that unusual, or is most of the territory dark?* A ramp with two
endpoints and a median gives no shape. Two territories with identical
min/median/max and completely different distributions render identically.

**The obvious implementation is the wrong one, and the codebase already says
so.** Computing the distribution from `queryRenderedFeatures` is what
`selection-metrics` abandoned, because it "silently undercounted whenever the
renderer hadn't loaded the relevant tiles (zoomed out, layer toggled off,
slow tile fetches mid-drag)". A histogram built that way would change shape
as tiles load, and the one thing a distribution must not do is move while you
read it.

**Change:** `/indicators/{slug}/stats` gains `histogram` — fixed bins over
the same scoped population it already reports `count`, `p05`, `p50` and `p95`
for, computed with `width_bucket` in the same query. The legend draws it,
marks the class breaks of the active ramp, and marks the hovered feature's
value with a readout. Server-authoritative, so it is stable while the map
loads, and scoped to the AOI exactly as the existing numbers are.

### 5. Powerful, but nothing is discoverable, and nothing is reachable from the keyboard

Every feature has to be found by looking at it. The draw tools are 28 px
glyphs with `title` attributes and no labels. Switching AOI, changing
indicator, toggling a layer, moving between the three pages: all mouse-only,
all several clicks deep. There is no keyboard path to anything, no shortcuts,
and nothing anywhere tells a user what the application can do.

"Easy but powerful" is a layering problem, and the pattern that solves it is
well-established: leave the surface quiet, and put the whole command surface
one keystroke away.

**Change:** a command palette on `⌘K`/`Ctrl-K` covering AOIs, indicators,
layer toggles, draw modes, navigation and downloads; single-key shortcuts for
the frequent actions; and `?` for a shortcuts sheet, so the shortcuts are
discoverable rather than folklore.

### 6. The map has no spatial context — reviewed, not fixed here

Everything floats in black. `main.ts` says why, and the reason is good:

> Boots MapLibre with a minimal offline style (no basemap — the prototype
> avoids external tile dependencies and licensing drift)

Avoiding an external basemap is right for this delivery: it keeps the
dependency inventory honest and the licence statement simple. But the
consequence is that a user cannot tell where they are, how big anything is,
or which way is north.

The clean answer needs no external tiles at all. `serving_geo.roads` already
exists as `feature_v WHERE type_slug = 'road'` — the schema anticipates
roads-as-features — and it returns **0 rows**, because the street network is
loaded into `curated_geo.road_edge` as routing topology (`osm_id`, `source`,
`target`, `cost`) and never as features. Writing road features alongside the
routing edges would light up `serving_geo.roads`, make `get_tile?type_slug=road`
serve streets through the existing tile path, and give the map self-hosted
context with no new tile function, no external dependency, and OSM's own
attribution which the project already owes.

That is an ingest change, not a web-app change, so it is out of scope here
and recommended separately. **What this spec does instead** is reduce the
cost of having no basemap: a compass with north-reset, a live coordinate and
zoom readout, and a fit-to-AOI control, so orientation is at least
*recoverable* rather than absent.

---

## Not changed, and why

- **Bundle size.** `index` is 1.25 MB, 335 kB gzipped — MapLibre and
  terra-draw, and a map application is allowed to be a map application. Code
  splitting would help the two Svelte pages, which are already 34 and 37 kB.
  Not worth the churn before a delivery.
- **The two-framework split.** Vanilla TS for the map, Svelte for the other
  two. It is a real cost and the wrong time to pay it.
- **Catalog sorting and ops filtering.** Both would be welcome; neither is a
  defect, and both are additive later.

---

## Tests

| Layer | What |
|---|---|
| `apps/web` vitest | progress segmentation across every run outcome; toolbar pressed-state; command registry filtering and ranking; histogram binning and the hovered-value marker |
| `tests/unit` | the stats histogram's shape and edge cases (empty population, single value, all-equal) |
| `tests/integration` | `/indicators/{slug}/stats` returns bins that sum to `count`, scoped by AOI and feature type |
| `tests/e2e` | the palette opens on `⌘K` and runs a command; the sidebar collapses; the map is usable at 390 px |

## Maintenance checklist

No migration (1 skipped). `serving_geo` unchanged — the histogram is a query
over existing relations, not a new view (2 skipped). `explore` unchanged
(3 skipped). CLI unaffected (4). Tests added (5). `docs/architecture/API.md`
and the delivery documentation updated for the new response field (6).

---

## What the implementation found

Five things. Three were bugs in the work itself, which is the point of
writing them down: each one passed an assertion before it was caught by
looking.

### 1. The catalog's sideways scroll was not the table

The review said "the table overflows with no scroll affordance". The table
has had `overflow-x-auto` all along, and it was clipping correctly: the
wrapper measured 342 px with a `scrollWidth` of 717, exactly as intended.
The page still scrolled 320 px sideways.

The cause is a rule most people meet once: **an `overflow` container only
clips absolutely-positioned descendants when it is itself a containing
block.** The wrapper was `position: static`, and Tailwind's `sr-only` is
`position: absolute` — so the "Open" label in the last header cell escaped
the scroll box, was laid out at the table's full width, and pushed the
document's scroll area out to 710 px. `document.body.scrollWidth` read 390
the whole time.

One word fixes it (`relative`), and the lesson is in how it was found:
`scrollWidth` said nothing was wrong, and `window.scrollTo(9999, 0)` then
reading `window.scrollX` said it was. The e2e test measures the scroll.

### 2. `hidden` does nothing against `display: flex`

The palette and the shortcuts sheet were painted over the map from the
moment the page loaded, on every page view, through four separate probe
runs that all reported them hidden.

`root.hidden = true` sets the property; the `hidden` *attribute* works
through `display: none` in the UA stylesheet, and any author `display`
declaration outranks it. `.palette { display: flex }` meant the attribute
had no effect at all. Every assertion of the form `expect(el.hidden)` passed,
because the property was true and the pixels were not listening.

Caught by taking a screenshot of what was supposed to be the palette and
seeing the shortcuts sheet instead. `.palette[hidden] { display: none }` is
the fix; the e2e test reads `getComputedStyle`, not `.hidden`.

### 3. Running one command killed the keyboard

After the palette closed, `document.activeElement` could still be its input
— an element keeps focus when its container is hidden. The input matched the
"is the user typing?" guard that both the palette and the shortcut handler
use, so **every single-key shortcut was swallowed by a text field nobody
could see**. Pressing `?` or `\` after running one command did nothing until
the user clicked somewhere else.

`restoreFocus` did not save it: the element the user came from is usually
`<body>`, and `body.focus()` is a no-op. The close path now blurs the input
unconditionally before hiding.

It is worth noting how this presented. The first probe pressed `⌘K`, ran a
command, then pressed `?` — and reported "shortcuts sheet: false". Run the
same keys on a fresh page and everything worked. That is the shape of a bug
that gets filed as flaky and closed as unreproducible.

### 4. A media query that was correct and did nothing

The narrow-viewport block was written near the top of `style.css`, two
hundred lines before `.selection-toolbar { top: 12px }`. Equal specificity,
later source position: the original won. The media query matched, the rule
was right, and the draw tools stayed underneath the new sidebar toggle with
the circle tool hidden behind it — measured at 390 px, the toggle occupied
y 59–87 and the circle button sat at y 64.

Raising specificity would have worked and left the next person the same
trap. The override moved to the end of the file instead.

### 5. "Lowest 100%" — the estimator, not the phrasing

The first percentile readout counted a value's whole bin as "below" it,
which puts everything in the top bin at the 100th percentile. The readout
then said *"613 m · higher than 100% of features"* on exactly the outlier a
planner is most likely to click.

The mid-rank estimator — everything in lower bins, plus half of the value's
own — is both the standard answer and the honest one here, because a bin's
members are indistinguishable at this resolution and claiming otherwise
asserts more than the data supports. A population of one now returns no rank
at all rather than "higher than 50%".

---

## Verified

- `apps/web` vitest: **134 tests**, up from 73 — distribution binning and
  marking, command ranking and the quality gate, progress segmentation by
  outcome, sidebar and drawer behaviour.
- `tests/integration`: the stats histogram — bins summing to `count`, the
  maximum landing inside the histogram, AOI scoping, the degenerate
  single-value and empty-population cases, and bin-count validation.
- `tests/e2e`: **24 tests** in a real browser — the palette opening, closing
  and ranking; shortcuts surviving a command; the shortcuts sheet; the
  legend's distribution; the drawer at 390 px; and zero horizontal scroll on
  all three pages.
- Screenshots at 1440, 1280, 834 and 390 for all three pages, before and
  after.

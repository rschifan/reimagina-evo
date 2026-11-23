<script lang="ts">
  import type { Feature, Polygon } from "geojson";
  import * as maplibregl from "maplibre-gl";
  import type { GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";
  import { onMount, untrack } from "svelte";

  import {
    downloadUrl,
    getComparison,
    getIndicatorStats,
    listTileSources,
    type IndicatorValueStats,
    type RuleInputs,
    type RuleMode,
    type RuleUnits,
  } from "../api";
  import {
    activateIndicator,
    choroplethConfig,
    clearLegend,
    defaultGrain,
    deltaDomainFromComparison,
    rampDomainFromStats,
    sharedRampDomain,
    removeIndicator,
    renderDeltaLegend,
    renderLegend,
    type ChoroplethConfig,
    type DeltaDomain,
  } from "../choropleth";
  import { HISTOGRAM_BINS } from "../distribution";
  import {
    extractTileSources,
    installInputLayers,
    refreshBaselineAoi,
    type TileSourceCatalog,
  } from "../layers";
  import { cn } from "../lib/utils";
  import { describeRule, refusalText, sharedNumbers, type Rule } from "../scenario";
  import { createMap, flyToAoi } from "../map-base";
  import { enableSelection, type SelectionController, type SelectionShape } from "../selection";
  import { installSelectionHandle } from "../selection-handle";
  import { TooltipController } from "../tooltip";

  import { anchorBox } from "./box-anchor";
  import { noValuesNote } from "./derive";
  import { bboxOf, inferShape, rulesToFeatureCollection, wktToGeometry } from "./rule-geometry";
  import RuleBox from "./RuleBox.svelte";
  import {
    applyNewRule,
    deleteRule,
    onMapRefresh,
    saveRule,
    setIndicator,
    setMode,
    EMPTY_RULE,
    ws,
  } from "./store.svelte";

  /**
   * One scenario on the map, on the indicators a rule can reach, with the
   * draw tools a rule is written over.
   *
   * Everything about drawing an indicator is `choropleth.ts`'s, as on the
   * map page — tile URLs included, so the baseline's stays byte-identical
   * and its cached tiles stay valid. This file decides only *what* to draw.
   */
  let container: HTMLDivElement;
  let legend: HTMLDivElement;
  let map: MapLibreMap | null = null;
  let config: ChoroplethConfig | null = null;
  let catalog: TileSourceCatalog | null = null;
  let tooltips: TooltipController | undefined;
  let selection: SelectionController | undefined;
  let ready = $state(false);
  let shape = $state<SelectionShape | null>(null);
  /** Bumped when a followed run lands: the same view, fresh tiles. */
  let refreshTick = $state(0);
  let renderToken = 0;
  let mapNote = $state("");

  // ── the rule being edited ────────────────────────────────────────────
  //
  // `new`: a shape just drawn. `rule`: a saved rule, loaded into terra-draw so
  // it can be dragged; its static outline is hidden meanwhile. Only one box at
  // a time, and none while a gesture's recompute is running.
  const RULES_SOURCE = "scenario-rules";
  const RULES_FILL = "scenario-rules-fill";
  const RULES_LINE = "scenario-rules-line";
  type Editing =
    | { kind: "new" }
    | {
        kind: "rule";
        rule: Rule;
        index: number;
        draggable: boolean;
        overlapNote: string | null;
      };
  let editing = $state<Editing | null>(null);
  let boxPos = $state<{ left: number; top: number } | null>(null);
  let boxEl = $state<HTMLDivElement | undefined>();
  /** The next publish is `edit()` loading the rule, not the planner moving it. */
  let loadingRule = false;
  /** The open rule was dragged or resized since it was opened. */
  let moved = false;
  let labels: maplibregl.Marker[] = [];
  /** MapLibre is fetching tiles. With `ws.busy` (a recompute running), it
   *  shows the map page's spinner — one look for "working" across the app. */
  let tilesLoading = $state(false);

  const indicator = $derived(ws.indicators.find((i) => i.slug === ws.slug) ?? null);

  onMount(() => {
    // The pilot's own centre is applied as soon as it is known; this is only
    // where the camera waits until then.
    map = createMap(container, [-115.45, 32.62], 11);
    const unsubscribeRefresh = onMapRefresh(() => (refreshTick += 1));
    // Handled, as the map page handles it: with no listener MapLibre writes
    // every failed tile to the console, so a tile server that is not running
    // reads as a broken page instead of saying what is wrong.
    map.on("error", (e) => {
      mapNote = `Map error: ${e.error?.message ?? "unknown"}`;
    });
    map.on("load", () => {
      void (async () => {
        try {
          const sources = await listTileSources();
          config = choroplethConfig(sources);
          catalog = extractTileSources(sources);
        } catch (err) {
          mapNote = "The map's tile sources could not be read; the table still works.";
          console.warn("tile sources unavailable", err);
          return;
        }
        if (!map) return;
        tooltips = new TooltipController(map, map.getContainer());
        // This page has no layer toggles, so it picks what starts on: streets
        // only. With no basemap they are the only way to tell where you are;
        // everything else stays off, buildings included, since they bury the
        // blocks a rule selects.
        installInputLayers(map, catalog, tooltips, ws.aoiId, (layer) => layer.typeSlug === "road");
        selection = enableSelection(map);
        // The rim handle that resizes a circle, as on the explore page; after
        // enableSelection so it paints above terra-draw's outline.
        installSelectionHandle(map, selection);
        installRulesLayer(map);
        // ⚠ untrack: the controller publishes synchronously from inside
        // whatever called it, effects included. Read tracked, `editing` here
        // became a dependency of the pilot effect (which clears the
        // selection), so opening a box re-ran it and wiped the shape.
        selection.subscribe((sel) => untrack(() => {
          ws.drawn = sel?.feature ?? null;
          if (!sel) {
            shape = null;
            // Cleared from outside (a pilot switch): the box goes with it.
            if (!loadingRule && editing && !ws.busy) closeBox(false);
            return;
          }
          if (selection?.isDrawing()) return;
          if (loadingRule) loadingRule = false;
          else if (editing?.kind === "rule") moved = true;
          if (editing === null) {
            ws.boxError = null;
            editing = { kind: "new" };
          }
          shape = null;
          placeBox();
        }));
        map.on("move", placeBox);
        map.on("dataloading", () => (tilesLoading = true));
        map.on("idle", () => (tilesLoading = false));
        map.on("click", RULES_FILL, (e) => {
          if (editing || ws.busy || selection?.isDrawing()) return;
          // Every rule under the click, not the topmost: a small rule inside a
          // later, larger one had no pixel of its own left to click. The
          // smallest is the one a click inside it means.
          const hits = [...(e.features ?? [])].sort(
            (a, b) => Number(a.properties?.area) - Number(b.properties?.area),
          );
          const index = Number(hits[0]?.properties?.index);
          if (Number.isFinite(index)) openRule(index);
        });
        map.on("mouseenter", RULES_FILL, () => {
          if (map) map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", RULES_FILL, () => {
          if (map) map.getCanvas().style.cursor = "";
        });
        // Tests drive the map through this in the browser suite; dev only.
        if (import.meta.env.DEV) (window as unknown as { __scenarioMap?: MapLibreMap }).__scenarioMap = map;
        ready = true;
      })();
    });
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      unsubscribeRefresh();
      for (const label of labels) label.remove();
      map?.remove();
      map = null;
    };
  });

  /** Rule outlines: above the indicator fill (choropleth.ts inserts below
   *  `scenario-rules*`), below terra-draw's shapes, so a rule being edited
   *  shows over its own outline. */
  function installRulesLayer(m: MapLibreMap): void {
    const before = m.getStyle()?.layers.find((l) => l.id.startsWith("td-"))?.id;
    m.addSource(RULES_SOURCE, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    m.addLayer(
      { id: RULES_FILL, type: "fill", source: RULES_SOURCE, paint: { "fill-color": "#22d3ee", "fill-opacity": 0.08 } },
      before,
    );
    m.addLayer(
      { id: RULES_LINE, type: "line", source: RULES_SOURCE, paint: { "line-color": "#22d3ee", "line-width": 1.5 } },
      before,
    );
  }

  // The selected scenario's rules, outlined and labelled. Labels are HTML
  // markers: the map has no glyph source (no third-party service may be
  // required at runtime), so a symbol layer cannot draw text.
  $effect(() => {
    const rules = ws.rules;
    if (!ready || !map) return;
    const fc = rulesToFeatureCollection(rules);
    (map.getSource(RULES_SOURCE) as GeoJSONSource | undefined)?.setData(fc);
    for (const label of labels) label.remove();
    labels = fc.features.map((feature) => {
      const element = document.createElement("div");
      element.className =
        "pointer-events-none rounded bg-surface/90 px-1.5 py-0.5 text-[10px] text-foreground";
      element.textContent = String(feature.properties?.label ?? "");
      const [[minX, minY], [maxX, maxY]] = bboxOf(feature.geometry as Polygon);
      return new maplibregl.Marker({ element })
        .setLngLat([(minX + maxX) / 2, (minY + maxY) / 2])
        .addTo(map as MapLibreMap);
    });
  });

  // Another scenario or pilot on screen: whatever was being edited is gone.
  // ⚠ Only the selection and the pilot are dependencies: read tracked,
  // `editing` made opening a box re-run this and close it at once.
  $effect(() => {
    void ws.selectedId;
    void ws.aoiId;
    untrack(() => {
      if (!ws.busy && editing) closeBox(true);
    });
  });

  /** Put the box next to the shape being edited, inside the map. */
  function placeBox(): void {
    if (!map || !editing) return;
    const geometry =
      ws.drawn?.geometry ??
      (editing.kind === "rule" ? wktToGeometry(editing.rule.areaWkt) : null);
    if (!geometry) return;
    const [[minLon, minLat], [maxLon, maxLat]] = bboxOf(geometry);
    const topLeft = map.project([minLon, maxLat]);
    const bottomRight = map.project([maxLon, minLat]);
    const canvas = map.getContainer();
    boxPos = anchorBox(
      { minX: topLeft.x, minY: topLeft.y, maxX: bottomRight.x, maxY: bottomRight.y },
      { width: canvas.clientWidth, height: canvas.clientHeight },
      { width: boxEl?.offsetWidth ?? 224, height: boxEl?.offsetHeight ?? 280 },
    );
  }

  /** Open a saved rule — `index` is its 1-based place in "later wins". */
  function openRule(index: number): void {
    if (editing || ws.busy) return;
    const ordered = [...ws.rules].sort((a, b) => a.ordinal - b.ordinal);
    const rule = ordered[index - 1];
    if (!rule || !map) return;
    const geometry = wktToGeometry(rule.areaWkt);
    const kind = inferShape(geometry);
    ws.boxError = null;
    editing = {
      kind: "rule",
      rule,
      index,
      draggable: kind !== null,
      overlapNote: overlapNote(index, ordered, geometry),
    };
    // Hide the static outline: the editable copy is what the planner moves.
    for (const id of [RULES_FILL, RULES_LINE]) map.setFilter(id, ["!=", ["get", "index"], index]);
    moved = false;
    if (kind !== null && selection) {
      loadingRule = true;
      selection.edit({ type: "Feature", properties: {}, geometry: geometry as Polygon }, kind);
    } else {
      placeBox();
    }
  }

  /**
   * Which later rules win over this one where they overlap, and for which
   * numbers. Precedence is the server's — the rules' order — and it is per
   * number: a later rule that sets only household size leaves this one's
   * storeys standing, so it is not named for them.
   *
   * ponytail: bounding boxes, not polygons — it can name a rule whose box
   * overlaps but whose shape does not. A polygon intersection when rules get
   * irregular enough for that to mislead.
   */
  function overlapNote(
    index: number,
    ordered: Rule[],
    geometry: Polygon | GeoJSON.MultiPolygon,
  ): string | null {
    const [[a0, b0], [a1, b1]] = bboxOf(geometry);
    const notes: string[] = [];
    for (let i = index; i < ordered.length; i += 1) {
      let other;
      try {
        other = bboxOf(wktToGeometry(ordered[i].areaWkt));
      } catch {
        continue;
      }
      const [[c0, d0], [c1, d1]] = other;
      if (!(a0 <= c1 && c0 <= a1 && b0 <= d1 && d0 <= b1)) continue;
      const shared = sharedNumbers(ordered[index - 1], ordered[i]);
      if (shared.length) notes.push(`Rule ${i + 1} also sets ${shared.join(", ")}`);
    }
    return notes.length ? `${notes.join("; ")}: where they overlap, the later rule wins.` : null;
  }

  function closeBox(clearShape: boolean): void {
    editing = null;
    boxPos = null;
    loadingRule = false;
    moved = false;
    ws.boxError = null;
    if (map) for (const id of [RULES_FILL, RULES_LINE]) map.setFilter(id, null);
    if (clearShape) selection?.clear();
  }

  async function apply(
    values: RuleInputs, scenarioName?: string, mode: RuleMode = "set", units: RuleUnits = {},
  ): Promise<void> {
    const area = ws.drawn;
    if (!area) return;
    const pilotAtStart = ws.aoiId;
    // A pilot switch mid-gesture clears the selection while busy, when the
    // effect above stands down; close here or the box outlives its city.
    if ((await applyNewRule(area, values, scenarioName, mode, units)) || ws.aoiId !== pilotAtStart) closeBox(true);
  }

  async function save(values: RuleInputs, mode: RuleMode = "set", units: RuleUnits = {}): Promise<void> {
    const current = editing;
    if (current?.kind !== "rule") return;
    const area: Feature<Polygon> | null = moved ? ws.drawn : null;
    const scenarioAtStart = ws.selectedId;
    if ((await saveRule(current.rule, area, values, mode, units)) || ws.selectedId !== scenarioAtStart) closeBox(true);
  }

  async function remove(): Promise<void> {
    const current = editing;
    if (current?.kind !== "rule") return;
    const scenarioAtStart = ws.selectedId;
    if ((await deleteRule(current.rule)) || ws.selectedId !== scenarioAtStart) closeBox(true);
  }

  function onKey(event: KeyboardEvent): void {
    const target = event.target as HTMLElement | null;
    if (target && /^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName)) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "Escape") {
      // Mid-recompute the box stays: it is where a refusal will be shown.
      if (ws.busy) return;
      if (editing) closeBox(true);
      else clearDrawing();
      return;
    }
    if (editing || ws.busy) return;
    const next = ({ r: "rectangle", p: "polygon", c: "circle" } as const)[event.key];
    if (next) draw(next);
  }

  // The pilot: clip the context layers to it and fly there.
  $effect(() => {
    const aoiId = ws.aoiId;
    if (!ready || !map || !catalog) return;
    refreshBaselineAoi(map, catalog, aoiId);
    const aoi = ws.aois.find((a) => a.aoi_id === aoiId);
    if (aoi) flyToAoi(map, aoi);
    selection?.clear();
  });

  // The overlay: whatever is selected, as values or as the difference.
  $effect(() => {
    const current = indicator;
    const scenarioId = ws.selectedId;
    const mode = ws.mode;
    const aoiId = ws.aoiId;
    void refreshTick;
    if (!ready || !map || !config) return;
    const myToken = ++renderToken;
    void render(myToken, current, scenarioId, mode, aoiId);
  });

  async function render(
    myToken: number,
    current: typeof indicator,
    scenarioId: string | null,
    mode: "value" | "delta",
    aoiId: string | null,
  ): Promise<void> {
    if (!map || !config) return;
    if (!current) {
      removeIndicator(map);
      clearLegend(legend);
      return;
    }
    const grain = defaultGrain(current);
    if (mode === "delta" && scenarioId) {
      let delta: DeltaDomain | null = null;
      let failure = "";
      try {
        delta = deltaDomainFromComparison(
          await getComparison(scenarioId, { indicator_slug: current.slug, aoi_id: aoiId }),
        );
      } catch (err) {
        console.warn("comparison unavailable; showing values", err);
        failure = await refusalText(err, "compare");
      }
      // A slower answer for a selection that has since moved is dropped
      // whole: drawing it would put one scenario's change on another's map.
      if (myToken !== renderToken || !map) return;
      if (delta) {
        activateIndicator(map, config, current, rampDomainFromStats(null, current), {
          tooltips, aoiId, grain, scenarioId, delta,
        });
        renderDeltaLegend(legend, current, delta);
        mapNote =
          `Change in ${current.name} against the baseline city: ` +
          `${delta.changed} changed, ${delta.unchanged} unchanged, ${delta.new} new.`;
        return;
      }
      // Said, not silent: the Change button otherwise seemed to do nothing.
      mapNote = `${failure} Showing values instead.`;
      setMode("value");
      return;
    }
    // A scenario is coloured on one scale with the baseline city: the
    // baseline's range widened to the scenario's, so a block the rule raised
    // shows how high, not just the darkest colour, and a block it did not
    // touch is not turned the palest by a rule's outliers.
    const statsFor = async (scenario: string | null): Promise<IndicatorValueStats | null> => {
      try {
        return await getIndicatorStats(current.slug, {
          feature_type: grain ?? undefined,
          aoi_id: aoiId ?? undefined,
          scenario_id: scenario ?? undefined,
          bins: HISTOGRAM_BINS,
        });
      } catch (err) {
        console.warn("stats unavailable; ramp from catalog bounds", err);
        return null;
      }
    };
    const [stats, base] = await Promise.all([
      statsFor(scenarioId),
      scenarioId ? statsFor(null) : Promise.resolve(null),
    ]);
    if (myToken !== renderToken || !map) return;
    // The baseline's range, so a colour means one value on every scenario —
    // unless the baseline has no values here (the edificability family on
    // the real pilots), when its "range" would be the catalog's safety
    // bounds and every block would paint in the palest colour.
    const baseUsable = scenarioId !== null && base !== null && base.count > 0;
    const domain = baseUsable
      ? sharedRampDomain(rampDomainFromStats(base, current), rampDomainFromStats(stats, current))
      : rampDomainFromStats(stats, current);
    // No `selection`: on the explore page it clips the layer to the drawn
    // area; here the planner edits a rule against the whole city.
    activateIndicator(map, config, current, domain, { tooltips, aoiId, grain, scenarioId });
    renderLegend(
      legend, current, domain, stats?.histogram ?? null, stats?.count_at_cap ?? null, stats?.count ?? 0,
      stats?.count_estimated ?? 0,
    );
    mapNote =
      stats && stats.count === 0
        ? noValuesNote(current.name, scenarioId !== null)
        : scenarioId && !baseUsable
          ? "Colours use this scenario's own range: the baseline city has no values here."
          : "";
  }

  function draw(next: SelectionShape) {
    if (!selection) return;
    selection.startDrawing(next);
    shape = next;
  }

  function clearDrawing() {
    selection?.clear();
    shape = null;
  }

  const SHAPES: Array<{ shape: SelectionShape; glyph: string; label: string }> = [
    { shape: "circle", glyph: "●", label: "Draw circle" },
    { shape: "rectangle", glyph: "▭", label: "Draw rectangle" },
    { shape: "polygon", glyph: "⬠", label: "Draw polygon" },
  ];
</script>

<section class="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
  <div
    class="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-line bg-surface px-3 py-2 text-xs"
  >
    <label class="flex items-center gap-2 text-muted-foreground">
      <span class="sr-only">Indicator</span>
      <select
        class="rounded border border-line bg-surface px-2 py-1 text-xs text-foreground"
        value={ws.slug ?? ""}
        onchange={(e) => setIndicator(e.currentTarget.value)}
      >
        {#each ws.indicators as option (option.slug)}
          <option value={option.slug}>{option.name}</option>
        {/each}
      </select>
    </label>
    <div role="group" aria-label="Show" class="flex overflow-hidden rounded border border-line">
      {#each [["delta", "Change"], ["value", "Values"]] as const as [mode, label] (mode)}
        <button
          type="button"
          aria-pressed={ws.mode === mode}
          disabled={mode === "delta" && ws.selectedId === null}
          title={mode === "delta" && ws.selectedId === null
            ? "Pick a scenario to see its change against the baseline city"
            : undefined}
          class={cn(
            "px-2.5 py-1 disabled:opacity-50",
            ws.mode === mode ? "bg-accent/10 text-accent" : "text-muted-foreground",
          )}
          onclick={() => setMode(mode)}
        >
          {label}
        </button>
      {/each}
    </div>
    {#if indicator}
      <details class="relative ml-auto">
        <summary class="cursor-pointer list-none rounded border border-line px-2 py-1 text-muted-foreground hover:text-foreground">
          Download ▾
        </summary>
        <div class="absolute right-0 z-20 mt-1 flex flex-col rounded border border-line bg-surface p-1 shadow">
          {#each [["csv", "CSV"], ["geojson", "GeoJSON"], ["parquet", "GeoParquet"]] as const as [format, label] (format)}
            <a
              class="rounded px-3 py-1 whitespace-nowrap hover:bg-accent/10 hover:text-foreground"
              href={downloadUrl(indicator.slug, format, { aoiId: ws.aoiId, grain: defaultGrain(indicator), scenarioId: ws.selectedId })}
              rel="noopener"
            >
              {label}
            </a>
          {/each}
        </div>
      </details>
    {/if}
  </div>

  <div class="flex min-h-0 flex-1 gap-2">
  <div class="relative min-h-[240px] min-w-0 flex-1 overflow-hidden rounded-lg border border-line">
    <!-- Two divs on purpose. MapLibre adds `.maplibregl-map { position:
         relative }` to the element it mounts on, and that unlayered rule beats
         Tailwind's layered `absolute`, so a container that was itself
         `absolute inset-0` collapsed to 0 px and the map drew nothing. The
         outer div keeps the position MapLibre never sees. -->
    <div class="absolute inset-0">
      <div bind:this={container} class="h-full w-full"></div>
    </div>
    <div
      role="toolbar"
      aria-label="Draw the area a rule applies to"
      class="absolute top-3 left-3 flex flex-col gap-1 rounded border border-line bg-surface/95 p-1"
    >
      <span class="text-center text-[10px] text-muted-foreground" aria-hidden="true">Draw</span>
      {#each SHAPES as item (item.shape)}
        <button
          type="button"
          class={cn(
            "size-8 rounded text-sm",
            shape === item.shape ? "bg-accent/15 text-accent" : "hover:bg-accent/5",
          )}
          aria-pressed={shape === item.shape}
          aria-label={item.label}
          title={item.label}
          disabled={!ready}
          onclick={() => draw(item.shape)}
        >
          {item.glyph}
        </button>
      {/each}
      <button
        type="button"
        class="size-8 rounded text-sm hover:bg-accent/5"
        aria-label="Clear the drawn area"
        title="Clear the drawn area"
        disabled={!ready}
        onclick={clearDrawing}
      >
        ×
      </button>
    </div>
    <!-- The page's messages and the map's own note, over the map: a line above
         it would push the map down under the planner's cursor. Always in the
         DOM, so a screen reader announces each new text. -->
    <div class="pointer-events-none absolute top-3 left-1/2 z-10 flex max-w-[60%] -translate-x-1/2 flex-col items-center gap-1 text-center text-xs">
      <p
        role="status"
        class={cn(
          "rounded border px-2 py-1",
          ws.message ? "border-line bg-surface/95" : "border-0 p-0",
          ws.message?.tone === "bad" ? "text-state-error" : "text-foreground",
        )}
      >
        {ws.message?.text ?? ""}
      </p>
      <p role="status" class={cn("rounded text-muted-foreground", mapNote && "border border-line bg-surface/95 px-2 py-1")}>
        {mapNote}
      </p>
    </div>
    <div
      class="map-loading"
      data-loading={ws.busy || tilesLoading ? "true" : "false"}
      aria-hidden="true"
    >
      <div class="map-loading__spinner" role="progressbar" aria-label="Recomputing"></div>
    </div>
    <div
      bind:this={legend}
      class="legend absolute right-3 bottom-8 max-w-[280px] rounded border border-line bg-surface/95 p-2 text-xs"
      hidden
    ></div>
    {#if editing}
      {@const current = editing}
      <div
        bind:this={boxEl}
        class="absolute z-10"
        style:left="{boxPos?.left ?? 56}px"
        style:top="{boxPos?.top ?? 12}px"
      >
        {#key current.kind === "rule" ? `rule-${current.rule.id}` : "new"}
          <RuleBox
            mode={current.kind}
            values={current.kind === "rule" ? current.rule.inputs : EMPTY_RULE}
            needsScenarioName={current.kind === "new" && ws.selectedId === null}
            defaultName={`Scenario ${ws.scenarios.length + 1}`}
            overlapNote={current.kind === "rule" ? current.overlapNote : null}
            busy={ws.busy}
            error={ws.boxError}
            draggable={current.kind === "rule" ? current.draggable : true}
            ruleMode={current.kind === "rule" ? (current.rule.mode ?? "set") : "set"}
            ruleUnits={current.kind === "rule" ? (current.rule.units ?? {}) : {}}
            onapply={(values, name, mode, units) => void apply(values, name, mode, units)}
            onsave={(values, mode, units) => void save(values, mode, units)}
            ondelete={() => void remove()}
            oncancel={() => closeBox(true)}
          />
        {/key}
      </div>
    {/if}
  </div>
  {#if ws.selectedId !== null}
    <!-- The rules, reachable without a pointer (a rule is otherwise opened
         only by clicking its outline), and how to add one. -->
    <aside aria-label="Rules" class="flex w-52 shrink-0 flex-col gap-2 overflow-y-auto rounded-lg border border-line bg-surface p-2 text-xs">
      <h3 class="font-medium text-foreground">
        Rules · {ws.scenarios.find((s) => s.id === ws.selectedId)?.name ?? ""}
      </h3>
      <p class="text-muted-foreground">Draw an area on the map to add a rule. Later rules win where they overlap.</p>
      <ol class="flex flex-col gap-1.5">
        {#each [...ws.rules].sort((a, b) => a.ordinal - b.ordinal) as rule, i (rule.id)}
          <li>
            <button
              type="button"
              class="w-full rounded border border-line px-2 py-1 text-left hover:text-foreground disabled:opacity-50"
              disabled={!ready || ws.busy || editing !== null}
              onclick={() => openRule(i + 1)}
            >
              {describeRule(rule, i + 1)}
            </button>
          </li>
        {/each}
      </ol>
    </aside>
  {/if}
  </div>
</section>

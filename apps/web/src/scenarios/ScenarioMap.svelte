<script lang="ts">
  import type { Map as MapLibreMap } from "maplibre-gl";
  import { onMount } from "svelte";

  import {
    downloadUrl,
    getComparison,
    getIndicatorStats,
    listTileSources,
    type IndicatorValueStats,
  } from "../api";
  import {
    activateIndicator,
    choroplethConfig,
    clearLegend,
    defaultGrain,
    deltaDomainFromComparison,
    rampDomainFromStats,
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
  import { createMap, flyToAoi } from "../map-base";
  import { enableSelection, type SelectionController, type SelectionShape } from "../selection";
  import { TooltipController } from "../tooltip";

  import { noValuesNote } from "./derive";
  import { onMapRefresh, setIndicator, setMode, ws } from "./store.svelte";

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
        installInputLayers(map, catalog, tooltips, ws.aoiId);
        // Buildings are the map page's business; here they bury the blocks
        // a rule selects. Streets stay: with no basemap they are the only
        // way to tell where you are.
        if (map.getLayer("baseline_buildings__fill")) {
          map.setLayoutProperty("baseline_buildings__fill", "visibility", "none");
          map.setLayoutProperty("baseline_buildings__line", "visibility", "none");
        }
        selection = enableSelection(map);
        selection.subscribe((sel) => {
          ws.drawn = sel?.feature ?? null;
          if (!sel) shape = null;
        });
        ready = true;
      })();
    });
    return () => {
      unsubscribeRefresh();
      map?.remove();
      map = null;
    };
  });

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
      try {
        delta = deltaDomainFromComparison(
          await getComparison(scenarioId, { indicator_slug: current.slug, aoi_id: aoiId }),
        );
      } catch (err) {
        console.warn("comparison unavailable; showing values", err);
      }
      // A slower answer for a selection that has since moved is dropped
      // whole: drawing it would put one scenario's change on another's map.
      if (myToken !== renderToken || !map) return;
      if (delta) {
        activateIndicator(
          map, config, current, rampDomainFromStats(null, current),
          tooltips, aoiId, grain, scenarioId, delta,
        );
        renderDeltaLegend(legend, current, delta);
        mapNote =
          `Change in ${current.name} against the baseline city: ` +
          `${delta.changed} changed, ${delta.unchanged} unchanged, ${delta.new} new.`;
        return;
      }
      setMode("value");
      return;
    }
    let stats: IndicatorValueStats | null = null;
    try {
      stats = await getIndicatorStats(current.slug, {
        feature_type: grain ?? undefined,
        aoi_id: aoiId ?? undefined,
        scenario_id: scenarioId ?? undefined,
        bins: HISTOGRAM_BINS,
      });
    } catch (err) {
      console.warn("stats unavailable; ramp from catalog bounds", err);
    }
    if (myToken !== renderToken || !map) return;
    const domain = rampDomainFromStats(stats, current);
    activateIndicator(map, config, current, domain, tooltips, aoiId, grain, scenarioId);
    renderLegend(
      legend, current, domain, stats?.histogram ?? null, stats?.count_at_cap ?? null, stats?.count ?? 0,
    );
    mapNote = stats && stats.count === 0 ? noValuesNote(current.name, scenarioId !== null) : "";
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

<section class="flex min-w-0 flex-col gap-2">
  <div class="flex flex-wrap items-center gap-3 text-xs">
    <label class="flex items-center gap-2 text-muted-foreground">
      Indicator
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
      {#each [["value", "Values"], ["delta", "Difference"]] as const as [mode, label] (mode)}
        <button
          type="button"
          aria-pressed={ws.mode === mode}
          disabled={mode === "delta" && ws.selectedId === null}
          title={mode === "delta" && ws.selectedId === null
            ? "Pick a scenario to see its difference from the baseline city"
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
      <span class="ml-auto flex items-center gap-2 text-muted-foreground">
        Download
        {#each [["csv", "CSV"], ["geojson", "GeoJSON"], ["parquet", "GeoParquet"]] as const as [format, label] (format)}
          <a
            class="rounded border border-line px-2 py-0.5 hover:text-foreground"
            href={downloadUrl(indicator.slug, format, { aoiId: ws.aoiId, grain: defaultGrain(indicator), scenarioId: ws.selectedId })}
            rel="noopener"
          >
            {label}
          </a>
        {/each}
      </span>
    {/if}
  </div>

  <div class="relative h-[60vh] min-h-[420px] overflow-hidden rounded-lg border border-line">
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
    <div
      bind:this={legend}
      class="legend absolute right-3 bottom-8 max-w-[280px] rounded border border-line bg-surface/95 p-2 text-xs"
      hidden
    ></div>
  </div>
  <p class="text-xs text-muted-foreground" role="status">{mapNote}</p>
</section>

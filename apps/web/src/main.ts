/**
 * Entry point for the reimagina read-side web app.
 *
 * Boots MapLibre with a minimal offline style (no basemap — the
 * prototype avoids external tile dependencies and licensing drift),
 * loads the tile-source catalog + indicator catalog from the API, and
 * wires:
 *   - AOI picker (whatever /aois returns — CDMX, Mexicali, …) → flies
 *     the map and re-scopes server-side tile filters via aoi_id.
 *   - Spatial-input layer toggles served by Martin's serving_geo.get_tile.
 *   - Indicator picker → server-side MVT choropleth via Martin's
 *     serving_geo.indicator_overlay_tile function.
 *   - CSV / GeoJSON / GeoParquet download buttons once an indicator is
 *     selected.
 */
import * as maplibregl from "maplibre-gl";
// maplibre-gl 6 ships its web worker as a separate module instead of
// inlining it, so the bundler has to emit it and the library has to be
// told where it landed. Without this the map renders its canvas and
// controls, then fails at "Worker failed to load" and parses no tiles.
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import "maplibre-gl/dist/maplibre-gl.css";
import { mount } from "svelte";

import TopBar from "./lib/TopBar.svelte";
import "./topbar.css";
import type { AOIRow, IndicatorSummary, TileSource } from "./api";
import type { StyleSpecification } from "maplibre-gl";

import {
  getActiveAoiId,
  populateAoiSelect,
  setKnownAois,
  subscribe as subscribeToAoi,
} from "./aoi";
import {
  getIndicatorStats,
  isAbortError,
  listAttributions,
  listAois,
  listIndicators,
  listTileSources,
} from "./api";
import {
  activateIndicator,
  choroplethConfig,
  markLegendValue,
  setValueHoverListener,
  clearGrainToggle,
  clearLegend,
  defaultGrain,
  populateIndicatorSelect,
  rampDomainFromStats,
  removeIndicator,
  renderGrainToggle,
  renderLegend,
} from "./choropleth";
import { HISTOGRAM_BINS } from "./distribution";
import {
  setDownloadAvailability,
  wireDownloadButtons,
} from "./download";
import { escapeHtml } from "./format";
import type { Command } from "./commands";
import { enablePalette } from "./palette";
import { SHORTCUTS, wireShortcuts } from "./shortcuts";
import { enableScenarioPanel } from "./scenario";
import { enableSidebar, type SidebarController } from "./sidebar";
import {
  INPUT_LAYERS,
  extractTileSources,
  installInputLayers,
  clearAggregationOverlayClipping,
  installAggregationOverlays,
  installRasterLayers,
  wireAggregationOverlayClipping,
  refreshAggregationOverlayAoi,
  refreshBaselineAoi,
  wireLayerToggles,
  wireRasterToggles,
} from "./layers";
import {
  enableSelection,
  type SelectionController,
  type SelectionGeometry,
  type SelectionShape,
} from "./selection";
import { installSelectionHandle } from "./selection-handle";
import { installSelectionMask } from "./selection-mask";
import { streamMetrics, type MetricResult, type MetricSpec } from "./selection-metrics";
import { TooltipController } from "./tooltip";

// Fallback centre when no AOIs come back from the API. CDMX-Cuauhtémoc
// remains a reasonable default for the prototype's first deployment.
const FALLBACK_CENTER: [number, number] = [-99.137, 19.432];
const FALLBACK_ZOOM = 12;

interface AppState {
  activeIndicator: IndicatorSummary | null;
  /**
   * The scenario the map is showing, or null for the baseline city.
   *
   * Read in four places and owned here because they must agree: the
   * indicator list (a scenario's results are what make the edificability
   * indicators offerable at all), the overlay's tile URL, the ramp's stats,
   * and the download URLs. A scenario resolved in one of them and not the
   * others is a map whose legend, picker and export describe different
   * cities.
   */
  activeScenarioId: string | null;
  /** Feature type the choropleth is currently drawn at. Defaults to
   *  the indicator's finest grain and is re-derived on every pick, so
   *  a coarse choice never leaks onto an indicator that lacks it. */
  activeGrain: string | null;
  /**
   * Places the hovered feature's value in the legend's distribution.
   *
   * Re-assigned on every indicator render so it closes over that
   * indicator's histogram and ramp. A single long-lived handler would mark
   * the new indicator's cursor against the previous one's bins — right-
   * looking and wrong, which is the worst kind.
   */
  markHovered?: (value: number | null) => void;
}


function setStatus(message: string): void {
  const el = document.getElementById("status");
  if (el) el.textContent = message;
}


function emptyStyle(): StyleSpecification {
  return {
    version: 8,
    name: "reimagina-empty",
    sources: {},
    layers: [
      {
        id: "background",
        type: "background",
        paint: { "background-color": "#0b1220" },
      },
    ],
  };
}


async function loadInitialState(): Promise<{
  sources: TileSource[];
  indicators: IndicatorSummary[];
  aois: AOIRow[];
}> {
  // Indicators are fetched after the AOIs, not alongside them: the list is
  // scoped to the active AOI so a picker never offers an indicator whose
  // overlay would render empty here. Resolving the AOI first costs one
  // sequential round trip and saves fetching a list we would discard.
  const [sources, aois] = await Promise.all([listTileSources(), listAois()]);
  setKnownAois(aois);
  const indicators = await listIndicators(getActiveAoiId());
  return { sources, indicators, aois };
}


function flyToAoi(map: maplibregl.Map, aoi: AOIRow): void {
  // Prefer the features bbox over the AOI polygon bbox. For irregular
  // AOIs (Mexicali muni — mostly desert with the urban core packed in
  // one corner) the polygon bbox frames the whole muni and the user
  // ends up looking at empty land. The features bbox is the envelope
  // of actual loaded blocks, so fitBounds lands them on the urban
  // extent. Fall back to the polygon bbox when no features have been
  // loaded yet (e.g. fresh AOI before ingest).
  const target = aoi.features_bbox ?? aoi.bbox;
  if (!target) return;
  const [minLon, minLat, maxLon, maxLat] = target;
  // ``maxZoom`` caps the fit so a tiny AOI doesn't slingshot past
  // street level (z=14 keeps analysis-unit layers above their
  // minzoom=8 with comfortable density). ``padding`` reserves
  // viewport edges so the polygon isn't clipped behind UI chrome.
  map.fitBounds(
    [
      [minLon, minLat],
      [maxLon, maxLat],
    ],
    { padding: 60, duration: 700, maxZoom: 14 },
  );
}


function renderAoiInfo(aoi: AOIRow | null): void {
  const el = document.getElementById("aoi-info");
  if (!el) return;
  if (!aoi) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  const desc = aoi.description ? `<div class="aoi-info-desc">${escapeHtml(aoi.description)}</div>` : "";
  el.innerHTML = `<div class="aoi-info-name">${escapeHtml(aoi.name)}</div>${desc}`;
}




async function main(): Promise<void> {
  // The nav bar is independent of every API call below, so mount it first:
  // it must be there even if the API is down and this function throws.
  const topbar = document.getElementById("topbar");
  if (topbar) mount(TopBar, { target: topbar, props: { current: "map" } });

  setStatus("Connecting to reimagina API…");

  // Show the spinner immediately so the user has feedback while the
  // first /aois + /tile-sources round-trip happens (before the map
  // exists and its dataloading events kick in).
  const loadingHost = document.getElementById("map-loading");
  if (loadingHost) {
    loadingHost.setAttribute("data-loading", "true");
    loadingHost.setAttribute("aria-hidden", "false");
  }

  let sources: TileSource[];
  let indicators: IndicatorSummary[];
  let aois: AOIRow[];
  try {
    ({ sources, indicators, aois } = await loadInitialState());
  } catch (err) {
    setStatus(`Failed to load API: ${(err as Error).message}`);
    if (loadingHost) loadingHost.setAttribute("data-loading", "false");
    throw err;
  }
  const tileCatalog = extractTileSources(sources);
  const overlayConfig = choroplethConfig(sources);

  // The AOI store was seeded inside loadInitialState (the indicator fetch
  // needs the resolved AOI), so the active id is already authoritative here.
  const initialAoi = aois.find((a) => a.aoi_id === getActiveAoiId()) ?? null;

  const state: AppState = {
    activeIndicator: null,
    activeGrain: null,
    activeScenarioId: null,
  };

  maplibregl.setWorkerUrl(maplibreWorkerUrl);
  const map = new maplibregl.Map({
    container: "map",
    style: emptyStyle(),
    center: FALLBACK_CENTER,
    zoom: FALLBACK_ZOOM,
    maxZoom: 18,
    minZoom: 5,
  });
  // ⚠ `showCompass: true`, which it was not.
  //
  // With no basemap there is nothing in the frame that says which way is
  // north — no coastline, no street grid, no labels — so a rotated map is
  // unrecoverable by looking at it. The compass is also the only way back
  // from an accidental rotate, which a trackpad makes easy to do and hard
  // to notice.
  map.addControl(
    new maplibregl.NavigationControl({ showCompass: true, visualizePitch: true }),
    "top-right",
  );
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

  // ⚠ A licence obligation, not a nicety.
  //
  // OpenStreetMap is ODbL and displaying it without credit is a breach, and
  // the reason this map has no external basemap in the first place is
  // licence hygiene. Publishing OSM-derived streets without the credit
  // would spend exactly what that decision was protecting.
  //
  // The text comes from the API, which derives it from the licence recorded
  // against each source that actually has features. Hardcoding it here
  // would credit OpenStreetMap on a synthetic territory — a false
  // provenance claim in a product whose argument is that its provenance is
  // checkable — and crediting a registered-but-unloaded source would claim
  // the map shows data it does not.
  void listAttributions()
    .then((rows) => {
      if (rows.length === 0) return;
      map.addControl(
        new maplibregl.AttributionControl({
          customAttribution: rows.map((row) => row.text),
          // ⚠ `compact: true`, and the text is NOT shortened to achieve it.
          //
          // Expanded, four real sources render as one 200-character line
          // spanning the whole viewport:
          //
          //   © OpenStreetMap contributors | IMIP Mexicali (Datos abiertos
          //   del IMIP) | Overture Maps Foundation (ODbL-1.0 / Overture
          //   terms) | INEGI (Libre uso — Términos de Uso de la Información
          //   del INEGI)
          //
          // The obvious fix is to trim each credit, and that is the wrong
          // one: the wording comes from `data_source_catalog.license` via
          // `attribution.py`, whose own comment warns that an entry claiming
          // "this exact wording is what the licence asks for" is worse than
          // the honest fallback when it is wrong. Editing four providers'
          // credit lines for layout is a licence judgement, and not one to
          // make for a line break.
          //
          // Compact keeps every word and moves it behind the standard
          // MapLibre (i) control — the same affordance every multi-source
          // map uses, one click away and always visible.
          compact: true,
        }),
        "bottom-right",
      );
      // ⚠ `compact: true` is not enough on its own, and the reason is in
      // MapLibre's `_updateCompact`:
      //
      //     this._container.classList.add(
      //       'maplibregl-compact', 'maplibregl-compact-show')
      //
      // It adds BOTH classes, so a compact control renders *expanded* on
      // load and only collapses once the user first interacts with the map.
      // Until then four real sources are still a 200-character bar across
      // the viewport — which is exactly what `compact: true` was added to
      // stop, so without this the fix does nothing for anyone who has not
      // panned yet.
      //
      // Removing `-show` is stable: `_updateCompact` only re-adds it when
      // `maplibregl-compact` is absent, and that class stays. Done on the
      // next frame so the control's own first pass has already run.
      requestAnimationFrame(() => {
        document
          .querySelectorAll(".maplibregl-ctrl-attrib.maplibregl-compact-show")
          .forEach((el) => el.classList.remove("maplibregl-compact-show"));
      });
    })
    .catch((err) => {
      // Loud, because silence here is a licence breach rather than a
      // missing nicety: a map that drew OSM geometry and failed to credit
      // it must say so somewhere a developer will see.
      console.error("could not resolve data attribution", err);
    });

  // Collapsing the panel changes the canvas size, and MapLibre only learns
  // that when told. Without the resize the map keeps its old width and the
  // reclaimed space renders as empty page.
  const appEl = document.querySelector<HTMLElement>(".app");
  const sidebarToggle =
    document.querySelector<HTMLButtonElement>("#sidebar-toggle");
  const sidebarScrim = document.querySelector<HTMLElement>("#sidebar-scrim");
  let sidebar: SidebarController | null = null;
  if (appEl && sidebarToggle) {
    sidebar = enableSidebar(appEl, sidebarToggle, sidebarScrim, () => {
      // After the CSS transition, not during it: resizing mid-animation
      // makes MapLibre re-render every frame of it for nothing.
      window.setTimeout(() => map.resize(), 180);
    });
  }

  // Loading indicator: visible whenever MapLibre has tile fetches in
  // flight. ``dataloading`` fires per-source-fetch, ``idle`` fires once
  // every source has finished. ``idle`` is the canonical "nothing to
  // do" event — using it as the off signal means a missed ``data``
  // event can't strand the spinner on.
  const setLoading = (v: boolean) => {
    if (loadingHost) {
      loadingHost.setAttribute("data-loading", v ? "true" : "false");
      loadingHost.setAttribute("aria-hidden", v ? "false" : "true");
    }
  };
  map.on("dataloading", () => setLoading(true));
  map.on("idle", () => setLoading(false));

  map.on("load", () => {
    // Tooltip controller is the only thing that knows how to render
    // hover content. Providers are registered by the installers below
    // so each layer type declares its own "what do I show on hover"
    // in one place — main.ts stays agnostic of per-layer details.
    const tooltipController = new TooltipController(map, map.getContainer());

    installInputLayers(map, tileCatalog, tooltipController, getActiveAoiId());
    installRasterLayers(map, sources, tooltipController);
    installAggregationOverlays(map, sources, tooltipController, getActiveAoiId());

    // ── Sub-AOI selection tool ─────────────────────────────────────
    //
    // Spotlight-mask architecture (Felt / Mapbox-Studio idiom): one
    // fullscreen fill layer covering the world with the selection
    // cut out as a hole. Dimming is one constant-cost GPU draw, not
    // O(N×L) feature-state classifications across N visible features
    // and L data layers — at CDMX scale that's the difference between
    // a smooth 60 fps drag and a juddery one.
    //
    // Layer order matters: install the mask BEFORE ``enableSelection``
    // so terra-draw's layers (added inside ``enableSelection``) paint
    // on top of the mask — otherwise the cyan shape outline gets
    // covered by the dim wash.
    const mask = installSelectionMask(map);
    const selectionController = enableSelection(map);
    mask.bindController(selectionController);
    // Custom radius handle: a single visible cyan dot on the circle's
    // rim, mousedown→drag scales the circle uniformly. Industry-
    // standard "drag the radius" affordance — terra-draw's per-vertex
    // selection points are hidden for circles in favour of this.
    // Installed AFTER ``enableSelection`` so the handle layer sits on
    // top of terra-draw's outline (it should — adding later means it
    // paints on top in MapLibre's default ordering).
    installSelectionHandle(map, selectionController);
    // Clip aggregation overlays to the active selection. Called
    // after the controller exists so ordering is: data layers →
    // mask → terra-draw layers, with the value-clipping wired in
    // after the fact (only mutates layer filters + feature-state).
    wireAggregationOverlayClipping(map, sources, selectionController);
    // The wiring functions return disposers for future SPA-teardown
    // / embed-remount paths. Today the map lives for the lifetime of
    // the page so we don't store them — call sites in the wiring
    // functions are testable in isolation against a stub map.
    _wireSelectionToolbar(selectionController);
    _wireSelectionPanel(selectionController);
    // Selection lives at session-only scope; switching AOI clears it
    // so a circle drawn in CDMX doesn't bleed into Mexicali's view.

    // Centre on the initial AOI's bounds (replaces the old hardcoded
    // CUAUHTEMOC_CENTER fly-to). Bail to fallback when no AOIs exist.
    if (initialAoi) flyToAoi(map, initialAoi);

    const toggleContainer = document.getElementById("layer-toggles");
    if (toggleContainer) {
      wireLayerToggles(toggleContainer, map);
      wireRasterToggles(toggleContainer, map, sources, getActiveAoiId());
    }

    const aoiSelect = document.getElementById("aoi-select") as HTMLSelectElement | null;
    if (aoiSelect) populateAoiSelect(aoiSelect);

    /**
     * Re-frame the map on the active AOI.
     *
     * The map already does this when the AOI *changes*; this is the way
     * back after panning away. With no basemap there is nothing in an empty
     * frame to navigate by, so "I have lost the data" is a state a user can
     * reach in one drag and could not previously leave.
     */
    const fitToActiveAoi = (): void => {
      const id = getActiveAoiId();
      const aoi = aois.find((a) => a.aoi_id === id);
      if (aoi) flyToAoi(map, aoi);
    };

    // React to any AOI change — picker, localStorage hydration, or a
    // future programmatic setActiveAoiId call. One central handler
    // keeps every consumer (map, tiles, ramp) in sync.
    subscribeToAoi(async (aoi) => {
      renderAoiInfo(aoi);
      if (aoi) flyToAoi(map, aoi);
      // Drain prior aggregation-overlay clipping disposers BEFORE the
      // selection clear and the rewire below, so each per-overlay
      // ``sourcedata`` listener is unbound exactly once. Without this
      // drain, every AOI switch leaks one listener per overlay —
      // benign at low-AOI counts, but a real per-tile cost over a
      // long session.
      clearAggregationOverlayClipping();
      // Baseline layers are AOI-scoped too (r039), so switching pilot
      // re-frames the spatial context instead of leaving every other
      // alcaldía drawn around the new AOI.
      refreshBaselineAoi(map, tileCatalog, aoi?.aoi_id ?? null);
      refreshAggregationOverlayAoi(map, sources, aoi?.aoi_id ?? null);
      // Switching AOI invalidates any active circle/rectangle/polygon
      // selection — a CDMX-drawn circle has no meaningful intersection
      // with a Mexicali viewport. Clear up-front so the metrics panel
      // doesn't keep showing stale numbers.
      selectionController.clear();
      // Re-wire the clipping for the new AOI's overlays. Mirrors the
      // initial install in the map.on("load") block.
      wireAggregationOverlayClipping(map, sources, selectionController);
      // Re-render the picker so AOI-scoped observation overlays
      // (``source_config.aois``) hide on AOIs they don't apply to.
      if (toggleContainer) {
        wireRasterToggles(toggleContainer, map, sources, aoi?.aoi_id ?? null);
      }
      // The indicator list is AOI-scoped, so it has to be re-fetched: an
      // indicator computed for CDMX may have no values in Mexicali, and
      // offering it would render an empty overlay.
      await refreshIndicatorList(aoi?.aoi_id ?? null);
      // Re-render the active indicator so its tile URL + colour ramp
      // reflect the new AOI scope. No active indicator = no work.
      if (state.activeIndicator) {
        await renderIndicator(state.activeIndicator);
      }
    });

    /**
     * Re-fetch the AOI-scoped indicator list and rebuild the picker.
     *
     * Declared as a hoisted function, and it resolves the ``<select>`` by id
     * rather than closing over one: the AOI store fires its subscriber
     * immediately on registration, which happens before the ``const select``
     * below is initialised, so a closure over it would hit the temporal dead
     * zone on the very first call.
     *
     * If the active indicator is absent from the new list, the overlay is
     * torn down rather than left painting the previous AOI's tiles.
     */
    async function refreshIndicatorList(aoiId: string | null): Promise<void> {
      try {
        indicators = await listIndicators(aoiId, state.activeScenarioId);
      } catch (err) {
        console.warn("indicator list refresh failed; keeping previous list", err);
        return;
      }
      const el = document.getElementById("indicator-select") as HTMLSelectElement | null;
      if (!el) return;
      const previous = state.activeIndicator?.slug ?? "";
      populateIndicatorSelect(el, indicators);
      const stillListed = indicators.some((i) => i.slug === previous);
      el.value = stillListed ? previous : "";
      if (previous && !stillListed) {
        state.activeIndicator = null;
        state.activeGrain = null;
        removeIndicator(map);
        const grainEl = document.getElementById("grain-toggle");
        if (grainEl) clearGrainToggle(grainEl);
        const legendEl = document.getElementById("legend");
        if (legendEl) clearLegend(legendEl);
        const downloadsEl = document.getElementById("downloads");
        if (downloadsEl) setDownloadAvailability(downloadsEl, false);
        setStatus(
          "Indicator cleared — it has no values in " +
            (state.activeScenarioId ? "this scenario" : "the baseline city") +
            " here.",
        );
      }
    }

    const select = document.getElementById("indicator-select") as HTMLSelectElement | null;
    const grainToggle = document.getElementById("grain-toggle");
    const legend = document.getElementById("legend");
    const downloads = document.getElementById("downloads");

    // Monotonic token bumped on every renderIndicator entry so a slow
    // stats fetch from request N doesn't overwrite the result of a
    // newer request N+1. Without this, switching AOI or indicator
    // rapidly leaves the map showing the older combination's ramp +
    // tile source — a subtle "stale state" bug Codex flagged.
    let renderToken = 0;

    async function renderIndicator(indicator: IndicatorSummary): Promise<void> {
      const myToken = ++renderToken;
      const aoiId = getActiveAoiId();
      setStatus(`Loading ${indicator.name} stats…`);
      const grain = state.activeGrain ?? defaultGrain(indicator) ?? undefined;
      let stats = null;
      try {
        stats = await getIndicatorStats(indicator.slug, {
          feature_type: grain,
          aoi_id: aoiId ?? undefined,
          // The ramp is read under the same scenario the tiles are, or the
          // colours describe a distribution the map is not drawing.
          scenario_id: state.activeScenarioId ?? undefined,
          // Asked for alongside the percentiles rather than in a second
          // request: it is the same scoped population and the same scan, and
          // a legend whose shape arrives after its numbers reads as a bug.
          bins: HISTOGRAM_BINS,
        });
      } catch (err) {
        console.warn("stats fetch failed, falling back to catalog bounds", err);
      }
      // If a newer call has arrived, drop this result silently — the
      // caller that bumped the token owns the next render.
      if (myToken !== renderToken) return;

      const domain = rampDomainFromStats(stats, indicator);
      activateIndicator(
        map, overlayConfig, indicator, domain,
        tooltipController, aoiId, selectionController, grain,
        state.activeScenarioId,
      );
      // The ramp is per-grain — an AGEB mean spans a much narrower
      // range than its child blocks — so the toggle re-renders with
      // the legend rather than once at pick time.
      if (grainToggle) {
        renderGrainToggle(grainToggle, indicator, grain ?? null, (picked) => {
          state.activeGrain = picked;
          void renderIndicator(indicator);
        });
      }
      const histogram = stats?.histogram ?? null;
      if (legend) renderLegend(legend, indicator, domain, histogram);
      // The hovered feature's place in the distribution. Registered after
      // the legend exists, and re-registered per render so it closes over
      // this indicator's histogram rather than a stale one.
      state.markHovered = (value: number | null): void => {
        if (legend) markLegendValue(legend, indicator, domain, histogram, value);
      };
      setValueHoverListener((value) => state.markHovered?.(value));
      if (downloads) setDownloadAvailability(downloads, true);
      setStatus(
        `Showing ${indicator.name} (${indicator.unit ?? "no unit"})` +
          (domain.fromStats
            ? ` · ramp ${domain.min.toFixed(2)}–${domain.max.toFixed(2)} (p05–p95).`
            : "."),
      );
    }

    if (select) {
      populateIndicatorSelect(select, indicators);
      select.addEventListener("change", async () => {
        const slug = select.value;
        if (!slug) {
          state.activeIndicator = null;
          state.activeGrain = null;
          removeIndicator(map);
          if (grainToggle) clearGrainToggle(grainToggle);
          if (legend) clearLegend(legend);
          if (downloads) setDownloadAvailability(downloads, false);
          setStatus("Indicator cleared.");
          return;
        }
        const indicator = indicators.find((i) => i.slug === slug);
        if (!indicator) return;
        state.activeIndicator = indicator;
        // Reset to the new indicator's finest grain: the previous
        // pick may not be in its applies_to at all, and asking for a
        // grain an indicator was never computed at returns an empty
        // overlay that looks like missing data.
        state.activeGrain = defaultGrain(indicator);
        await renderIndicator(indicator);
      });

      // Open on the first indicator rather than on "(none)". An empty overlay
      // is the same picture as an AOI with no data at all, so the map greeted
      // every visitor with the one view that cannot be told apart from a
      // broken one.
      //
      // Dispatching the event rather than calling `renderIndicator` directly:
      // the handler above already resets the grain, renders, wires the legend,
      // enables the downloads and sets the status line. Reaching past it would
      // mean keeping a second copy of that sequence in step with this one.
      if (!select.value && indicators.length > 0) {
        select.value = indicators[0].slug;
        select.dispatchEvent(new Event("change"));
      }
    }

    if (downloads) {
      wireDownloadButtons(
        downloads,
        () => state.activeIndicator?.slug ?? null,
        () => state.activeScenarioId,
      );
      setDownloadAvailability(downloads, false);
    }

    // ── Scenario authoring ────────────────────────────────────────────
    //
    // The panel owns creating a scenario, drawing rules on it and asking for
    // the recompute; what it hands back is one fact — which scenario the map
    // is showing — and this is where that fact turns into a view.
    //
    // Built after the indicator wiring above, because changing scenario
    // re-runs both halves of it: the list (a scenario's results are what make
    // the five edificability indicators offerable in the first place) and the
    // active overlay (whose tile URL, ramp and exports all carry the id).
    // Not captured, for the reason the selection wiring above gives: the
    // controller's handle is a disposer for a teardown this page does not
    // have. What the page needs from the panel arrives through the callback.
    enableScenarioPanel(map, {
      onScenarioChange: (scenarioId) => {
        void (async () => {
          state.activeScenarioId = scenarioId;
          await refreshIndicatorList(getActiveAoiId());
          if (state.activeIndicator) {
            await renderIndicator(state.activeIndicator);
          } else if (select && !select.value && indicators.length > 0) {
            // The baseline may have had nothing to offer here while the
            // scenario does — the edificability family exists only under one.
            // Opening on its first indicator beats an empty overlay, which is
            // the one view indistinguishable from a broken map.
            select.value = indicators[0].slug;
            select.dispatchEvent(new Event("change"));
          }
          refreshCommands();
        })();
      },
      activeIndicatorSlug: () => state.activeIndicator?.slug ?? null,
      activeIndicatorUnit: () => state.activeIndicator?.unit ?? null,
    });

    // ── Command palette ───────────────────────────────────────────────
    //
    // Built after the catalogs resolve, because most of what it offers *is*
    // the catalogs: every AOI, every indicator, every layer toggle. It is
    // also the only surface in the app that can list what the app does —
    // the draw tools are 28 px glyphs and the rest is three `<select>`s in
    // a 1,173 px scroll.
    //
    // Rebuilt whenever the AOI changes, since the indicator list is scoped
    // to the AOI and a palette offering an indicator with no values here
    // would hand back an empty map.
    const buildCommands = (): Command[] => {
      const list: Command[] = [
        {
          id: "view.fit",
          title: "Fit map to the active AOI",
          group: "View",
          hint: "f",
          keywords: "zoom extent frame",
          run: () => fitToActiveAoi(),
        },
        {
          id: "view.panel",
          title: sidebar?.collapsed() ? "Show panel" : "Hide panel",
          group: "View",
          hint: "\\",
          keywords: "sidebar collapse",
          run: () => sidebar?.toggle(),
        },
        {
          id: "view.north",
          title: "Reset bearing to north",
          group: "View",
          keywords: "rotate compass orientation",
          run: () => {
            map.easeTo({ bearing: 0, pitch: 0 });
          },
        },
      ];

      for (const shape of ["rectangle", "polygon", "circle"] as const) {
        list.push({
          id: `draw.${shape}`,
          title: `Draw ${shape} selection`,
          group: "Select",
          hint: shape[0],
          keywords: "area sub-aoi metrics",
          run: () => selectionController?.startDrawing(shape),
        });
      }
      list.push({
        id: "draw.clear",
        title: "Clear selection",
        group: "Select",
        hint: "esc",
        run: () => selectionController?.clear(),
      });

      const activeId = getActiveAoiId();
      for (const aoi of aois) {
        list.push({
          id: `aoi.${aoi.aoi_id}`,
          title: aoi.name,
          group: "Area of interest",
          active: aoi.aoi_id === activeId,
          keywords: "aoi city territory place",
          run: () => {
            if (!aoiSelect) return;
            aoiSelect.value = aoi.aoi_id;
            aoiSelect.dispatchEvent(new Event("change"));
          },
        });
      }

      for (const indicator of indicators) {
        list.push({
          id: `indicator.${indicator.slug}`,
          title: indicator.name,
          group: "Indicator",
          hint: indicator.unit ?? undefined,
          active: indicator.slug === state.activeIndicator?.slug,
          keywords: indicator.slug,
          run: () => {
            if (!select) return;
            select.value = indicator.slug;
            select.dispatchEvent(new Event("change"));
          },
        });
      }

      for (const toggle of document.querySelectorAll<HTMLInputElement>(
        "#layer-toggles input[type=checkbox]",
      )) {
        const name =
          toggle.closest("label")?.textContent?.trim() ?? toggle.value;
        list.push({
          id: `layer.${toggle.id || name}`,
          title: `${toggle.checked ? "Hide" : "Show"} ${name}`,
          group: "Layer",
          active: toggle.checked,
          keywords: "toggle visibility",
          run: () => toggle.click(),
        });
      }

      for (const [title, href] of [
        ["Indicator catalogue", "/catalog.html"],
        ["Run dashboard", "/ops.html"],
      ] as const) {
        list.push({
          id: `go.${href}`,
          title,
          group: "Go to",
          keywords: "navigate page",
          run: () => {
            window.location.href = href;
          },
        });
      }
      return list;
    };

    const palette = enablePalette(buildCommands(), SHORTCUTS);
    // The command list describes current state — which layer is on, which
    // indicator is drawn — so it is rebuilt on open rather than cached.
    const refreshCommands = (): void => palette.setCommands(buildCommands());
    subscribeToAoi(() => refreshCommands());

    wireShortcuts({
      onPalette: () => {
        refreshCommands();
        palette.open();
      },
      onFit: () => fitToActiveAoi(),
      onPanel: () => sidebar?.toggle(),
      onDraw: (shape) => selectionController?.startDrawing(shape),
      onClear: () => selectionController?.clear(),
      isPaletteOpen: () => palette.isOpen(),
    });

    setStatus("Map ready. Press ⌘K for commands, ? for shortcuts.");
  });
  map.on("error", (e) => {
    setStatus(`Map error: ${e.error?.message ?? "unknown"}`);
  });
}


function _wireSelectionToolbar(controller: SelectionController): () => void {
  const toolbar = document.querySelector(".selection-toolbar");
  if (!toolbar) return () => undefined;
  const buttons = Array.from(
    toolbar.querySelectorAll<HTMLButtonElement>(".selection-toolbar__btn"),
  );
  // Active-mode highlighting: the button matching the current draw
  // shape gets ``aria-pressed=true``. Cleared on every clear/finish
  // so the user always sees the toolbar's truth.
  //
  // ⚠ Only buttons that name a shape take part. This read
  // `btn.dataset.shape ?? null` and compared that to the active shape, so
  // the clear button — which carries `data-action="clear"` and no
  // `data-shape` — matched `null === null` at rest and was pressed. Its
  // resting state was therefore solid cyan via
  // `.selection-toolbar__btn[aria-pressed="true"]`: a palette of three draw
  // modes showing "clear" as the selected one, and a false `aria-pressed`
  // read aloud to anyone using a screen reader.
  //
  // `aria-pressed` is removed rather than set to "false" on a button that is
  // not a toggle: a button that is never pressed is not a toggle button, and
  // saying so is the difference between "not currently active" and "cannot
  // be active".
  const setPressed = (shape: SelectionShape | null): void => {
    for (const btn of buttons) {
      const target = btn.dataset.shape;
      if (target === undefined) {
        btn.removeAttribute("aria-pressed");
        continue;
      }
      btn.setAttribute("aria-pressed", target === shape ? "true" : "false");
    }
  };
  setPressed(null);

  // Stash the per-button click handler so dispose can detach each
  // listener cleanly. Without this, repeated mounts (HMR, future
  // map-recreation paths) accumulate stacked handlers on the same
  // DOM nodes.
  const clickHandlers = new Map<HTMLButtonElement, () => void>();
  for (const btn of buttons) {
    const onClick = (): void => {
      if (btn.dataset.action === "clear") {
        controller.clear();
        setPressed(null);
        return;
      }
      const shape = btn.dataset.shape as SelectionShape | undefined;
      if (!shape) return;
      controller.startDrawing(shape);
      setPressed(shape);
    };
    btn.addEventListener("click", onClick);
    clickHandlers.set(btn, onClick);
  }
  // When a shape is finished or cleared via the map (Esc, double-
  // click), the toolbar still needs to reflect "no active shape" —
  // subscribe so the visual stays honest.
  const unsubscribe = controller.subscribe((sel) => {
    if (sel === null) setPressed(null);
  });

  return () => {
    for (const [btn, onClick] of clickHandlers) {
      btn.removeEventListener("click", onClick);
    }
    clickHandlers.clear();
    unsubscribe();
  };
}


function _wireSelectionPanel(
  controller: SelectionController,
): () => void {
  const panel = document.getElementById("selection-panel");
  const list = panel?.querySelector(".selection-panel__metrics") as HTMLDListElement | null;
  const totalValue = panel?.querySelector(".selection-panel__total-value") as HTMLSpanElement | null;
  if (!panel || !list) return () => undefined;

  // Default metric specs derived from INPUT_LAYERS. Each one binds a
  // user-facing label to the canonical ``type_slug`` recognised by
  // the /metrics/within-polygon endpoint. Future grain additions
  // land in the panel automatically by extending INPUT_LAYERS — no
  // wiring change here.
  const specs: MetricSpec[] = INPUT_LAYERS.map((l) => ({
    label: `${l.label} selected`,
    typeSlug: l.typeSlug,
  }));

  // Supersede in-flight metrics requests when a newer selection
  // arrives. Same pattern as ``renderIndicator``'s ``renderToken``
  // — without it, a slow request from N can clobber the result of a
  // newer request N+1, leaving the panel showing the wrong polygon's
  // numbers.
  let inFlight: AbortController | null = null;

  const render = async (selection: SelectionGeometry | null): Promise<void> => {
    // Abort any in-flight request: either we have a new selection or
    // the user cleared the panel; the previous result is no longer
    // wanted regardless.
    if (inFlight !== null) {
      inFlight.abort();
      inFlight = null;
    }
    if (selection === null) {
      panel.hidden = true;
      list.innerHTML = "";
      if (totalValue) totalValue.textContent = "0";
      return;
    }
    const aoiId = getActiveAoiId();
    if (aoiId === null) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    if (totalValue) totalValue.textContent = "…";

    const ctrl = new AbortController();
    inFlight = ctrl;
    // Progressive rendering: results stream in as each per-type
    // request resolves. Small layers (block ~50 ms, ageb ~20 ms,
    // muni ~10 ms) appear in the panel before the heavy buildings
    // request settles. The user sees three-of-four numbers in
    // under 100 ms even when buildings are still in flight.
    const accumulated = new Map<string, MetricResult>();
    const repaint = (): void => {
      let total = 0;
      for (const r of accumulated.values()) total += r.featuresCounted;
      if (totalValue) totalValue.textContent = total.toLocaleString();
      // Show every spec that's resolved — including zero-count rows
      // (display "0") and per-spec errors (display "—"). Hiding
      // zero-counts conflated "no features matched" with "fetch
      // failed" and left the user wondering whether the panel was
      // mid-load or done. Per-spec rows are cheap (one row per
      // INPUT_LAYER) so showing all of them is fine.
      const rows = [...accumulated.values()];
      list.innerHTML = rows
        .map(
          (r) =>
            `<dt>${escapeHtml(r.spec.label)}</dt><dd>${escapeHtml(r.display)}</dd>`,
        )
        .join("");
    };
    const onResult = (result: MetricResult): void => {
      // Drop if a newer request superseded this one while we waited.
      if (inFlight !== ctrl) return;
      accumulated.set(result.spec.typeSlug, result);
      repaint();
    };
    try {
      await streamMetrics(selection, specs, aoiId, onResult, ctrl.signal);
    } catch (err) {
      // Subscriber callbacks are fire-and-forget, so a rethrow here
      // lands as an unhandled rejection nobody sees. Per-spec failures
      // already render as "—" in the panel; log the rest.
      if (!isAbortError(err)) console.warn("selection metrics failed:", err);
    }
    if (inFlight === ctrl) inFlight = null;
  };

  // Debounced subscription. The endpoint is fast (sub-100 ms for a
  // small polygon) but firing one HTTP request per terra-draw change
  // event during a drag burns network + DB cycles for results the
  // user never sees. 150 ms trailing matches the visual cadence the
  // operator perceives as "live".
  const unsubscribe = controller.subscribe(
    (sel) => { void render(sel); },
    { debounceMs: 150 },
  );

  return () => {
    if (inFlight !== null) {
      inFlight.abort();
      inFlight = null;
    }
    unsubscribe();
  };
}


void main();

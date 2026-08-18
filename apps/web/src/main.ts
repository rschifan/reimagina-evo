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
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
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
  listAois,
  listIndicators,
  listTileSources,
} from "./api";
import {
  activateIndicator,
  choroplethConfig,
  clearGrainToggle,
  clearLegend,
  defaultGrain,
  populateIndicatorSelect,
  rampDomainFromStats,
  removeIndicator,
  renderGrainToggle,
  renderLegend,
} from "./choropleth";
import {
  setDownloadAvailability,
  wireDownloadButtons,
} from "./download";
import { escapeHtml } from "./format";
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
  /** Feature type the choropleth is currently drawn at. Defaults to
   *  the indicator's finest grain and is re-derived on every pick, so
   *  a coarse choice never leaks onto an indicator that lacks it. */
  activeGrain: string | null;
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

  const state: AppState = { activeIndicator: null, activeGrain: null };

  const map = new maplibregl.Map({
    container: "map",
    style: emptyStyle(),
    center: FALLBACK_CENTER,
    zoom: FALLBACK_ZOOM,
    maxZoom: 18,
    minZoom: 5,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

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
        indicators = await listIndicators(aoiId);
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
        setStatus("Indicator cleared — it has no values in this AOI.");
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
      if (legend) renderLegend(legend, indicator, domain);
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
    }

    if (downloads) {
      wireDownloadButtons(downloads, () => state.activeIndicator?.slug ?? null);
      setDownloadAvailability(downloads, false);
    }

    setStatus("Map ready. Spatial input layers loaded.");
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
  const setPressed = (shape: SelectionShape | null): void => {
    for (const btn of buttons) {
      const target = btn.dataset.shape ?? null;
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

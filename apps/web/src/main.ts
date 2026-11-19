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
 *   - CSV / GeoJSON / GeoParquet download links once an indicator is
 *     selected, scoped to the AOI and grain on the map.
 */
// The map itself — style, worker, controls, attribution — is built in
// `map-base.ts`, which the scenario workspace's map shares.
import "maplibre-gl/dist/maplibre-gl.css";
import { mount } from "svelte";

import TopBar from "./lib/TopBar.svelte";
import "./topbar.css";
import type { AOIRow, FeatureIndicatorValue, IndicatorSummary, TileSource } from "./api";
import { createBanner } from "./banner";
import { createMap, flyToAoi, mapErrorBanner } from "./map-base";

import {
  getActiveAoiId,
  populateAoiSelect,
  setKnownAois,
  subscribe as subscribeToAoi,
} from "./aoi";
import {
  fetchMetricsWithinPolygon,
  getFeatureIndicators,
  getIndicatorStats,
  indicatorWithinPolygon,
  isAbortError,
  listAois,
  listIndicators,
  listTileSources,
} from "./api";
import {
  activateIndicator,
  choroplethConfig,
  setValueHoverListener,
  clearGrainToggle,
  clearLegend,
  defaultGrain,
  overlayFeatureAt,
  populateIndicatorSelect,
  rampDomainFromStats,
  removeIndicator,
  renderGrainToggle,
  renderLegend,
  setOverlayPinned,
} from "./choropleth";
import { HISTOGRAM_BINS, markLegendValue } from "./distribution";
import { grainCount } from "./format";
import { renderDownloads } from "./download";
import { renderInspector } from "./inspector";
import type { Command } from "./commands";
import { enablePalette } from "./palette";
import { SHORTCUTS, wireShortcuts } from "./shortcuts";
import { enableSidebar, type SidebarController } from "./sidebar";
import {
  extractTileSources,
  installInputLayers,
  installAggregationOverlays,
  installRasterLayers,
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
import {
  INITIAL_CARD,
  nextCardState,
  renderSelectionCard,
  renderingContext,
  type CardContext,
  type SelectionStats,
} from "./selection-card";
import { wireSheet } from "./sheet";
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
  /** The grain the overlay is actually drawn at (set once it is drawn), so
   *  a pin knows what it pinned. */
  drawnGrain: string | null;
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


/** The banner over the map: loading and errors only, one channel per writer. */
const setBanner = createBanner(document.getElementById("map-banner"));




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




/** The picked indicator's description and its catalog link, or nothing. */
function renderIndicatorInfo(indicator: IndicatorSummary | null): void {
  const desc = document.getElementById("indicator-description");
  if (desc) {
    desc.textContent = indicator?.description ?? "";
    // Clamped to four lines in CSS; the title carries the rest.
    desc.title = desc.textContent;
    desc.hidden = !indicator?.description;
  }
  const about = document.getElementById("indicator-about") as HTMLAnchorElement | null;
  if (about) {
    about.hidden = indicator === null;
    if (indicator) about.href = `/catalog.html#/i/${encodeURIComponent(indicator.slug)}`;
  }
}


async function main(): Promise<void> {
  // The nav bar is independent of every API call below, so mount it first:
  // it must be there even if the API is down and this function throws.
  const topbar = document.getElementById("topbar");
  if (topbar) mount(TopBar, { target: topbar, props: { current: "map" } });

  setBanner("loading", "Connecting to the reimagina API…");

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
    setBanner("loading", null);
    setBanner("api", `Could not reach the API: ${(err as Error).message}`);
    if (loadingHost) loadingHost.setAttribute("data-loading", "false");
    throw err;
  }
  setBanner("loading", null);
  const tileCatalog = extractTileSources(sources);
  const overlayConfig = choroplethConfig(sources);

  // The AOI store was seeded inside loadInitialState (the indicator fetch
  // needs the resolved AOI), so the active id is already authoritative here.
  const initialAoi = aois.find((a) => a.aoi_id === getActiveAoiId()) ?? null;

  const state: AppState = {
    activeIndicator: null,
    activeGrain: null,
    drawnGrain: null,
  };

  const map = createMap("map", FALLBACK_CENTER, FALLBACK_ZOOM);

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
    _wireSelectionToolbar(selectionController);
    // Selection lives at session-only scope; switching AOI clears it
    // so a circle drawn in CDMX doesn't bleed into Mexicali's view.

    // Centre on the initial AOI's bounds (replaces the old hardcoded
    // CUAUHTEMOC_CENTER fly-to). Bail to fallback when no AOIs exist.
    if (initialAoi) flyToAoi(map, initialAoi);

    const toggleContainer = document.getElementById("layer-toggles");
    // "N on" beside the collapsed Layers heading, so a layer left on is not
    // hidden behind a closed group. Every toggle is a checkbox whose change
    // bubbles here, the palette's included.
    const updateLayersSummary = (): void => {
      const el = document.getElementById("layers-summary");
      if (el && toggleContainer) {
        el.textContent = `${toggleContainer.querySelectorAll("input[type=checkbox]:checked").length} on`;
      }
    };
    if (toggleContainer) {
      wireLayerToggles(toggleContainer, map);
      wireRasterToggles(toggleContainer, map, sources, getActiveAoiId());
      toggleContainer.addEventListener("change", updateLayersSummary);
      updateLayersSummary();
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

    // ── Block inspector ─────────────────────────────────────────────
    //
    // A click on the overlay pins the block: outline, card, one request
    // for its values. Declared before the AOI subscription, which fires
    // on registration and closes the card.
    const inspectorEl = document.getElementById("inspector");
    let pinned: { id: string; grain: string | null; rows: FeatureIndicatorValue[] | string } | null = null;
    let pinRequest: AbortController | null = null;
    // What the selection card asks about: the indicator being rendered, at its
    // grain, and once its stats arrive, the legend's AOI-wide median.
    let cardContext: CardContext = renderingContext(null, null);

    const selectionCard = _wireSelectionCard(selectionController, () => cardContext);

    const raiseSheet = wireSheet(inspectorEl, document.getElementById("selection-panel"));

    const showInspector = (): void => {
      if (!inspectorEl || !pinned) return;
      renderInspector(
        inspectorEl, pinned, pinned.rows, state.activeIndicator?.slug ?? null,
        (slug) => {
          const el = document.getElementById("indicator-select") as HTMLSelectElement | null;
          // A value the picker does not list would set it to "(none)".
          if (!el || !indicators.some((i) => i.slug === slug)) return;
          el.value = slug;
          el.dispatchEvent(new Event("change"));
        },
        closeInspector,
      );
    };

    function closeInspector(): void {
      pinRequest?.abort();
      pinRequest = null;
      if (pinned) setOverlayPinned(map, overlayConfig, pinned.id, false);
      pinned = null;
      if (inspectorEl) {
        inspectorEl.hidden = true;
        inspectorEl.replaceChildren();
      }
    }

    async function pin(id: string): Promise<void> {
      closeInspector();
      const mine: NonNullable<typeof pinned> = { id, grain: state.drawnGrain, rows: "Loading…" };
      pinned = mine;
      setOverlayPinned(map, overlayConfig, id, true);
      showInspector();
      raiseSheet("inspector");
      const ctrl = new AbortController();
      pinRequest = ctrl;
      try {
        mine.rows = await getFeatureIndicators(id, {
          aoi_id: getActiveAoiId() ?? undefined,
          signal: ctrl.signal,
        });
      } catch (err) {
        if (isAbortError(err)) return;
        console.warn("feature values failed:", err);
        // In the card, not the banner: it is about this block, not the map.
        mine.rows = "Could not load this block's values.";
      }
      if (ctrl.signal.aborted) return;
      pinRequest = null;
      showInspector();
    }

    // A click on a block pins it; a click on empty map closes the card. A
    // click the selection owns (drawing, or on one of its handles) does
    // neither.
    map.on("click", (e) => {
      if (selectionController.ownsClick(e.point)) return;
      const id = overlayFeatureAt(map, e.point);
      if (id === null) closeInspector();
      else if (id !== pinned?.id) void pin(id);
    });

    // React to any AOI change — picker, localStorage hydration, or a
    // future programmatic setActiveAoiId call. One central handler
    // keeps every consumer (map, tiles, ramp) in sync.
    subscribeToAoi(async (aoi) => {
      // A pinned block belongs to the AOI it was clicked in; this also
      // aborts its request.
      closeInspector();
      if (aoi) flyToAoi(map, aoi);
      // Baseline layers are AOI-scoped too (r039), so switching pilot
      // re-frames the spatial context instead of leaving every other
      // alcaldía drawn around the new AOI.
      refreshBaselineAoi(map, tileCatalog, aoi?.aoi_id ?? null);
      refreshAggregationOverlayAoi(map, sources, aoi?.aoi_id ?? null);
      // Switching AOI invalidates any active circle/rectangle/polygon
      // selection — a CDMX-drawn circle has no meaningful intersection
      // with a Mexicali viewport. Clearing also clears the selection card.
      selectionController.clear();
      // Re-render the picker so AOI-scoped observation overlays
      // (``source_config.aois``) hide on AOIs they don't apply to.
      if (toggleContainer) {
        wireRasterToggles(toggleContainer, map, sources, aoi?.aoi_id ?? null);
        updateLayersSummary();
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
      if (previous && !stillListed) clearIndicator();
    }

    /** Take the overlay and everything describing it off the page. Resolves
     *  its elements by id for the same hoisting reason as above. */
    function clearIndicator(): void {
      closeInspector();
      state.activeIndicator = null;
      state.activeGrain = null;
      cardContext = renderingContext(null, null);
      selectionCard.refresh();
      setBanner("values", null);
      removeIndicator(map);
      renderIndicatorInfo(null);
      const grainEl = document.getElementById("grain-toggle");
      if (grainEl) clearGrainToggle(grainEl);
      const legendEl = document.getElementById("map-legend");
      if (legendEl) clearLegend(legendEl);
      const downloadsEl = document.getElementById("downloads");
      if (downloadsEl) renderDownloads(downloadsEl, null, { aoiId: null, grain: null });
    }

    const select = document.getElementById("indicator-select") as HTMLSelectElement | null;
    const grainToggle = document.getElementById("grain-toggle");
    const legend = document.getElementById("map-legend");
    const downloads = document.getElementById("downloads");

    // Monotonic token bumped on every renderIndicator entry so a slow
    // stats fetch from request N doesn't overwrite the result of a
    // newer request N+1. Without this, switching AOI or indicator
    // rapidly leaves the map showing the older combination's ramp +
    // tile source — a subtle "stale state" bug.
    let renderToken = 0;

    async function renderIndicator(indicator: IndicatorSummary): Promise<void> {
      const myToken = ++renderToken;
      const aoiId = getActiveAoiId();
      const grain = state.activeGrain ?? defaultGrain(indicator) ?? undefined;
      renderIndicatorInfo(indicator);
      // The selection card dims now and asks once the stats land, so its numbers
      // never arrive ahead of the legend and map they are read against.
      cardContext = renderingContext(indicator, grain ?? null);
      selectionCard.dim();
      setBanner("loading", `Loading ${indicator.name}…`);
      let stats = null;
      try {
        stats = await getIndicatorStats(indicator.slug, {
          feature_type: grain,
          aoi_id: aoiId ?? undefined,
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
      activateIndicator(map, overlayConfig, indicator, domain, {
        tooltips: tooltipController, aoiId, grain, pinHint: true,
      });
      state.drawnGrain = grain ?? null;
      // The new source has no feature-state: re-outline the pinned block and
      // show the new indicator's rank. A pin from another grain is a
      // different feature, so it closes.
      if (pinned) {
        if (pinned.grain === state.drawnGrain) {
          setOverlayPinned(map, overlayConfig, pinned.id, true);
          showInspector();
        } else {
          closeInspector();
        }
      }
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
      if (legend) {
        renderLegend(legend, indicator, domain, histogram, stats?.count_at_cap ?? null, stats?.count ?? 0);
      }
      // The hovered feature's place in the distribution. Registered after
      // the legend exists, and re-registered per render so it closes over
      // this indicator's histogram rather than a stale one.
      state.markHovered = (value: number | null): void => {
        if (legend) markLegendValue(legend, histogram, value);
      };
      setValueHoverListener((value) => state.markHovered?.(value));
      if (downloads) renderDownloads(downloads, indicator.slug, { aoiId, grain: grain ?? null });
      // What a download holds: the stats count is the same scoped population.
      const scopeEl = document.getElementById("downloads-scope");
      if (scopeEl) {
        const aoiName = aois.find((a) => a.aoi_id === aoiId)?.name;
        scopeEl.textContent = stats
          ? grainCount(stats.count, grain ?? null) +
            (aoiName ? ` · ${aoiName} only` : "")
          : "";
      }
      setBanner("loading", null);
      // Cleared by the next successful stats load, for any indicator.
      setBanner(
        "values",
        stats ? null : `Could not load the values of ${indicator.name}; colours use the catalog range.`,
      );
      cardContext = { ...cardContext, aoiStats: stats };
      selectionCard.refresh();
    }

    if (select) {
      populateIndicatorSelect(select, indicators);
      select.addEventListener("change", async () => {
        const slug = select.value;
        if (!slug) {
          clearIndicator();
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
      // the handler above already resets the grain, renders, wires the legend
      // and points the downloads at it. Reaching past it would
      // mean keeping a second copy of that sequence in step with this one.
      //
      // `?indicator=<slug>` picks it instead — the catalog's "Open on map".
      // A slug with no values in this AOI is not in the picker, so it falls
      // back to the first one rather than to an empty map.
      if (!select.value && indicators.length > 0) {
        const wanted = new URLSearchParams(location.search).get("indicator");
        select.value = indicators.some((i) => i.slug === wanted)
          ? (wanted as string)
          : indicators[0].slug;
        select.dispatchEvent(new Event("change"));
      }
    }

    // ── Command palette ───────────────────────────────────────────────
    //
    // Built after the catalogs resolve, because most of what it offers *is*
    // the catalogs: every AOI, every indicator, every layer toggle. It is
    // also the only surface in the app that can list what the app does —
    // the draw tools are 28 px glyphs and the layer toggles sit in a
    // collapsed group.
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
      // Esc closes the block card first; the next Esc clears the selection.
      onClear: () => (pinned ? closeInspector() : selectionController?.clear()),
      isPaletteOpen: () => palette.isOpen(),
    });
  });
  const mapError = mapErrorBanner(
    (message) => setBanner("map", message),
    () => setBanner("map", null),
  );
  map.on("error", (e) => mapError.error(`The map could not load some data: ${e.error?.message ?? "unknown error"}`));
  map.on("idle", () => mapError.idle());
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


/**
 * Keep the selection card in step with the selection: one request per settled
 * change (150 ms debounce), the previous one aborted, the previous numbers
 * kept on screen, dimmed, until the answer replaces them. Returns `dim`, for
 * when the indicator or grain starts changing under a standing selection (no
 * request), and `refresh`, to ask again once its AOI stats are in.
 */
function _wireSelectionCard(
  controller: SelectionController,
  current: () => CardContext,
): { dim: () => void; refresh: () => void } {
  const card = document.getElementById("selection-panel");
  if (!card) return { dim: () => undefined, refresh: () => undefined };
  let state = INITIAL_CARD;
  // What the numbers on screen were fetched for: stale numbers keep their unit.
  let shown: CardContext = renderingContext(null, null);
  let inFlight: AbortController | null = null;
  const paint = (): void => renderSelectionCard(card, state, shown);

  const update = async (selection: SelectionGeometry | null): Promise<void> => {
    inFlight?.abort();
    inFlight = null;
    const aoiId = getActiveAoiId();
    if (selection === null || aoiId === null) {
      state = nextCardState(state, { type: "cleared" });
      paint();
      return;
    }
    state = nextCardState(state, { type: "selection-changed" });
    paint();
    const token = state.token;
    const ctx = current();
    const ctrl = new AbortController();
    inFlight = ctrl;
    const polygon = selection.feature.geometry;
    let stats: SelectionStats | null = null;
    try {
      if (ctx.indicator) {
        stats = await indicatorWithinPolygon({
          aoi_id: aoiId,
          polygon,
          indicator_slug: ctx.indicator.slug,
          feature_type: ctx.grain ?? undefined,
        }, { signal: ctrl.signal });
      } else {
        const res = await fetchMetricsWithinPolygon(
          { aoi_id: aoiId, polygon, type_slugs: ["block"] },
          { signal: ctrl.signal },
        );
        stats = { count: res.results.find((r) => r.type_slug === "block")?.count ?? 0 };
      }
    } catch (err) {
      if (isAbortError(err)) return;
      // Shown in the card as a one-line failure; the detail goes to the console.
      console.warn("selection summary failed:", err);
    }
    if (inFlight === ctrl) inFlight = null;
    const next = nextCardState(state, { type: "response", token, stats });
    if (next === state) return;
    state = next;
    shown = ctx;
    paint();
  };

  controller.subscribe((sel) => { void update(sel); }, { debounceMs: 150 });
  return {
    dim: () => {
      inFlight?.abort();
      inFlight = null;
      state = nextCardState(state, { type: "context-changed" });
      paint();
    },
    refresh: () => { void update(controller.getSelection()); },
  };
}


void main();

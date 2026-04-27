/**
 * Entry point for the reimagina read-side web app.
 *
 * Boots MapLibre with a minimal offline style (no basemap — the
 * prototype avoids external tile dependencies and licensing drift),
 * loads the tile-source catalog + indicator catalog from the API, and
 * wires:
 *   - AOI picker (Cuauhtémoc / Tlalpan / Mexicali / …) → flies the map
 *     and re-scopes server-side tile filters via aoi_id.
 *   - Spatial-input layer toggles served by Martin's serving_geo.get_tile.
 *   - Indicator picker → server-side MVT choropleth via Martin's
 *     serving_geo.indicator_overlay_tile function.
 *   - CSV / GeoJSON / GeoParquet download buttons once an indicator is
 *     selected.
 */
import maplibregl from "maplibre-gl";
import type { AOIRow, IndicatorSummary, TileSource } from "./api";
import type { StyleSpecification } from "maplibre-gl";

import {
  getActiveAoiId,
  populateAoiSelect,
  setKnownAois,
  subscribe as subscribeToAoi,
} from "./aoi";
import { getIndicatorStats, listAois, listIndicators, listTileSources } from "./api";
import {
  activateIndicator,
  choroplethConfig,
  clearLegend,
  defaultGrain,
  populateIndicatorSelect,
  rampDomainFromStats,
  removeIndicator,
  renderLegend,
} from "./choropleth";
import {
  setDownloadAvailability,
  wireDownloadButtons,
} from "./download";
import {
  extractTileSources,
  installInputLayers,
  installAggregationOverlays,
  installRasterLayers,
  refreshAggregationOverlayAoi,
  wireLayerToggles,
  wireRasterToggles,
} from "./layers";
import { TooltipController } from "./tooltip";

// Fallback centre when no AOIs come back from the API. CDMX-Cuauhtémoc
// remains a reasonable default for the prototype's first deployment.
const FALLBACK_CENTER: [number, number] = [-99.137, 19.432];
const FALLBACK_ZOOM = 12;

interface AppState {
  activeIndicator: IndicatorSummary | null;
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
  const [sources, indicators, aois] = await Promise.all([
    listTileSources(),
    listIndicators(),
    listAois(),
  ]);
  return { sources, indicators, aois };
}


function flyToAoi(map: maplibregl.Map, aoi: AOIRow): void {
  if (!aoi.bbox) return;
  const [minLon, minLat, maxLon, maxLat] = aoi.bbox;
  const lonSpan = Math.abs(maxLon - minLon);
  const latSpan = Math.abs(maxLat - minLat);
  // Wide AOIs (Mexicali = 1.4°, ~100 km) make fitBounds zoom out far
  // enough that the analysis-unit layers stop rendering — block /
  // AGEB minzoom is 11 in INPUT_LAYERS. Centring on the bbox centroid
  // at z=11 gives the user a usable view immediately, even if some of
  // the AOI is offscreen.
  const WIDE_AOI_DEGREE_THRESHOLD = 0.5;
  if (lonSpan > WIDE_AOI_DEGREE_THRESHOLD || latSpan > WIDE_AOI_DEGREE_THRESHOLD) {
    map.flyTo({
      center: [(minLon + maxLon) / 2, (minLat + maxLat) / 2],
      zoom: 11,
      duration: 700,
    });
    return;
  }
  // Tight AOIs (Cuauhtémoc, Tlalpan) fit naturally above z=11 — let
  // fitBounds frame the polygon with a small padding for breathing room.
  map.fitBounds(
    [
      [minLon, minLat],
      [maxLon, maxLat],
    ],
    { padding: 40, duration: 700, maxZoom: 14 },
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
  const desc = aoi.description ? `<div class="aoi-info-desc">${_escapeHtml(aoi.description)}</div>` : "";
  el.innerHTML = `<div class="aoi-info-name">${_escapeHtml(aoi.name)}</div>${desc}`;
}


function _escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}


async function main(): Promise<void> {
  setStatus("Connecting to reimagina API…");

  let sources: TileSource[];
  let indicators: IndicatorSummary[];
  let aois: AOIRow[];
  try {
    ({ sources, indicators, aois } = await loadInitialState());
  } catch (err) {
    setStatus(`Failed to load API: ${(err as Error).message}`);
    throw err;
  }
  const tileCatalog = extractTileSources(sources);
  const overlayConfig = choroplethConfig(sources);

  // Seed the AOI store before anything else so initial tile-source
  // installs and the first map fitBounds use the resolved active AOI.
  setKnownAois(aois);
  const initialAoi = aois.find((a) => a.aoi_id === getActiveAoiId()) ?? null;

  const state: AppState = { activeIndicator: null };

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

  map.on("load", () => {
    // Tooltip controller is the only thing that knows how to render
    // hover content. Providers are registered by the installers below
    // so each layer type declares its own "what do I show on hover"
    // in one place — main.ts stays agnostic of per-layer details.
    const tooltipController = new TooltipController(map, map.getContainer());

    installInputLayers(map, tileCatalog, tooltipController);
    installRasterLayers(map, sources, tooltipController);
    installAggregationOverlays(map, sources, tooltipController, getActiveAoiId());

    // Centre on the initial AOI's bounds (replaces the old hardcoded
    // CUAUHTEMOC_CENTER fly-to). Bail to fallback when no AOIs exist.
    if (initialAoi) flyToAoi(map, initialAoi);

    const toggleContainer = document.getElementById("layer-toggles");
    if (toggleContainer) {
      wireLayerToggles(toggleContainer, map);
      wireRasterToggles(toggleContainer, map, sources);
    }

    const aoiSelect = document.getElementById("aoi-select") as HTMLSelectElement | null;
    if (aoiSelect) populateAoiSelect(aoiSelect);

    // React to any AOI change — picker, localStorage hydration, or a
    // future programmatic setActiveAoiId call. One central handler
    // keeps every consumer (map, tiles, ramp) in sync.
    subscribeToAoi(async (aoi) => {
      renderAoiInfo(aoi);
      if (aoi) flyToAoi(map, aoi);
      refreshAggregationOverlayAoi(map, sources, aoi?.aoi_id ?? null);
      // Re-render the active indicator so its tile URL + colour ramp
      // reflect the new AOI scope. No active indicator = no work.
      if (state.activeIndicator) {
        await renderIndicator(state.activeIndicator);
      }
    });

    const select = document.getElementById("indicator-select") as HTMLSelectElement | null;
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
      const grain = defaultGrain(indicator) ?? undefined;
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
      activateIndicator(map, overlayConfig, indicator, domain, tooltipController, aoiId);
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
          removeIndicator(map);
          if (legend) clearLegend(legend);
          if (downloads) setDownloadAvailability(downloads, false);
          setStatus("Indicator cleared.");
          return;
        }
        const indicator = indicators.find((i) => i.slug === slug);
        if (!indicator) return;
        state.activeIndicator = indicator;
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


void main();

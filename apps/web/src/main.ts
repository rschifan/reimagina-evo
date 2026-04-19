/**
 * Entry point for the reimagina read-side web app.
 *
 * Boots MapLibre with a minimal offline style (no basemap — the
 * prototype avoids external tile dependencies and licensing drift),
 * loads the tile-source catalog + indicator catalog from the API, and
 * wires:
 *   - Spatial-input layer toggles (municipalities / AGEBs / blocks /
 *     buildings) served by Martin's serving_geo.get_tile.
 *   - Indicator picker → server-side MVT choropleth via Martin's
 *     serving_geo.indicator_overlay_tile function.
 *   - CSV / GeoJSON / GeoParquet download buttons once an indicator is
 *     selected.
 */
import maplibregl from "maplibre-gl";
import type { IndicatorSummary, TileSource } from "./api";
import type { StyleSpecification } from "maplibre-gl";

import { listIndicators, listTileSources } from "./api";
import {
  activateIndicator,
  choroplethConfig,
  clearLegend,
  populateIndicatorSelect,
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
  wireLayerToggles,
} from "./layers";

const CUAUHTEMOC_CENTER: [number, number] = [-99.137, 19.432];

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
}> {
  const [sources, indicators] = await Promise.all([
    listTileSources(),
    listIndicators(),
  ]);
  return { sources, indicators };
}


async function main(): Promise<void> {
  setStatus("Connecting to reimagina API…");

  let sources: TileSource[];
  let indicators: IndicatorSummary[];
  try {
    ({ sources, indicators } = await loadInitialState());
  } catch (err) {
    setStatus(`Failed to load API: ${(err as Error).message}`);
    throw err;
  }
  const tileCatalog = extractTileSources(sources);
  const overlayConfig = choroplethConfig(sources);

  const state: AppState = { activeIndicator: null };

  const map = new maplibregl.Map({
    container: "map",
    style: emptyStyle(),
    center: CUAUHTEMOC_CENTER,
    zoom: 12,
    maxZoom: 18,
    minZoom: 5,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

  map.on("load", () => {
    installInputLayers(map, tileCatalog);

    const toggleContainer = document.getElementById("layer-toggles");
    if (toggleContainer) wireLayerToggles(toggleContainer, map);

    const select = document.getElementById("indicator-select") as HTMLSelectElement | null;
    const legend = document.getElementById("legend");
    const downloads = document.getElementById("downloads");

    if (select) {
      populateIndicatorSelect(select, indicators);
      select.addEventListener("change", () => {
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
        activateIndicator(map, overlayConfig, indicator);
        if (legend) renderLegend(legend, indicator);
        if (downloads) setDownloadAvailability(downloads, true);
        setStatus(`Showing ${indicator.name} (${indicator.unit ?? "no unit"}).`);
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

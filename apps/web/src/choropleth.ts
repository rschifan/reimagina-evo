/**
 * Indicator choropleth: picker, MapLibre source + style, legend.
 *
 * The overlay layer points at Martin's serving_geo.indicator_overlay_tile
 * function with `indicator_slug` (and optionally scenario_id) appended
 * per-selection. Rebuilding the source on every pick is simpler and
 * more predictable than mutating its tiles URL — MapLibre's tile cache
 * is scoped per source, so the old indicator's tiles stop being fetched
 * the instant the source is replaced.
 */
import type { Map as MapLibreMap } from "maplibre-gl";

import type { IndicatorSummary, TileSource } from "./api";

const OVERLAY_SOURCE = "indicator_overlay";
const OVERLAY_LAYER = "indicator_overlay__fill";
const RAMP_START = "#f7fbff";
const RAMP_END = "#08306b";


export interface ChoroplethConfig {
  tilesUrlTemplate: string;      // e.g. http://localhost:3000/indicator_overlay_tile/{z}/{x}/{y}
  sourceLayer: string;           // MVT inner layer name — "indicator_overlay"
  minzoom: number;
  maxzoom: number;
}


export function choroplethConfig(sources: TileSource[]): ChoroplethConfig {
  const overlay = sources.find((s) => s.layer_id === "indicator_overlay");
  if (!overlay?.source_config.tiles_url_template) {
    throw new Error("API did not return an indicator_overlay tiles_url_template");
  }
  return {
    tilesUrlTemplate: overlay.source_config.tiles_url_template,
    sourceLayer: (overlay.source_config.source_layer as string) ?? "indicator_overlay",
    minzoom: (overlay.source_config.minzoom as number) ?? 10,
    maxzoom: (overlay.source_config.maxzoom as number) ?? 16,
  };
}


export function populateIndicatorSelect(
  select: HTMLSelectElement,
  indicators: IndicatorSummary[],
): void {
  // Clear everything except the first "(none)" option.
  while (select.options.length > 1) select.remove(1);
  for (const indicator of indicators) {
    const option = document.createElement("option");
    option.value = indicator.slug;
    option.textContent = indicator.name;
    select.appendChild(option);
  }
}


export function activateIndicator(
  map: MapLibreMap,
  config: ChoroplethConfig,
  indicator: IndicatorSummary,
): void {
  removeIndicator(map);

  // `{z}/{x}/{y}` in the template is literal and must survive query-
  // string composition, so use a plain concatenation — URLSearchParams
  // or `new URL(template)` would reject the curly-brace placeholders.
  const tileUrl = `${config.tilesUrlTemplate}?indicator_slug=${encodeURIComponent(indicator.slug)}`;

  map.addSource(OVERLAY_SOURCE, {
    type: "vector",
    tiles: [tileUrl],
    minzoom: config.minzoom,
    maxzoom: config.maxzoom,
  });

  const minValue = indicator.valid_min ?? 0;
  const maxValue = indicator.valid_max ?? 1;

  map.addLayer({
    id: OVERLAY_LAYER,
    type: "fill",
    source: OVERLAY_SOURCE,
    "source-layer": config.sourceLayer,
    paint: {
      "fill-color": [
        "interpolate",
        ["linear"],
        ["coalesce", ["to-number", ["get", "value_numeric"]], minValue],
        minValue, RAMP_START,
        maxValue, RAMP_END,
      ],
      "fill-opacity": 0.75,
      "fill-outline-color": "rgba(17, 24, 39, 0.7)",
    },
  });
}


export function removeIndicator(map: MapLibreMap): void {
  if (map.getLayer(OVERLAY_LAYER)) {
    map.removeLayer(OVERLAY_LAYER);
  }
  if (map.getSource(OVERLAY_SOURCE)) {
    map.removeSource(OVERLAY_SOURCE);
  }
}


export function renderLegend(
  container: HTMLElement,
  indicator: IndicatorSummary,
): void {
  const minLabel = indicator.valid_min ?? 0;
  const maxLabel = indicator.valid_max ?? 1;
  container.hidden = false;
  container.innerHTML = `
    <div class="name">${escapeHtml(indicator.name)}</div>
    <div class="gradient"></div>
    <div class="bounds"><span>${formatNumber(minLabel)}</span><span>${formatNumber(maxLabel)}</span></div>
    <div class="unit">${escapeHtml(indicator.unit ?? "")}</div>
  `.trim();
}


export function clearLegend(container: HTMLElement): void {
  container.hidden = true;
  container.innerHTML = "";
}


function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}


function formatNumber(value: number): string {
  if (Math.abs(value) >= 100) return value.toFixed(0);
  if (Math.abs(value) >= 1) return value.toFixed(2);
  return value.toFixed(3);
}

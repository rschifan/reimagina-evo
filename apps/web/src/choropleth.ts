/**
 * Indicator choropleth: picker, MapLibre source + style, legend.
 *
 * The overlay layer points at Martin's serving_geo.indicator_overlay_tile
 * function with `indicator_slug` (and optionally scenario_id) appended
 * per-selection. Rebuilding the source on every pick is simpler and
 * more predictable than mutating its tiles URL — MapLibre's tile cache
 * is scoped per source, so the old indicator's tiles stop being fetched
 * the instant the source is replaced.
 *
 * Colour ramp domain.
 * Catalog `valid_min` / `valid_max` are safety caps, not visual ranges.
 * Using them for the ramp (e.g. 0→500 for buildings/ha) washes a
 * realistic city view almost completely white because p95 ≈ 18% of
 * valid_max. We drive the ramp off the observed p05→p95 from the
 * /indicators/{slug}/stats endpoint and fall back to the catalog
 * bounds only when stats aren't available (zero rows, etc.).
 */
import type { Map as MapLibreMap } from "maplibre-gl";

import type { IndicatorSummary, IndicatorValueStats, TileSource } from "./api";

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


export interface RampDomain {
  min: number;
  max: number;
  /** Observed percentiles — shown in the legend when present. */
  p05: number | null;
  p50: number | null;
  p95: number | null;
  /** Whether the domain came from observed stats (true) or catalog (false). */
  fromStats: boolean;
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


export function rampDomainFromStats(
  stats: IndicatorValueStats | null,
  indicator: IndicatorSummary,
): RampDomain {
  // Prefer observed p05→p95 so outliers don't dominate the ramp.
  if (stats && stats.count > 0 && stats.p05 !== null && stats.p95 !== null && stats.p95 > stats.p05) {
    return {
      min: stats.p05,
      max: stats.p95,
      p05: stats.p05,
      p50: stats.p50,
      p95: stats.p95,
      fromStats: true,
    };
  }
  // Flat distribution (p05 == p95) — widen to min/max so we still see something.
  if (stats && stats.count > 0 && stats.min !== null && stats.max !== null && stats.max > stats.min) {
    return {
      min: stats.min,
      max: stats.max,
      p05: stats.p05,
      p50: stats.p50,
      p95: stats.p95,
      fromStats: true,
    };
  }
  // No data — fall back to catalog safety bounds.
  return {
    min: indicator.valid_min ?? 0,
    max: indicator.valid_max ?? 1,
    p05: null,
    p50: null,
    p95: null,
    fromStats: false,
  };
}


export function activateIndicator(
  map: MapLibreMap,
  config: ChoroplethConfig,
  indicator: IndicatorSummary,
  domain: RampDomain,
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

  // If domain.max==min we've got a degenerate range; widen so the linear
  // interpolation doesn't divide by zero (MapLibre silently produces NaN).
  const rampMax = domain.max === domain.min ? domain.min + 1 : domain.max;

  map.addLayer({
    id: OVERLAY_LAYER,
    type: "fill",
    source: OVERLAY_SOURCE,
    "source-layer": config.sourceLayer,
    paint: {
      "fill-color": [
        "interpolate",
        ["linear"],
        ["coalesce", ["to-number", ["get", "value_numeric"]], domain.min],
        domain.min, RAMP_START,
        rampMax, RAMP_END,
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
  domain: RampDomain,
): void {
  container.hidden = false;
  const p50Row =
    domain.fromStats && domain.p50 !== null
      ? `<div class="p50">median · ${formatNumber(domain.p50)}</div>`
      : "";
  const scopeRow = domain.fromStats
    ? `<div class="scope">ramp: observed p05–p95</div>`
    : `<div class="scope">ramp: catalog bounds (no data yet)</div>`;
  container.innerHTML = `
    <div class="name">${escapeHtml(indicator.name)}</div>
    <div class="gradient"></div>
    <div class="bounds"><span>${formatNumber(domain.min)}</span><span>${formatNumber(domain.max)}</span></div>
    <div class="unit">${escapeHtml(indicator.unit ?? "")}</div>
    ${p50Row}
    ${scopeRow}
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

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
import { escapeHtml, fmtNumber } from "./format";
import { enableHoverHighlight } from "./hover-highlight";
import type { SelectionController } from "./selection";
import { registerValueAwareLayer } from "./selection-filter";
import { SELECTION_MASK_LAYER_ID } from "./selection-mask";
import { mvtProvider, type TooltipController } from "./tooltip";

const OVERLAY_SOURCE = "indicator_overlay";
const OVERLAY_LAYER = "indicator_overlay__fill";
const OVERLAY_LINE_LAYER = "indicator_overlay__line";
const RAMP_START = "#f7fbff";
const RAMP_END = "#08306b";

// The currently-active indicator's disposers — tooltip deregistration
// and hover-highlight teardown. Cleared and re-populated on every
// activateIndicator so switching indicators doesn't leak providers.
let _activeDisposers: Array<() => void> = [];


export interface ChoroplethConfig {
  tilesUrlTemplate: string;      // e.g. http://localhost:3000/indicator_overlay_tile/{z}/{x}/{y}
  sourceLayer: string;           // MVT inner layer name — "indicator_overlay"
  minzoom: number;
  maxzoom: number;
}


export interface RampDomain {
  min: number;
  max: number;
  /** Observed median — shown in the legend when present. */
  p50: number | null;
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
    return { min: stats.p05, max: stats.p95, p50: stats.p50, fromStats: true };
  }
  // Flat distribution (p05 == p95) — widen to min/max so we still see something.
  if (stats && stats.count > 0 && stats.min !== null && stats.max !== null && stats.max > stats.min) {
    return { min: stats.min, max: stats.max, p50: stats.p50, fromStats: true };
  }
  // No data — fall back to catalog safety bounds.
  return {
    min: indicator.valid_min ?? 0,
    max: indicator.valid_max ?? 1,
    p50: null,
    fromStats: false,
  };
}


/** Pick the finest grain this indicator applies to (block > ageb > …).
 *
 * The catalog lists `applies_to` in finest-to-coarsest order, so taking
 * the first entry gives us the high-detail rendering the user generally
 * wants. A future UI upgrade can add a radio button to switch grains.
 */
export function defaultGrain(indicator: IndicatorSummary): string | null {
  return indicator.applies_to[0] ?? null;
}


export function activateIndicator(
  map: MapLibreMap,
  config: ChoroplethConfig,
  indicator: IndicatorSummary,
  domain: RampDomain,
  tooltips?: TooltipController,
  aoiId?: string | null,
  selectionController?: SelectionController,
  grain?: string | null,
): void {
  removeIndicator(map);

  // `{z}/{x}/{y}` in the template is literal and must survive query-
  // string composition, so use a plain concatenation — URLSearchParams
  // or `new URL(template)` would reject the curly-brace placeholders.
  const params = new URLSearchParams({ indicator_slug: indicator.slug });
  if (grain) params.set("feature_type", grain);
  if (aoiId) params.set("aoi_id", aoiId);
  const tileUrl = `${config.tilesUrlTemplate}?${params.toString()}`;

  // promoteId surfaces MVT feature_id as feature.id so setFeatureState
  // can target the right feature for hover highlighting — same
  // contract every other vector source in the app uses.
  map.addSource(OVERLAY_SOURCE, {
    type: "vector",
    tiles: [tileUrl],
    minzoom: config.minzoom,
    maxzoom: config.maxzoom,
    promoteId: "feature_id",
  });

  // If domain.max==min we've got a degenerate range; widen so the linear
  // interpolation doesn't divide by zero (MapLibre silently produces NaN).
  const rampMax = domain.max === domain.min ? domain.min + 1 : domain.max;
  const defaultOutline = "rgba(17, 24, 39, 0.7)";

  // Insert below the selection mask so the dim wash sits ON TOP of
  // the choropleth fill — without ``beforeId`` MapLibre appends the
  // layer to the end of the stack and the choropleth paints over the
  // mask, leaving the indicator at full opacity outside the
  // selection. Falls back to ``undefined`` (top of stack) when no
  // mask is installed (tests, embedded maps).
  const maskBeforeId = map.getLayer(SELECTION_MASK_LAYER_ID)
    ? SELECTION_MASK_LAYER_ID
    : undefined;

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
      "fill-outline-color": defaultOutline,
    },
  }, maskBeforeId);

  // Companion line layer at width 0 so enableHoverHighlight can push
  // it to the standard thick stroke on hover. MapLibre's
  // fill-outline-color is hardcoded to 1px and can't carry the
  // hover-width bump on its own.
  map.addLayer({
    id: OVERLAY_LINE_LAYER,
    type: "line",
    source: OVERLAY_SOURCE,
    "source-layer": config.sourceLayer,
    paint: {
      "line-color": defaultOutline,
      "line-width": 0,
    },
  }, maskBeforeId);

  // Wire hover + tooltip via the same helpers every other vector
  // layer in the app uses. Disposers get stashed for removeIndicator
  // to call on teardown so switching to a different indicator
  // doesn't leave stale providers behind.
  _activeDisposers.push(
    enableHoverHighlight(map, {
      fillLayerId: OVERLAY_LAYER,
      lineLayerId: OVERLAY_LINE_LAYER,
      defaultFillOpacity: 0.75,
      defaultOutlineColor: defaultOutline,
      defaultLineWidth: 0,
      hoverFillOpacity: 0.9,
    }),
  );

  // Clip the choropleth to the active selection (if any). Value
  // layers should render features ONLY where the user has selected
  // — outside the selection, they don't render at all (the spotlight
  // mask still dims the spatial baseline beneath). When the user
  // clears the selection, the disposer restores the layer's filter.
  if (selectionController) {
    _activeDisposers.push(
      registerValueAwareLayer(map, selectionController, {
        layerIds: [OVERLAY_LAYER, OVERLAY_LINE_LAYER],
        sourceName: OVERLAY_SOURCE,
        sourceLayer: config.sourceLayer,
      }),
    );
  }

  if (tooltips) {
    _activeDisposers.push(
      tooltips.register(mvtProvider({
        id: `indicator::${indicator.slug}`,
        layerId: OVERLAY_LAYER,
        format: (feature) => {
          const props = feature.properties ?? {};
          const raw = props.value_numeric;
          if (raw === undefined || raw === null) return null;
          const num = typeof raw === "number" ? raw : Number(raw);
          if (Number.isNaN(num)) return null;
          const formatted = fmtNumber(num);
          const unit = indicator.unit ? ` ${indicator.unit}` : "";
          return {
            title: indicator.name,
            detail: `${formatted}${unit}`,
          };
        },
      })),
    );
  }
}


export function removeIndicator(map: MapLibreMap): void {
  for (const dispose of _activeDisposers) {
    try {
      dispose();
    } catch {
      // Disposers should never throw, but swallow defensively so one
      // bad dispose doesn't skip the rest and leave MapLibre layers
      // stranded.
    }
  }
  _activeDisposers = [];
  if (map.getLayer(OVERLAY_LINE_LAYER)) {
    map.removeLayer(OVERLAY_LINE_LAYER);
  }
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


/** Human label for a feature-type slug. Falls back to the slug so a
 *  new grain shows up readable-ish rather than not at all. */
const GRAIN_LABELS: Record<string, string> = {
  block: "Blocks",
  ageb: "AGEBs",
  municipality: "Municipalities",
  lot: "Lots",
};


/**
 * Render the grain switcher for ``indicator`` into ``container`` and
 * call ``onPick`` when the user changes it.
 *
 * Hidden entirely for single-grain indicators: eleven of the shipped
 * thirteen apply only to ``block``, and a radio group with one option
 * is furniture, not a control. Coarser grains are served by the same
 * ``indicator_overlay_tile`` function via ``feature_type``, so
 * switching is a re-render, not a new code path.
 */
export function renderGrainToggle(
  container: HTMLElement,
  indicator: IndicatorSummary,
  active: string | null,
  onPick: (grain: string) => void,
): void {
  const grains = indicator.applies_to ?? [];
  if (grains.length < 2) {
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  container.hidden = false;
  const label = document.createElement("span");
  label.className = "grain-toggle__label";
  label.textContent = "Grain";
  container.replaceChildren(label);
  for (const grain of grains) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "grain-toggle__btn";
    btn.textContent = GRAIN_LABELS[grain] ?? grain;
    btn.setAttribute("aria-pressed", grain === active ? "true" : "false");
    btn.addEventListener("click", () => {
      if (grain === active) return;
      onPick(grain);
    });
    container.append(btn);
  }
}


export function clearGrainToggle(container: HTMLElement): void {
  container.hidden = true;
  container.replaceChildren();
}


export function clearLegend(container: HTMLElement): void {
  container.hidden = true;
  container.innerHTML = "";
}


/** Legend-only formatter — uses ``toFixed`` (no locale separators) so
 *  the gradient endpoints read as compact engineering numbers rather
 *  than long thousand-separated strings. Distinct from ``fmtNumber``
 *  in ``./format`` which is locale-aware for tooltips / panel rows. */
function formatNumber(value: number): string {
  if (Math.abs(value) >= 100) return value.toFixed(0);
  if (Math.abs(value) >= 1) return value.toFixed(2);
  return value.toFixed(3);
}

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
import type {
  ExpressionSpecification,
  MapLayerMouseEvent,
  Map as MapLibreMap,
} from "maplibre-gl";

import type {
  IndicatorSummary,
  IndicatorValueStats,
  TileSource,
  ValueHistogram,
} from "./api";
import { distributionSvg, percentileOf } from "./distribution";
import { escapeHtml, fmtNumber } from "./format";
import { enableHoverHighlight } from "./hover-highlight";
import { tileUrlFor } from "./scenario";
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


/**
 * Why the ramp spans what it spans.
 *
 * `uniform` is separate from `empty` because the two look identical in the
 * ramp — neither can be derived from the data, so both fall back to catalog
 * bounds — and are opposites in the legend. A layer where every block reports
 * the same number is not a layer with no data; it is usually a layer whose
 * destinations are missing, so every block reported the same cutoff. Saying
 * "no data yet" there sends the reader looking for a compute that already ran.
 */
export type RampBasis = "percentile" | "min-max" | "uniform" | "empty";


export interface RampDomain {
  min: number;
  max: number;
  /** Observed median — shown in the legend when present. */
  p50: number | null;
  /** How many features the stats covered; 0 when there are none. */
  count: number;
  /** Where `min`/`max` came from, and what to tell the reader. */
  basis: RampBasis;
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
  const count = stats?.count ?? 0;
  // Prefer observed p05→p95 so outliers don't dominate the ramp.
  if (stats && count > 0 && stats.p05 !== null && stats.p95 !== null && stats.p95 > stats.p05) {
    return {
      min: stats.p05,
      max: stats.p95,
      p50: stats.p50,
      count,
      basis: "percentile",
      fromStats: true,
    };
  }
  // Flat distribution (p05 == p95) — widen to min/max so we still see something.
  if (stats && count > 0 && stats.min !== null && stats.max !== null && stats.max > stats.min) {
    return {
      min: stats.min,
      max: stats.max,
      p50: stats.p50,
      count,
      basis: "min-max",
      fromStats: true,
    };
  }
  // Nothing to derive a span from: either no rows at all, or rows that all
  // carry the same value. Both fall back to catalog safety bounds, and the
  // legend must not confuse the second for the first.
  return {
    min: indicator.valid_min ?? 0,
    max: indicator.valid_max ?? 1,
    p50: count > 0 ? (stats?.p50 ?? stats?.min ?? null) : null,
    count,
    basis: count > 0 ? "uniform" : "empty",
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


/**
 * The baseline colour expression: `value_numeric` on a light-to-dark blue ramp.
 *
 * Lifted out of `activateIndicator` unchanged so the delta ramp has something
 * to be compared against.
 */
export function valueRamp(domain: RampDomain): ExpressionSpecification {
  // If domain.max==min we've got a degenerate range; widen so the linear
  // interpolation doesn't divide by zero (MapLibre silently produces NaN).
  const rampMax = domain.max === domain.min ? domain.min + 1 : domain.max;
  return [
    "interpolate",
    ["linear"],
    ["coalesce", ["to-number", ["get", "value_numeric"]], domain.min],
    domain.min, RAMP_START,
    rampMax, RAMP_END,
  ];
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
  // The active scenario, or null/absent for the baseline city. Only the tile
  // URL reads it; everything else about the layer is the same either way,
  // which is the point of resolving the overlay in SQL (r043).
  scenarioId?: string | null,
): void {
  removeIndicator(map);

  // The URL is built by `scenario.tileUrlFor` rather than here, because
  // whether `scenario_id` is on it is a decision with a cache attached: the
  // baseline URL has to stay byte-identical to the one that existed before
  // scenarios, and that is asserted where the builder lives.
  const tileUrl = tileUrlFor({
    template: config.tilesUrlTemplate,
    slug: indicator.slug,
    grain,
    aoiId,
    scenarioId,
  });

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
      "fill-color": valueRamp(domain),
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

  // Publish the hovered feature's value so the legend can mark where it
  // falls in the distribution.
  //
  // A bus rather than another `activateIndicator` parameter, matching
  // `hover-highlight`'s own suspend bus: the listener is owned by the page
  // and lives longer than any one indicator, while these handlers are torn
  // down and rebuilt on every pick. Registered here so they land in
  // `_activeDisposers` and cannot outlive the layer they read from.
  const onMove = (event: MapLayerMouseEvent): void => {
    const raw = event.features?.[0]?.properties?.["value_numeric"];
    const value = typeof raw === "number" ? raw : Number(raw);
    _valueHoverListener?.(Number.isFinite(value) ? value : null);
  };
  const onLeave = (): void => _valueHoverListener?.(null);
  map.on("mousemove", OVERLAY_LAYER, onMove);
  map.on("mouseleave", OVERLAY_LAYER, onLeave);
  _activeDisposers.push(() => {
    map.off("mousemove", OVERLAY_LAYER, onMove);
    map.off("mouseleave", OVERLAY_LAYER, onLeave);
    // Clear the mark on teardown: a cursor left pointing at the previous
    // indicator's value is worse than no cursor, because it looks current.
    _valueHoverListener?.(null);
  });

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


/**
 * Who hears the hovered value, if anyone.
 *
 * Module-level and single-slot on purpose, mirroring
 * `hover-highlight`'s suspend bus: there is one map and one legend, and a
 * subscriber list would imply a fan-out this page does not have.
 */
let _valueHoverListener: ((value: number | null) => void) | null = null;


/** Register (or with ``null``, clear) the hovered-value listener. */
export function setValueHoverListener(
  listener: ((value: number | null) => void) | null,
): void {
  _valueHoverListener = listener;
}


/**
 * The legend's one-line account of where the colour scale came from.
 *
 * Exported so it can be asserted directly: the `uniform` wording is the whole
 * point of `RampBasis`, and it is the line a reader checks when a layer comes
 * out one flat colour.
 */
export function rampScopeText(domain: RampDomain): string {
  switch (domain.basis) {
    case "percentile":
      return "ramp: observed p05\u2013p95";
    case "min-max":
      return "ramp: observed min\u2013max (p05 = p95)";
    case "uniform":
      return (
        `ramp: catalog bounds \u2014 all ${fmtNumber(domain.count)} values ` +
        `identical (${formatNumber(domain.p50 ?? domain.min)})`
      );
    case "empty":
      return "ramp: catalog bounds (no data yet)";
  }
}


export function renderLegend(
  container: HTMLElement,
  indicator: IndicatorSummary,
  domain: RampDomain,
  histogram: ValueHistogram | null = null,
): void {
  container.hidden = false;
  const p50Row =
    domain.fromStats && domain.p50 !== null
      ? `<div class="p50">median · ${formatNumber(domain.p50)}</div>`
      : "";
  const scopeRow = `<div class="scope">${rampScopeText(domain)}</div>`;
  // The distribution, and a slot the hover handler rewrites in place.
  //
  // Kept as a sibling of the gradient rather than replacing it: the gradient
  // answers "what does this colour mean" and the histogram answers "how many
  // are like this". A legend needs both, and the second is the one that was
  // missing.
  const dist = distributionSvg(histogram, {
    rampMin: domain.min,
    rampMax: domain.max,
    median: domain.p50,
  }, indicator.unit ?? "");
  const distBlock = dist
    ? `<div class="dist-wrap" data-distribution>${dist}</div>` +
      `<div class="dist-readout" data-readout hidden></div>`
    : "";
  container.innerHTML = `
    <div class="name">${escapeHtml(indicator.name)}</div>
    <div class="gradient"></div>
    <div class="bounds"><span>${formatNumber(domain.min)}</span><span>${formatNumber(domain.max)}</span></div>
    <div class="unit">${escapeHtml(indicator.unit ?? "")}</div>
    ${distBlock}
    ${p50Row}
    ${scopeRow}
  `.trim();
}


/**
 * Move the legend's cursor to ``value``, or clear it when ``null``.
 *
 * Called from the hover path, so it must be cheap and must not re-render the
 * whole legend: rebuilding `innerHTML` on every mousemove would drop the
 * `<title>` tooltips mid-hover and churn the DOM at pointer rate.
 */
export function markLegendValue(
  container: HTMLElement,
  indicator: IndicatorSummary,
  domain: RampDomain,
  histogram: ValueHistogram | null,
  value: number | null,
): void {
  const wrap = container.querySelector<HTMLElement>("[data-distribution]");
  const readout = container.querySelector<HTMLElement>("[data-readout]");
  if (!wrap || !readout || !histogram) return;
  wrap.innerHTML = distributionSvg(
    histogram,
    {
      rampMin: domain.min,
      rampMax: domain.max,
      median: domain.p50,
      hovered: value,
    },
    indicator.unit ?? "",
  );
  if (value === null) {
    readout.hidden = true;
    readout.textContent = "";
    return;
  }
  const pct = percentileOf(value, histogram);
  const unit = indicator.unit ? ` ${indicator.unit}` : "";
  readout.hidden = false;
  // "higher than 81% of blocks" rather than "p81": a rank stated as a
  // comparison is a finding, and a bare number is another reading of the
  // same thing the map already shows.
  readout.textContent =
    pct === null
      ? `${formatNumber(value)}${unit}`
      : `${formatNumber(value)}${unit} · higher than ${pct}% of features`;
}


/** Human label for a feature-type slug. Falls back to the slug so a
 *  new grain shows up readable-ish rather than not at all. */
const GRAIN_LABELS: Record<string, string> = {
  block: "Blocks",
  municipality: "Municipalities",
  lot: "Lots",
};


/**
 * Render the grain switcher for ``indicator`` into ``container`` and
 * call ``onPick`` when the user changes it.
 *
 * Always rendered, including for the eleven of thirteen shipped
 * indicators published at ``block`` only — their single option shows
 * as a disabled chip. Hiding the row entirely made the control
 * invisible on most of the catalog, so nobody learned grain switching
 * existed; a disabled chip states the grain the numbers are at, which
 * is worth showing on its own. Coarser grains are served by the same
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
  if (grains.length === 0) {
    // No declared grain at all — nothing truthful to show.
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  const single = grains.length === 1;
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
    // A lone grain is a statement, not a choice: disabled so it can't
    // be clicked, but still marked pressed so it reads as "this is
    // what you are looking at" rather than as an unavailable option.
    btn.disabled = single;
    if (single) {
      btn.title = `${indicator.name} is only published at this grain`;
    }
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

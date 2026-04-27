/**
 * Map-layer wiring.
 *
 * Consumes /tile-sources from the API, transforms each live_mvt layer
 * into a MapLibre source + fill/line style, and attaches UI toggles in
 * the sidebar. The `indicator_overlay` layer is installed but left hidden
 * until `choropleth.ts` configures it — that separation keeps the input-
 * layer scaffold independent of indicator styling.
 */
import type { Map as MapLibreMap } from "maplibre-gl";

import type { TileSource } from "./api";
import { enableHoverHighlight } from "./hover-highlight";
import {
  cogPointProvider,
  mvtProvider,
  type TooltipController,
} from "./tooltip";

export interface InputLayerSpec {
  layerId: string;         // infra/tiles/layers.json layer_id
  sourceName: string;      // MapLibre source name
  sourceLayerName: string; // MVT source layer name inside each tile
  typeSlug: string;        // serving_geo.get_tile query_params filter value
  label: string;           // UI label
  fillColor: string;
  fillOpacity: number;
  lineColor: string;
  lineWidth: number;
  visible: boolean;
  minzoom?: number;
}

// Ordered smallest-grain first so the sidebar reads bottom-up in
// hierarchy: building → block → AGEB → municipality. This matches
// how operators reason about "which finer units roll up into this
// coarser one?".
//
// Colour palette is tuned for a dark map background (#0b1220). Earlier
// values (block lineWidth=0.3 + lineColor=#4b5563, ageb fill 0.05) were
// effectively invisible at z=11 over Mexicali — the user reported a
// blank map even though the tile bytes were correct. Lifting widths
// + lightening colours gives the operator a usable structural skeleton
// for any AOI that lacks a dense building layer (e.g. Mexicali, where
// no Overture buildings are ingested).
export const INPUT_LAYERS: InputLayerSpec[] = [
  {
    layerId: "baseline_live:building",
    sourceName: "baseline_buildings",
    sourceLayerName: "features",
    typeSlug: "building",
    label: "Buildings (Overture)",
    fillColor: "#93c5fd",
    fillOpacity: 0.35,
    lineColor: "#60a5fa",
    lineWidth: 0,
    visible: false,
    minzoom: 14,
  },
  {
    layerId: "baseline_live:block",
    sourceName: "baseline_blocks",
    sourceLayerName: "features",
    typeSlug: "block",
    label: "Blocks (manzanas)",
    fillColor: "transparent",
    fillOpacity: 0,
    lineColor: "#9ca3af",
    lineWidth: 0.6,
    visible: true,
    // Match AGEBs at z=11 so blocks remain visible when zoomed out to
    // city scale — the prior bump to 15 hid them too aggressively.
    minzoom: 11,
  },
  {
    layerId: "baseline_live:ageb",
    sourceName: "baseline_agebs",
    sourceLayerName: "features",
    typeSlug: "ageb",
    label: "AGEBs",
    fillColor: "#374151",
    fillOpacity: 0.20,
    lineColor: "#cbd5e1",
    lineWidth: 1.2,
    visible: true,
    minzoom: 11,
  },
  {
    layerId: "baseline_live:municipality",
    sourceName: "baseline_municipalities",
    sourceLayerName: "features",
    typeSlug: "municipality",
    label: "Municipalities (alcaldías)",
    fillColor: "#475569",
    fillOpacity: 0.30,
    lineColor: "#e5e7eb",
    lineWidth: 2.0,
    visible: true,
    minzoom: 6,
  },
];

export interface TileSourceCatalog {
  baselineTilesUrl: string;
  indicatorOverlayTilesUrl: string;
}


export function extractTileSources(sources: TileSource[]): TileSourceCatalog {
  const baseline = sources.find((s) => s.layer_id === "baseline_live");
  const indicator = sources.find((s) => s.layer_id === "indicator_overlay");
  if (!baseline?.source_config.tiles_url_template) {
    throw new Error("API did not return a baseline_live tiles_url_template");
  }
  if (!indicator?.source_config.tiles_url_template) {
    throw new Error("API did not return an indicator_overlay tiles_url_template");
  }
  return {
    baselineTilesUrl: baseline.source_config.tiles_url_template,
    indicatorOverlayTilesUrl: indicator.source_config.tiles_url_template,
  };
}


export function installInputLayers(
  map: MapLibreMap,
  catalog: TileSourceCatalog,
  tooltips?: TooltipController,
): void {
  for (const layer of INPUT_LAYERS) {
    const tileUrl = `${catalog.baselineTilesUrl}?type_slug=${encodeURIComponent(layer.typeSlug)}`;
    // promoteId surfaces the MVT 'feature_id' property as feature.id so
    // setFeatureState({...id}) can find the right feature for hover
    // highlighting. Every reimagina tile function emits feature_id.
    map.addSource(layer.sourceName, {
      type: "vector",
      tiles: [tileUrl],
      minzoom: layer.minzoom ?? 0,
      maxzoom: 16,
      promoteId: "feature_id",
    });

    if (layer.fillColor !== "transparent" && layer.fillOpacity > 0) {
      map.addLayer({
        id: `${layer.sourceName}__fill`,
        type: "fill",
        source: layer.sourceName,
        "source-layer": layer.sourceLayerName,
        layout: { visibility: layer.visible ? "visible" : "none" },
        paint: {
          "fill-color": layer.fillColor,
          "fill-opacity": layer.fillOpacity,
        },
      });
    }
    map.addLayer({
      id: `${layer.sourceName}__line`,
      type: "line",
      source: layer.sourceName,
      "source-layer": layer.sourceLayerName,
      layout: { visibility: layer.visible ? "visible" : "none" },
      paint: {
        "line-color": layer.lineColor,
        "line-width": layer.lineWidth,
      },
    });

    // Register a tooltip provider per baseline layer — vector features
    // carry their identity (type_slug + feature_id) in the MVT
    // properties, so the hover surface is immediately useful without
    // any extra wiring. If a layer ever has no fill we still probe
    // the line layer so the hover lands on the stroke.
    const hasFill = layer.fillColor !== "transparent" && layer.fillOpacity > 0;
    const fillLayerId = `${layer.sourceName}__fill`;
    const lineLayerId = `${layer.sourceName}__line`;
    const hitLayerId = hasFill ? fillLayerId : lineLayerId;

    if (tooltips) {
      tooltips.register(mvtProvider({
        id: `baseline::${layer.layerId}`,
        layerId: hitLayerId,
        format: (feature) => {
          const props = feature.properties ?? {};
          const id = typeof props.feature_id === "string" ? props.feature_id : null;
          return {
            title: layer.label,
            detail: id ? `id: ${id.slice(0, 8)}…` : undefined,
          };
        },
      }));
    }

    // Hover highlight: lift fill opacity slightly and swap outline
    // colour to the standard yellow so the hovered feature reads on
    // both dark and light fills.
    if (hasFill) {
      enableHoverHighlight(map, {
        fillLayerId,
        lineLayerId,
        defaultFillOpacity: layer.fillOpacity,
        defaultOutlineColor: layer.lineColor,
        defaultLineWidth: layer.lineWidth,
      });
    }
  }
}


export function wireLayerToggles(
  container: HTMLElement,
  map: MapLibreMap,
): void {
  container.innerHTML = "";
  for (const layer of INPUT_LAYERS) {
    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = layer.visible;
    checkbox.addEventListener("change", () => {
      const visibility = checkbox.checked ? "visible" : "none";
      for (const suffix of ["__fill", "__line"]) {
        const id = `${layer.sourceName}${suffix}`;
        if (map.getLayer(id)) {
          map.setLayoutProperty(id, "visibility", visibility);
        }
      }
    });
    const text = document.createElement("span");
    text.className = "layer-label";
    text.textContent = layer.label;
    label.append(checkbox, text);
    container.append(label);
  }
}


// ── Raster layers + aggregation overlays ────────────────────────────
//
// Each registered raster ships as `source_mode: 'raster_cog'` (titiler
// tiles, native pixel resolution). Its zonal-aggregated observations
// ship as one or more `source_mode: 'aggregation_overlay'` entries
// (Martin MVT choropleth, one per feature_type / method combination).
// We install both kinds as hidden MapLibre layers and render a nested
// sidebar group per parent raster so the tree structure is obvious.

const RASTER_SOURCE_PREFIX = "raster_cog__";
const AGG_SOURCE_PREFIX = "agg_overlay__";
const DEFAULT_RASTER_OPACITY = 0.65;
const DEFAULT_AGG_OPACITY = 0.7;


function rasterSourceName(layerId: string): string {
  return `${RASTER_SOURCE_PREFIX}${layerId}`;
}


function aggSourceName(layerId: string): string {
  // layerId is already namespaced as "aggregation_overlay::<uuid>" on
  // the API side; collapse the separator for MapLibre's source-name
  // rules (no ':' allowed in some style specs).
  return `${AGG_SOURCE_PREFIX}${layerId.replace(/::/g, "__")}`;
}


export function installRasterLayers(
  map: MapLibreMap,
  sources: TileSource[],
  tooltips?: TooltipController,
): void {
  for (const src of sources) {
    if (src.source_mode !== "raster_cog") continue;
    if (src.available === false) continue;
    const tmpl = src.source_config.tiles_url_template;
    if (!tmpl) continue;
    const sourceName = rasterSourceName(src.layer_id);
    if (map.getSource(sourceName)) continue;
    map.addSource(sourceName, {
      type: "raster",
      tiles: [tmpl],
      tileSize: 256,
      minzoom: src.source_config.minzoom ?? 0,
      maxzoom: src.source_config.maxzoom ?? 18,
    });
    const rasterLayerId = `${sourceName}__raster`;
    map.addLayer({
      id: rasterLayerId,
      type: "raster",
      source: sourceName,
      layout: { visibility: "none" },
      paint: { "raster-opacity": DEFAULT_RASTER_OPACITY },
    });

    // Raster pixel readout — titiler exposes /cog/point/{lng,lat}
    // which returns the band value under the cursor. The provider
    // reuses the same base (derived from the tile URL template) so
    // the dev vs prod host stays consistent without extra config.
    if (tooltips) {
      const endpointBase = _deriveCogEndpointBase(tmpl);
      const cogUrl = src.source_config.cog_url ?? null;
      if (endpointBase && cogUrl) {
        tooltips.register(cogPointProvider({
          id: `raster::${src.layer_id}`,
          layerId: rasterLayerId,
          title: src.logical_name || src.layer_id,
          cogUrl,
          endpointBase,
        }));
      }
    }
  }
}


/** Derive `<scheme>://<host>/cog` from a titiler tile URL template.
 *  Example: `http://localhost:8000/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?...`
 *  → `http://localhost:8000/cog`.
 *
 *  Returning null skips the raster tooltip for an unexpected URL shape
 *  rather than emitting broken HTTP calls — the choropleth overlays
 *  still work in that case.
 */
function _deriveCogEndpointBase(tilesUrlTemplate: string): string | null {
  const i = tilesUrlTemplate.indexOf("/tiles/");
  if (i < 0) return null;
  return tilesUrlTemplate.slice(0, i);
}


/** Append `aoi_id=<id>` to a Martin tile URL template. The template
 *  may already carry query string params from upstream (e.g.
 *  `?attribute_key=…&feature_type_slug=…`), so we use `&` if a `?` is
 *  already present and `?` otherwise. The `{z}/{x}/{y}` placeholders
 *  must survive composition — that's why we don't reach for the URL
 *  constructor here. */
function _appendAoiId(template: string, aoiId: string): string {
  const sep = template.includes("?") ? "&" : "?";
  return `${template}${sep}aoi_id=${encodeURIComponent(aoiId)}`;
}


/** Update every aggregation-overlay source's tile URLs to scope to
 *  ``aoiId`` (or remove the scoping when ``null``). Called whenever
 *  the active AOI changes so the visible choropleths re-fetch tiles
 *  for the picked city. */
export function refreshAggregationOverlayAoi(
  map: MapLibreMap,
  sources: TileSource[],
  aoiId: string | null,
): void {
  for (const src of sources) {
    if (src.source_mode !== "aggregation_overlay") continue;
    if (src.available === false) continue;
    const cfg = src.source_config;
    if (!cfg.tiles_url_template) continue;
    const sourceName = aggSourceName(src.layer_id);
    const source = map.getSource(sourceName) as unknown as
      { setTiles?: (urls: string[]) => void } | undefined;
    if (!source || typeof source.setTiles !== "function") continue;
    const url = aoiId
      ? _appendAoiId(cfg.tiles_url_template, aoiId)
      : cfg.tiles_url_template;
    source.setTiles([url]);
  }
}


export function installAggregationOverlays(
  map: MapLibreMap,
  sources: TileSource[],
  tooltips?: TooltipController,
  aoiId?: string | null,
): void {
  for (const src of sources) {
    if (src.source_mode !== "aggregation_overlay") continue;
    if (src.available === false) continue;
    const cfg = src.source_config;
    if (!cfg.tiles_url_template) continue;
    const sourceName = aggSourceName(src.layer_id);
    if (map.getSource(sourceName)) continue;
    const tileUrl = aoiId
      ? _appendAoiId(cfg.tiles_url_template, aoiId)
      : cfg.tiles_url_template;
    map.addSource(sourceName, {
      type: "vector",
      tiles: [tileUrl],
      minzoom: cfg.minzoom ?? 10,
      maxzoom: cfg.maxzoom ?? 16,
      promoteId: "feature_id",
    });
    // Colour ramp uses the p05 / p95 of the observation's current
    // value range so a few outliers don't flatten the palette. Fall
    // back to min/max if the percentiles are missing (tiny datasets).
    const range = cfg.value_range ?? null;
    const lo = range?.p05 ?? range?.min ?? 0;
    const hi = range?.p95 ?? range?.max ?? 1;
    const fillId = `${sourceName}__fill`;
    const lineId = `${sourceName}__line`;
    map.addLayer({
      id: fillId,
      type: "fill",
      source: sourceName,
      "source-layer": cfg.source_layer ?? "aggregation_overlay",
      layout: { visibility: "none" },
      paint: {
        "fill-color": [
          "interpolate", ["linear"], ["to-number", ["get", "value_numeric"]],
          lo, "#fff5eb",
          (lo + hi) / 2, "#f16913",
          hi, "#7f2704",
        ],
        "fill-opacity": DEFAULT_AGG_OPACITY,
        "fill-outline-color": "rgba(0,0,0,0.2)",
      },
    });
    // Dedicated line layer so hover can thicken the stroke — MapLibre's
    // fill-outline-color is hardcoded to 1px width and can't be bumped.
    // Default width is 0 so it's invisible until the feature is hovered.
    map.addLayer({
      id: lineId,
      type: "line",
      source: sourceName,
      "source-layer": cfg.source_layer ?? "aggregation_overlay",
      layout: { visibility: "none" },
      paint: {
        "line-color": "rgba(0,0,0,0.2)",
        "line-width": 0,
      },
    });

    if (tooltips) {
      const title = src.logical_name
        ?? `${cfg.parent_raster_layer_slug ?? "aggregation"} · ${cfg.target_feature_type_slug}`;
      const attrKey = cfg.attribute_key ?? "value";
      tooltips.register(mvtProvider({
        id: `aggregation::${src.layer_id}`,
        layerId: fillId,
        format: (feature) => {
          const props = feature.properties ?? {};
          const raw = props.value_numeric;
          if (raw === undefined || raw === null) return null;
          const num = typeof raw === "number" ? raw : Number(raw);
          if (Number.isNaN(num)) return null;
          return {
            title,
            detail: `${attrKey}: ${_fmtNumber(num)}`,
          };
        },
      }));
    }

    // Aggregation fills start already opaque (0.7) — the hover bump
    // keeps the choropleth colour legible while the line layer above
    // provides the thick yellow border once feature-state fires.
    enableHoverHighlight(map, {
      fillLayerId: fillId,
      lineLayerId: lineId,
      defaultFillOpacity: DEFAULT_AGG_OPACITY,
      defaultOutlineColor: "rgba(0,0,0,0.2)",
      defaultLineWidth: 0,
      hoverFillOpacity: 0.85,
    });
  }
}


function _fmtNumber(value: number): string {
  if (Number.isInteger(value)) return value.toLocaleString();
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 0 : abs >= 1 ? 2 : 3;
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}


// ── Nested toggle tree ──────────────────────────────────────────────

interface RasterGroup {
  parentSlug: string;   // label for the accordion header
  parentLabel: string;  // display name
  raster: TileSource | null;        // raster_cog entry, if any
  aggregations: TileSource[];       // aggregation_overlay entries
}


function groupRastersAndOverlays(sources: TileSource[]): RasterGroup[] {
  const bySlug = new Map<string, RasterGroup>();
  const orphans: RasterGroup = {
    parentSlug: "__orphans__",
    parentLabel: "Derived overlays (no parent raster)",
    raster: null,
    aggregations: [],
  };

  for (const src of sources) {
    if (src.source_mode === "raster_cog") {
      const slug = src.layer_id;
      const g = bySlug.get(slug) ?? {
        parentSlug: slug,
        parentLabel: src.logical_name || slug,
        raster: null,
        aggregations: [],
      };
      g.raster = src;
      g.parentLabel = src.logical_name || slug;
      bySlug.set(slug, g);
    } else if (src.source_mode === "aggregation_overlay") {
      const parent = src.source_config.parent_raster_layer_slug;
      if (parent) {
        const g = bySlug.get(parent) ?? {
          parentSlug: parent,
          parentLabel: parent,
          raster: null,
          aggregations: [],
        };
        g.aggregations.push(src);
        bySlug.set(parent, g);
      } else {
        orphans.aggregations.push(src);
      }
    }
  }

  // Sort aggregations within each group by grain, smallest first.
  // The API returns rows ordered alphabetically by target_feature_type_slug,
  // which puts ageb before block — visually wrong for a hierarchy
  // where blocks are finer than AGEBs.
  const grainOrder = _grainOrderFromInputLayers();
  const grainRank = (slug: string | null | undefined): number => {
    if (!slug) return Number.MAX_SAFE_INTEGER;
    const i = grainOrder.indexOf(slug);
    return i < 0 ? Number.MAX_SAFE_INTEGER : i;
  };
  for (const group of bySlug.values()) {
    group.aggregations.sort(
      (a, b) => grainRank(a.source_config.target_feature_type_slug)
        - grainRank(b.source_config.target_feature_type_slug),
    );
  }
  orphans.aggregations.sort(
    (a, b) => grainRank(a.source_config.target_feature_type_slug)
      - grainRank(b.source_config.target_feature_type_slug),
  );

  const groups = [...bySlug.values()].sort((a, b) =>
    a.parentLabel.localeCompare(b.parentLabel),
  );
  if (orphans.aggregations.length > 0) groups.push(orphans);
  return groups;
}


function _grainOrderFromInputLayers(): string[] {
  // Reuse the canonical smallest-first ordering declared in INPUT_LAYERS
  // so changing sidebar order in one place flows to the aggregation
  // children automatically.
  return INPUT_LAYERS.map((l) => l.typeSlug);
}


export function wireRasterToggles(
  container: HTMLElement,
  map: MapLibreMap,
  sources: TileSource[],
): void {
  const groups = groupRastersAndOverlays(sources);
  if (groups.length === 0) return;

  const heading = document.createElement("div");
  heading.className = "layer-group-heading";
  heading.textContent = "Raster overlays";
  container.append(heading);

  for (const group of groups) {
    const details = document.createElement("details");
    details.className = "raster-group";
    // Open the group by default so the structure is visible without
    // clicking — collapses can be a later UX pass.
    details.open = true;

    const summary = document.createElement("summary");
    summary.className = "raster-group-summary";
    summary.textContent = group.parentLabel;
    details.append(summary);

    if (group.raster) {
      const rasterLayerId = `${rasterSourceName(group.raster.layer_id)}__raster`;
      details.append(
        _toggleRow({
          label: "Raster tiles (native resolution)",
          available: group.raster.available !== false,
          onChange: (checked) => {
            if (!map.getLayer(rasterLayerId)) return;
            map.setLayoutProperty(
              rasterLayerId, "visibility", checked ? "visible" : "none",
            );
          },
        }),
      );
    }

    for (const agg of group.aggregations) {
      const aggFillId = `${aggSourceName(agg.layer_id)}__fill`;
      const aggLineId = `${aggSourceName(agg.layer_id)}__line`;
      const cfg = agg.source_config;
      const target = cfg.target_feature_type_slug ?? "?";
      const method = cfg.method ?? "?";
      const count = cfg.value_range?.count;
      const countSuffix = count !== undefined ? ` · ${count.toLocaleString()} rows` : "";
      const label =
        agg.logical_name
          ?? `Aggregated to ${target} (${method})${countSuffix}`;
      details.append(
        _toggleRow({
          label,
          available: agg.available !== false,
          unavailableReason: agg.available === false
            ? "recipe registered but never run"
            : undefined,
          onChange: (checked) => {
            const visibility = checked ? "visible" : "none";
            // Fill + line toggle together: the line layer only
            // renders on hover (width=0 otherwise), but it still
            // needs its layout visibility in lockstep so an
            // invisible fill can't get a hover border on it.
            for (const id of [aggFillId, aggLineId]) {
              if (map.getLayer(id)) {
                map.setLayoutProperty(id, "visibility", visibility);
              }
            }
          },
        }),
      );
    }

    container.append(details);
  }
}


function _toggleRow(opts: {
  label: string;
  available: boolean;
  unavailableReason?: string;
  onChange: (checked: boolean) => void;
}): HTMLElement {
  const row = document.createElement("label");
  row.className = "layer-toggle-row";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = false;
  checkbox.disabled = !opts.available;
  checkbox.addEventListener("change", () => opts.onChange(checkbox.checked));
  const text = document.createElement("span");
  text.className = "layer-label";
  text.textContent = opts.available
    ? opts.label
    : `${opts.label}${opts.unavailableReason ? ` (${opts.unavailableReason})` : " (unavailable)"}`;
  row.append(checkbox, text);
  return row;
}

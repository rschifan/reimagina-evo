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
import { fmtNumber } from "./format";
import { enableHoverHighlight } from "./hover-highlight";
import type { SelectionController } from "./selection";
import { registerValueAwareLayer } from "./selection-filter";
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
    // On by default now that r038 bounds the cost. Previously this layer
    // could request 1.3-2.6 MB tiles at overview zoom, so shipping it off
    // was the only thing keeping the map usable; with footprints served
    // only from z14 a request below that is a 204 and a z14 tile is ~31 KB.
    visible: true,
    // z14 because get_tile (migration r038) stops serving building
    // footprints below it — a request at 11-13 returns an empty tile, so a
    // lower floor here only buys wasted round-trips.
    //
    // The earlier value was 11, justified by "ST_Simplify keeps tiles
    // bounded". Measured, that was wrong twice over: on a dataset dense in
    // one alcaldia a z12 tile reached 2.59 MB, and the reason the z10/z11
    // rungs looked cheap is that ~10 m simplification collapses a ~10 m
    // footprint to a degenerate ring, which ST_AsMVTGeom drops — 1,826 of
    // 37,858 buildings survived. Cheap because 95% was discarded.
    //
    // At z12 one pixel is ~36 m, so footprints are sub-pixel anyway. For
    // building information at AOI scale use the
    // urban_form_block_building_density choropleth, which is the layer that
    // question actually wants.
    minzoom: 14,
  },
  {
    layerId: "baseline_live:block",
    sourceName: "baseline_blocks",
    sourceLayerName: "features",
    typeSlug: "block",
    label: "Blocks (manzanas)",
    // Fill is a near-zero opacity wash for hit detection only.
    // MapLibre skips mousemove events on layers with fill-opacity=0,
    // so a literally-transparent block layer was un-hoverable.
    // 0.01 alpha is mathematically invisible against the dark
    // background but registers as a renderable hit-target — the user
    // sees structurally just the line, but hover works on the whole
    // polygon interior.
    fillColor: "#9ca3af",
    fillOpacity: 0.01,
    lineColor: "#9ca3af",
    lineWidth: 0.6,
    visible: true,
    // z=8 is the floor the tile function honours for analysis_unit
    // features (r010 caps role='analysis_unit' at z>=8). The
    // simplification ladder collapses sub-50m manzanas at z<10, so in
    // practice the rendered density tapers off naturally — but the
    // user always sees *something* the moment they zoom in past
    // country scale. Earlier minzoom=11 hid blocks entirely under z=11
    // and the user reported the map feeling empty at city scale.
    minzoom: 8,
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
    visible: false,
    // AGEBs are ~50× larger than manzanas — they survive even the
    // z<10 simplification and read clearly at z=8.
    minzoom: 8,
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
    visible: false,
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
      // The baseline tile function (serving_geo.get_tile, r010) emits
      // the feature uuid as the MVT property literally named ``id`` —
      // not ``feature_id``. The choropleth + aggregation tile functions
      // use ``feature_id`` instead, which is why those hover paths
      // work but the baseline ones silently no-opped: MapLibre's
      // setFeatureState requires the promoted id to actually exist on
      // the feature, otherwise hover state has nowhere to attach.
      promoteId: "id",
    });

    // Industry standard: every polygon layer gets a fill layer, even
    // when the visible style is line-only. The fill catches mousemove
    // events across the entire feature interior — without it, hovering
    // a 0.6 px-wide block stroke is effectively impossible. The block
    // spec uses a near-zero alpha (0.01) for the same reason MapLibre
    // refuses to fire mouse events on layers with fill-opacity=0; an
    // invisible-but-renderable fill is the standard workaround.
    const fillLayerId = `${layer.sourceName}__fill`;
    const lineLayerId = `${layer.sourceName}__line`;

    map.addLayer({
      id: fillLayerId,
      type: "fill",
      source: layer.sourceName,
      "source-layer": layer.sourceLayerName,
      layout: { visibility: layer.visible ? "visible" : "none" },
      paint: {
        "fill-color": layer.fillColor,
        "fill-opacity": layer.fillOpacity,
      },
    });
    map.addLayer({
      id: lineLayerId,
      type: "line",
      source: layer.sourceName,
      "source-layer": layer.sourceLayerName,
      layout: { visibility: layer.visible ? "visible" : "none" },
      paint: {
        "line-color": layer.lineColor,
        "line-width": layer.lineWidth,
      },
    });

    // Tooltip provider keys off the fill layer so the hit-target is
    // the polygon interior, not the thin stroke. Same property bag
    // either way — the format function reads MVT props, doesn't care
    // which layer caught the hit.
    if (tooltips) {
      tooltips.register(mvtProvider({
        id: `baseline::${layer.layerId}`,
        layerId: fillLayerId,
        format: (feature) => _baselineTooltipFormat(layer, feature.properties ?? {}),
      }));
    }

    // Hover highlight: fill + line both react via feature-state. For
    // visually-line-only layers (block) the helper still bumps the
    // fill alpha to ~0.20 on hover so the user gets an unmistakable
    // "this feature is selected" highlight matching what every other
    // industry GIS tool does (Felt, Mapbox Studio, kepler.gl).
    enableHoverHighlight(map, {
      fillLayerId,
      lineLayerId,
      defaultFillOpacity: layer.fillOpacity,
      defaultOutlineColor: layer.lineColor,
      defaultLineWidth: layer.lineWidth,
    });
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


function _baselineMinzoomForGrain(typeSlug: string | null | undefined): number | undefined {
  // Returns the minzoom of the INPUT_LAYERS row whose typeSlug matches.
  // Used by aggregation overlays so a block-grain choropleth lights up
  // at the same zoom the underlying blocks are visible — the catalog
  // default of 10 was hiding the censo overlay even at z=8 where
  // blocks already render.
  if (!typeSlug) return undefined;
  const match = INPUT_LAYERS.find((l) => l.typeSlug === typeSlug);
  return match?.minzoom;
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
 *  Example: `http://localhost:8001/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?...`
 *  → `http://localhost:8001/cog`.
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


// Disposers for the per-overlay value-aware filter registrations.
// Mirrors ``choropleth.ts``'s _activeDisposers idiom.
const _aggregationFilterDisposers: Array<() => void> = [];


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
    // Match the baseline layer's minzoom for the same target grain so
    // a censo / rollup overlay starts rendering at the same zoom the
    // user already sees the underlying blocks / AGEBs / munis. The
    // server catalog still ships ``minzoom: 10`` (legacy default), so
    // we deliberately *override* it here when we have a baseline
    // anchor — otherwise the catalog wins and the user has to zoom in
    // past the baseline floor before the choropleth lights up.
    const targetMinzoom = _baselineMinzoomForGrain(cfg.target_feature_type_slug);
    const minzoom = targetMinzoom ?? cfg.minzoom ?? 10;
    map.addSource(sourceName, {
      type: "vector",
      tiles: [tileUrl],
      minzoom,
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
            detail: `${attrKey}: ${fmtNumber(num)}`,
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


/**
 * Drain and call every disposer accumulated by
 * :func:`wireAggregationOverlayClipping`. Call BEFORE re-wiring on
 * AOI change so ``sourcedata`` listeners don't pile up across
 * switches.
 */
export function clearAggregationOverlayClipping(): void {
  for (const dispose of _aggregationFilterDisposers) {
    try {
      dispose();
    } catch (err) {
      // Disposers should never throw; swallow so one bad dispose
      // doesn't strand the rest with stale filters.
      console.warn("aggregation clipping dispose:", err);
    }
  }
  _aggregationFilterDisposers.length = 0;
}


/**
 * Wire every aggregation-overlay layer to clip-on-selection. Called
 * once after ``enableSelection`` creates the controller. Split from
 * ``installAggregationOverlays`` because the controller doesn't
 * exist at install time — terra-draw layers must paint on top of
 * the mask, mask install happens between data and terra-draw, and
 * the controller is the output of ``enableSelection`` (which adds
 * terra-draw's layers).
 */
export function wireAggregationOverlayClipping(
  map: MapLibreMap,
  sources: TileSource[],
  controller: SelectionController,
): void {
  for (const src of sources) {
    if (src.source_mode !== "aggregation_overlay") continue;
    if (src.available === false) continue;
    const cfg = src.source_config;
    const sourceName = aggSourceName(src.layer_id);
    if (!map.getSource(sourceName)) continue;
    const fillId = `${sourceName}__fill`;
    const lineId = `${sourceName}__line`;
    if (!map.getLayer(fillId) && !map.getLayer(lineId)) continue;
    _aggregationFilterDisposers.push(
      registerValueAwareLayer(map, controller, {
        layerIds: [fillId, lineId],
        sourceName,
        sourceLayer: cfg.source_layer ?? "aggregation_overlay",
      }),
    );
  }
}


/** Per-layer hover tooltip formatter.
 *
 * The MVT exposes different fields by zoom (per ``serving_geo.get_tile``):
 *   - all zooms: id, type_slug, role
 *   - z >= 11 + role='context': name (so municipalities + states render
 *     their NOMGEO at city scale)
 *   - z >= 12: f.attributes flattened to top-level keys
 *     (CVE_ENT, CVE_MUN, CVE_LOC, CVE_AGEB, CVE_MZA, AMBITO, TIPOMZA)
 *   - z >= 14: same plus current observations as flat keys
 *     (censo_2020_pobtot, …, raster zonal-stats outputs, etc.)
 *
 * Each layer surfaces whatever's richest at the current zoom — so the
 * user always sees the most informative thing available without having
 * to wonder why a tooltip is sparse.
 */
function _baselineTooltipFormat(
  layer: InputLayerSpec,
  props: Record<string, unknown>,
): { title: string; detail?: string } | null {
  const id = typeof props.id === "string" ? props.id : null;

  switch (layer.typeSlug) {
    case "municipality":
    case "state": {
      // ``name`` is always emitted by r010 at z >= 11 (and z >= 8 for
      // context roles via the role-context branch). Falls back to the
      // generic layer label if ``name`` is missing for any reason.
      const name = typeof props.name === "string" ? props.name : null;
      return {
        title: name ?? layer.label,
        detail: layer.typeSlug === "municipality"
          ? "Alcaldía / municipality"
          : "Estado / state",
      };
    }
    case "ageb": {
      const cvegeo = _composeCvegeo(props, /* withMza */ false);
      return {
        title: cvegeo ? `AGEB · ${cvegeo}` : "AGEB",
        detail: _formatPopulationLine(props),
      };
    }
    case "block": {
      const cvegeo = _composeCvegeo(props, /* withMza */ true);
      const popLine = _formatPopulationLine(props);
      return {
        title: cvegeo ? `Manzana · ${cvegeo}` : "Block (manzana)",
        detail: popLine ?? (id ? `id: ${id.slice(0, 8)}…` : undefined),
      };
    }
    case "building": {
      return {
        title: "Building",
        detail: id ? `id: ${id.slice(0, 8)}…` : undefined,
      };
    }
    default:
      return {
        title: layer.label,
        detail: id ? `id: ${id.slice(0, 8)}…` : undefined,
      };
  }
}


/** Compose the canonical 16-char CVEGEO from MVT-flattened components.
 *  Returns null when any required piece is missing (z < 12). */
function _composeCvegeo(
  props: Record<string, unknown>,
  withMza: boolean,
): string | null {
  const ent = _readString(props, "CVE_ENT");
  const mun = _readString(props, "CVE_MUN");
  const loc = _readString(props, "CVE_LOC");
  const ageb = _readString(props, "CVE_AGEB");
  if (!ent || !mun || !loc || !ageb) return null;
  if (!withMza) return `${ent} ${mun} ${loc} ${ageb}`;
  const mza = _readString(props, "CVE_MZA");
  if (!mza) return `${ent} ${mun} ${loc} ${ageb}`;
  return `${ent} ${mun} ${loc} ${ageb} ${mza}`;
}


/** Optional census-population line for blocks / AGEBs that have it.
 *  Only populated at z >= 14 when the MV merges observations. */
function _formatPopulationLine(props: Record<string, unknown>): string | undefined {
  const pob = _readNumber(props, "censo_2020_pobtot");
  if (pob === null) return undefined;
  const viv = _readNumber(props, "censo_2020_vivtot");
  const parts = [`Pop. 2010: ${fmtNumber(pob)}`];
  if (viv !== null) parts.push(`viv: ${fmtNumber(viv)}`);
  return parts.join(" · ");
}


function _readString(props: Record<string, unknown>, key: string): string | null {
  const raw = props[key];
  return typeof raw === "string" && raw.length > 0 ? raw : null;
}


function _readNumber(props: Record<string, unknown>, key: string): number | null {
  const raw = props[key];
  if (typeof raw === "number" && !Number.isNaN(raw)) return raw;
  if (typeof raw === "string") {
    const n = Number(raw);
    return Number.isNaN(n) ? null : n;
  }
  return null;
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
  activeAoiId: string | null = null,
): void {
  // Hide the previous render before re-flowing — wireRasterToggles is
  // re-invoked on AOI change, so leftover groups would stack.
  for (const el of Array.from(container.querySelectorAll(".raster-group, .layer-group-heading"))) {
    el.remove();
  }
  // Filter the source list by AOI affinity. An overlay carrying
  // ``source_config.aois = [aoi_id, ...]`` is shown only for those
  // AOIs; missing or empty list ⇒ "always visible" (matches the
  // observation-overlay default for keys with no aoi_feature link).
  const filtered = sources.filter((s) => {
    const aois = s.source_config.aois;
    if (!aois || aois.length === 0) return true;
    if (activeAoiId === null) return true;
    return aois.includes(activeAoiId);
  });
  const groups = groupRastersAndOverlays(filtered);
  if (groups.length === 0) return;

  const heading = document.createElement("div");
  heading.className = "layer-group-heading";
  heading.textContent = "Raster overlays";
  container.append(heading);

  // Mutual-exclusion bookkeeping for aggregation overlays. Two
  // observation choropleths fighting for the same blocks gives the
  // user a render-order coin flip — only the top layer's colour ramp
  // is visible. Enforcing single-selection makes the UI honest about
  // that constraint and matches the user's mental model of "which
  // attribute am I looking at right now?".
  type ExclusiveEntry = {
    checkbox: HTMLInputElement;
    show: (visible: boolean) => void;
  };
  const exclusiveOverlays: ExclusiveEntry[] = [];
  const setActiveOverlay = (active: ExclusiveEntry | null): void => {
    for (const e of exclusiveOverlays) {
      if (e === active) continue;
      if (e.checkbox.checked) e.checkbox.checked = false;
      e.show(false);
    }
    if (active) active.show(true);
  };

  for (const group of groups) {
    const details = document.createElement("details");
    details.className = "raster-group";
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

      const show = (visible: boolean): void => {
        const visibility = visible ? "visible" : "none";
        // Fill + line toggle together: the line layer only renders on
        // hover (width=0 otherwise), but its layout visibility must
        // stay in lockstep so an invisible fill can't pick up a hover
        // border.
        for (const id of [aggFillId, aggLineId]) {
          if (map.getLayer(id)) {
            map.setLayoutProperty(id, "visibility", visibility);
          }
        }
      };

      const row = _toggleRow({
        label,
        available: agg.available !== false,
        unavailableReason: agg.available === false
          ? "recipe registered but never run"
          : undefined,
        onChange: (checked) => {
          const entry = exclusiveOverlays.find((e) => e.checkbox === checkbox);
          if (!entry) {
            show(checked);
            return;
          }
          setActiveOverlay(checked ? entry : null);
        },
      });
      // Grab the row's checkbox so we can register it for mutual
      // exclusion. _toggleRow's first child is always the input.
      const checkbox = row.querySelector("input[type='checkbox']") as HTMLInputElement;
      if (checkbox) exclusiveOverlays.push({ checkbox, show });
      details.append(row);
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

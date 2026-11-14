/**
 * Typed fetch helpers for the reimagina API.
 *
 * The TS interfaces below intentionally describe only the subset of
 * each endpoint's response that the web app reads today. The API
 * returns more fields; structural typing lets us ignore them without
 * imposing a full schema mirror that would drift silently whenever the
 * backend shape changes. If a new field is added and the frontend
 * starts reading it, add it here then — not before.
 */

import { API_BASE, ApiError, isAbortError, jsonRequest } from "./http";

// Re-exported so existing map-app importers keep their import site. The
// definitions moved to ./http when the catalog page needed the same
// request/abort semantics without inheriting this module's map-specific types.
export { ApiError, isAbortError };

export interface IndicatorSummary {
  slug: string;
  name: string;
  unit: string | null;
  applies_to: string[];
  valid_min: number | null;
  valid_max: number | null;
}

export interface TileSource {
  layer_id: string;
  logical_name?: string | null;
  description?: string | null;
  source_mode?: string;
  available?: boolean;
  source_config: {
    tiles_url_template?: string;
    source_layer?: string | null;
    minzoom?: number | null;
    maxzoom?: number | null;
    // Resolved file:// URL that titiler opens (populated for raster_cog).
    // Used by the hover-tooltip provider to call titiler's /cog/point
    // endpoint under the cursor.
    cog_url?: string | null;
    // Aggregation overlay (source_mode='aggregation_overlay') fields.
    // Each overlay renders one MVT choropleth of a recipe's current
    // observations. parent_raster_layer_slug links the overlay back to
    // the raster_cog source it was derived from (null for
    // observation→observation rollups with no raster ancestor).
    attribute_key?: string | null;
    target_feature_type_slug?: string | null;
    method?: string | null;
    parent_raster_layer_slug?: string | null;
    value_range?: {
      count: number;
      min: number | null;
      p05: number | null;
      p50: number | null;
      p95: number | null;
      max: number | null;
    } | null;
    /**
     * AOI affinity for an aggregation_overlay. When non-empty, the
     * picker shows this overlay only for AOIs in the list. Censo
     * block-level overlays span both AOIs and surface for either; the
     * per-AOI WorldPop ingests surface only for their own AOI.
     * Empty / missing = always visible (defensive default).
     */
    aois?: string[] | null;
  };
}

export interface IndicatorValueStats {
  count: number;
  min: number | null;
  p05: number | null;
  p50: number | null;
  p95: number | null;
  max: number | null;
  /** Present only when ``bins`` was requested; ``null`` when nothing to bin. */
  histogram: ValueHistogram | null;
}

/**
 * Equal-width bin counts over an indicator's **full** observed range.
 *
 * Deliberately not clipped to the ramp's p05–p95: the ramp clips so one
 * outlier cannot flatten the colour scale, and a histogram that clipped the
 * same way would hide the very thing it exists to show. The legend draws the
 * ramp's extent on top of this.
 */
export interface ValueHistogram {
  bins: number;
  lo: number;
  hi: number;
  /** ``(hi - lo) / bins``. Zero when every value is identical. */
  width: number;
  /** One count per bin, left to right. Sums to the response's ``count``. */
  counts: number[];
}


/** Catalog indicators. With ``aoiId``, only those holding current values in
 *  that AOI — the picker should not offer an indicator whose overlay would
 *  render empty, because an empty map reads as a broken indicator rather
 *  than an uncomputed one. */
export function listIndicators(aoiId?: string | null): Promise<IndicatorSummary[]> {
  const qs = aoiId ? `?aoi_id=${encodeURIComponent(aoiId)}` : "";
  return jsonRequest<IndicatorSummary[]>(`/indicators${qs}`);
}


export function getIndicatorStats(
  slug: string,
  options: {
    scenario_id?: string;
    feature_type?: string;
    aoi_id?: string;
    /** Ask for a distribution alongside the percentiles. */
    bins?: number;
  } = {},
): Promise<IndicatorValueStats> {
  const params = new URLSearchParams();
  if (options.scenario_id) params.set("scenario_id", options.scenario_id);
  if (options.feature_type) params.set("feature_type", options.feature_type);
  if (options.aoi_id) params.set("aoi_id", options.aoi_id);
  if (options.bins) params.set("bins", String(options.bins));
  const qs = params.toString();
  return jsonRequest<IndicatorValueStats>(
    `/indicators/${encodeURIComponent(slug)}/stats${qs ? `?${qs}` : ""}`,
  );
}

/** A credit line the licence of some loaded source requires. */
export interface AttributionRow {
  provider: string;
  license: string | null;
  text: string;
}

/**
 * Credit lines for the data actually loaded.
 *
 * Fetched rather than hardcoded: the street layer is OpenStreetMap in a
 * real pilot and a generated grid in a demo, so a frontend that always
 * printed "© OpenStreetMap contributors" would make a false provenance
 * claim on every synthetic territory.
 */
export function listAttributions(): Promise<AttributionRow[]> {
  return jsonRequest<AttributionRow[]>("/tile-sources/attribution");
}

export function listTileSources(): Promise<TileSource[]> {
  return jsonRequest<TileSource[]>("/tile-sources");
}


export interface AOIRow {
  aoi_id: string;
  name: string;
  description: string | null;
  /** [minLon, minLat, maxLon, maxLat] in EPSG:4326. Null when the AOI
   *  was inserted without geometry (rare). */
  bbox: [number, number, number, number] | null;
  /** [minLon, minLat, maxLon, maxLat] envelope of every block loaded
   *  inside this AOI — i.e. where the actual data lives. Distinct
   *  from {@link bbox} above: ``bbox`` is the muni polygon envelope,
   *  which for huge AOIs (Mexicali) covers a lot of empty desert.
   *  ``features_bbox`` is the urban extent. The picker prefers this
   *  for fitBounds so the user lands on the populated area, not on
   *  empty land. Null when no blocks are loaded yet. */
  features_bbox: [number, number, number, number] | null;
}


export function listAois(): Promise<AOIRow[]> {
  return jsonRequest<AOIRow[]>("/aois");
}

export interface WithinPolygonRequest {
  aoi_id: string;
  polygon: GeoJSON.Polygon;
  type_slugs: string[];
}


export interface WithinPolygonResultRow {
  type_slug: string;
  count: number;
}


export interface WithinPolygonResponse {
  results: WithinPolygonResultRow[];
}


export async function fetchMetricsWithinPolygon(
  body: WithinPolygonRequest,
  init: { signal?: AbortSignal } = {},
): Promise<WithinPolygonResponse> {
  // POST not GET because the polygon payload doesn't fit cleanly in a
  // query string and (more importantly) browsers cache GETs by URL —
  // we don't want a re-drawn polygon at the same vertices to read a
  // stale 304. POST + AbortController is also the standard pattern
  // for "supersede an in-flight request when a newer selection
  // arrives".
  const response = await fetch(`${API_BASE}/metrics/within-polygon`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(body),
    signal: init.signal,
  });
  if (!response.ok) {
    throw new ApiError(
      `${response.status} ${response.statusText} on /metrics/within-polygon`,
      response,
    );
  }
  return response.json() as Promise<WithinPolygonResponse>;
}


export function downloadUrl(
  slug: string,
  format: "csv" | "geojson" | "parquet",
): string {
  return `${API_BASE}/indicators/${encodeURIComponent(slug)}/values.${format}`;
}

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

const API_BASE = "/api";

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
    // Raster (source_mode='raster_cog') fields. Populated alongside
    // tiles_url_template so the frontend can expose the colormap /
    // rescale in the UI without re-parsing the URL.
    colormap_name?: string | null;
    rescale?: string | null;
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
    input_kind?: string | null;
    parent_raster_layer_id?: string | null;
    parent_raster_layer_slug?: string | null;
    value_range?: {
      count: number;
      min: number | null;
      p05: number | null;
      p50: number | null;
      p95: number | null;
      max: number | null;
    } | null;
  };
}

async function jsonRequest<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new ApiError(`${response.status} ${response.statusText} on ${path}`, response);
  }
  return response.json() as Promise<T>;
}

export class ApiError extends Error {
  constructor(message: string, readonly response: Response) {
    super(message);
    this.name = "ApiError";
  }
}

export interface IndicatorValueStats {
  count: number;
  min: number | null;
  p05: number | null;
  p50: number | null;
  p95: number | null;
  max: number | null;
}


export function listIndicators(): Promise<IndicatorSummary[]> {
  return jsonRequest<IndicatorSummary[]>("/indicators");
}


export function getIndicatorStats(
  slug: string,
  options: { scenario_id?: string; feature_type?: string; aoi_id?: string } = {},
): Promise<IndicatorValueStats> {
  const params = new URLSearchParams();
  if (options.scenario_id) params.set("scenario_id", options.scenario_id);
  if (options.feature_type) params.set("feature_type", options.feature_type);
  if (options.aoi_id) params.set("aoi_id", options.aoi_id);
  const qs = params.toString();
  return jsonRequest<IndicatorValueStats>(
    `/indicators/${encodeURIComponent(slug)}/stats${qs ? `?${qs}` : ""}`,
  );
}

export function listTileSources(): Promise<TileSource[]> {
  return jsonRequest<TileSource[]>("/tile-sources");
}


export interface AOIRow {
  aoi_id: string;
  project_id: string;
  name: string;
  description: string | null;
  /** [minLon, minLat, maxLon, maxLat] in EPSG:4326. Null when the AOI
   *  was inserted without geometry (rare). */
  bbox: [number, number, number, number] | null;
  /** [lon, lat] from ST_PointOnSurface — a point guaranteed to lie
   *  inside the polygon. The picker prefers this over the bbox centre
   *  for irregular AOIs (Mexicali's municipality is L-shaped around
   *  the river delta; bbox centre is in agricultural desert south of
   *  the populated area). Null when the AOI has no geometry. */
  centroid: [number, number] | null;
}


export function listAois(): Promise<AOIRow[]> {
  return jsonRequest<AOIRow[]>("/aois");
}

export function downloadUrl(
  slug: string,
  format: "csv" | "geojson" | "parquet",
): string {
  return `${API_BASE}/indicators/${encodeURIComponent(slug)}/values.${format}`;
}

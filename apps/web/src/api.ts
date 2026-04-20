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
  source_config: {
    tiles_url_template?: string;
    source_layer?: string | null;
    minzoom?: number | null;
    maxzoom?: number | null;
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
  options: { scenario_id?: string; feature_type?: string } = {},
): Promise<IndicatorValueStats> {
  const params = new URLSearchParams();
  if (options.scenario_id) params.set("scenario_id", options.scenario_id);
  if (options.feature_type) params.set("feature_type", options.feature_type);
  const qs = params.toString();
  return jsonRequest<IndicatorValueStats>(
    `/indicators/${encodeURIComponent(slug)}/stats${qs ? `?${qs}` : ""}`,
  );
}

export function listTileSources(): Promise<TileSource[]> {
  return jsonRequest<TileSource[]>("/tile-sources");
}

export function downloadUrl(
  slug: string,
  format: "csv" | "geojson" | "parquet",
): string {
  return `${API_BASE}/indicators/${encodeURIComponent(slug)}/values.${format}`;
}

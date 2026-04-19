/**
 * Typed fetch helpers for the reimagina API.
 *
 * Hand-written TS types that mirror the FastAPI Pydantic response models
 * in apps/api/routers/*.py. If the server changes a shape, the runtime
 * JSON will diverge from these types and `tsc --noEmit` won't catch it
 * — treat these as a fast-moving contract and regenerate (e.g. via
 * openapi-typescript) once the API surface stabilises.
 */

const API_BASE = "/api";

export interface IndicatorSummary {
  slug: string;
  name: string;
  description: string | null;
  model_family: string | null;
  computation_kind: string;
  value_type: string;
  unit: string | null;
  valid_min: number | null;
  valid_max: number | null;
  aggregation_method: string;
  applies_to: string[];
}

export interface IndicatorValue {
  feature_id: string;
  feature_type: string;
  indicator_slug: string;
  scenario_id: string | null;
  value_numeric: number | null;
  value_text: string | null;
  quality_flag: string;
  confidence: number | null;
  computed_at: string;
  unit: string | null;
  indicator_name: string | null;
}

export interface TileSource {
  layer_id: string;
  logical_name: string | null;
  description: string | null;
  source_mode: "live_mvt" | "pmtiles_snapshot";
  implemented: boolean;
  available: boolean;
  source_config: {
    kind: string;
    tilejson_url?: string;
    tiles_url_template?: string;
    url?: string;
    query_params?: Record<string, unknown>;
    minzoom?: number | null;
    maxzoom?: number | null;
  };
  latest_snapshot?: Record<string, unknown> | null;
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

export function listIndicators(): Promise<IndicatorSummary[]> {
  return jsonRequest<IndicatorSummary[]>("/indicators");
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

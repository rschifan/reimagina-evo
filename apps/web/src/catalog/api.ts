/**
 * Catalog API client.
 *
 * Deliberately not importing `../api.ts`: that module's types describe what
 * the *map* reads (tile sources, AOI bboxes), and coupling the catalog to it
 * would make every map change a catalog change. Both import `../http.ts`
 * instead, so request and abort semantics stay identical.
 *
 * Today this reads the endpoints that exist on main. The asset-graph
 * endpoints (freshness, lineage, materializations) attach here when r041
 * lands; the shapes below are the half that needs no migration.
 */
import { jsonRequest } from "../http";

export interface IndicatorSummary {
  slug: string;
  name: string;
  unit: string | null;
  applies_to: string[];
  valid_min: number | null;
  valid_max: number | null;
}

export interface IndicatorStats {
  count: number;
  min: number | null;
  p05: number | null;
  p50: number | null;
  p95: number | null;
  max: number | null;
}

export interface AOIRow {
  aoi_id: string;
  name: string;
}

/** Indicators, optionally only those with values in `aoiId`. */
export function listIndicators(
  aoiId: string | null,
  init: { signal?: AbortSignal } = {},
): Promise<IndicatorSummary[]> {
  const qs = aoiId ? `?aoi_id=${encodeURIComponent(aoiId)}` : "";
  return jsonRequest<IndicatorSummary[]>(`/indicators${qs}`, init);
}

export function listAois(init: { signal?: AbortSignal } = {}): Promise<AOIRow[]> {
  return jsonRequest<AOIRow[]>("/aois", init);
}

export function getStats(
  slug: string,
  opts: { feature_type?: string; aoi_id?: string | null } = {},
  init: { signal?: AbortSignal } = {},
): Promise<IndicatorStats> {
  const p = new URLSearchParams();
  if (opts.feature_type) p.set("feature_type", opts.feature_type);
  if (opts.aoi_id) p.set("aoi_id", opts.aoi_id);
  const qs = p.toString();
  return jsonRequest<IndicatorStats>(
    `/indicators/${encodeURIComponent(slug)}/stats${qs ? `?${qs}` : ""}`,
    init,
  );
}

/** Download URL for one indicator's values. Not fetched — handed to an
 *  anchor so the browser streams it and the API sets Content-Disposition. */
export function downloadUrl(slug: string, format: "csv" | "geojson" | "parquet"): string {
  return `/api/indicators/${encodeURIComponent(slug)}/values.${format}`;
}

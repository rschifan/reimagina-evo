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


export interface DependencyEdge {
  indicator_slug: string;
  indicator_name: string;
  depends_on_slug: string;
  depends_on_name: string;
  depends_on_unit: string | null;
  weight: number | null;
}

export interface IndicatorDependencies {
  slug: string;
  /** What this indicator is computed from. Empty for a direct indicator. */
  upstream: DependencyEdge[];
  /** What would have to be recomputed if this changed. */
  downstream: DependencyEdge[];
}

export function getDependencies(
  slug: string,
  init: { signal?: AbortSignal } = {},
): Promise<IndicatorDependencies> {
  return jsonRequest<IndicatorDependencies>(
    `/indicators/${encodeURIComponent(slug)}/dependencies`,
    init,
  );
}


export interface LineageNode {
  asset_key: string;
  kind: "source" | "external" | "derived";
  /** Hops from the indicator. 1 is a direct input. */
  depth: number;
  /** Freshness of that asset. `absent` means no materialization has been
   *  recorded — the state of every derived asset until the compute path
   *  writes one. */
  state: string | null;
}

export interface IndicatorLineage {
  slug: string;
  asset_key: string;
  upstream: LineageNode[];
  downstream: LineageNode[];
}

export function getLineage(
  slug: string,
  init: { signal?: AbortSignal } = {},
): Promise<IndicatorLineage> {
  return jsonRequest<IndicatorLineage>(
    `/indicators/${encodeURIComponent(slug)}/lineage`,
    init,
  );
}


// ── Recomputation impact (testing aid) ──────────────────────────────────────
//
// Declared here rather than imported from the ops page for the same reason
// this module does not import `../api.ts`: pages do not depend on each other's
// clients. Both go through `../http.ts`.

export interface ImpactPreview {
  indicators: string[];
  count: number;
  noop: boolean;
  reason: string;
}

export interface ChangeInput {
  dataset: string;
  attribute: string;
  indicators: string[];
  /** A hand-bumped stand-in for "this dataset changed", not a real field. */
  is_version_marker: boolean;
}

export interface DatasetDependency {
  dataset: string;
  relation: string;
  indicators: string[];
}

export interface ChangeVocabulary {
  attributes: ChangeInput[];
  datasets: DatasetDependency[];
  /** Registered datasets no indicator reads — changing one recomputes nothing. */
  unconsumed: string[];
}

export function listChangeInputs(
  init: { signal?: AbortSignal } = {},
): Promise<ChangeVocabulary> {
  return jsonRequest<ChangeVocabulary>("/jobs/attributes", init);
}

/** Ask the planner what a change would invalidate. Persists nothing. */
export function previewImpact(
  body: {
    changed_attributes?: string[];
    edited_features?: string[];
    changed_datasets?: string[];
  },
  init: { signal?: AbortSignal } = {},
): Promise<ImpactPreview> {
  return jsonRequest<ImpactPreview>("/jobs/preview", {
    ...init,
    method: "POST",
    body,
  });
}

export interface SubmitRunResponse {
  /** Null on a noop — nothing was persisted, so there is no run to open. */
  run_id: string | null;
  status: string;
}

/** Submit the change-set the preview just described. */
export function submitRun(
  body: { changed_attributes?: string[]; changed_datasets?: string[] },
  init: { signal?: AbortSignal } = {},
): Promise<SubmitRunResponse> {
  // `wait` is never sent: it holds the request open for the whole compute.
  return jsonRequest<SubmitRunResponse>("/jobs", { ...init, method: "POST", body });
}

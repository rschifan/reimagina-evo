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
 *  than an uncomputed one.
 *
 *  With ``scenarioId``, that scenario's results count toward ``aoiId`` as well
 *  as the baseline's. Both halves matter: without it the picker hides the five
 *  edificability indicators, which have no baseline rows at all and so exist
 *  only inside a scenario — exactly what a scenario is authored to show. It
 *  has no effect without ``aoiId``, which is what narrows. */
export function listIndicators(
  aoiId?: string | null,
  scenarioId?: string | null,
): Promise<IndicatorSummary[]> {
  const params = new URLSearchParams();
  if (aoiId) params.set("aoi_id", aoiId);
  if (scenarioId) params.set("scenario_id", scenarioId);
  const qs = params.toString();
  return jsonRequest<IndicatorSummary[]>(`/indicators${qs ? `?${qs}` : ""}`);
}


/** One indicator's catalog metadata, whether or not it holds values anywhere.
 *
 *  Unscoped on purpose where {@link listIndicators} is scoped: the density
 *  bound the proposal form prechecks against has to be readable *before* a
 *  scenario exists, and ``edificability_block_density`` has no baseline rows,
 *  so the AOI-scoped list cannot be asked for it. */
export function getIndicator(slug: string): Promise<IndicatorSummary> {
  return jsonRequest<IndicatorSummary>(`/indicators/${encodeURIComponent(slug)}`);
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
  /** The workspace this AOI belongs to. Read because creating a scenario
   *  needs it: ``POST /scenarios`` takes ``project_id`` rather than deriving
   *  it from the AOI, and this row is where an HTTP client already has it. */
  project_id: string;
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
  scenarioId?: string | null,
): string {
  const qs = scenarioId ? `?scenario_id=${encodeURIComponent(scenarioId)}` : "";
  return `${API_BASE}/indicators/${encodeURIComponent(slug)}/values.${format}${qs}`;
}


// ── scenarios ───────────────────────────────────────────────────────────
//
// The eight routes of `apps/api/routers/scenarios.py`, typed. Shapes mirror
// that module's pydantic models and, as everywhere else in this file, only
// the fields the map reads.
//
// ⚠ No refusal is interpreted here. Every one of these throws the `ApiError`
// `jsonRequest` throws, carrying the unread `Response`; `scenario.ts`'s
// `refusalText` is the single place a status becomes a sentence, so that a
// duplicate name reads the same wherever it is reported.


export interface ScenarioRow {
  id: string;
  project_id: string;
  aoi_id: string | null;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
  /** Null means the edits have not been expanded into overrides yet — a
   *  different statement from "expanded, and there was nothing to do". */
  materialized_at: string | null;
}


export interface EditRow {
  id: string;
  ordinal: number;
  type_id: string;
  attribute_key: string;
  value_numeric: number;
  geometry_wkt: string;
  created_at: string;
}


export interface ScenarioDetail extends ScenarioRow {
  override_count: number;
  edits: EditRow[];
}


export interface WrittenEdit {
  id: string;
  attribute_key: string;
  value_numeric: number;
}


export interface EditsWritten {
  scenario_id: string;
  feature_type: string;
  edits: WrittenEdit[];
}


/** One proposal: the drawn area and the three numbers proposed over it.
 *  The field names are the API's, which are the domain's, so a refusal can
 *  name the field the form posted without a translation table. */
export interface ProposalBody {
  area: GeoJSON.Feature | GeoJSON.FeatureCollection | GeoJSON.Geometry;
  levels: number;
  unit_size: number;
  people_per_household: number;
}


export interface RecomputeResponse {
  scenario_id: string;
  /** Per *edit*: how many features that edit's area caught inside the AOI.
   *  Zero is an answer — the area caught nothing — not an omission. */
  matched_per_edit: Record<string, number>;
  overrides_written: number;
  overrides_removed: number;
  results_superseded: number;
  /** Null for a submit that found nothing to recompute. */
  run_id: string | null;
  run_status: string;
  deduplicated: boolean;
}


export interface DeleteScenarioResponse {
  scenario_id: string;
  overrides: number;
  versions_retired: number;
  results_superseded: number;
  materializations: number;
}


/**
 * ⚠ Three counts and no total, mirroring the endpoint.
 *
 * ``changed + unchanged + new === row_count`` always — and that sum is *less*
 * than the AOI's block count whenever the baseline indicator has gaps, since a
 * feature with no numeric value on either side is absent from the rows by
 * design. There is deliberately no ``total`` to render against, and inventing
 * one here would be a lie about the size of the territory; the server's model
 * forbids extra keys, so it would also 500 the day it was sent back.
 */
export interface ComparisonSummary {
  changed: number;
  unchanged: number;
  new: number;
  /** Null when no row has a baseline to subtract from. Averaged over every
   *  comparable row, the untouched zeros included. */
  mean_delta: number | null;
  /** Whether a positive delta is an improvement. From the catalog, because
   *  only the catalog knows: four of the twelve indicators are distances. */
  higher_is_better: boolean;
}


export interface ComparisonRow {
  feature_id: string;
  baseline: number | null;
  scenario: number | null;
  /** Null for a ``new`` row: a number with nothing to compare against is not
   *  a change of that size. */
  delta: number | null;
  state: string;
}


export interface ComparisonResponse {
  scenario_id: string;
  indicator_slug: string;
  aoi_id: string | null;
  summary: ComparisonSummary;
  /** The whole comparison, whatever ``limit`` did to ``rows``. */
  row_count: number;
  rows: ComparisonRow[];
  rows_truncated: boolean;
}


export function listScenarios(
  filters: { projectId?: string | null; aoiId?: string | null } = {},
): Promise<ScenarioRow[]> {
  const params = new URLSearchParams();
  if (filters.projectId) params.set("project_id", filters.projectId);
  if (filters.aoiId) params.set("aoi_id", filters.aoiId);
  const qs = params.toString();
  return jsonRequest<ScenarioRow[]>(`/scenarios${qs ? `?${qs}` : ""}`);
}


export function createScenario(body: {
  project_id: string;
  name: string;
  aoi_id?: string | null;
  description?: string | null;
}): Promise<ScenarioDetail> {
  return jsonRequest<ScenarioDetail>("/scenarios", { body });
}


export function getScenario(scenarioId: string): Promise<ScenarioDetail> {
  return jsonRequest<ScenarioDetail>(`/scenarios/${encodeURIComponent(scenarioId)}`);
}


export function deleteScenario(scenarioId: string): Promise<DeleteScenarioResponse> {
  return jsonRequest<DeleteScenarioResponse>(
    `/scenarios/${encodeURIComponent(scenarioId)}`,
    { method: "DELETE" },
  );
}


export function addScenarioEdits(
  scenarioId: string,
  body: ProposalBody,
): Promise<EditsWritten> {
  return jsonRequest<EditsWritten>(
    `/scenarios/${encodeURIComponent(scenarioId)}/edits`,
    { body },
  );
}


/**
 * Remove one rule from a scenario.
 *
 * ⚠ Not through {@link jsonRequest}: the route answers 204 with an empty body,
 * and `response.json()` on no bytes rejects with a SyntaxError — a successful
 * delete reported to the user as "Unexpected end of JSON input". The status is
 * the whole answer, so there is nothing to parse.
 */
export async function removeScenarioEdit(
  scenarioId: string,
  editId: string,
): Promise<void> {
  const path =
    `/scenarios/${encodeURIComponent(scenarioId)}/edits` +
    `?edit_id=${encodeURIComponent(editId)}`;
  const response = await fetch(`${API_BASE}${path}`, {
    method: "DELETE",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new ApiError(`${response.status} ${response.statusText} on ${path}`, response);
  }
}


export function recomputeScenario(scenarioId: string): Promise<RecomputeResponse> {
  return jsonRequest<RecomputeResponse>(
    `/scenarios/${encodeURIComponent(scenarioId)}/recompute`,
    { method: "POST" },
  );
}


export function getComparison(
  scenarioId: string,
  options: { indicator_slug: string; aoi_id?: string | null; limit?: number },
): Promise<ComparisonResponse> {
  const params = new URLSearchParams({ indicator_slug: options.indicator_slug });
  if (options.aoi_id) params.set("aoi_id", options.aoi_id);
  if (options.limit) params.set("limit", String(options.limit));
  return jsonRequest<ComparisonResponse>(
    `/scenarios/${encodeURIComponent(scenarioId)}/comparison?${params.toString()}`,
  );
}


/** One run's current state, as `GET /jobs/{run_id}` serves it. */
export interface RunView {
  run_id: string;
  status: string;
  units: Array<{ indicator: string; status: string; detail: string | null }>;
}


/** Read a run's status. Used to follow the recompute a scenario enqueued:
 *  `POST /scenarios/{id}/recompute` answers 202 with a run id, and the
 *  results the map draws do not exist until that run lands. */
export function getRun(runId: string): Promise<RunView> {
  return jsonRequest<RunView>(`/jobs/${encodeURIComponent(runId)}`);
}

/**
 * Typed fetch helpers for the API.
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
  /** What the indicator measures and how to read it. */
  description: string | null;
  /** Null for indicators no family claims, which today are the composites. */
  model_family: string | null;
  /** A bound that means "beyond measurement" (the distance indicators report
   *  1500 m for an unreachable block); ``null`` when there is none. Unlike
   *  ``valid_min``/``valid_max``, which only validate, this changes how a value
   *  reads and paints. */
  cap_value: number | null;
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
  /** Over the same rows as ``count``; ``null`` when there are none. */
  sum: number | null;
  /** Rows ``>= cap_value`` over the same rows as ``count``; ``null`` when the
   *  indicator has no cap. Optional only so fixtures written before the field
   *  still type; the API always sends it. */
  count_at_cap?: number | null;
  /** Rows on blocks whose scenario inputs are estimated (Overture maps none
   *  or too little of their buildings), over the same rows as ``count``.
   *  Optional for the same reason as ``count_at_cap``. */
  count_estimated?: number;
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
 *  than an uncomputed one. The scenario workspace does not use this list:
 *  it offers {@link listReachableIndicators}. */
export function listIndicators(aoiId?: string | null): Promise<IndicatorSummary[]> {
  const params = new URLSearchParams();
  if (aoiId) params.set("aoi_id", aoiId);
  const qs = params.toString();
  return jsonRequest<IndicatorSummary[]>(`/indicators${qs ? `?${qs}` : ""}`);
}


/** An indicator a scenario rule can change, and the number a summary table
 *  shows for it: a total for counts, a median for rates. */
export interface ReachableIndicator extends IndicatorSummary {
  statistic: "sum" | "median";
}


/** What a scenario rule can change, inputs before dependents. Derived on the
 *  server from the catalog, so the workspace never offers an indicator a rule
 *  cannot move. */
export function listReachableIndicators(): Promise<ReachableIndicator[]> {
  return jsonRequest<ReachableIndicator[]>("/scenarios/indicators");
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

/** One feature's value for one indicator, with its exact rank among the
 *  AOI's features of the same grain (`count` 0 when there are none). */
export interface FeatureIndicatorValue {
  slug: string;
  name: string;
  unit: string | null;
  cap_value: number | null;
  value: number;
  count_below: number;
  count_equal: number;
  count: number;
}

/** Every current indicator value of one feature; `[]` when it has none. */
export function getFeatureIndicators(
  featureId: string,
  options: { aoi_id?: string; scenario_id?: string; signal?: AbortSignal } = {},
): Promise<FeatureIndicatorValue[]> {
  const params = new URLSearchParams();
  if (options.aoi_id) params.set("aoi_id", options.aoi_id);
  if (options.scenario_id) params.set("scenario_id", options.scenario_id);
  const qs = params.toString();
  return jsonRequest<FeatureIndicatorValue[]>(
    `/features/${encodeURIComponent(featureId)}/indicators${qs ? `?${qs}` : ""}`,
    { signal: options.signal },
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


/** Features of each type intersecting ``polygon``. POST, not GET: the polygon
 *  does not fit a query string, and a GET cached by URL would answer a redrawn
 *  shape at the same vertices with a stale 304. */
export function fetchMetricsWithinPolygon(
  body: WithinPolygonRequest,
  init: { signal?: AbortSignal } = {},
): Promise<WithinPolygonResponse> {
  return jsonRequest<WithinPolygonResponse>("/metrics/within-polygon", { body, signal: init.signal });
}


/** An indicator's ``/stats`` numbers over the features whose centroid lies in
 *  ``polygon`` (no histogram). */
export function indicatorWithinPolygon(
  body: { aoi_id: string; polygon: GeoJSON.Polygon; indicator_slug: string; feature_type?: string },
  init: { signal?: AbortSignal } = {},
): Promise<IndicatorValueStats> {
  return jsonRequest<IndicatorValueStats>("/metrics/indicator-within-polygon", { body, signal: init.signal });
}


/** Asked for on every download: the API's ceiling rather than its 100,000
 *  default, which cut larger AOIs short without saying so. */
export const DOWNLOAD_ROW_CAP = 500_000;

/** Download URL for one indicator's values, scoped like the map or stats
 *  beside it. Not fetched — handed to an anchor so the browser streams it and
 *  the API sets Content-Disposition. */
export function downloadUrl(
  slug: string,
  format: "csv" | "geojson" | "parquet",
  scope: { aoiId: string | null; grain: string | null; scenarioId?: string | null },
): string {
  const p = new URLSearchParams({ limit: String(DOWNLOAD_ROW_CAP) });
  if (scope.aoiId) p.set("aoi_id", scope.aoiId);
  if (scope.grain) p.set("feature_type", scope.grain);
  if (scope.scenarioId) p.set("scenario_id", scope.scenarioId);
  return `${API_BASE}/indicators/${encodeURIComponent(slug)}/values.${format}?${p}`;
}


// ── scenarios ───────────────────────────────────────────────────────────
//
// The routes of `apps/api/routers/scenarios.py`, typed. Shapes mirror
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
  /** Null means the rules have not been expanded into overrides yet — a
   *  different statement from "expanded, and there was nothing to do". */
  materialized_at: string | null;
}


/** A `GET /scenarios` row: the scenario plus what a summary of all of them
 *  needs. Only the list carries these; a detail does not. */
export interface ScenarioListRow extends ScenarioRow {
  rule_count: number;
  /** The newest run (its envelope, the id the Runs page uses), or null. A
   *  queued run has no envelope until a worker opens it, so during that
   *  window this names the previous run. */
  latest_run: { id: string; status: string } | null;
}


/** The numbers a rule sets, by the API's names. A number left out is not set:
 *  every block keeps its own value for it. */
export interface RuleInputs {
  levels?: number;
  unit_size?: number;
  people_per_household?: number;
  /** Share of the block built on, `0 < c ≤ 1`, when set; with `abs` a signed
   *  share change (0.05 = +5 points), with `pct` a percent of the block's own. */
  lot_coverage?: number;
}


/** Per number, how a rule applies it (r059): `set` (the blocks' new value),
 *  `abs` (an amount; coverage as a share) or `pct`. Missing: `set`. One rule
 *  can so set one number and change another. */
export type RuleUnits = Partial<Record<keyof RuleInputs, RuleUnit>>;
/** How one number of a rule applies: an amount, a percentage, or "set" (the
 *  block's new value). */
export type RuleUnit = "abs" | "pct" | "set";
/** What a rule request carries besides its area. */
export type RuleChange = { inputs: RuleInputs; units?: RuleUnits };

/** One rule as the server stores it: a row with its own id. */
export interface RuleRow {
  id: string;
  /** Its place in "later wins": fixed when the rule was created. */
  ordinal: number;
  type_id: string;
  inputs: RuleInputs;
  /** Absent on rows from servers before r058: every number set. */
  units?: RuleUnits;
  geometry_wkt: string;
  created_at: string;
  updated_at: string;
}


export interface ScenarioDetail extends ScenarioRow {
  override_count: number;
  rules: RuleRow[];
}


export interface RecomputeResponse {
  scenario_id: string;
  /** Per rule: how many blocks its area caught inside the AOI. */
  matched_per_rule: Record<string, number>;
  overrides_written: number;
  overrides_removed: number;
  results_superseded: number;
  /** Null when nothing needed recomputing. */
  run_id: string | null;
  run_status: string;
  deduplicated: boolean;
}


/** A rule written (or deleted), materialised, and its recompute enqueued. */
export interface RuleChangeResponse extends RecomputeResponse {
  /** The rule as stored; null after a delete. */
  rule: RuleRow | null;
}


export interface DeleteScenarioResponse {
  scenario_id: string;
  overrides: number;
  versions_deleted: number;
  results_deleted: number;
  job_runs_deleted: number;
  materializations: number;
  runs_deleted: number;
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
): Promise<ScenarioListRow[]> {
  const params = new URLSearchParams();
  if (filters.projectId) params.set("project_id", filters.projectId);
  if (filters.aoiId) params.set("aoi_id", filters.aoiId);
  const qs = params.toString();
  return jsonRequest<ScenarioListRow[]>(`/scenarios${qs ? `?${qs}` : ""}`);
}


type Area = GeoJSON.Feature | GeoJSON.FeatureCollection | GeoJSON.Geometry;


/** A created scenario, and its first rule's change when one was sent. */
export interface CreatedScenario extends ScenarioDetail {
  first_rule: RuleChangeResponse | null;
}


/** Create a scenario, optionally with its first rule: one transaction, so a
 *  refused rule (4xx) leaves no scenario behind. */
export function createScenario(body: {
  project_id: string;
  name: string;
  aoi_id: string;
  description?: string | null;
  rule?: { area: Area } & RuleChange;
}): Promise<CreatedScenario> {
  return jsonRequest<CreatedScenario>("/scenarios", { body });
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


/** Draw a rule. Written and materialised in one transaction, so a refusal
 *  (4xx) stores nothing; then its recompute is enqueued. */
export function addRule(
  scenarioId: string,
  body: { area: Area } & RuleChange,
): Promise<RuleChangeResponse> {
  return jsonRequest<RuleChangeResponse>(
    `/scenarios/${encodeURIComponent(scenarioId)}/rules`,
    { body },
  );
}


/** Replace a rule's numbers (its whole set), and its area when given. It
 *  keeps its id and its place in "later wins"; a refusal changes nothing. */
export function updateRule(
  scenarioId: string,
  ruleId: string,
  body: { area?: Area } & RuleChange,
): Promise<RuleChangeResponse> {
  return jsonRequest<RuleChangeResponse>(
    `/scenarios/${encodeURIComponent(scenarioId)}/rules/${encodeURIComponent(ruleId)}`,
    { method: "PATCH", body },
  );
}


/** Delete a rule — one row, one request — and recompute what is left. */
export function deleteRule(scenarioId: string, ruleId: string): Promise<RuleChangeResponse> {
  return jsonRequest<RuleChangeResponse>(
    `/scenarios/${encodeURIComponent(scenarioId)}/rules/${encodeURIComponent(ruleId)}`,
    { method: "DELETE" },
  );
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


/** Read a run's status. Used to follow the recompute a rule change enqueued:
 *  writing a rule answers with a run id, and the
 *  results the map draws do not exist until that run lands. */
export function getRun(runId: string): Promise<RunView> {
  return jsonRequest<RunView>(`/jobs/${encodeURIComponent(runId)}`);
}

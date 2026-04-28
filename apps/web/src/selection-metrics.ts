/**
 * Server-backed sub-AOI metrics.
 *
 * Calls ``POST /metrics/within-polygon`` (see
 * ``apps/api/routers/metrics.py``) for an authoritative count of
 * features intersecting the selection polygon. The endpoint runs
 * ``ST_Intersects`` against the GiST-indexed
 * ``serving_geo.feature.geometry`` column — sub-100 ms even at CDMX
 * scale (1.6 M building polygons).
 *
 * Supersedes the previous client-side path that walked
 * ``queryRenderedFeatures`` + ``querySourceFeatures`` and ran a JS
 * point-in-polygon test. That approach silently undercounted
 * whenever the renderer hadn't loaded the relevant tiles (zoomed
 * out, layer toggled off, slow tile fetches mid-drag) — the server
 * answer doesn't depend on what's rendered.
 *
 * Cancellation contract: pass an ``AbortSignal`` so the caller can
 * supersede an in-flight request when a newer selection arrives.
 * The same ``renderToken`` pattern that ``main.ts:renderIndicator``
 * uses for stats fetches.
 */
import { fetchMetricsWithinPolygon, type WithinPolygonResponse } from "./api";
import type { SelectionGeometry } from "./selection";


export interface MetricSpec {
  /** Shown in the panel — "Buildings selected", "Blocks selected", … */
  label: string;
  /**
   * Feature type slug recognised by the API (``building``, ``block``,
   * ``ageb``, ``municipality``). Maps directly to the
   * ``serving_geo.feature.type_slug`` column.
   */
  typeSlug: string;
  /**
   * Optional formatter for display. Defaults to a locale-aware
   * integer formatter — counts always come back as integers.
   */
  formatter?: (n: number) => string;
}


export interface MetricResult {
  spec: MetricSpec;
  /** Count of features inside the selection. ``0`` when the endpoint
   *  returned no row for this type or the request errored — pair
   *  with ``display === "—"`` to distinguish "0 selected" from a
   *  fetch failure if needed. */
  featuresCounted: number;
  /** Pre-formatted string for the panel — ``"—"`` for empty / errored
   *  results, formatted count otherwise. */
  display: string;
}


/**
 * Stream per-spec metrics: fires one HTTP request per spec in
 * parallel, calls ``onResult`` as each resolves. Lets the panel
 * render fast layers (block / ageb / muni — sub-100ms each) while
 * the slow layer (buildings — hundreds of ms) is still in flight.
 *
 * Returns a Promise that resolves once all specs have either
 * resolved or errored. Aborts via ``signal`` cancel ALL in-flight
 * requests; the returned promise rejects with AbortError.
 *
 * Why one request per spec rather than one batched request: the
 * batched version's response time = max(spec query times). Parallel
 * lets the panel show 3-of-4 numbers in <100 ms even when the
 * fourth (buildings) takes 500 ms+. Same total work on the server
 * (Postgres pools the connections); much better perceived
 * latency.
 */
export async function streamMetrics(
  selection: SelectionGeometry,
  specs: MetricSpec[],
  aoiId: string,
  onResult: (result: MetricResult) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (specs.length === 0) return;
  await Promise.all(
    specs.map(async (spec) => {
      let response: WithinPolygonResponse;
      try {
        response = await fetchMetricsWithinPolygon({
          aoi_id: aoiId,
          polygon: selection.feature.geometry,
          type_slugs: [spec.typeSlug],
        }, { signal });
      } catch (err) {
        if ((err as { name?: string }).name === "AbortError") {
          // Propagate so Promise.all rejects with AbortError; the
          // caller's catch already handles the abort case as "drop
          // result" rather than user-visible failure.
          throw err;
        }
        // Per-spec failure: emit a "—" for this spec only, leave
        // siblings to resolve normally. The user sees which layer
        // failed, not a blanket panel error.
        onResult({ spec, featuresCounted: 0, display: "—" });
        console.warn(`metrics: ${spec.typeSlug} fetch failed:`, err);
        return;
      }
      const row = response.results.find((r) => r.type_slug === spec.typeSlug);
      const n = row?.count ?? 0;
      // Genuine zero counts render as "0" so the panel can
      // distinguish "selection valid, no features matched" from a
      // fetch failure (which renders as "—"). The panel filter
      // currently hides zero-count rows; both reads remain
      // available for future UI tweaks.
      const formatter = spec.formatter ?? _defaultFormatter;
      onResult({
        spec,
        featuresCounted: n,
        display: n === 0 ? "0" : formatter(n),
      });
    }),
  );
}


function _defaultFormatter(n: number): string {
  return n.toLocaleString();
}

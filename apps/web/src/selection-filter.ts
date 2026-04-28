/**
 * Hide value-layer features outside the active selection.
 *
 * Industry-standard "clip choropleth to selected region" pattern.
 * For value layers (indicator overlays, aggregation overlays), the
 * spotlight mask's dim-outside treatment reads wrong — the user
 * wants the choropleth visible ONLY where they've selected, not
 * dimmed everywhere. This module implements that.
 *
 * Mechanism (mirrors :mod:`hover-highlight`'s feature-state idiom):
 *   1. Source must already have ``promoteId: "feature_id"`` so
 *      ``feature.id`` is populated from the stable MVT property.
 *   2. On every (debounced) selection change, walk the source's
 *      currently-loaded features, bbox-prefilter, point-in-polygon
 *      test on each centroid, and ``setFeatureState({source,
 *      sourceLayer, id}, {inSelection: true})`` for inside features.
 *   3. Install the layer filter
 *      ``["boolean", ["feature-state", "inSelection"], false]`` —
 *      MapLibre stops rendering features that don't have the state
 *      flag. Outside-the-selection features therefore disappear
 *      entirely (no dim wash, just absent).
 *   4. ``sourcedata`` re-classifies when new tiles parse so newly-
 *      loaded features get the same treatment.
 *   5. On clear, restore the layer's pre-selection filter (or null)
 *      and remove all feature-state.
 *
 * Why feature-state over ``["within", polygon]`` filter: maplibre 5
 * reliably evaluates feature-state in filter expressions, but
 * ``within`` has shown silent-failure behaviour in this codebase
 * already. Feature-state matches the path :func:`enableHoverHighlight`
 * uses successfully across the same sources.
 *
 * Why client-side rather than a server-side polygon filter on the
 * tile function: each unique polygon would invalidate the tile
 * cache; during drag the user generates dozens of distinct polygons.
 * Client-side keeps tiles cached and only mutates feature-state.
 *
 * Reuse contract: this module knows nothing about ``INPUT_LAYERS``
 * or our application layer ids. Callers register any vector source
 * + layer ids they want clipped.
 */
import type { FilterSpecification, Map as MapLibreMap } from "maplibre-gl";

import {
  pointInPolygon,
  polygonBbox,
  representativePoint,
} from "./selection-geom";
import type { SelectionController, SelectionGeometry } from "./selection";


export interface ValueAwareLayerConfig {
  /** Fill / line / circle layer ids to clip. All listed layers share
   *  the same source + sourceLayer; their filters are kept in sync
   *  so the in-selection ANDing applies uniformly. */
  layerIds: string[];
  /** MapLibre source name. Source MUST have ``promoteId`` set so
   *  ``feature.id`` is populated. */
  sourceName: string;
  /** Vector source layer (the inner MVT layer name). */
  sourceLayer: string;
  /** Optional pre-existing filter on each layer. ANDed with the in-
   *  selection check so this module doesn't clobber whatever the
   *  caller had. Pass ``null`` (or omit) when the layer had no filter. */
  baseFilter?: FilterSpecification | null;
}


export function registerValueAwareLayer(
  map: MapLibreMap,
  controller: SelectionController,
  cfg: ValueAwareLayerConfig,
): () => void {
  // ``coalesce`` reads the feature-state and falls back to false
  // for features that haven't been flagged. ``== true`` makes the
  // filter accept only flagged features. TypeScript's
  // FilterSpecification union over-constrains the array shape for
  // expression-form filters, so we cast at the boundary.
  const inSelectionFilter = [
    "==",
    ["coalesce", ["feature-state", "inSelection"], false],
    true,
  ] as unknown as FilterSpecification;

  // Snapshot the filter live on the layer at registration time.
  // Same idea as ``enableHoverHighlight`` capturing the layer's
  // current colour / line-width for unhover restore: the dispose /
  // clear path puts back exactly what we changed, regardless of
  // whether a future caller mutated the filter after our register.
  // ``cfg.baseFilter`` (when explicitly passed) takes precedence —
  // the caller is asserting "compose with this exact base".
  const restoreFilters = new Map<string, FilterSpecification | null>();
  for (const layerId of cfg.layerIds) {
    if (!map.getLayer(layerId)) continue;
    const live = cfg.baseFilter !== undefined
      ? cfg.baseFilter
      : (map.getFilter(layerId) as FilterSpecification | undefined) ?? null;
    restoreFilters.set(layerId, live);
  }
  // Compose with the caller's base filter when explicitly provided.
  // Use the same ``!= null`` test as ``restoreFilters`` above — a
  // truthiness check would silently drop a ``false`` filter (which
  // MapLibre treats as a valid "filter everything out" expression),
  // diverging from the restore-path semantics.
  const hasBase = cfg.baseFilter !== undefined && cfg.baseFilter !== null;
  const activeFilter: FilterSpecification = hasBase
    ? (["all", cfg.baseFilter, inSelectionFilter] as unknown as FilterSpecification)
    : inSelectionFilter;

  // Track the ids we currently flag inSelection so we can clear them
  // efficiently when the selection changes (rather than walking every
  // feature on every transition).
  const flaggedIds = new Set<string | number>();

  const clearFeatureState = (): void => {
    for (const id of flaggedIds) {
      map.removeFeatureState(
        { source: cfg.sourceName, sourceLayer: cfg.sourceLayer, id },
        "inSelection",
      );
    }
    flaggedIds.clear();
  };

  const classifyAndMark = (selection: SelectionGeometry): void => {
    const features = _collectFeatures(map, cfg.sourceName, cfg.sourceLayer);
    const polygonRings = selection.feature.geometry.coordinates;
    const bbox = polygonBbox(polygonRings[0]);
    const nextIds = new Set<string | number>();
    for (const f of features) {
      const id = f.id ?? (f.properties as { feature_id?: string | number })?.feature_id;
      if (id === undefined || id === null) continue;
      if (nextIds.has(id)) continue;
      const pt = representativePoint(f.geometry);
      if (pt === null) continue;
      if (bbox !== null) {
        const [x, y] = pt;
        if (x < bbox[0] || x > bbox[2] || y < bbox[1] || y > bbox[3]) continue;
      }
      if (!pointInPolygon(pt, polygonRings)) continue;
      nextIds.add(id);
    }
    // Diff: only setFeatureState for newly-inside ids, only
    // removeFeatureState for newly-outside ones. Reduces GPU buffer
    // invalidation churn vs. clear-and-reset every cycle.
    for (const id of flaggedIds) {
      if (!nextIds.has(id)) {
        map.removeFeatureState(
          { source: cfg.sourceName, sourceLayer: cfg.sourceLayer, id },
          "inSelection",
        );
      }
    }
    for (const id of nextIds) {
      if (!flaggedIds.has(id)) {
        map.setFeatureState(
          { source: cfg.sourceName, sourceLayer: cfg.sourceLayer, id },
          { inSelection: true },
        );
      }
    }
    flaggedIds.clear();
    for (const id of nextIds) flaggedIds.add(id);
  };

  let activeSelection: SelectionGeometry | null = null;

  const apply = (selection: SelectionGeometry | null): void => {
    activeSelection = selection;
    if (selection === null) {
      // Restore pre-selection filter and drop all in-selection state.
      // ``restoreFilters`` was snapshotted at registration time so we
      // put back exactly what was on each layer, not a clobbered
      // ``null`` that erases any filter set after registration.
      clearFeatureState();
      for (const layerId of cfg.layerIds) {
        if (!map.getLayer(layerId)) continue;
        map.setFilter(layerId, restoreFilters.get(layerId) ?? null);
      }
      return;
    }
    // Order matters: classify (set feature-state for inside features)
    // BEFORE setFilter. Reverse order causes a one-frame blank flash
    // — setFilter immediately re-evaluates the layer with the
    // ``feature-state.inSelection``-keyed expression, but no
    // feature has the state yet, so EVERY feature filters out. The
    // subsequent setFeatureState calls then bring the inside ones
    // back. Also, ``queryRenderedFeatures`` inside classifyAndMark
    // would return nothing on the post-filter pass; only
    // ``querySourceFeatures`` would carry the load. Classifying
    // first means the un-filtered render still feeds
    // queryRenderedFeatures and the transition is
    // ``all features → all features → only inside``, no flash.
    classifyAndMark(selection);
    for (const layerId of cfg.layerIds) {
      if (!map.getLayer(layerId)) continue;
      map.setFilter(layerId, activeFilter);
    }
  };

  // ``sourcedata`` re-classifies newly-arrived tiles so features
  // loaded after the selection was drawn (e.g. user pans into new
  // territory) get the same in-selection treatment.
  const onSourceData = (e: { sourceId?: string; isSourceLoaded?: boolean }): void => {
    if (activeSelection === null) return;
    if (e.sourceId !== cfg.sourceName) return;
    if (!e.isSourceLoaded) return;
    classifyAndMark(activeSelection);
  };
  map.on("sourcedata", onSourceData);

  const unsubscribe = controller.subscribe(apply, { debounceMs: 150 });

  return () => {
    unsubscribe();
    map.off("sourcedata", onSourceData);
    clearFeatureState();
    for (const layerId of cfg.layerIds) {
      if (!map.getLayer(layerId)) continue;
      map.setFilter(layerId, restoreFilters.get(layerId) ?? null);
    }
  };
}


// ─── Feature collection ───────────────────────────────────────────────


function _collectFeatures(
  map: MapLibreMap,
  sourceName: string,
  sourceLayer: string,
): GeoJSON.Feature[] {
  // Union of rendered + source features — same robustness trick the
  // metrics module uses; each path drops features in different
  // cases (low zoom: source not parsed; tiny opacity: not painted).
  const out: GeoJSON.Feature[] = [];
  try {
    const layers = map.getStyle().layers ?? [];
    const layerIds = layers
      .filter((l) => "source" in l && l.source === sourceName)
      .map((l) => l.id);
    if (layerIds.length > 0) {
      const rendered = map.queryRenderedFeatures({ layers: layerIds });
      out.push(...(rendered as unknown as GeoJSON.Feature[]));
    }
  } catch (err) {
    console.warn(`selection-filter: queryRenderedFeatures failed for ${sourceName}:`, err);
  }
  try {
    const source = map.querySourceFeatures(sourceName, { sourceLayer });
    out.push(...(source as unknown as GeoJSON.Feature[]));
  } catch (err) {
    console.warn(
      `selection-filter: querySourceFeatures failed for ${sourceName}/${sourceLayer}:`, err,
    );
  }
  return out;
}

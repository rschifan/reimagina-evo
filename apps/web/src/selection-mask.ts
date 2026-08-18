/**
 * Spotlight mask overlay for sub-AOI selections.
 *
 * Industry-standard pattern (Felt, Mapbox Studio, kepler.gl, Google
 * Maps focus regions): one full-viewport fill layer covering the
 * world with the active selection cut out as a hole. The dim
 * "outside" effect is then a single GPU-cheap fill draw, not N
 * per-feature classifications across N data layers.
 *
 * Why this beats per-layer feature-state classification at the scales
 * we care about (1.6 M building polygons across CDMX):
 *   - O(1) work per drag frame instead of O(N×L) where N is
 *     visible features and L is registered layers.
 *   - One ``setData`` call on a tiny GeoJSON source (≤67 vertices for
 *     a 64-step circle, 5 for a rectangle) — MapLibre updates a
 *     handful of vertex-buffer bytes per frame.
 *   - No ``setFeatureState``/``removeFeatureState`` churn (each call
 *     invalidates GPU buffers for the affected feature; doing it
 *     thousands of times during a drag stutters even on M-class
 *     hardware).
 *   - No ``sourcedata`` re-classification when new tiles arrive —
 *     the mask is a polygon, not feature-keyed, so it dims tiles
 *     that arrive after the selection was drawn for free.
 *   - No coupling to MVT ``promoteId`` or ``feature_id`` schemes;
 *     works against any layer the user has rendered, including
 *     rasters and the AGEB / municipality overlays.
 *
 * Visual contract: a uniform dark wash sits over everything outside
 * the selection ring. The selection's interior renders untouched —
 * features, choropleths, rasters all keep their natural styling. The
 * cyan terra-draw outline must be added AFTER this layer so it
 * paints on top of the wash; calling :func:`installSelectionMask`
 * BEFORE :func:`enableSelection` is the supported order in
 * ``main.ts``.
 *
 * The trade-off is that "highlight inside" cannot recolor features
 * — it just leaves them at their natural styling. For the prototype
 * "compute metrics for a sub-area" workflow that's exactly right:
 * the user wants to see the data inside as-they-know-it, not a
 * recoloured variant.
 */
import type { GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";

import type { SelectionController, SelectionGeometry } from "./selection";


// ``DIM_COLOR`` matches the app background so dimmed areas read as
// "fading into the canvas" rather than "tinted blue/grey".
// ``DIM_OPACITY`` is strong enough to drop the eye's attention without
// costing the operator geographic context for the rest of the AOI.
const DIM_COLOR = "#0b1220";
const DIM_OPACITY = 0.7;


/** Default mask layer id. Exported so callers (e.g. ``choropleth.ts``)
 *  can pass it as a ``beforeId`` when adding their own layers, ensuring
 *  data overlays sit BELOW the dim wash and get dimmed too. */
export const SELECTION_MASK_LAYER_ID = "__selection_mask__fill";
const SELECTION_MASK_SOURCE_ID = "__selection_mask";


// World-extent outer ring that wraps the selection's outer ring as a
// hole. Web Mercator clips at ±85.0511°, but stretching the ring a
// touch past that doesn't hurt — MapLibre clips for us. Going to ±180
// in longitude covers the whole map regardless of zoom / pan.
//
// Winding: counter-clockwise (SW → NW → NE → SE → SW) per RFC 7946
// §3.1.6 for exterior rings. The selection's outer ring (passed as
// the second ring) is the interior / hole — RFC says interior rings
// are CW, which terra-draw's polygon emission already satisfies for
// the shapes we draw.
const _WORLD_RING: number[][] = [
  [-180, -85.0511],
  [-180,  85.0511],
  [ 180,  85.0511],
  [ 180, -85.0511],
  [-180, -85.0511],
];


export interface SelectionMaskHandle {
  /** Wire the mask up to a selection controller. Returns the
   *  unsubscribe handle. */
  bindController(controller: SelectionController): () => void;
}


/**
 * Install the mask source + fill layer. Call AFTER
 * ``installInputLayers`` (so the mask paints over data layers) and
 * BEFORE ``enableSelection`` (so terra-draw's shape outline paints on
 * top of the mask), then ``bindController`` once the controller
 * exists.
 */
export function installSelectionMask(map: MapLibreMap): SelectionMaskHandle {
  const sourceId = SELECTION_MASK_SOURCE_ID;
  const layerId = SELECTION_MASK_LAYER_ID;

  // HMR-safe idempotency: dispose any prior installation rather than
  // returning a dead no-op handle. On Vite hot reload the map
  // instance survives but the module re-evaluates — the previous
  // module's source/layer + closures are stale, and any "dead handle"
  // pattern leaves the new bindController() registering against a
  // detached subscription. Removing first lets the install path
  // below recreate the layer with the new module's closures.
  if (map.getLayer(layerId)) map.removeLayer(layerId);
  if (map.getSource(sourceId)) map.removeSource(sourceId);

  map.addSource(sourceId, {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: layerId,
    type: "fill",
    source: sourceId,
    paint: {
      "fill-color": DIM_COLOR,
      "fill-opacity": DIM_OPACITY,
      "fill-antialias": false,
    },
  });

  const sourceRef = map.getSource(sourceId) as GeoJSONSource;

  const setSelection = (selection: SelectionGeometry | null): void => {
    if (selection === null) {
      sourceRef.setData({ type: "FeatureCollection", features: [] });
      return;
    }
    const polygonRings = selection.feature.geometry.coordinates;
    if (!polygonRings.length) return;
    // The mask is a polygon with the world as outer ring and the
    // selection's outer ring as a hole. Anything inside the hole
    // gets painted with the underlying data layers; the rest gets
    // the dim wash on top.
    sourceRef.setData({
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry: {
            type: "Polygon",
            coordinates: [_WORLD_RING, polygonRings[0]],
          },
          properties: {},
        },
      ],
    });
  };

  return {
    bindController(controller: SelectionController): () => void {
      // Live (undebounced) subscriber: the mask MUST track
      // terra-draw's shape at 60 fps for the spotlight to feel
      // attached to the gesture. setData on a 5- to 67-vertex
      // polygon is the cheapest operation MapLibre exposes —
      // comfortably under 1 ms per frame.
      return controller.subscribe(setSelection);
    },
  };
}

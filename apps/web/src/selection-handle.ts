/**
 * Custom radius handle for circle selections.
 *
 * Industry-standard pattern (Felt, Mapbox-Studio, Google Maps draw,
 * kepler.gl): a SINGLE visible dot on the circle's rim that the user
 * drags to change the radius. The centre stays fixed, the radius
 * follows the cursor. Discoverable, low-visual-noise, and matches
 * what every map user already expects from a "draw a circle" tool.
 *
 * Why a separate map layer instead of styling terra-draw's selection
 * points: terra-draw renders one selection point per polygon vertex
 * (64 for a circle). Per-vertex styling can hide the others, but the
 * styling-function path was unreliable in our tests — sometimes
 * returning size 0 for all points, sometimes for none, depending on
 * obscure property timing. A standalone source + layer we own
 * outright is deterministic and easier to reason about.
 *
 * Architecture:
 *   - One GeoJSON source whose feature is a Point at the east-
 *     cardinal of the active circle's rim (vertex 0 — terra-draw's
 *     starting angle for circle generation).
 *   - One MapLibre circle layer paints the dot in cyan above
 *     terra-draw's outline.
 *   - mousedown on the layer → start a custom drag: capture initial
 *     centre + radius, then on every mousemove compute the new
 *     radius from cursor distance and call
 *     ``controller.setCircleRing``. terra-draw's
 *     ``updateFeatureGeometry`` flows the new polygon back through
 *     the store; the change event re-publishes; this module's
 *     subscriber updates the handle position; mask + metrics update
 *     via their own subscribers.
 *   - mouseup → end drag.
 *
 * Reuse: nothing here is specific to ``INPUT_LAYERS``. The module
 * speaks to the controller and the map only.
 */
import type { GeoJSONSource, Map as MapLibreMap, MapLayerMouseEvent } from "maplibre-gl";

import type { SelectionController, SelectionGeometry } from "./selection";


// Cyan matches terra-draw's shape outline (visual continuity); the
// dark stroke matches the app background so the dot reads as "raised"
// against whatever feature colour sits under it.
const SOURCE_ID = "__selection_handle";
const LAYER_ID = "__selection_handle__dot";
const RADIUS = 8;
const COLOR = "#22d3ee";
const OUTLINE_COLOR = "#0b1220";


export function installSelectionHandle(
  map: MapLibreMap,
  controller: SelectionController,
): void {
  const sourceId = SOURCE_ID;
  const layerId = LAYER_ID;

  // HMR-safe install: dispose any prior layer/source before adding
  // the new ones. Same idempotency contract as ``selection-mask``.
  if (map.getLayer(layerId)) map.removeLayer(layerId);
  if (map.getSource(sourceId)) map.removeSource(sourceId);

  map.addSource(sourceId, {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  map.addLayer({
    id: layerId,
    type: "circle",
    source: sourceId,
    paint: {
      "circle-radius": RADIUS,
      "circle-color": COLOR,
      "circle-stroke-color": OUTLINE_COLOR,
      "circle-stroke-width": 2,
    },
  });

  const sourceRef = map.getSource(sourceId) as GeoJSONSource;

  // Track the latest selection so the drag handler's mousedown path
  // has the current ring + centre to snapshot. Updated on every
  // controller emission.
  let activeCenter: [number, number] | null = null;
  let activeRing: number[][] | null = null;

  const updateHandlePosition = (selection: SelectionGeometry | null): void => {
    if (selection === null || selection.shape !== "circle") {
      sourceRef.setData({ type: "FeatureCollection", features: [] });
      activeCenter = null;
      activeRing = null;
      return;
    }
    const ring = selection.feature.geometry.coordinates[0];
    if (!ring || ring.length < 3) return;
    // Centroid (≡ centre for the uniform 64-vertex polygon) and the
    // first vertex (≡ east cardinal — terra-draw starts the circle
    // sweep at angle 0). Recomputed on every emission so the handle
    // tracks the user's drag-to-move gesture too.
    let cx = 0, cy = 0, n = 0;
    for (let i = 0; i < ring.length - 1; i++) {
      cx += ring[i][0]; cy += ring[i][1]; n++;
    }
    if (n === 0) return;
    cx /= n; cy /= n;
    activeCenter = [cx, cy];
    activeRing = ring;
    const handlePoint: [number, number] = [ring[0][0], ring[0][1]];

    sourceRef.setData({
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry: { type: "Point", coordinates: handlePoint },
          properties: {},
        },
      ],
    });
  };
  controller.subscribe(updateHandlePosition);

  // ── Drag wiring ────────────────────────────────────────────────
  //
  // Snapshot-based resize: at mousedown we capture the centre, the
  // ring, and the cursor's initial distance from the centre. Every
  // mousemove computes the cursor's CURRENT distance from the
  // SNAPSHOT centre, derives the absolute scale relative to the
  // SNAPSHOT initial radius, and rebuilds the ring by scaling the
  // SNAPSHOT vertices around the SNAPSHOT centre. No iterative drift
  // — the cursor stays glued to the rim regardless of speed.
  //
  // ``stopPropagation`` on the underlying DOM event keeps terra-
  // draw from also seeing the gesture as a feature drag. Window-
  // level mousemove/mouseup keeps the resize alive even if the
  // cursor briefly leaves the canvas during a fast drag.

  const cursor = map.getCanvasContainer();
  type DragSnapshot = {
    center: [number, number];
    initialRadius: number;
    snapshotRing: number[][];
  };
  let drag: DragSnapshot | null = null;

  const onWindowMouseMove = (e: MouseEvent): void => {
    if (drag === null) return;
    const rect = cursor.getBoundingClientRect();
    const lngLat = map.unproject([e.clientX - rect.left, e.clientY - rect.top]);
    const currentRadius = _planarDistance(drag.center, [lngLat.lng, lngLat.lat]);
    if (currentRadius <= 0 || drag.initialRadius <= 0) return;
    // Clamp to a sensible range so a stray jitter near the centre
    // doesn't collapse the circle to a point or balloon past the
    // viewport. Across normal use the cursor stays 1:1 with the rim.
    const scale = Math.min(Math.max(currentRadius / drag.initialRadius, 0.05), 20);
    const cx = drag.center[0];
    const cy = drag.center[1];
    // terra-draw rejects coordinates with more than ~9 decimals via
    // its built-in feature validator ("excessive precision" error).
    // Round to 8 decimals (~1mm at the equator — well below any
    // user-visible drag granularity).
    const newRing: number[][] = drag.snapshotRing.map(([x, y]) => [
      _round8(cx + (x - cx) * scale),
      _round8(cy + (y - cy) * scale),
    ]);
    controller.setCircleRing(newRing);
  };

  const onWindowMouseUp = (): void => {
    drag = null;
    window.removeEventListener("mousemove", onWindowMouseMove);
    window.removeEventListener("mouseup", onWindowMouseUp);
    cursor.style.cursor = "";
    // Restore the camera pan handler we disabled at mousedown so
    // post-resize map drags (in empty space) keep working.
    map.dragPan.enable();
  };

  const onLayerMouseDown = (e: MapLayerMouseEvent): void => {
    if (activeCenter === null || activeRing === null) return;
    e.originalEvent.stopPropagation();
    e.originalEvent.preventDefault();
    // Disable the map's dragPan for the duration of the resize.
    // ``stopPropagation`` keeps terra-draw from seeing the gesture,
    // but MapLibre's dragPan handler is registered on the canvas
    // separately and already received the mousedown — without this
    // explicit ``disable``, the camera pans alongside the resize.
    // Mirrors what terra-draw's select mode does internally for
    // inside-feature drags via the ``setMapDraggability`` callback.
    map.dragPan.disable();

    // Capture the cursor's initial distance from the centre from the
    // mousedown event itself — using ``activeRing[0]`` (a vertex
    // position) would mean "cursor radius == handle radius" only if
    // the user clicked exactly on the handle's pixel. Using the
    // event's lngLat lets the user grab anywhere on the visible dot
    // without the radius snapping mid-drag.
    const initialRadius = _planarDistance(
      activeCenter,
      [e.lngLat.lng, e.lngLat.lat],
    );
    if (initialRadius <= 0) return;

    drag = {
      center: [activeCenter[0], activeCenter[1]],
      initialRadius,
      // Shallow-copy each vertex so the snapshot is immune to
      // terra-draw mutating the live ring during the drag.
      snapshotRing: activeRing.map(([x, y]) => [x, y]),
    };
    cursor.style.cursor = "ew-resize";
    window.addEventListener("mousemove", onWindowMouseMove);
    window.addEventListener("mouseup", onWindowMouseUp);
  };

  // Pointer cursor on hover so the handle feels grabbable.
  const onLayerMouseEnter = (): void => {
    cursor.style.cursor = "ew-resize";
  };
  const onLayerMouseLeave = (): void => {
    cursor.style.cursor = "";
  };

  map.on("mousedown", layerId, onLayerMouseDown);
  map.on("mouseenter", layerId, onLayerMouseEnter);
  map.on("mouseleave", layerId, onLayerMouseLeave);

}


/** Trim float noise so a drag doesn't emit 17-significant-digit
 *  coordinates into the polygon ring. */
function _round8(n: number): number {
  return Math.round(n * 1e8) / 1e8;
}


/**
 * Planar (lng/lat Euclidean) distance. Good enough for the prototype's
 * city-scale circles where projection skew at CDMX/Mexicali latitudes
 * is sub-percent over a few-km radius. We're computing a SCALE FACTOR
 * (ratio of two same-projection distances), so the formula's absolute
 * accuracy doesn't matter — only the consistency of the metric for
 * "before drag" vs "during drag", which planar distance preserves.
 */
function _planarDistance(a: [number, number], b: [number, number]): number {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  return Math.sqrt(dx * dx + dy * dy);
}

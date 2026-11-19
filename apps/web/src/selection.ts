/**
 * Sub-AOI selection store + draw lifecycle.
 *
 * Public surface:
 *   - ``enableSelection(map)`` — idempotent factory; returns the
 *     :class:`SelectionController` for this map.
 *   - The controller exposes ``startDrawing(shape)`` / ``clear()``
 *     for the toolbar buttons,
 *     ``getSelection()`` for one-shot reads, and ``subscribe()``
 *     for reactive consumers.
 *
 * Three downstream consumers live in sibling modules:
 *   - :mod:`selection-mask` dims out-of-selection features via a
 *     spotlight overlay layer (Felt / Mapbox-Studio idiom).
 *   - :mod:`selection-card` summarises the selection (block count,
 *     the active indicator inside it) bottom-right of the map.
 *   - ``main.ts`` wires the toolbar UI.
 *
 * The store pattern matches ``aoi.ts`` (manual EventEmitter,
 * subscribe-on-init fires immediately so consumers can bootstrap)
 * so anyone reading the codebase can transfer their mental model
 * for free.
 */
import {
  TerraDraw,
  TerraDrawCircleMode,
  TerraDrawPolygonMode,
  TerraDrawRectangleMode,
  TerraDrawSelectMode,
} from "terra-draw";
import { TerraDrawMapLibreGLAdapter } from "terra-draw-maplibre-gl-adapter";
import type { Map as MapLibreMap, PointLike } from "maplibre-gl";
import type { Feature, Polygon } from "geojson";

import { setHoverGloballySuspended } from "./hover-highlight";
import { SELECTION_HANDLE_LAYER_ID } from "./selection-handle";
import { setTooltipGloballySuspended } from "./tooltip";


export type SelectionShape = "circle" | "rectangle" | "polygon";


export interface SelectionGeometry {
  /**
   * GeoJSON Polygon of the user-drawn shape. Circles are emitted by
   * terra-draw as ~64-vertex polygon approximations; rectangles as
   * 5 vertices; polygons as the user clicked them. Every downstream
   * subscriber treats these uniformly because the geometry type is
   * always ``Polygon`` — the ``shape`` discriminator is informational
   * only.
   */
  feature: Feature<Polygon>;
  shape: SelectionShape;
}


export interface SubscribeOptions {
  /** Debounce subsequent calls to the listener until ``ms`` have
   *  passed without another change. Use a non-zero value (e.g. 150)
   *  for expensive subscribers like metrics aggregation or per-tile
   *  paint-property updates so they don't run every drag frame —
   *  terra-draw fires ``change`` events at ~60 Hz during a drag.
   *  The first call (the bootstrap fire on ``subscribe``) always
   *  runs immediately regardless of this value. ``null``/``0``
   *  means "fire every event" (live preview semantics). */
  debounceMs?: number;
}


export interface SelectionController {
  startDrawing(shape: SelectionShape): void;
  clear(): void;
  getSelection(): SelectionGeometry | null;
  /**
   * The map click at `point` belongs to the selection, not to the map. A
   * click that began while a shape was being drawn belongs to the shape, the
   * one that finishes it included (terra-draw is in select mode by the time
   * the click lands, hence the capture at pointerdown). A finished selection
   * owns only clicks on its handles (see {@link hitsSelectionHandle}).
   */
  ownsClick(point: PointLike): boolean;
  /** Subscribe to selection changes. Fires immediately with the
   *  current state so consumers can bootstrap without an extra
   *  ``getSelection()`` call. Returns an unsubscribe function. */
  subscribe(listener: SelectionListener, options?: SubscribeOptions): () => void;
  /**
   * Replace the active circle's outer ring without changing its id.
   * Used by the custom radius handle in ``selection-handle.ts`` —
   * the handle module owns the geometry math (snapshot at mousedown,
   * scale on each mousemove); this method is a thin pass-through to
   * ``draw.updateFeatureGeometry``. No-op for non-circle selections
   * so the handle can call unconditionally during a drag without
   * checking shape.
   */
  setCircleRing(ring: number[][]): void;
}


type SelectionListener = (selection: SelectionGeometry | null) => void;


// ─── Per-map controller cache ────────────────────────────────────────
//
// terra-draw needs to be ``start``ed on a map and registers DOM
// listeners; recreating it every call would leak listeners and would
// also break the singleton subscription pattern we rely on. Cache by
// map instance — ``enableSelection`` is safe to call from anywhere.

const _controllers = new WeakMap<MapLibreMap, SelectionController>();


function _ensureClosedPolygonFeature(
  feat: Feature<Polygon>,
): Feature<Polygon> {
  const rings = feat.geometry?.coordinates;
  if (!rings || rings.length === 0) return feat;
  let mutated = false;
  const closed = rings.map((ring) => {
    if (!Array.isArray(ring) || ring.length < 3) return ring;
    const first = ring[0];
    const last = ring[ring.length - 1];
    if (!Array.isArray(first) || !Array.isArray(last)) return ring;
    if (first[0] === last[0] && first[1] === last[1]) return ring;
    mutated = true;
    return [...ring, [first[0], first[1]]];
  });
  if (!mutated) return feat;
  return {
    ...feat,
    geometry: { type: "Polygon", coordinates: closed },
  };
}


// ─── Mode names — match the keys terra-draw uses internally ─────────

/**
 * True while a shape is being drawn (terra-draw's circle / rectangle / polygon
 * modes): map clicks then belong to the shape. A finished shape sits in
 * `select` mode, where clicks are the map's again.
 */
export function isDrawMode(mode: string): boolean {
  return mode === "circle" || mode === "rectangle" || mode === "polygon";
}

/** terra-draw's vertex and midpoint dots (its default `td` prefix), and the
 *  radius handle. */
const HANDLE_LAYERS = ["td-point", "td-point-marker", SELECTION_HANDLE_LAYER_ID];

/**
 * True when `point` is on a shape's vertex handle or on the radius handle. A
 * click there edits the shape and must not pin the block underneath. The
 * shape's interior is not a handle: a click inside the selection still pins.
 */
export function hitsSelectionHandle(
  map: Pick<MapLibreMap, "getLayer" | "queryRenderedFeatures">,
  point: PointLike,
): boolean {
  // Only layers in the style: querying a missing one fires a map `error`.
  const layers = HANDLE_LAYERS.filter((id) => map.getLayer(id));
  return layers.length > 0 && map.queryRenderedFeatures(point, { layers }).length > 0;
}

const TD_MODE: Record<SelectionShape, string> = {
  circle: "circle",
  rectangle: "rectangle",
  polygon: "polygon",
};


// ── Map-gesture suppression while drawing ───────────────────────────
//
// terra-draw's MapLibre adapter SHOULD pre-empt the map's drag
// handler, but on maplibre-gl 4.x the order of mousedown listener
// registration means the map's ``dragPan`` handler can win the
// first gesture — the user clicks to start a circle and ends up
// panning the map instead. Explicitly disabling the map's
// gestures while a shape is being drawn makes terra-draw the only
// consumer of those events.
export function syncMapGestures(
  map: Pick<MapLibreMap, "dragPan" | "boxZoom" | "doubleClickZoom">,
  mode: string,
): void {
  const drawing = isDrawMode(mode);
  // Suspend BOTH global hover-highlight and tooltips while a shape is
  // being drawn: the user's clicks place its vertices, so a hover
  // outline or tooltip is noise that fights for the same input. A
  // finished shape (select mode) keeps them: hovering a block and its
  // "Click to pin" tooltip are how pinning is found with a selection on.
  setHoverGloballySuspended(drawing);
  setTooltipGloballySuspended(drawing);
  // Two regimes for the competing-gesture handlers (left-click
  // drag, shift+drag, double-click):
  //
  //   - DRAW modes (circle/rectangle/polygon): disable them. The
  //     user's first click should start the draw gesture, not pan
  //     the map. Without this, dragPan races terra-draw's
  //     mousedown handler and sometimes wins.
  //
  //   - SELECT and STATIC: leave them ENABLED. terra-draw's select
  //     mode cooperates with the map via ``setMapDraggability`` — its
  //     ``onDragStart`` disables pan ONLY for the duration of an
  //     inside-feature drag. Outside drags fall through to
  //     ``dragPan`` so the user can reframe the map without
  //     clearing the selection.
  //
  // Wheel zoom, pinch, keyboard, right-click rotate stay on in
  // every regime — they live on independent inputs.
  for (const h of [map.dragPan, map.boxZoom, map.doubleClickZoom]) {
    if (drawing) h.disable();
    else h.enable();
  }
}


export function enableSelection(map: MapLibreMap): SelectionController {
  const cached = _controllers.get(map);
  if (cached) return cached;

  // ── State ─────────────────────────────────────────────────────────
  let _selection: SelectionGeometry | null = null;
  const _listeners = new Set<SelectionListener>();

  const _emit = (): void => {
    for (const listener of _listeners) {
      try {
        listener(_selection);
      } catch (err) {
        // One bad subscriber must not break the others. Logging keeps
        // the default visible without forcing every consumer to wrap
        // their handler in try/catch.
        console.error("selection subscriber threw:", err);
      }
    }
  };

  // ── terra-draw setup ──────────────────────────────────────────────
  //
  // All three drawing modes are registered up-front. ``startDrawing``
  // just calls ``setMode(name)``; the previously-drawn feature stays
  // in the snapshot until the next finish event replaces it.
  //
  // ``TerraDrawSelectMode`` is what lets the user drag the drawn
  // shape's center handle (move) or vertex handles (resize) after
  // the initial draw. We enable it with edit flags per shape so the
  // affordances match what the user expects: circles get
  // drag-and-resize handles, rectangles get corner resize, polygons
  // get per-vertex drag.
  // Selection-shape styling: outline-only so the user can see the
  // features they're selecting. Terra-draw's default is a translucent
  // blue fill which made every block under the shape look greyer than
  // its outside-the-shape neighbour — exactly the opposite of what
  // dimming should look like. Using a near-zero fill (0.05 leaves
  // just enough tint to confirm the shape is filled, not just an
  // outline) keeps the inside features at their natural opacity.
  // terra-draw typings model hex colours as a template-literal type
  // (`#${string}`) so a plain ``"#22d3ee"`` doesn't match without a
  // cast. Declaring once, reusing for all three modes.
  const _shapeStyles = {
    fillColor: "#22d3ee" as `#${string}`,
    fillOpacity: 0.05,
    outlineColor: "#22d3ee" as `#${string}`,
    outlineWidth: 2,
    outlineOpacity: 1,
  };
  // Read by SelectMode's ``selectionPointWidth`` styling fn below.
  // terra-draw stamps ``mode: "select"`` on selection points, not the
  // parent shape's mode — closure capture is the only way to style
  // those dots per shape without forking terra-draw.
  let _activeSelectedShape: SelectionShape | null = null;
  const draw = new TerraDraw({
    adapter: new TerraDrawMapLibreGLAdapter({ map }),
    modes: [
      new TerraDrawCircleMode({ styles: _shapeStyles }),
      new TerraDrawRectangleMode({ styles: _shapeStyles }),
      new TerraDrawPolygonMode({ styles: _shapeStyles }),
      new TerraDrawSelectMode({
        // Keep the feature selected when the user clicks outside it.
        // Default is true: an outside-click deselects the feature,
        // which here would mean the user can no longer drag-to-move
        // the circle until they click on it again. With outside-pan
        // re-enabled (see ``syncMapGestures``), outside clicks /
        // drags should be camera operations, not selection-clear
        // operations. The toolbar's × button is the explicit clear.
        allowManualDeselection: false,
        styles: {
          // SelectMode renders the SELECTED feature (post-finish)
          // with its own ``selectedPolygon*`` styles, NOT the
          // mode-specific draw styles. Without these, the rectangle
          // came back to terra-draw's default vivid blue fill the
          // moment the gesture finished — opaque enough to hide the
          // underlying features and exactly what the user reported.
          selectedPolygonColor: "#22d3ee" as `#${string}`,
          selectedPolygonFillOpacity: 0.05,
          selectedPolygonOutlineColor: "#22d3ee" as `#${string}`,
          selectedPolygonOutlineWidth: 2,
          selectedPolygonOutlineOpacity: 1,
          // Per-vertex selection-point dots:
          //   - circle:    hidden (size 0) — replaced by the custom
          //                radius handle in ``selection-handle.ts``.
          //                That module renders ONE visible dot at the
          //                east-cardinal rim point. Hiding terra-
          //                draw's 64 per-vertex handles avoids the
          //                "necklace of beads" the user rejected.
          //   - rectangle: 4 corner handles (drag a corner → opposite
          //                stays fixed; standard rectangle resize).
          //   - polygon:   all vertex handles visible — per-vertex drag
          //                IS the editing affordance.
          //
          // We can't read the parent feature's mode from the selection
          // point's own properties (terra-draw stamps ``mode: "select"``
          // there, not the parent shape). Closure-capture the active
          // shape from the controller instead.
          selectionPointWidth: () => {
            return _activeSelectedShape === "circle" ? 0 : 6;
          },
          selectionPointColor: "#22d3ee" as `#${string}`,
          selectionPointOutlineColor: "#0b1220" as `#${string}`,
          selectionPointOutlineWidth: 2,
          // Mid-points (insert-new-vertex affordance) — only emitted
          // for polygon mode per the flags below, so the size only
          // matters there.
          midPointWidth: 5,
          midPointColor: "#22d3ee" as `#${string}`,
          midPointOutlineColor: "#0b1220" as `#${string}`,
          midPointOutlineWidth: 1,
        },
        flags: {
          // Circles: drag-to-move only. Resize is handled by the
          // custom radius handle in ``selection-handle.ts`` (a
          // separate map layer with a single visible dot on the
          // rim) — terra-draw's per-vertex resize affordance is
          // disabled so accidental rim drags can't deform the
          // circle into a non-circle.
          circle:    { feature: { draggable: true, rotateable: false, scaleable: false, coordinates: { midpoints: false, draggable: false, deletable: false } } },
          // Rectangles: drag any corner to resize; the diagonally
          // opposite corner stays fixed (standard rectangle resize
          // affordance the user already knows from every drawing
          // tool). ``scaleable: false`` because we don't want a
          // separate shift+drag scaling on top of the corner drag.
          rectangle: { feature: { draggable: true, rotateable: false, scaleable: false, coordinates: { midpoints: false, draggable: true,  deletable: false, resizable: "opposite-fixed" } } },
          // Polygons: per-vertex handles are how the user reshapes;
          // midpoints add new vertices on click.
          polygon:   { feature: { draggable: true, rotateable: false, scaleable: false, coordinates: { midpoints: true,  draggable: true,  deletable: true  } } },
        },
      }),
    ],
  });
  draw.start();
  syncMapGestures(map, "static");

  // Whether the click now landing began while drawing; see `ownsClick`.
  let _drawingAtPointerDown = false;
  map.getContainer().addEventListener(
    "pointerdown",
    () => { _drawingAtPointerDown = isDrawMode(draw.getMode()); },
    { capture: true },
  );

  // ── terra-draw → store bridge ────────────────────────────────────
  //
  // ``finish`` fires when the user closes a draw gesture (releases
  // the circle radius, double-clicks the polygon's last vertex). At
  // that point we (a) snapshot the geometry, (b) automatically
  // switch to select-mode so the user can immediately drag/resize.
  //
  // ``change`` fires while the user is actively dragging a handle in
  // select-mode. We replay it through the store so subscribers see
  // live updates — the dim opacity on outside features recomputes
  // every frame, and the metrics panel stays in sync.
  const _publishLatest = (shape: SelectionShape): void => {
    const snapshot = draw.getSnapshot();
    if (snapshot.length === 0) {
      _selection = null;
      _emit();
      return;
    }
    // The snapshot mixes the user's drawn polygon with terra-draw's
    // own bookkeeping features — selection-point dots (Points),
    // mid-point dots (Points), the closing-point indicator (Point).
    // ``snapshot[snapshot.length - 1]`` is the most recently mutated
    // feature, which during a drag is usually one of those auxiliary
    // points, not the polygon. Filter to features whose ``mode``
    // property matches one of our shape modes — that's the
    // canonical "user-drawn" tag terra-draw stamps on the polygon
    // itself but never on the bookkeeping features.
    const polygons = snapshot.filter((f) => {
      const mode = (f.properties as { mode?: string } | null)?.mode;
      return (
        f.geometry?.type === "Polygon"
        && (mode === "circle" || mode === "rectangle" || mode === "polygon")
      );
    }) as Feature<Polygon>[];
    if (polygons.length === 0) {
      // Mid-drag terra-draw can briefly emit a snapshot with no
      // user polygon (e.g. between geometry replacement and the
      // re-emit). Bail rather than null out the selection — the
      // next change event will deliver a complete snapshot.
      return;
    }
    const raw = polygons[polygons.length - 1];
    // Defensive ring closure: GeoJSON RFC 7946 §3.1.6 requires that
    // the first and last positions of a LinearRing be identical.
    // PostGIS ``ST_GeomFromGeoJSON`` and MapLibre's GeoJSON parser
    // both throw / silently drop on open rings. terra-draw closes
    // most shapes itself but the rare open-ring case (some 1.x
    // builds, programmatic feature load) leaks through and causes
    // a silent metrics-returns-zero failure on the server side.
    // Cheap to guarantee here once for every downstream consumer.
    const feat = _ensureClosedPolygonFeature(raw);
    _selection = { feature: feat, shape };
    _activeSelectedShape = shape;
    _emit();
  };

  let _activeShape: SelectionShape | null = null;

  draw.on("finish", (id) => {
    if (_activeShape === null) return;
    _publishLatest(_activeShape);
    // Auto-enter select mode + pre-select the shape we just drew, so
    // the very next mousedown on the shape's interior or vertex
    // handles drags it. Without ``selectFeature`` the user has to
    // click the shape once to select it before they can move it,
    // which feels broken — production apps (Felt, Mapbox Studio)
    // all auto-select on finish.
    draw.setMode("select");
    try {
      draw.selectFeature(id);
    } catch {
      // No-op if the id is no longer in the snapshot (e.g. user
      // cleared between finish and this callback).
    }
    syncMapGestures(map, "select");
  });
  draw.on("change", () => {
    // terra-draw's ``change`` event shape (type argument, ordering)
    // has churned across 1.x point releases — filtering by type
    // silently dropped drag-update events on 1.28. The robust path
    // is to re-publish on every change and let ``_publishLatest``
    // do the right thing: empty snapshot → clear; non-empty →
    // re-emit the latest geometry. Idempotent + cheap.
    if (_selection === null) {
      const snapshot = draw.getSnapshot();
      if (snapshot.length === 0) return;
      // First geometry just landed — fall through to publish so we
      // capture redraws started before ``finish`` fires (rare but
      // possible in fast-drag scenarios).
    }
    const shape = _selection?.shape ?? _activeShape ?? "polygon";
    _publishLatest(shape);
  });

  // ── Controller surface ────────────────────────────────────────────
  const controller: SelectionController = {
    startDrawing(shape: SelectionShape): void {
      // Replace any existing drawing — one selection at a time keeps
      // the metrics + dimming math unambiguous.
      controller.clear();
      _activeShape = shape;
      draw.setMode(TD_MODE[shape]);
      syncMapGestures(map, TD_MODE[shape]);
    },
    clear(): void {
      _activeShape = null;
      _activeSelectedShape = null;
      draw.clear();
      _selection = null;
      draw.setMode("static");
      syncMapGestures(map, "static");
      _emit();
    },
    getSelection(): SelectionGeometry | null {
      return _selection;
    },
    ownsClick(point: PointLike): boolean {
      return _drawingAtPointerDown || hitsSelectionHandle(map, point);
    },
    setCircleRing(ring: number[][]): void {
      if (_selection?.shape !== "circle") return;
      if (!ring || ring.length < 4) return;  // need at least a closed triangle
      const featureId = _selection.feature.id;
      if (featureId === undefined || featureId === null) return;
      draw.updateFeatureGeometry(featureId, {
        type: "Polygon",
        coordinates: [ring],
      });
    },
    subscribe(listener, options): () => void {
      const debounceMs = options?.debounceMs ?? 0;
      // Debounced listeners get wrapped — every change resets a
      // trailing timer; the wrapped listener only fires once the
      // user stops dragging (no change for ``debounceMs`` ms).
      // Live listeners (debounceMs <= 0) fire on every change.
      let wrapped: SelectionListener;
      if (debounceMs > 0) {
        let timer: number | null = null;
        wrapped = (sel) => {
          // Clear-on-null bypasses the debounce — when the user hits
          // the ``×`` button the panel/dimming should reset
          // immediately, not 150 ms later. The user expects the
          // visible state to track the toolbar.
          if (sel === null) {
            if (timer !== null) {
              window.clearTimeout(timer);
              timer = null;
            }
            listener(null);
            return;
          }
          if (timer !== null) window.clearTimeout(timer);
          timer = window.setTimeout(() => {
            timer = null;
            listener(sel);
          }, debounceMs);
        };
      } else {
        wrapped = listener;
      }
      // Bootstrap BEFORE registering: if the listener synchronously
      // calls ``clear()`` from its bootstrap path (reasonable for
      // UI-init), the resulting ``_emit()`` would otherwise re-fire
      // ``wrapped`` while we're still inside this function and the
      // listener would be called twice with the same state. Adding
      // ``wrapped`` after the bootstrap call keeps the contract
      // "fires immediately with the current state" without double-
      // firing on a self-modifying init.
      try {
        listener(_selection);
      } catch (err) {
        console.error("selection subscriber threw on init:", err);
      }
      _listeners.add(wrapped);
      return () => {
        _listeners.delete(wrapped);
      };
    },
  };

  _controllers.set(map, controller);
  return controller;
}

/**
 * Reusable hover-highlight for MapLibre vector fill / line layers.
 *
 * Why separate from the tooltip system:
 *   - tooltip is about content (what to show).
 *   - highlight is about style (how the hovered feature looks).
 * Both want to follow the same "register per layer, dispatch centrally"
 * pattern but they're orthogonal — a layer can have one without the
 * other (e.g. raster readouts have no feature to highlight).
 *
 * Standard MapLibre idiom, encapsulated:
 *   1. Source must be added with ``promoteId`` so ``feature.id`` is
 *      populated from a stable MVT property (we use ``feature_id``
 *      emitted by every reimagina tile function).
 *   2. On mousemove/mouseleave the feature-state ``hover`` flag flips
 *      for the feature under the cursor.
 *   3. Paint expressions on the fill / line read that flag and switch
 *      between default and highlight variants. Using feature-state
 *      (rather than per-mousemove ``setPaintProperty``) keeps the hover
 *      transition handled by MapLibre's renderer and scales to 10k+
 *      features without JS hot-path work.
 *
 * Callers describe their current default styling via the ``default``
 * object and the helper replaces the relevant paint props with
 * case-expressions that fold in the highlight overrides. Existing
 * callers that already set paint at layer-add time just pass those
 * numbers through — no duplicated colour constants.
 */
import type { Map as MapLibreMap } from "maplibre-gl";


/** Standard hover stroke — white, thick enough to read over dense
 *  choropleth fills without drowning out the colour. */
export const HOVER_OUTLINE_COLOR = "#ffffff";
export const HOVER_LINE_WIDTH = 3.5;


export interface HoverHighlightConfig {
  /** Optional MapLibre fill layer. When present, mousemove fires on
   *  this layer and the helper updates fill-opacity + fill-outline-color
   *  case expressions. Layers without a fill (block stroke-only) can
   *  skip this and pass ``lineLayerId`` instead — hover hits register
   *  on the line layer directly. */
  fillLayerId?: string;
  /** Optional matching line layer whose stroke thickens on hover. */
  lineLayerId?: string;
  /** Current default fill-outline-color. Used for the non-hover branch
   *  of the case expression so the caller's existing styling is
   *  preserved when the feature isn't hovered. */
  defaultOutlineColor?: string;
  /** Current default fill-opacity. Some layers (e.g. sparse baselines)
   *  want a small opacity lift on hover so the feature pops without
   *  taking over the colour the choropleth relies on. */
  defaultFillOpacity?: number;
  /** Hover fill-opacity override. Defaults to max(default + 0.15, 0.5). */
  hoverFillOpacity?: number;
  /** Current default line-width. */
  defaultLineWidth?: number;
  /** Hover line-width override. Defaults to HOVER_LINE_WIDTH. */
  hoverLineWidth?: number;
}


/**
 * Wire up hover highlighting for a fill / line / fill+line layer combo.
 *
 * At least one of ``fillLayerId`` or ``lineLayerId`` must resolve to a
 * registered MapLibre layer. The helper auto-detects which one(s) are
 * present and:
 *   - registers mousemove / mouseleave handlers on the hit-target
 *     layer (fill if present, line otherwise);
 *   - replaces fill-opacity + fill-outline-color when a fill exists;
 *   - replaces line-color + line-width when a line exists;
 *   - flips the canvas cursor to ``pointer`` while hovered.
 *
 * Returns a dispose function that un-registers listeners and clears
 * any residual feature-state. Callers don't need to call it in
 * normal use — the handlers live for the life of the map.
 */
export function enableHoverHighlight(
  map: MapLibreMap,
  cfg: HoverHighlightConfig,
): () => void {
  const fillLayer = cfg.fillLayerId ? map.getLayer(cfg.fillLayerId) : null;
  const lineLayer = cfg.lineLayerId ? map.getLayer(cfg.lineLayerId) : null;
  // Hit-target priority: fill > line. A fill polygon catches mouse
  // events across the whole feature interior; a line catches them only
  // along the stroke pixels. When both are available the fill is
  // strictly better hover-detection. When only line is available
  // (block layer = transparent fill), we wire the events on the line.
  const hitLayer = fillLayer ?? lineLayer;
  const hitLayerId = fillLayer ? cfg.fillLayerId : cfg.lineLayerId;
  if (!hitLayer || !hitLayerId) return () => undefined;

  const source = (hitLayer as unknown as { source?: string }).source;
  const sourceLayer = (hitLayer as unknown as { sourceLayer?: string }).sourceLayer;
  if (!source || !sourceLayer) {
    // Promoted-id hover needs source + source-layer; bail rather than
    // silently install a no-op handler.
    return () => undefined;
  }

  const hoverFillOpacity = cfg.hoverFillOpacity
    ?? Math.max(0.5, (cfg.defaultFillOpacity ?? 0.35) + 0.15);
  const hoverLineWidth = cfg.hoverLineWidth ?? HOVER_LINE_WIDTH;
  const defaultOutlineColor = cfg.defaultOutlineColor ?? "rgba(0,0,0,0.2)";
  const defaultFillOpacity = cfg.defaultFillOpacity ?? 0;
  const defaultLineWidth = cfg.defaultLineWidth ?? 1;

  const hoveredCase = (hot: unknown, cold: unknown) => [
    "case",
    ["boolean", ["feature-state", "hover"], false],
    hot,
    cold,
  ];

  if (cfg.fillLayerId && fillLayer) {
    map.setPaintProperty(cfg.fillLayerId, "fill-opacity",
      hoveredCase(hoverFillOpacity, defaultFillOpacity) as never);
    map.setPaintProperty(cfg.fillLayerId, "fill-outline-color",
      hoveredCase(HOVER_OUTLINE_COLOR, defaultOutlineColor) as never);
  }

  if (cfg.lineLayerId && lineLayer) {
    const currentColor = (map.getPaintProperty(cfg.lineLayerId, "line-color")
      ?? defaultOutlineColor) as unknown;
    map.setPaintProperty(cfg.lineLayerId, "line-color",
      hoveredCase(HOVER_OUTLINE_COLOR, currentColor) as never);
    map.setPaintProperty(cfg.lineLayerId, "line-width",
      hoveredCase(hoverLineWidth, defaultLineWidth) as never);
  }

  let hoveredId: string | number | null = null;
  // MapLibre manages its drag cursor ('grab' / 'grabbing') on the
  // canvas container, not the canvas element. Setting cursor on the
  // canvas alone gets overridden by the container's 'grab' style, so
  // the hover 'pointer' never appears. Set it on the container and
  // MapLibre's drag handlers don't fight it while idle.
  const container = map.getCanvasContainer();

  const clearHover = (): void => {
    if (hoveredId !== null) {
      map.removeFeatureState({ source, sourceLayer, id: hoveredId }, "hover");
      hoveredId = null;
    }
    container.style.cursor = "";
  };

  const onMove = (e: { features?: Array<{ id?: string | number }> }) => {
    const next = e.features?.[0]?.id;
    if (next === undefined || next === null) {
      clearHover();
      return;
    }
    if (next === hoveredId) return;
    if (hoveredId !== null) {
      map.removeFeatureState({ source, sourceLayer, id: hoveredId }, "hover");
    }
    hoveredId = next;
    map.setFeatureState({ source, sourceLayer, id: hoveredId }, { hover: true });
    container.style.cursor = "pointer";
  };

  map.on("mousemove", hitLayerId, onMove);
  map.on("mouseleave", hitLayerId, clearHover);

  return () => {
    map.off("mousemove", hitLayerId, onMove);
    map.off("mouseleave", hitLayerId, clearHover);
    clearHover();
  };
}

import { describe, expect, it } from "vitest";

import {
  INPUT_LAYERS,
  groupRastersAndOverlays,
  installAggregationOverlays,
  minzoomForGrain,
} from "./layers";

/**
 * The zoom at which `serving_geo.get_tile` starts serving
 * `role = 'infrastructure'` features.
 *
 * From the function's own role gate:
 *
 * ```sql
 * WHEN z < 8  THEN f.role = 'context'
 * WHEN z < 12 THEN f.role IN ('context', 'analysis_unit')
 * ELSE TRUE
 * ```
 *
 * Measured against the running database rather than read off the migration:
 * a z11 road tile is 0 bytes, a z12 one is 2,714.
 */
const INFRASTRUCTURE_FLOOR = 12;

describe("INPUT_LAYERS", () => {
  const streets = INPUT_LAYERS.find((l) => l.typeSlug === "road");

  it("includes a street layer", () => {
    expect(streets).toBeDefined();
  });

  // ⚠ The pair that fails invisibly when it drifts.
  //
  // A client `minzoom` below what the server serves produces requests that
  // can only come back empty — and on a map, empty is indistinguishable
  // from missing data. The two numbers live in different languages, in
  // different repositories' worth of distance, and nothing else compares
  // them.
  it("floors streets at the zoom the tile function starts serving them", () => {
    expect(streets?.minzoom).toBe(INFRASTRUCTURE_FLOOR);
  });

  it("draws streets beneath every analysis layer", () => {
    // `installInputLayers` adds layers in array order, so later entries
    // draw on top. Context that draws over the analysis units is not
    // context.
    expect(INPUT_LAYERS[0]?.typeSlug).toBe("road");
  });

  it("marks streets as line geometry", () => {
    // Which is what skips the fill layer. A fill on a LineString renders
    // nothing and leaves a dead hit-target that swallows the hover the line
    // layer should have caught.
    expect(streets?.geometry).toBe("line");
  });

  it("starts every spatial layer unchecked", () => {
    expect(INPUT_LAYERS.every((l) => l.visible === false)).toBe(true);
  });

  it("labels the municipalities layer plainly", () => {
    expect(INPUT_LAYERS.find((l) => l.typeSlug === "municipality")?.label).toBe("Municipalities");
  });

  it("lists overlays without a parent raster directly, under no heading", () => {
    const groups = groupRastersAndOverlays([
      { layer_id: "a", source_mode: "aggregation_overlay", source_config: {} },
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].parentLabel).toBe("");
  });

  it("starts block overlays where blocks render", () => {
    expect(minzoomForGrain("block")).toBe(8);
    expect(minzoomForGrain(null)).toBeUndefined();
  });

  it("gives streets a zoom-dependent width", () => {
    // A fixed hairline is invisible when zoomed in and a fixed thick line
    // is noise when zoomed out; every basemap interpolates, and it is why a
    // street layer reads as ground rather than as content.
    expect(Array.isArray(streets?.lineWidth)).toBe(true);
    expect((streets?.lineWidth as unknown[])[0]).toBe("interpolate");
  });

  it("keeps every layer's client floor at or above the server's", () => {
    // Blocks and AGEBs are `analysis_unit` (served from z8); buildings have
    // their own r038 floor at z14; roads are `infrastructure` (z12). None
    // may ask for tiles below what it will receive.
    for (const layer of INPUT_LAYERS) {
      expect(layer.minzoom ?? 0).toBeGreaterThanOrEqual(
        layer.typeSlug === "road" ? INFRASTRUCTURE_FLOOR : 0,
      );
    }
  });
});


describe("aggregation overlay colour ramp", () => {
  // A territory where every area has the same value is a real case (one
  // AGEB, a uniform raster), and MapLibre rejects interpolate stops that are
  // not strictly ascending: the whole layer then fails with a banner.
  it("keeps its stops strictly ascending when the range is constant", () => {
    const layers: { paint?: Record<string, unknown> }[] = [];
    const map = {
      getSource: () => undefined,
      addSource: () => undefined,
      addLayer: (l: { paint?: Record<string, unknown> }) => layers.push(l),
      getLayer: () => undefined,
    };
    // Measured on the synthetic territory: four AGEBs, one population each.
    const v = 1795.2482855523704;
    installAggregationOverlays(map as never, [{
      layer_id: "a",
      source_mode: "aggregation_overlay",
      source_config: {
        tiles_url_template: "/t/{z}/{x}/{y}",
        value_range: { count: 4, min: v, p05: v, p50: v, p95: v, max: v },
      },
    } as never]);
    const ramp = layers[0].paint!["fill-color"] as number[];
    expect(ramp[3]).toBeLessThan(ramp[5]);
    expect(ramp[5]).toBeLessThan(ramp[7]);
  });
});

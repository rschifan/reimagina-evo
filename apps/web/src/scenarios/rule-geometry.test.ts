import { describe, expect, it } from "vitest";

import type { EditGroup } from "../scenario";
import { inferShape, rulesToFeatureCollection, wktToGeometry } from "./rule-geometry";

const SQUARE = "POLYGON((0 0,1 0,1 1,0 1,0 0))";

function circleRing(n = 64, r = 0.01): number[][] {
  const ring = Array.from({ length: n }, (_, i) => {
    const a = (2 * Math.PI * i) / n;
    // At the equator, where round in degrees is round on the ground.
    return [10 + r * Math.cos(a), r * Math.sin(a)];
  });
  return [...ring, ring[0]];
}

describe("wktToGeometry", () => {
  it("reads a polygon, holes included", () => {
    const g = wktToGeometry("POLYGON((0 0,4 0,4 4,0 4,0 0),(1 1,2 1,2 2,1 1))");
    expect(g.type).toBe("Polygon");
    expect(g.coordinates).toHaveLength(2);
    expect(g.coordinates[0][1]).toEqual([4, 0]);
  });

  it("reads a multipolygon", () => {
    const g = wktToGeometry("MULTIPOLYGON(((0 0,1 0,1 1,0 0)),((5 5,6 5,6 6,5 5)))");
    expect(g.type).toBe("MultiPolygon");
    expect(g.coordinates).toHaveLength(2);
  });

  it("reads negative and decimal coordinates", () => {
    const g = wktToGeometry("POLYGON((-115.49 32.62,-115.46 32.62,-115.46 32.64,-115.49 32.62))");
    expect(g.type === "Polygon" && g.coordinates[0][0]).toEqual([-115.49, 32.62]);
  });

  it("refuses anything else", () => {
    expect(() => wktToGeometry("POINT(0 0)")).toThrow();
  });
});

describe("inferShape", () => {
  it("knows a rectangle", () => {
    expect(inferShape(wktToGeometry(SQUARE))).toBe("rectangle");
  });

  it("knows a circle", () => {
    expect(inferShape({ type: "Polygon", coordinates: [circleRing()] })).toBe("circle");
  });

  // Review: terra-draw's circles are round on the ground, so at Mexicali's
  // 32.6° they are ~1.19× wider than tall in degrees; a test in degrees
  // called every saved circle a polygon and lost its rim handle.
  it("knows a circle drawn at Mexicali's latitude", () => {
    const lat = 32.6;
    const scale = 1 / Math.cos((lat * Math.PI) / 180);
    const ring = Array.from({ length: 64 }, (_, i) => {
      const a = (2 * Math.PI * i) / 64;
      return [-115.47 + 0.004 * scale * Math.cos(a), lat + 0.004 * Math.sin(a)];
    });
    expect(inferShape({ type: "Polygon", coordinates: [[...ring, ring[0]]] })).toBe("circle");
  });

  it("calls anything else a polygon", () => {
    expect(inferShape(wktToGeometry("POLYGON((0 0,2 0,3 1,1 2,0 1,0 0))"))).toBe("polygon");
    // A slanted four-sided shape is not a rectangle the rectangle tool can edit.
    expect(inferShape(wktToGeometry("POLYGON((0 0,2 1,1 3,-1 2,0 0))"))).toBe("polygon");
  });

  it("cannot drag a shape in several pieces", () => {
    expect(inferShape(wktToGeometry("MULTIPOLYGON(((0 0,1 0,1 1,0 0)),((5 5,6 5,6 6,5 5)))"))).toBeNull();
  });
});

describe("rulesToFeatureCollection", () => {
  const group = (ordinal: number, levels: number, wkt = SQUARE): EditGroup => ({
    areaWkt: wkt,
    levels,
    unitSize: 60,
    peoplePerHousehold: 3.6,
    lotCoverage: 0.6,
    editIds: [`e${ordinal}`, `f${ordinal}`],
    complete: true,
    ordinal,
  });

  it("numbers rules from 1 in their order and labels them", () => {
    const fc = rulesToFeatureCollection([group(5, 3), group(1, 6)]);
    expect(fc.features.map((f) => f.properties?.index)).toEqual([1, 2]);
    expect(fc.features[0].properties?.label).toBe("Rule 1 · 6 st · 60 m² · 3.6 people · 60 %");
    expect(fc.features[1].properties?.editIds).toBe("e5,f5");
  });

  it("skips a rule whose area cannot be read rather than failing the map", () => {
    const fc = rulesToFeatureCollection([group(1, 6, "GEOMETRYCOLLECTION EMPTY")]);
    expect(fc.features).toHaveLength(0);
  });
});

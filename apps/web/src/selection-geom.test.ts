import { describe, expect, it } from "vitest";

import {
  pointInPolygon,
  polygonBbox,
  representativePoint,
} from "./selection-geom";

describe("polygonBbox", () => {
  it("returns null for empty rings", () => {
    expect(polygonBbox([])).toBeNull();
  });

  it("returns the tight extent for a unit square", () => {
    const square = [
      [0, 0],
      [1, 0],
      [1, 1],
      [0, 1],
      [0, 0],
    ];
    expect(polygonBbox(square)).toEqual([0, 0, 1, 1]);
  });

  it("handles negative + positive coordinates", () => {
    expect(
      polygonBbox([
        [-99.14, 19.43],
        [-99.13, 19.43],
        [-99.13, 19.44],
        [-99.14, 19.44],
        [-99.14, 19.43],
      ]),
    ).toEqual([-99.14, 19.43, -99.13, 19.44]);
  });
});

describe("pointInPolygon", () => {
  // 0,0 → 10,0 → 10,10 → 0,10 → 0,0
  const square: GeoJSON.Polygon["coordinates"] = [
    [
      [0, 0],
      [10, 0],
      [10, 10],
      [0, 10],
      [0, 0],
    ],
  ];

  it("classifies an interior point as inside", () => {
    expect(pointInPolygon([5, 5], square)).toBe(true);
  });

  it("classifies a point clearly outside as false", () => {
    expect(pointInPolygon([20, 20], square)).toBe(false);
  });

  it("returns false for empty rings input", () => {
    expect(pointInPolygon([0, 0], [])).toBe(false);
  });

  it("respects holes — point in a hole is outside", () => {
    const withHole: GeoJSON.Polygon["coordinates"] = [
      [
        [0, 0],
        [10, 0],
        [10, 10],
        [0, 10],
        [0, 0],
      ],
      // Inner ring: 4,4 → 6,4 → 6,6 → 4,6
      [
        [4, 4],
        [6, 4],
        [6, 6],
        [4, 6],
        [4, 4],
      ],
    ];
    expect(pointInPolygon([5, 5], withHole)).toBe(false);
    expect(pointInPolygon([1, 1], withHole)).toBe(true);
  });
});

describe("representativePoint", () => {
  it("returns null for null geometry", () => {
    expect(representativePoint(null)).toBeNull();
  });

  it("returns the point itself for Point geometries", () => {
    expect(
      representativePoint({ type: "Point", coordinates: [3, 4] }),
    ).toEqual([3, 4]);
  });

  it("returns the area-weighted centroid of a polygon's outer ring", () => {
    const square: GeoJSON.Polygon = {
      type: "Polygon",
      coordinates: [
        [
          [0, 0],
          [10, 0],
          [10, 10],
          [0, 10],
          [0, 0],
        ],
      ],
    };
    const pt = representativePoint(square)!;
    expect(pt[0]).toBeCloseTo(5, 6);
    expect(pt[1]).toBeCloseTo(5, 6);
  });

  it("falls back to vertex mean for zero-area degenerate rings", () => {
    // Three collinear points → zero area → vertex mean
    const degenerate: GeoJSON.Polygon = {
      type: "Polygon",
      coordinates: [
        [
          [0, 0],
          [2, 0],
          [4, 0],
          [0, 0],
        ],
      ],
    };
    const pt = representativePoint(degenerate)!;
    // Mean of (0,0), (2,0), (4,0) — closing vertex excluded.
    expect(pt[0]).toBeCloseTo(2, 6);
    expect(pt[1]).toBeCloseTo(0, 6);
  });

  it("uses the first sub-polygon for MultiPolygon", () => {
    const mp: GeoJSON.MultiPolygon = {
      type: "MultiPolygon",
      coordinates: [
        [
          [
            [0, 0],
            [2, 0],
            [2, 2],
            [0, 2],
            [0, 0],
          ],
        ],
        [
          [
            [100, 100],
            [101, 100],
            [101, 101],
            [100, 101],
            [100, 100],
          ],
        ],
      ],
    };
    const pt = representativePoint(mp)!;
    expect(pt[0]).toBeCloseTo(1, 6);
    expect(pt[1]).toBeCloseTo(1, 6);
  });

  it("returns null for empty MultiPolygon", () => {
    expect(
      representativePoint({ type: "MultiPolygon", coordinates: [] }),
    ).toBeNull();
  });

  it("returns the first vertex for LineString", () => {
    expect(
      representativePoint({
        type: "LineString",
        coordinates: [
          [1, 2],
          [3, 4],
        ],
      }),
    ).toEqual([1, 2]);
  });
});

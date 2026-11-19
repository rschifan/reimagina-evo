import { describe, expect, it } from "vitest";

import { anchorBox } from "./box-anchor";

const MAP = { width: 1000, height: 600 };
const BOX = { width: 220, height: 260 };

const inside = (p: { left: number; top: number }) =>
  p.left >= 8 && p.top >= 8 && p.left + BOX.width <= MAP.width - 8 && p.top + BOX.height <= MAP.height - 8;

describe("anchorBox", () => {
  it("prefers the right of the shape, level with its top", () => {
    expect(anchorBox({ minX: 100, minY: 100, maxX: 300, maxY: 200 }, MAP, BOX)).toEqual({
      left: 308,
      top: 100,
    });
  });

  it("goes left when the right side has no room", () => {
    const p = anchorBox({ minX: 700, minY: 100, maxX: 900, maxY: 200 }, MAP, BOX);
    expect(p.left).toBe(700 - 8 - BOX.width);
  });

  it("keeps the box inside the map", () => {
    const shapes = [
      { minX: -50, minY: -50, maxX: 20, maxY: 20 },
      { minX: 980, minY: 580, maxX: 1100, maxY: 700 },
      { minX: 0, minY: 500, maxX: 40, maxY: 590 },
      { minX: -100, minY: -100, maxX: 1200, maxY: 900 },
    ];
    for (const shape of shapes) expect(inside(anchorBox(shape, MAP, BOX))).toBe(true);
  });
});

import { describe, expect, it } from "vitest";

import { clampZoom } from "./map-base";

describe("clampZoom", () => {
  it("raises a camera below the floor", () => {
    expect(clampZoom({ zoom: 7.2 }, 8).zoom).toBe(8);
  });
  it("leaves a camera above the floor alone", () => {
    expect(clampZoom({ zoom: 11 }, 8).zoom).toBe(11);
  });
});

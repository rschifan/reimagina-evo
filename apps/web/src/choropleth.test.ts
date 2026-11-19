import { describe, expect, it } from "vitest";

import { overlayMinzoom } from "./choropleth";

describe("overlayMinzoom", () => {
  const config = { tilesUrlTemplate: "", sourceLayer: "x", minzoom: 10, maxzoom: 16 };
  it("follows the block baseline, not the catalog's 10", () => {
    expect(overlayMinzoom(config, "block")).toBe(8);
  });
  it("falls back to the catalog when the grain is unknown", () => {
    expect(overlayMinzoom(config, null)).toBe(10);
  });
});

import { describe, expect, it } from "vitest";

import { grainCount, overlayMinzoom } from "./choropleth";

describe("overlayMinzoom", () => {
  const config = { tilesUrlTemplate: "", sourceLayer: "x", minzoom: 10, maxzoom: 16 };
  it("follows the block baseline, not the catalog's 10", () => {
    expect(overlayMinzoom(config, "block")).toBe(8);
  });
  it("falls back to the catalog when the grain is unknown", () => {
    expect(overlayMinzoom(config, null)).toBe(10);
  });
});


describe("grainCount", () => {
  it("counts one in the singular and more in the plural", () => {
    expect(grainCount(1, "block")).toBe("1 block");
    expect(grainCount(2, "block")).toBe("2 blocks");
    expect(grainCount(1, "municipality")).toBe("1 municipality");
    expect(grainCount(19058, "block")).toBe(`${(19058).toLocaleString()} blocks`);
  });

  it("falls back to features for no grain", () => {
    expect(grainCount(1, null)).toBe("1 feature");
    expect(grainCount(2, null)).toBe("2 features");
  });
});

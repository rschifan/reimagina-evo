import { describe, expect, it } from "vitest";

import { grainCount } from "./choropleth";

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

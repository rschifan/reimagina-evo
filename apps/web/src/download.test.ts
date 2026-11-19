import { describe, expect, it } from "vitest";

import { downloadUrl } from "./api";

describe("downloadUrl", () => {
  it("scopes a download to the AOI and grain on screen, at the API's row ceiling", () => {
    const url = downloadUrl("x", "csv", { aoiId: "a", grain: "block" });
    expect(url).toContain("aoi_id=a");
    expect(url).toContain("feature_type=block");
    expect(url).toContain("limit=500000");
    expect(url.startsWith("/api/indicators/x/values.csv?")).toBe(true);
  });

  it("carries a scenario and leaves out a scope it does not have", () => {
    const url = downloadUrl("x", "parquet", { aoiId: null, grain: null, scenarioId: "s1" });
    expect(url).toContain("scenario_id=s1");
    expect(url).not.toContain("aoi_id");
    expect(url).not.toContain("feature_type");
  });
});

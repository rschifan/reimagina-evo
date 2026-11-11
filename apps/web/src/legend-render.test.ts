// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import type { IndicatorSummary, IndicatorValueStats } from "./api";
import { rampDomainFromStats, renderLegend } from "./choropleth";

const PARK = {
  slug: "accessibility_block_park_network_m",
  name: "Distance to park or recreation site (network)",
  unit: "m",
  grain: "block",
  valid_min: 0,
  valid_max: 1500,
} as IndicatorSummary;

function uniformStats(): IndicatorValueStats {
  return {
    count: 19058,
    min: 1500,
    p05: 1500,
    p50: 1500,
    p95: 1500,
    max: 1500,
    histogram: null,
  };
}

describe("renderLegend", () => {
  // The legend is the only place that tells a reader a flat layer is flat,
  // so the wording has to survive the render, not just the helper.
  it("tells the reader a uniform layer has rows, not no data", () => {
    const el = document.createElement("div");
    renderLegend(el, PARK, rampDomainFromStats(uniformStats(), PARK), null);

    const scope = el.querySelector(".scope")?.textContent ?? "";
    expect(scope).toContain("19,058");
    expect(scope).toContain("identical");
    expect(scope).not.toContain("no data");
    expect(el.hidden).toBe(false);
  });

  it("still says no data when there are genuinely no rows", () => {
    const el = document.createElement("div");
    const empty: IndicatorValueStats = {
      count: 0,
      min: null,
      p05: null,
      p50: null,
      p95: null,
      max: null,
      histogram: null,
    };
    renderLegend(el, PARK, rampDomainFromStats(empty, PARK), null);
    expect(el.querySelector(".scope")?.textContent ?? "").toContain("no data yet");
  });
});

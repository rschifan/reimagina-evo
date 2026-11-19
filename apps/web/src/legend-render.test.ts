// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import type { IndicatorSummary, IndicatorValueStats } from "./api";
import { CAP_COLOR, markLegendValue, rampDomainFromStats, renderLegend } from "./choropleth";

const PARK = {
  slug: "accessibility_block_park_network_m",
  name: "Distance to park or recreation site (network)",
  unit: "m",
  // `applies_to`, not `grain`: the catalog field was renamed and this
  // fixture kept the old name, so the `as IndicatorSummary` cast stopped
  // overlapping and `tsc --noEmit` — i.e. `npm run build` — failed on it.
  applies_to: ["block"],
  valid_min: 0,
  valid_max: 1500,
  cap_value: 1500,
} as IndicatorSummary;

const LIVABILITY = {
  slug: "livability_block_score",
  name: "Livability score",
  unit: "score_0_1",
  applies_to: ["block"],
  valid_min: 0,
  valid_max: 1,
  cap_value: null,
} as IndicatorSummary;

function spreadStats(): IndicatorValueStats {
  return {
    count: 19058,
    min: 0,
    p05: 400,
    p50: 1500,
    p95: 1500,
    max: 1500,
    sum: null,
    histogram: { lo: 0, hi: 1500, width: 750, bins: 2, counts: [7277, 11781] },
  };
}

function uniformStats(): IndicatorValueStats {
  return {
    count: 19058,
    min: 1500,
    p05: 1500,
    p50: 1500,
    p95: 1500,
    max: 1500,
    sum: null,
    histogram: null,
  };
}

describe("renderLegend", () => {
  // The legend is the only place that tells a reader a flat layer is flat,
  // so the wording has to survive the render, not just the helper.
  it("tells the reader a uniform layer has rows, not no data", () => {
    const el = document.createElement("div");
    renderLegend(el, PARK, rampDomainFromStats(uniformStats(), PARK), null, 19058, 19058);

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
      sum: null,
      histogram: null,
    };
    renderLegend(el, PARK, rampDomainFromStats(empty, PARK), null, 0, 0);
    expect(el.querySelector(".scope")?.textContent ?? "").toContain("no data yet");
  });
});

describe("the cap in the legend", () => {
  it("names the cap and says how many blocks sit on it", () => {
    const el = document.createElement("div");
    const stats = spreadStats();
    renderLegend(el, PARK, rampDomainFromStats(stats, PARK), stats.histogram, 11781, 19058);
    const text = el.textContent ?? "";
    expect(text).toContain("1,500 m+ (cap)");
    expect(text).toContain("62% of blocks at the 1,500 m cap");
    expect(el.innerHTML).toContain(CAP_COLOR);
  });

  it("keeps the swatch but claims no share when the count is unknown", () => {
    const el = document.createElement("div");
    renderLegend(el, PARK, rampDomainFromStats(null, PARK), null, null, 0);
    expect(el.textContent).toContain("1,500 m+ (cap)");
    expect(el.textContent).not.toContain("% of blocks");
  });

  it("says nothing about a cap for an indicator without one", () => {
    const el = document.createElement("div");
    const stats = { ...spreadStats(), min: 0.1, p05: 0.2, p50: 0.41, p95: 0.7, max: 0.9, histogram: null };
    renderLegend(el, LIVABILITY, rampDomainFromStats(stats, LIVABILITY), null, null, 19058);
    expect(el.textContent ?? "").not.toContain("cap");
    expect(el.innerHTML).not.toContain(CAP_COLOR);
  });

  it("writes the unit as words", () => {
    const el = document.createElement("div");
    const ind = { ...LIVABILITY, unit: "units_per_ha" } as IndicatorSummary;
    renderLegend(el, ind, rampDomainFromStats(null, ind), null, null, 0);
    expect(el.textContent).toContain("units / ha");
    expect(el.textContent).not.toContain("units_per_ha");
  });
});

describe("markLegendValue", () => {
  it("marks the hovered value without a bin-level rank", () => {
    const el = document.createElement("div");
    const stats = spreadStats();
    const domain = rampDomainFromStats(stats, PARK);
    renderLegend(el, PARK, domain, stats.histogram, 11781, 19058);
    markLegendValue(el, PARK, domain, stats.histogram, 1500);
    expect(el.querySelector(".dist__cursor")).not.toBeNull();
    expect(el.textContent ?? "").not.toContain("higher than");
  });
});

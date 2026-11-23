// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import type { IndicatorSummary, IndicatorValueStats } from "./api";
import { CAP_COLOR, rampDomainFromStats, renderGrainToggle, renderLegend } from "./choropleth";
import { markLegendValue } from "./distribution";

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

  it("does not claim every block when one is short of the cap", () => {
    const el = document.createElement("div");
    const stats = spreadStats();
    renderLegend(el, PARK, rampDomainFromStats(stats, PARK), stats.histogram, 19057, 19058);
    expect(el.textContent).toContain(">99% of blocks at the 1,500 m cap");
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
  const rendered = () => {
    const el = document.createElement("div");
    const stats = spreadStats();
    renderLegend(el, PARK, rampDomainFromStats(stats, PARK), stats.histogram, 11781, 19058);
    return { el, histogram: stats.histogram! };
  };
  const current = (el: HTMLElement) =>
    Array.from(el.querySelectorAll(".dist__bar"), (b) => b.classList.contains("dist__bar--current"));

  it("marks the hovered value without a bin-level rank", () => {
    const { el, histogram } = rendered();
    markLegendValue(el, histogram, 1500);
    const cursor = el.querySelector(".dist__cursor")!;
    expect(cursor.getAttribute("visibility")).toBeNull();
    expect(cursor.getAttribute("x1")).toBe("240.00");
    expect(current(el)).toEqual([false, true]);
    expect(el.textContent ?? "").not.toContain("higher than");
  });

  it("moves the one cursor in place and hides it on leave, without redrawing", () => {
    const { el, histogram } = rendered();
    const svg = el.querySelector("svg");
    markLegendValue(el, histogram, 375);
    expect(el.querySelector(".dist__cursor")?.getAttribute("x1")).toBe("60.00");
    expect(current(el)).toEqual([true, false]);
    markLegendValue(el, histogram, null);
    expect(el.querySelector("svg")).toBe(svg);
    expect(el.querySelectorAll(".dist__cursor")).toHaveLength(1);
    expect(el.querySelector(".dist__cursor")?.getAttribute("visibility")).toBe("hidden");
    expect(current(el)).toEqual([false, false]);
  });
});

describe("one number style in the legend", () => {
  it("prints the bounds like the cap swatch: separators and the unit as words", () => {
    const el = document.createElement("div");
    const stats = spreadStats();
    renderLegend(el, PARK, rampDomainFromStats(stats, PARK), stats.histogram, 11781, 19058);
    const bounds = el.querySelector(".bounds")?.textContent ?? "";
    expect(bounds).toMatch(/ m/);
    expect(bounds).not.toMatch(/\d{4}/);
    expect(el.textContent ?? "").not.toContain("p05");
  });

  it("prints a bound at the cap exactly as the median at the cap", () => {
    const el = document.createElement("div");
    const stats = spreadStats();
    renderLegend(el, PARK, rampDomainFromStats(stats, PARK), stats.histogram, 11781, 19058);
    const [lo, hi] = Array.from(el.querySelectorAll(".bounds span"), (s) => s.textContent);
    expect(lo).toBe("400 m");
    expect(hi).toBe("1,500 m+ (cap)");
    expect(el.querySelector(".p50")?.textContent).toBe("median · 1,500 m+ (cap)");
  });
});

describe("a layer where every block sits at the cap", () => {
  it("prints every value as the cap, scope line and bar title included", () => {
    const el = document.createElement("div");
    const stats = { ...uniformStats(), histogram: { lo: 1500, hi: 1500, width: 0, bins: 1, counts: [19058] } };
    renderLegend(el, PARK, rampDomainFromStats(stats, PARK), stats.histogram, 19058, 19058);
    // The low bound is the catalog floor of the one-colour ramp, not a value.
    const bounds = Array.from(el.querySelectorAll(".bounds span"), (s) => s.textContent);
    expect(bounds).toEqual(["0 m", "1,500 m+ (cap)"]);
    expect(el.querySelector(".cap-key")?.textContent).toBe("1,500 m+ (cap)");
    expect(el.querySelector(".scope")?.textContent).toContain("identical (1,500 m+ (cap))");
    expect(el.querySelector("title")?.textContent).toBe("19058 at 1,500 m+ (cap)");
  });
});

describe("renderGrainToggle", () => {
  it("shows nothing when the indicator has one grain", () => {
    const el = document.createElement("div");
    renderGrainToggle(el, PARK, "block", () => undefined);
    expect(el.hidden).toBe(true);
    expect(el.children.length).toBe(0);
  });

  it("offers a choice when there is one", () => {
    const el = document.createElement("div");
    const two = { ...PARK, applies_to: ["block", "municipality"] } as IndicatorSummary;
    renderGrainToggle(el, two, "block", () => undefined);
    expect(el.hidden).toBe(false);
    expect(el.querySelectorAll("button").length).toBe(2);
  });
});

describe("the legend's top bound", () => {
  const FLOORS = { ...LIVABILITY, slug: "urban_form_block_planned_floors", name: "Planned Floors", unit: "floors", valid_min: 0, valid_max: 60 } as IndicatorSummary;

  it("says the darkest colour includes everything above it, and the real maximum", () => {
    const el = document.createElement("div");
    const s = { count: 19058, min: 1, p05: 1, p50: 1, p95: 1.72, max: 8.99, sum: null, histogram: null } as IndicatorValueStats;
    renderLegend(el, FLOORS, rampDomainFromStats(s, FLOORS), null, null, 19058);
    const top = el.querySelector(".bounds span:last-child")!.textContent!;
    expect(top).toContain("1.72");
    expect(top).toContain("≥");
    expect(top).toContain("max 8.99");
  });

  it("prints a plain top when the ramp already reaches the maximum", () => {
    const el = document.createElement("div");
    const s = { count: 3, min: 0, p05: 0, p50: 0.5, p95: 1, max: 1, sum: null, histogram: null } as IndicatorValueStats;
    renderLegend(el, LIVABILITY, rampDomainFromStats(s, LIVABILITY), null, null, 3);
    const top = el.querySelector(".bounds span:last-child")!.textContent!;
    expect(top).not.toContain("≥");
    expect(top).not.toContain("max");
  });
});


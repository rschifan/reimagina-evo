import { describe, expect, it } from "vitest";

import type { ValueHistogram } from "./api";
import {
  binLabel,
  binOf,
  distributionSvg,
  percentileOf,
  positionOf,
} from "./distribution";

/** Four bins over 0–100, twenty values, deliberately lopsided. */
function histogram(overrides: Partial<ValueHistogram> = {}): ValueHistogram {
  return {
    bins: 4,
    lo: 0,
    hi: 100,
    width: 25,
    counts: [12, 5, 2, 1],
    ...overrides,
  };
}

/** Every value identical — the single-feature AOI, and the common case. */
const FLAT: ValueHistogram = {
  bins: 1,
  lo: 42,
  hi: 42,
  width: 0,
  counts: [1],
};

describe("binOf", () => {
  it("places a value in the bin that contains it", () => {
    expect(binOf(10, histogram())).toBe(0);
    expect(binOf(30, histogram())).toBe(1);
    expect(binOf(99, histogram())).toBe(3);
  });

  it("folds the maximum into the last bin rather than past it", () => {
    // Mirrors the server's `LEAST(width_bucket(...), bins)`. Without it the
    // single value a reader is most likely to click — the extreme — marks no
    // bin at all.
    expect(binOf(100, histogram())).toBe(3);
  });

  it("has no bin for a value outside the range", () => {
    expect(binOf(-1, histogram())).toBeNull();
    expect(binOf(101, histogram())).toBeNull();
  });

  it("puts everything in the one bin when every value is identical", () => {
    expect(binOf(42, FLAT)).toBe(0);
  });

  it("refuses a non-finite value rather than computing with it", () => {
    expect(binOf(Number.NaN, histogram())).toBeNull();
    expect(binOf(Number.POSITIVE_INFINITY, histogram())).toBeNull();
  });
});

describe("positionOf", () => {
  it("maps the range onto 0–1", () => {
    expect(positionOf(0, histogram())).toBe(0);
    expect(positionOf(50, histogram())).toBe(0.5);
    expect(positionOf(100, histogram())).toBe(1);
  });

  it("clamps rather than running off the end of the strip", () => {
    expect(positionOf(-50, histogram())).toBe(0);
    expect(positionOf(500, histogram())).toBe(1);
  });

  it("puts a flat distribution's marker in the middle", () => {
    // The only honest answer, and the one that keeps the marker visible
    // rather than hard against an edge.
    expect(positionOf(42, FLAT)).toBe(0.5);
  });
});

describe("percentileOf", () => {
  it("uses the mid-rank of the value's own bin", () => {
    // 12 below the second bin, half of its 5 → 14.5 of 20.
    expect(percentileOf(30, histogram())).toBe(73);
  });

  // ⚠ The phrasing bug this estimator exists to avoid. Counting a value's
  // whole bin as "below" puts every value in the top bin at 100 %, which
  // renders as "higher than 100% of features" — wrong, and obviously wrong,
  // on exactly the outlier a planner is most likely to click.
  it("never reports a value as higher than all of them", () => {
    expect(percentileOf(100, histogram())).toBeLessThan(100);
    expect(percentileOf(100, histogram())).toBeGreaterThan(90);
  });

  it("never reports the minimum as higher than none of them", () => {
    expect(percentileOf(0, histogram())).toBeGreaterThan(0);
  });

  it("declines to rank a population of one", () => {
    // "higher than 50%" with a single feature is an artefact of the
    // arithmetic, not a finding.
    expect(percentileOf(42, FLAT)).toBeNull();
  });

  it("has no rank for a value outside the range", () => {
    expect(percentileOf(999, histogram())).toBeNull();
  });
});

describe("binLabel", () => {
  it("names a bin's edges with the unit", () => {
    expect(binLabel(0, histogram(), "m")).toBe("0 – 25 m");
  });

  it("omits the unit when the indicator has none", () => {
    expect(binLabel(1, histogram(), "")).toBe("25 – 50");
  });

  it("names the single value when every value is identical", () => {
    expect(binLabel(0, FLAT, "m")).toBe("42 m");
  });
});

describe("distributionSvg", () => {
  it("draws nothing at all when there is nothing to draw", () => {
    // An empty frame would imply a population of none rather than no answer.
    const marks = { rampMin: 0, rampMax: 1, median: null };
    expect(distributionSvg(null, marks)).toBe("");
    expect(
      distributionSvg(histogram({ counts: [0, 0, 0, 0] }), marks),
    ).toBe("");
  });

  it("draws one bar per bin", () => {
    const svg = distributionSvg(histogram(), {
      rampMin: 10,
      rampMax: 90,
      median: 20,
    });
    expect(svg.match(/class="dist__bar/g)).toHaveLength(4);
  });

  it("keeps a one-count bin visible against a tall peak", () => {
    // A tail bin that rounds to half a pixel disappears, and the tails are
    // where the interesting features are.
    const svg = distributionSvg(histogram({ counts: [400, 0, 0, 1] }), {
      rampMin: 0,
      rampMax: 100,
      median: 10,
    });
    const heights = [...svg.matchAll(/height="([\d.]+)"/g)].map((m) =>
      Number(m[1]),
    );
    expect(Math.min(...heights.filter((h) => h > 0))).toBeGreaterThanOrEqual(1.5);
  });

  it("marks exactly the hovered value's bin", () => {
    const svg = distributionSvg(histogram(), {
      rampMin: 0,
      rampMax: 100,
      median: 20,
      hovered: 30,
    });
    expect(svg.match(/dist__bar--current/g)).toHaveLength(1);
    expect(svg).toContain("dist__cursor");
  });

  it("marks no bin when nothing is hovered", () => {
    const svg = distributionSvg(histogram(), {
      rampMin: 0,
      rampMax: 100,
      median: 20,
      hovered: null,
    });
    expect(svg).not.toContain("dist__bar--current");
    expect(svg).not.toContain("dist__cursor");
  });

  it("shows the span the colour ramp resolves", () => {
    // Outside it the choropleth saturates and two very different values
    // share a colour — a property of the ramp worth seeing rather than
    // inferring.
    const svg = distributionSvg(histogram(), {
      rampMin: 25,
      rampMax: 75,
      median: 20,
    });
    expect(svg).toContain("dist__ramp");
  });

  it("gives every bar a readable title", () => {
    const svg = distributionSvg(
      histogram(),
      { rampMin: 0, rampMax: 100, median: 20 },
      "m",
    );
    expect(svg).toContain("<title>12 at 0 – 25 m</title>");
  });

  it("escapes a unit rather than injecting it", () => {
    const svg = distributionSvg(histogram(), {
      rampMin: 0,
      rampMax: 100,
      median: null,
    }, '"><script>alert(1)</script>');
    expect(svg).not.toContain("<script>");
    expect(svg).toContain("&lt;script&gt;");
  });
});

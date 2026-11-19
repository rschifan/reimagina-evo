import { createExpression } from "@maplibre/maplibre-gl-style-spec";
import type { ExpressionSpecification } from "maplibre-gl";
import { describe, expect, it } from "vitest";

import type { IndicatorSummary, IndicatorValueStats } from "./api";
import {
  CAP_COLOR,
  DELTA_NEUTRAL,
  DELTA_NEW,
  deltaStops,
  RAMP_STOPS,
  rampDomainFromStats,
  rampScopeText,
  valueRamp,
} from "./choropleth";

/** A catalog entry whose safety bounds are nothing like a view range. */
function indicator(overrides: Partial<IndicatorSummary> = {}): IndicatorSummary {
  return {
    slug: "accessibility_block_park_network_m",
    name: "Distance to park or recreation site (network)",
    unit: "m",
    grain: "block",
    valid_min: 0,
    valid_max: 1500,
    ...overrides,
  } as IndicatorSummary;
}

function stats(overrides: Partial<IndicatorValueStats> = {}): IndicatorValueStats {
  return {
    count: 0,
    min: null,
    p05: null,
    p50: null,
    p95: null,
    max: null,
    sum: null,
    histogram: null,
    ...overrides,
  };
}

describe("rampDomainFromStats", () => {
  it("spans the observed p05-p95 when the distribution has spread", () => {
    const domain = rampDomainFromStats(
      stats({ count: 19058, min: 0, p05: 90, p50: 410, p95: 1180, max: 1500 }),
      indicator(),
    );
    expect(domain).toMatchObject({
      min: 90,
      max: 1180,
      p50: 410,
      count: 19058,
      basis: "percentile",
      fromStats: true,
    });
  });

  it("widens to min-max when p05 equals p95 but the values still differ", () => {
    const domain = rampDomainFromStats(
      stats({ count: 1200, min: 250, p05: 900, p50: 900, p95: 900, max: 1500 }),
      indicator(),
    );
    expect(domain).toMatchObject({ min: 250, max: 1500, basis: "min-max", fromStats: true });
  });

  // The Mexicali park case: the amenity layer carries no park destinations, so
  // every block reports the 1500 m cutoff. There is no span to derive, but
  // there are 19,058 rows — reporting that as "no data" sends the reader
  // hunting for a compute that already ran.
  it("calls an all-identical layer uniform, not empty", () => {
    const domain = rampDomainFromStats(
      stats({ count: 19058, min: 1500, p05: 1500, p50: 1500, p95: 1500, max: 1500 }),
      indicator(),
    );
    expect(domain.basis).toBe("uniform");
    expect(domain.fromStats).toBe(false);
    expect(domain.count).toBe(19058);
    expect(domain.p50).toBe(1500);
    // Falls back to catalog bounds, because nothing else spans.
    expect(domain).toMatchObject({ min: 0, max: 1500 });
  });

  it("calls a layer with no rows empty", () => {
    const domain = rampDomainFromStats(stats({ count: 0 }), indicator());
    expect(domain).toMatchObject({ basis: "empty", fromStats: false, count: 0, p50: null });
  });

  it("treats absent stats as empty", () => {
    expect(rampDomainFromStats(null, indicator()).basis).toBe("empty");
  });

  it("falls back to 0..1 when the catalog declares no bounds", () => {
    const domain = rampDomainFromStats(
      stats({ count: 0 }),
      indicator({ valid_min: null, valid_max: null } as Partial<IndicatorSummary>),
    );
    expect(domain).toMatchObject({ min: 0, max: 1 });
  });
});

describe("rampScopeText", () => {
  it("distinguishes a uniform layer from an empty one", () => {
    const uniform = rampScopeText(
      rampDomainFromStats(
        stats({ count: 19058, min: 1500, p05: 1500, p50: 1500, p95: 1500, max: 1500 }),
        indicator(),
      ),
      indicator(),
    );
    expect(uniform).toContain("19,058");
    expect(uniform).toContain("identical");
    expect(uniform).not.toContain("no data");

    expect(rampScopeText(rampDomainFromStats(stats({ count: 0 }), indicator()), indicator())).toContain(
      "no data yet",
    );
  });

  it("names the percentile basis in plain words when the ramp came from spread", () => {
    const text = rampScopeText(
      rampDomainFromStats(
        stats({ count: 19058, min: 0, p05: 90, p50: 410, p95: 1180, max: 1500 }),
        indicator(),
      ),
      indicator(),
    );
    expect(text).toContain("middle 90%");
    expect(text).not.toContain("p05");
  });
});

/** What MapLibre's own engine paints for `value_numeric`, as `#rrggbb`. */
function paintedAt(expression: ExpressionSpecification, value: number): string {
  const compiled = createExpression(expression, "fill-color", {
    type: "color",
    "property-type": "data-driven",
    expression: { interpolated: true, parameters: ["zoom", "feature"] },
  } as never);
  if (compiled.result !== "success") throw new Error("MapLibre rejects the ramp");
  const c = compiled.value.evaluate({ zoom: 12 } as never, {
    type: 3,
    properties: { value_numeric: value },
  } as never) as { r: number; g: number; b: number };
  return "#" + [c.r, c.g, c.b].map((v) => Math.round(v * 255).toString(16).padStart(2, "0")).join("");
}

describe("valueRamp and the cap", () => {
  const domain = rampDomainFromStats(
    stats({ count: 19058, min: 0, p05: 400, p50: 1500, p95: 1500, max: 1500 }),
    indicator(),
  );

  it("paints values at or past the cap in the cap colour", () => {
    const ramp = valueRamp(domain, 1500);
    expect(ramp[0]).toBe("case");
    expect(paintedAt(ramp, 1500)).toBe(CAP_COLOR);
    expect(paintedAt(ramp, 1600)).toBe(CAP_COLOR);
    expect(paintedAt(ramp, 1499)).not.toBe(CAP_COLOR);
  });

  it("has no cap colour anywhere when the indicator declares no cap", () => {
    expect(JSON.stringify(valueRamp(domain, null))).not.toContain(CAP_COLOR);
  });

  it("keeps the ramp's high end off the darkest blue, which vanishes on the map", () => {
    expect(RAMP_STOPS).not.toContain("#08306b");
    expect(JSON.stringify(valueRamp(domain, null))).not.toContain("#08306b");
  });

  it("uses a cap colour the scenario delta palette does not", () => {
    for (const higherIsBetter of [true, false]) {
      expect(deltaStops(higherIsBetter)).not.toContain(CAP_COLOR);
    }
    expect([DELTA_NEW, DELTA_NEUTRAL, ...RAMP_STOPS]).not.toContain(CAP_COLOR);
  });
});

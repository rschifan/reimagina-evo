// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import type { IndicatorSummary } from "./api";
import { deltaTooltip, rampDomainFromStats, renderLegend, valueTooltip } from "./choropleth";
import { renderInspector } from "./inspector";

/**
 * A block whose scenario inputs the fit had to guess (Overture maps none or
 * too little of its buildings) carries ``estimated`` in the overlay tile and
 * is counted in the stats; every place a block's number is read says so in
 * words, not colour.
 */
const DENSITY = {
  slug: "edificability_block_density",
  name: "Housing Density (potential)",
  unit: "units_per_ha",
  applies_to: ["block"],
  valid_min: 0,
  valid_max: 2000,
  cap_value: null,
} as IndicatorSummary;

const LABEL = "estimated — buildings not fully mapped";

describe("estimated blocks", () => {
  it("the value tooltip says so, and says nothing for a mapped block", () => {
    expect(valueTooltip({ value_numeric: 40, estimated: "no_footprint" }, DENSITY, false)?.detail).toContain(LABEL);
    expect(valueTooltip({ value_numeric: 40 }, DENSITY, false)?.detail).not.toContain("estimated");
  });

  it("the delta tooltip says so", () => {
    expect(deltaTooltip({ state: "changed", delta: 3, estimated: "thin_footprint" }, DENSITY)?.detail).toContain(LABEL);
    expect(deltaTooltip({ state: "unchanged", delta: 0, estimated: "thin_footprint" }, DENSITY)?.detail).toContain(LABEL);
    expect(deltaTooltip({ state: "unchanged", delta: 0 }, DENSITY)?.detail).not.toContain("estimated");
  });

  it("the legend counts them, and is silent when there are none", () => {
    const el = document.createElement("div");
    renderLegend(el, DENSITY, rampDomainFromStats(null, DENSITY), null, null, 14540, 2097);
    expect(el.textContent).toContain(`2,097 of 14,540 blocks ${LABEL}`);
    renderLegend(el, DENSITY, rampDomainFromStats(null, DENSITY), null, null, 14540, 0);
    expect(el.textContent).not.toContain("estimated");
  });

  it("the inspector says so for a pinned estimated block", () => {
    const el = document.createElement("div");
    const noop = (): void => {};
    renderInspector(el, { id: "b1", grain: "block", estimated: "no_footprint" }, [], null, noop, noop);
    expect(el.textContent).toContain("Estimated — buildings not fully mapped");
    renderInspector(el, { id: "b1", grain: "block" }, [], null, noop, noop);
    expect(el.textContent).not.toContain("Estimated");
  });
});

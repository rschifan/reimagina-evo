// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import type { IndicatorSummary } from "./api";
import { INITIAL_CARD, nextCardState, renderSelectionCard, type CardState } from "./selection-card";
import { isDrawMode } from "./selection";

const PARK = {
  slug: "accessibility_block_park_network_m",
  name: "Distance to park",
  unit: "m",
  applies_to: ["block"],
  cap_value: 1500,
} as IndicatorSummary;

const DENSITY = {
  slug: "urban_form_block_building_density",
  name: "Building density",
  unit: "buildings_per_ha",
  applies_to: ["block"],
  cap_value: null,
} as IndicatorSummary;

const shown = (stats: CardState["stats"]): CardState => ({ visible: true, stale: false, token: 1, stats });

function render(state: CardState, indicator: IndicatorSummary | null, aoiStats: { p50: number | null } | null = null): HTMLElement {
  const host = document.createElement("div");
  renderSelectionCard(host, state, indicator, aoiStats);
  return host;
}

describe("nextCardState", () => {
  it("keeps the previous numbers on a new selection, marked stale", () => {
    const before = shown({ count: 12 });
    const after = nextCardState(before, { type: "selection-changed" });
    expect(after).toEqual({ visible: true, stale: true, token: 2, stats: { count: 12 } });
  });

  it("ignores a response to a superseded request", () => {
    const pending = nextCardState(nextCardState(shown({ count: 12 }), { type: "selection-changed" }), { type: "selection-changed" });
    expect(nextCardState(pending, { type: "response", token: 2, stats: { count: 99 } })).toBe(pending);
  });

  it("replaces the numbers and clears stale on the current response", () => {
    const pending = nextCardState(shown({ count: 12 }), { type: "selection-changed" });
    expect(nextCardState(pending, { type: "response", token: pending.token, stats: { count: 30 } })).toEqual({
      visible: true, stale: false, token: 2, stats: { count: 30 },
    });
  });

  it("hides and forgets on clear, so a late response cannot reopen it", () => {
    const cleared = nextCardState(shown({ count: 12 }), { type: "cleared" });
    expect(cleared).toEqual({ visible: false, stale: false, token: 2, stats: null });
    expect(nextCardState(cleared, { type: "response", token: 1, stats: { count: 5 } })).toBe(cleared);
  });
});

describe("renderSelectionCard", () => {
  it("shows a capped indicator's median, share at the cap and the AOI median", () => {
    const host = render(shown({ count: 200, p05: 300, p50: 1500, p95: 1500, count_at_cap: 124 }), PARK, { p50: 900 });
    const text = host.textContent ?? "";
    expect(text).toContain("Selection · 200 blocks");
    expect(text).toContain("Median 1,500 m+");
    expect(text).toContain("62% at the cap");
    expect(text).toContain("Whole AOI: median 900 m");
    expect(host.hidden).toBe(false);
  });

  it("never rounds the share at the cap to all or none", () => {
    const most = render(shown({ count: 200, p05: 1500, p50: 1500, p95: 1500, count_at_cap: 199 }), PARK).textContent ?? "";
    expect(most).toContain(">99% at the cap");
    const few = render(shown({ count: 300, p05: 100, p50: 400, p95: 900, count_at_cap: 1 }), PARK).textContent ?? "";
    expect(few).toContain("<1% at the cap");
  });

  it("counts one block in the singular", () => {
    const text = render(shown({ count: 1, p05: 400, p50: 400, p95: 400, count_at_cap: 0 }), PARK).textContent ?? "";
    expect(text).toContain("Selection · 1 block");
    expect(text).not.toContain("1 blocks");
  });

  it("shows the p05–p95 range and no cap for an uncapped indicator", () => {
    const text = render(shown({ count: 40, p05: 2, p50: 10, p95: 48.5, count_at_cap: null }), DENSITY).textContent ?? "";
    expect(text).toContain("Median 10 buildings / ha");
    expect(text).toContain("2 – 48.5 buildings / ha");
    expect(text).not.toContain("cap");
  });

  it("says so when no block is inside", () => {
    const text = render(shown({ count: 0, p05: null, p50: null, p95: null }), PARK).textContent ?? "";
    expect(text).toContain("No blocks inside");
    expect(text).not.toContain("Median");
  });

  it("shows only the block count with no indicator", () => {
    const host = render(shown({ count: 7 }), null, { p50: 900 });
    expect(host.textContent).toBe("Selection · 7 blocks");
  });

  it("dims stale numbers rather than replacing them", () => {
    const host = render({ visible: true, stale: true, token: 2, stats: { count: 7 } }, null);
    expect(host.classList.contains("is-stale")).toBe(true);
    expect(host.textContent).toBe("Selection · 7 blocks");
  });

  it("stays hidden until the first numbers arrive", () => {
    expect(render(nextCardState(INITIAL_CARD, { type: "selection-changed" }), PARK).hidden).toBe(true);
  });
});

describe("isDrawMode", () => {
  it("is true only while a shape is being drawn", () => {
    expect(["circle", "rectangle", "polygon"].map(isDrawMode)).toEqual([true, true, true]);
    expect(["select", "static"].map(isDrawMode)).toEqual([false, false]);
  });
});

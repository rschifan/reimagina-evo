// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

import type { FeatureIndicatorValue } from "./api";
import { rankText, renderInspector } from "./inspector";

function row(over: Partial<FeatureIndicatorValue> = {}): FeatureIndicatorValue {
  return {
    slug: "accessibility_block_park_network_m",
    name: "Distance to park",
    unit: "m",
    cap_value: 1500,
    value: 444,
    count_below: 5908,
    count_equal: 1,
    count: 19058,
    ...over,
  };
}

describe("rankText", () => {
  it("states the share at the cap for a capped value", () => {
    expect(rankText(row({ value: 1500, count_equal: 11781 }))).toBe("At the cap, with 62% of blocks");
  });

  it("reads a distance as farther than", () => {
    expect(rankText(row())).toBe("Farther than 31% of blocks");
  });

  it("reads anything else as higher than", () => {
    expect(rankText(row({ unit: "buildings_per_ha", cap_value: null }))).toBe("Higher than 31% of blocks");
  });

  it("makes no comparison with fewer than two values", () => {
    expect(rankText(row({ count: 1, count_below: 0 }))).toBe("");
  });

  it("never claims every block or none, and never doubles a comparative", () => {
    expect(rankText(row({ count_below: 19057 }))).toBe("Farther than nearly all blocks");
    expect(rankText(row({ count_below: 1, count: 300 }))).toBe("Farther than under 1% of blocks");
    const density = { unit: "buildings_per_ha", cap_value: null };
    expect(rankText(row({ ...density, count_below: 19057 }))).toBe("Higher than nearly all blocks");
    expect(rankText(row({ ...density, count_below: 1, count: 300 }))).toBe("Higher than under 1% of blocks");
    expect(rankText(row({ value: 1500, count_equal: 1, count: 300 }))).toBe("At the cap, with under 1% of blocks");
    expect(rankText(row({ value: 1500, count_equal: 299, count: 300 }))).toBe("At the cap, with nearly all blocks");
  });

  it("keeps exact none and all as plain percentages", () => {
    expect(rankText(row({ count_below: 0 }))).toBe("Farther than 0% of blocks");
    expect(rankText(row({ value: 1500, count_equal: 300, count: 300 }))).toBe("At the cap, with 100% of blocks");
  });

  it("uses the grain it is given", () => {
    expect(rankText(row(), "municipality")).toBe("Farther than 31% of municipalities");
  });
});

describe("renderInspector", () => {
  const rows = [
    row(),
    row({ slug: "urban_form_block_building_density", name: "Building density", unit: "buildings_per_ha", cap_value: null, value: 28.9 }),
  ];

  it("shows the id, the active value with its rank, and the other values", () => {
    const host = document.createElement("div");
    renderInspector(host, { id: "f-1", grain: "block" }, rows, rows[0].slug, () => undefined, () => undefined);
    expect(host.hidden).toBe(false);
    expect(host.textContent).toContain("f-1");
    expect(host.querySelector(".inspector__active")?.textContent).toContain("444 m");
    expect(host.querySelector(".inspector__active")?.textContent).toContain("Farther than 31% of blocks");
    const others = [...host.querySelectorAll<HTMLButtonElement>(".inspector__row")];
    expect(others.map((b) => b.textContent)).toEqual([expect.stringContaining("Building density")]);
  });

  it("switches the map when another value is clicked, and closes on the close button", () => {
    const host = document.createElement("div");
    const onPick = vi.fn();
    const onClose = vi.fn();
    renderInspector(host, { id: "f-1", grain: "block" }, rows, rows[0].slug, onPick, onClose);
    host.querySelector<HTMLButtonElement>(".inspector__row")?.click();
    expect(onPick).toHaveBeenCalledWith("urban_form_block_building_density");
    host.querySelector<HTMLButtonElement>(".inspector__close")?.click();
    expect(onClose).toHaveBeenCalled();
  });

  it("says so when the block has no value for the indicator on the map", () => {
    const host = document.createElement("div");
    renderInspector(host, { id: "f-1", grain: "block" }, rows, "other_slug", () => undefined, () => undefined);
    expect(host.querySelector(".inspector__active")?.textContent).toMatch(/no value/i);
    expect(host.querySelectorAll(".inspector__row")).toHaveLength(2);
  });

  it("shows a status line in place of the values while loading or after a failure", () => {
    const host = document.createElement("div");
    renderInspector(host, { id: "f-1", grain: "block" }, "Could not load this block's values.", null, () => undefined, () => undefined);
    expect(host.textContent).toContain("Could not load this block's values.");
    expect(host.querySelector(".inspector__row")).toBeNull();
  });
});

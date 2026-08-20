import { describe, expect, it } from "vitest";

import {
  applyFilters,
  boundsFraction,
  facetsOf,
  familyOf,
  filtersFromHash,
  filtersToHash,
  slugFromHash,
} from "./derive";
import type { IndicatorSummary } from "./api";

const item = (slug: string, applies_to: string[]): IndicatorSummary => ({
  slug,
  name: slug.replaceAll("_", " "),
  unit: null,
  applies_to,
  valid_min: null,
  valid_max: null,
});

const ITEMS = [
  item("urban_form_block_compactness", ["block", "ageb"]),
  item("urban_form_block_composite", ["block"]),
  item("accessibility_block_park_network_m", ["block"]),
  item("edificability_block_density", ["block"]),
];

describe("familyOf", () => {
  it("takes the leading slug segment", () => {
    expect(familyOf("accessibility_block_park_network_m")).toBe("accessibility");
  });

  it("falls back rather than returning empty", () => {
    expect(familyOf("")).toBe("other");
  });
});

describe("facetsOf", () => {
  it("counts families and grains across the whole set", () => {
    const f = facetsOf(ITEMS);
    expect(f.family).toEqual({ urban: 2, accessibility: 1, edificability: 1 });
    expect(f.grain).toEqual({ block: 4, ageb: 1 });
  });
});

describe("applyFilters", () => {
  it("matches substrings in slug or name", () => {
    expect(applyFilters(ITEMS, { q: "compact", families: [], grains: [] })).toHaveLength(1);
  });

  it("is any-of within a facet", () => {
    const out = applyFilters(ITEMS, {
      q: "",
      families: ["accessibility", "edificability"],
      grains: [],
    });
    expect(out).toHaveLength(2);
  });

  it("is and-of across facets", () => {
    const out = applyFilters(ITEMS, { q: "", families: ["urban"], grains: ["ageb"] });
    expect(out.map((i) => i.slug)).toEqual(["urban_form_block_compactness"]);
  });

  it("returns everything when nothing is selected", () => {
    expect(applyFilters(ITEMS, { q: "", families: [], grains: [] })).toHaveLength(4);
  });
});

describe("hash round trip", () => {
  it("survives a full set of filters", () => {
    const filters = { q: "park", families: ["accessibility"], grains: ["block"] };
    expect(filtersFromHash(filtersToHash(filters))).toEqual(filters);
  });

  it("collapses an empty filter set to a bare route", () => {
    expect(filtersToHash({ q: "", families: [], grains: [] })).toBe("#/");
  });

  it("reads a detail slug, and only from the detail route", () => {
    expect(slugFromHash("#/i/urban_form_block_composite")).toBe(
      "urban_form_block_composite",
    );
    expect(slugFromHash("#/?q=park")).toBeNull();
  });
});

describe("boundsFraction", () => {
  it("positions a value inside its declared range", () => {
    expect(boundsFraction(25, 0, 100)).toBeCloseTo(0.25);
  });

  it("clamps outside the range rather than overflowing the bar", () => {
    expect(boundsFraction(150, 0, 100)).toBe(1);
    expect(boundsFraction(-5, 0, 100)).toBe(0);
  });

  it("returns null when the catalog declares no usable bounds", () => {
    expect(boundsFraction(5, null, 100)).toBeNull();
    expect(boundsFraction(5, 10, 10)).toBeNull();
  });
});

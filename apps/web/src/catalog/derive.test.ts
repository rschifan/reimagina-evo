import { describe, expect, it } from "vitest";

import {
  applyFilters,
  boundsFraction,
  capNotes,
  EMPTY_FILTERS,
  facetsOf,
  familyOf,
  filtersFromHash,
  filtersToHash,
  slugFromHash,
  type CatalogRow,
  type Filters,
} from "./derive";

const item = (
  slug: string,
  applies_to: string[],
  extra: Partial<CatalogRow> = {},
): CatalogRow => ({
  slug,
  name: slug.replaceAll("_", " "),
  description: null,
  model_family: slug.split("_")[0],
  computation_kind: "direct",
  value_type: "numeric",
  aggregation_method: "weighted_mean",
  unit: null,
  applies_to,
  valid_min: null,
  valid_max: null,
  has_values: true,
  ...extra,
});

const ITEMS = [
  item("urban_form_block_compactness", ["block", "ageb"], { model_family: "urban_form" }),
  item("urban_form_block_composite", ["block"], {
    model_family: null,
    computation_kind: "composite",
    has_values: false,
  }),
  item("accessibility_block_park_network_m", ["block"], {
    description: "Metres to the nearest park along the walking network.",
  }),
  item("edificability_block_density", ["block"], { model_family: "urban_form" }),
];

const filters = (patch: Partial<Filters["facets"]> & { q?: string } = {}): Filters => {
  const { q = "", ...facets } = patch;
  return { q, facets: { ...EMPTY_FILTERS.facets, ...facets } };
};

describe("familyOf", () => {
  it("uses the catalog's family, not the slug prefix", () => {
    // `edificability_*` is filed under urban_form; a prefix guess split it off.
    expect(familyOf(ITEMS[3])).toBe("urban_form");
  });

  it("names a missing family instead of guessing one", () => {
    expect(familyOf(ITEMS[1])).toBe("unassigned");
  });
});

describe("facetsOf", () => {
  it("counts every facet across the whole set", () => {
    const f = facetsOf(ITEMS);
    expect(f.family).toEqual({ urban_form: 2, unassigned: 1, accessibility: 1 });
    expect(f.kind).toEqual({ direct: 3, composite: 1 });
    expect(f.grain).toEqual({ block: 4, ageb: 1 });
    expect(f.values).toEqual({ "has values": 3, "no values": 1 });
  });

  it("offers no values facet when nothing is scoped to an AOI", () => {
    const f = facetsOf(ITEMS.map((i) => ({ ...i, has_values: null })));
    expect(f.values).toEqual({});
  });
});

describe("applyFilters", () => {
  it("matches substrings in slug or name", () => {
    expect(applyFilters(ITEMS, filters({ q: "compact" }))).toHaveLength(1);
  });

  it("matches the description too", () => {
    const out = applyFilters(ITEMS, filters({ q: "walking network" }));
    expect(out.map((i) => i.slug)).toEqual(["accessibility_block_park_network_m"]);
  });

  it("is any-of within a facet", () => {
    expect(
      applyFilters(ITEMS, filters({ family: ["accessibility", "unassigned"] })),
    ).toHaveLength(2);
  });

  it("is and-of across facets", () => {
    const out = applyFilters(ITEMS, filters({ family: ["urban_form"], grain: ["ageb"] }));
    expect(out.map((i) => i.slug)).toEqual(["urban_form_block_compactness"]);
  });

  it("finds the indicators missing from the AOI", () => {
    const out = applyFilters(ITEMS, filters({ values: ["no values"] }));
    expect(out.map((i) => i.slug)).toEqual(["urban_form_block_composite"]);
  });

  it("returns everything when nothing is selected", () => {
    expect(applyFilters(ITEMS, EMPTY_FILTERS)).toHaveLength(4);
  });
});

describe("hash round trip", () => {
  it("survives a full set of filters", () => {
    const f = filters({
      q: "park",
      family: ["accessibility"],
      kind: ["direct"],
      grain: ["block"],
      values: ["no values"],
    });
    expect(filtersFromHash(filtersToHash(f))).toEqual(f);
  });

  it("still reads links made before kind and values existed", () => {
    expect(filtersFromHash("#/?family=accessibility&grain=block")).toEqual(
      filters({ family: ["accessibility"], grain: ["block"] }),
    );
  });

  it("collapses an empty filter set to a bare route", () => {
    expect(filtersToHash(EMPTY_FILTERS)).toBe("#/");
  });

  it("reads a detail slug, and only from the detail route", () => {
    expect(slugFromHash("#/i/urban_form_block_composite")).toBe(
      "urban_form_block_composite",
    );
    expect(slugFromHash("#/?q=park")).toBeNull();
  });
});

describe("capNotes", () => {
  const stats = (p05: number, p50: number, p95: number) => ({ p05, p50, p95 });

  it("warns when the median sits on the declared maximum", () => {
    // The culture-distance case: 1500 m is the cap and also "unreachable".
    expect(capNotes(stats(425, 1500, 1500), 0, 1500)).toEqual([
      "Half or more of the values sit at the declared maximum, 1500.",
    ]);
  });

  it("warns more softly when only the tail reaches a bound", () => {
    expect(capNotes(stats(0, 3, 9), 0, 10)).toEqual([
      "At least 5% of the values sit at the declared minimum, 0.",
    ]);
  });

  it("says nothing for a distribution inside its bounds", () => {
    expect(capNotes(stats(2, 5, 8), 0, 10)).toEqual([]);
  });

  it("says nothing without declared bounds", () => {
    expect(capNotes(stats(2, 5, 8), null, null)).toEqual([]);
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

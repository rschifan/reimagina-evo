/**
 * Pure derivations for the catalog index.
 *
 * Kept apart from the components on purpose: these are the only parts worth
 * unit-testing, and they run under the default `node` environment with no
 * DOM. Anything here must stay free of Svelte and of `document`.
 */
import type { IndicatorSummary, IndicatorStats } from "./api";

/** A catalog row plus whether it holds values in the active AOI. `null` when
 *  no AOI is active, so there is nothing to be missing from. */
export type CatalogRow = IndicatorSummary & { has_values: boolean | null };

export const FACETS = ["family", "kind", "grain", "values"] as const;
export type FacetKey = (typeof FACETS)[number];

/** The catalog's own family. Composites carry none, and saying so beats the
 *  slug-prefix guess this replaced, which filed `urban_form_*` as "urban" and
 *  split `edificability_*` away from the family it belongs to. */
export function familyOf(row: Pick<IndicatorSummary, "model_family">): string {
  return row.model_family ?? "unassigned";
}

function facetValues(row: CatalogRow, key: FacetKey): string[] {
  switch (key) {
    case "family":
      return [familyOf(row)];
    case "kind":
      return [row.computation_kind];
    case "grain":
      return row.applies_to;
    case "values":
      return row.has_values === null ? [] : [row.has_values ? "has values" : "no values"];
  }
}

export type Facets = Record<FacetKey, Record<string, number>>;

/** Counts for each facet value, computed over the UNFILTERED set so a count
 *  never collapses to zero just because the user narrowed by something else. */
export function facetsOf(rows: CatalogRow[]): Facets {
  const out = Object.fromEntries(FACETS.map((k) => [k, {}])) as Facets;
  for (const row of rows) {
    for (const key of FACETS) {
      for (const v of facetValues(row, key)) out[key][v] = (out[key][v] ?? 0) + 1;
    }
  }
  return out;
}

export interface Filters {
  q: string;
  facets: Record<FacetKey, string[]>;
}

export const EMPTY_FILTERS: Filters = {
  q: "",
  facets: { family: [], kind: [], grain: [], values: [] },
};

export function isFiltered(f: Filters): boolean {
  return f.q !== "" || FACETS.some((k) => f.facets[k].length > 0);
}

/** Substring match on slug, name or description, plus any-of within a facet
 *  and and-of across facets — the behaviour every faceted list has. */
export function applyFilters(rows: CatalogRow[], filters: Filters): CatalogRow[] {
  const q = filters.q.trim().toLowerCase();
  return rows.filter((row) => {
    if (q && !`${row.slug} ${row.name} ${row.description ?? ""}`.toLowerCase().includes(q)) {
      return false;
    }
    return FACETS.every((key) => {
      const picked = filters.facets[key];
      return !picked.length || facetValues(row, key).some((v) => picked.includes(v));
    });
  });
}

/** Serialise filters into a location hash so a narrowed view is linkable —
 *  most of a catalog's value is sending someone the exact thing you found. */
export function filtersToHash(filters: Filters): string {
  const p = new URLSearchParams();
  if (filters.q) p.set("q", filters.q);
  for (const key of FACETS) {
    if (filters.facets[key].length) p.set(key, filters.facets[key].join(","));
  }
  const qs = p.toString();
  return qs ? `#/?${qs}` : "#/";
}

export function filtersFromHash(hash: string): Filters {
  const qs = hash.includes("?") ? hash.slice(hash.indexOf("?") + 1) : "";
  const p = new URLSearchParams(qs);
  const list = (key: string) =>
    (p.get(key) ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  return {
    q: p.get("q") ?? "",
    facets: Object.fromEntries(FACETS.map((k) => [k, list(k)])) as Filters["facets"],
  };
}

/** `#/i/<slug>` → slug, anything else → null. */
export function slugFromHash(hash: string): string | null {
  const m = /^#\/i\/([^?]+)/.exec(hash);
  return m ? decodeURIComponent(m[1]) : null;
}

/**
 * Plain warnings when the observed values pile up on a declared bound.
 *
 * A bound is often a cap that also encodes "could not be measured" — the
 * distance indicators report 1500 m for an unreachable block — so a median on
 * the bound means the typical value is the cap, not a measurement. The band
 * on the detail page shows it; this says it.
 */
export function capNotes(
  stats: Pick<IndicatorStats, "p05" | "p50" | "p95">,
  min: number | null,
  max: number | null,
): string[] {
  const notes: string[] = [];
  const { p05, p50, p95 } = stats;
  if (max !== null) {
    if (p50 !== null && p50 >= max) {
      notes.push(`Half or more of the values sit at the declared maximum, ${max}.`);
    } else if (p95 !== null && p95 >= max) {
      notes.push(`At least 5% of the values sit at the declared maximum, ${max}.`);
    }
  }
  if (min !== null) {
    if (p50 !== null && p50 <= min) {
      notes.push(`Half or more of the values sit at the declared minimum, ${min}.`);
    } else if (p05 !== null && p05 <= min) {
      notes.push(`At least 5% of the values sit at the declared minimum, ${min}.`);
    }
  }
  return notes;
}

/** Where a value sits inside its declared safety bounds, 0–1, or null when
 *  the catalog declares no bounds. Drives the reference band on the detail
 *  page. */
export function boundsFraction(
  value: number | null,
  min: number | null,
  max: number | null,
): number | null {
  if (value === null || min === null || max === null || max <= min) return null;
  return Math.min(1, Math.max(0, (value - min) / (max - min)));
}

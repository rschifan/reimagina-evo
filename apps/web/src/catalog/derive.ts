/**
 * Pure derivations for the catalog index.
 *
 * Kept apart from the components on purpose: these are the only parts worth
 * unit-testing, and they run under the default `node` environment with no
 * DOM. Anything here must stay free of Svelte and of `document`.
 */
import type { IndicatorSummary } from "./api";

export interface Facets {
  family: Record<string, number>;
  grain: Record<string, number>;
}

/** The slug's leading segment — `accessibility_block_park_network_m` is the
 *  accessibility family. The API exposes `model_family` on the detail route
 *  but not on the list, and the prefix is how the shipped slugs are named. */
export function familyOf(slug: string): string {
  const head = slug.split("_")[0];
  return head || "other";
}

/** Counts for each facet value, computed over the UNFILTERED set so a count
 *  never collapses to zero just because the user narrowed by something else. */
export function facetsOf(items: IndicatorSummary[]): Facets {
  const family: Record<string, number> = {};
  const grain: Record<string, number> = {};
  for (const item of items) {
    const f = familyOf(item.slug);
    family[f] = (family[f] ?? 0) + 1;
    for (const g of item.applies_to) grain[g] = (grain[g] ?? 0) + 1;
  }
  return { family, grain };
}

export interface Filters {
  q: string;
  families: string[];
  grains: string[];
}

export const EMPTY_FILTERS: Filters = { q: "", families: [], grains: [] };

/** Substring match on slug or name, plus any-of semantics within a facet and
 *  and-of across facets — the behaviour every faceted list has, so it needs
 *  no explanation in the UI. */
export function applyFilters(
  items: IndicatorSummary[],
  filters: Filters,
): IndicatorSummary[] {
  const q = filters.q.trim().toLowerCase();
  return items.filter((item) => {
    if (q && !`${item.slug} ${item.name}`.toLowerCase().includes(q)) return false;
    if (filters.families.length && !filters.families.includes(familyOf(item.slug))) {
      return false;
    }
    if (
      filters.grains.length &&
      !item.applies_to.some((g) => filters.grains.includes(g))
    ) {
      return false;
    }
    return true;
  });
}

/** Serialise filters into a location hash so a narrowed view is linkable —
 *  most of a catalog's value is sending someone the exact thing you found. */
export function filtersToHash(filters: Filters): string {
  const p = new URLSearchParams();
  if (filters.q) p.set("q", filters.q);
  if (filters.families.length) p.set("family", filters.families.join(","));
  if (filters.grains.length) p.set("grain", filters.grains.join(","));
  const qs = p.toString();
  return qs ? `#/?${qs}` : "#/";
}

export function filtersFromHash(hash: string): Filters {
  const qs = hash.includes("?") ? hash.slice(hash.indexOf("?") + 1) : "";
  const p = new URLSearchParams(qs);
  const list = (key: string) =>
    (p.get(key) ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  return { q: p.get("q") ?? "", families: list("family"), grains: list("grain") };
}

/** `#/i/<slug>` → slug, anything else → null. */
export function slugFromHash(hash: string): string | null {
  const m = /^#\/i\/([^?]+)/.exec(hash);
  return m ? decodeURIComponent(m[1]) : null;
}

/** Where a value sits inside its declared safety bounds, 0–1, or null when
 *  the catalog declares no bounds. Drives the reference band on the detail
 *  page: a p95 sitting at 0.04 of the declared range is the visual tell that
 *  the bound is a safety cap, not a realistic maximum. */
export function boundsFraction(
  value: number | null,
  min: number | null,
  max: number | null,
): number | null {
  if (value === null || min === null || max === null || max <= min) return null;
  return Math.min(1, Math.max(0, (value - min) / (max - min)));
}

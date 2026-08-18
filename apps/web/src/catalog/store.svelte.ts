/**
 * Catalog state.
 *
 * A `.svelte.ts` module is the runes replacement for a store: module-scope
 * `$state` is shared by every importer, and `$derived` recomputes without a
 * subscription. No `svelte/store`, no manual unsubscribe.
 */
import { isAbortError } from "../http";
import {
  getStats,
  listAois,
  listIndicators,
  type AOIRow,
  type IndicatorStats,
  type IndicatorSummary,
} from "./api";
import {
  applyFilters,
  facetsOf,
  EMPTY_FILTERS,
  type Filters,
} from "./derive";

/** How often to re-read the catalog while the tab is visible. Freshness is
 *  what an operator watches here, so this is tighter than the 30 s the tile
 *  and AOI caches use. Hidden tabs poll not at all. */
const POLL_MS = 5_000;
/** Backoff ceiling after repeated failed polls — so an API restart doesn't
 *  turn every open tab into a retry storm. Reset to POLL_MS on success. */
const BACKOFF_MAX_MS = 60_000;

// `$state.raw` because these arrays are replaced wholesale on each fetch;
// deep proxying every row would cost more than it buys.
let indicators = $state.raw<IndicatorSummary[]>([]);
let aois = $state.raw<AOIRow[]>([]);
let activeAoiId = $state<string | null>(null);
let filters = $state<Filters>({ ...EMPTY_FILTERS });
let loading = $state(false);
let refreshing = $state(false);
let error = $state<string | null>(null);
let lastLoadedAt = $state<Date | null>(null);

export const catalog = {
  get indicators() {
    return indicators;
  },
  get aois() {
    return aois;
  },
  get activeAoiId() {
    return activeAoiId;
  },
  get filters() {
    return filters;
  },
  /** True only on the first load, when there is nothing to show yet. A
   *  background refresh sets `refreshing` instead, so the table never
   *  blanks out under a reader mid-scan. */
  get loading() {
    return loading;
  },
  get refreshing() {
    return refreshing;
  },
  get error() {
    return error;
  },
  get lastLoadedAt() {
    return lastLoadedAt;
  },
  get facets() {
    return facetsOf(indicators);
  },
  get visible() {
    return applyFilters(indicators, filters);
  },
  get total() {
    return indicators.length;
  },

  setFilters(next: Filters) {
    filters = next;
  },

  async setAoi(id: string | null) {
    activeAoiId = id;
    await load({ background: false });
  },

  bySlug(slug: string): IndicatorSummary | null {
    return indicators.find((i) => i.slug === slug) ?? null;
  },
};

let inFlight: AbortController | null = null;

async function load({ background }: { background: boolean }): Promise<void> {
  inFlight?.abort();
  const ctrl = new AbortController();
  inFlight = ctrl;
  if (background) refreshing = true;
  else loading = true;
  try {
    const next = await listIndicators(activeAoiId, { signal: ctrl.signal });
    if (inFlight !== ctrl) return;
    indicators = next;
    lastLoadedAt = new Date();
    error = null;
  } catch (err) {
    if (isAbortError(err)) return;
    // Keep whatever is on screen: a failed refresh should degrade to "this
    // is what we last saw", not to an empty page.
    error = err instanceof Error ? err.message : String(err);
  } finally {
    if (inFlight === ctrl) {
      inFlight = null;
      loading = false;
      refreshing = false;
    }
  }
}

/** Boot: AOIs first, because the indicator list is AOI-scoped and asking
 *  before one is resolved would fetch a list we would immediately discard. */
export async function boot(): Promise<void> {
  loading = true;
  try {
    const rows = await listAois();
    aois = rows;
    activeAoiId = rows[0]?.aoi_id ?? null;
  } catch {
    // An AOI list we cannot read is not fatal — the unscoped catalog is
    // still worth showing.
    aois = [];
    activeAoiId = null;
  }
  await load({ background: false });
}

/**
 * Start the visibility-gated poll loop. Returns a teardown.
 *
 * Polling rather than SSE is deliberate: the existing `/jobs/{run_id}/events`
 * stream is scoped to one run, pins a daemon thread per subscriber and caps
 * at five minutes, so a dashboard left open all day is the wrong consumer for
 * it. A 5 s GET is cheaper than the connection it would replace.
 */
export function startPolling(): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let delay = POLL_MS;
  let stopped = false;

  const tick = async () => {
    if (stopped || document.hidden) return schedule();
    const before = error;
    await load({ background: true });
    delay = error && error === before ? Math.min(delay * 2, BACKOFF_MAX_MS) : POLL_MS;
    schedule();
  };

  const schedule = () => {
    if (stopped) return;
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => void tick(), delay);
  };

  const onVisibility = () => {
    if (document.hidden) return;
    delay = POLL_MS;
    void tick();
  };

  document.addEventListener("visibilitychange", onVisibility);
  schedule();

  return () => {
    stopped = true;
    if (timer !== null) clearTimeout(timer);
    document.removeEventListener("visibilitychange", onVisibility);
    inFlight?.abort();
  };
}

/** Per-indicator stats, fetched on demand by the detail view and cached for
 *  the life of the page — the numbers move only when a recompute runs, and
 *  the poll loop above already reloads the list when that happens. */
const statsCache = new Map<string, Promise<IndicatorStats>>();

export function statsFor(slug: string, grain: string | undefined): Promise<IndicatorStats> {
  const key = `${slug}::${grain ?? ""}::${activeAoiId ?? ""}`;
  const hit = statsCache.get(key);
  if (hit) return hit;
  const p = getStats(slug, { feature_type: grain, aoi_id: activeAoiId });
  statsCache.set(key, p);
  return p;
}

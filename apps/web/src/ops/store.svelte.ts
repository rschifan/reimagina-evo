/**
 * Ops page state.
 *
 * Module-scope runes rather than `svelte/store`, matching the catalog page.
 * This store owns the run list, the executors, the filters and the single poll
 * loop; the launch form's fields stay local to its component, because a draft
 * that survived a drill-out would reappear stale.
 */
import { isAbortError } from "../http";
import {
  cancelRun,
  getRunDetail,
  listAois,
  listRuns,
  rerunRun,
  submitRun,
  RUN_STATUSES,
  type AOIRow,
  type SubmitRunBody,
  type ExecutorRow,
  type RunDetailResponse,
  type RunRow,
  type RunStatus,
} from "./api";
import { clockOffset, isRunTerminal, statusCounts } from "./derive";

/**
 * One second, against the catalog's five.
 *
 * Freshness *is* the product here — an operator watches a run advance, and
 * units routinely finish in about a second, so a slower tick makes a real
 * transition invisible. The cost is one query per tick per open tab, which is
 * why the list, the counts and the executors travel in a single response
 * rather than three, and why a hidden tab polls not at all.
 */
const POLL_MS = 1_000;
/** Backoff ceiling, so an API restart does not turn every open tab into a
 *  retry storm. Reset to POLL_MS on the first success. */
const BACKOFF_MAX_MS = 60_000;
/** Beyond this without a successful refresh, the numbers on screen are a lie
 *  told by a truthful "updated 14:02:11" stamp. Say so instead. */
const STALE_AFTER_MS = 60_000;

let runs = $state.raw<RunRow[]>([]);
let executors = $state.raw<ExecutorRow[]>([]);
let counts = $state.raw<Record<string, number>>({});
let total = $state(0);
let limit = $state(50);
let filters = $state.raw<RunStatus[]>([]);
let openRun = $state.raw<RunDetailResponse | null>(null);
let openRunId = $state<string | null>(null);
let loading = $state(false);
let refreshing = $state(false);
let paused = $state(false);
let error = $state<string | null>(null);
let lastLoadedAt = $state<Date | null>(null);
let offsetMs = $state(0);
/** Actions in flight, keyed `{run_id}:{action}`. None of them is idempotent —
 *  rerun and launch each mint a new run — and there is no server-side dedupe,
 *  so disabling the button on this set is the only protection there is. */
let busy = $state.raw<Set<string>>(new Set());

// One controller per resource. The list and the detail are independent reads
// and must not abort each other — sharing a single controller meant a detail
// fetch cancelled the list fetch and vice versa, which at a two-second cadence
// can starve both.
let listInFlight: AbortController | null = null;
let detailInFlight: AbortController | null = null;

export const ops = {
  get runs() {
    return runs;
  },
  get executors() {
    return executors;
  },
  get counts() {
    return statusCounts(counts, RUN_STATUSES);
  },
  get total() {
    return total;
  },
  get limit() {
    return limit;
  },
  get filters() {
    return filters;
  },
  get openRun() {
    return openRun;
  },
  /** First load only. A background refresh sets `refreshing`, so the table
   *  never blanks out under someone mid-scan. */
  get loading() {
    return loading;
  },
  get refreshing() {
    return refreshing;
  },
  get paused() {
    return paused;
  },
  get error() {
    return error;
  },
  get lastLoadedAt() {
    return lastLoadedAt;
  },
  /** Client epoch corrected onto the server's clock. Every duration and the
   *  stalled badge measure against this, never `Date.now()` raw. */
  get offsetMs() {
    return offsetMs;
  },
  get stale() {
    if (!lastLoadedAt) return false;
    return Date.now() - lastLoadedAt.getTime() > STALE_AFTER_MS;
  },
  isBusy(key: string) {
    return busy.has(key);
  },
};

/**
 * Idempotent on purpose. These setters are driven from `$effect`s watching the
 * URL hash, and an unconditional write on every effect run is a self-feeding
 * update cycle — Svelte stops it with `effect_update_depth_exceeded`, which
 * surfaces as a page frozen on its loading skeleton.
 */
export function setFilters(next: RunStatus[]): void {
  if (next.length === filters.length && next.every((s, i) => s === filters[i])) {
    return;
  }
  filters = next;
  void load({ background: true });
}

export function loadMore(): void {
  // Raise the limit rather than advancing an offset: under a two-second poll a
  // new run shifts every offset window, so paging would duplicate and drop rows
  // as you read.
  limit += 50;
  void load({ background: true });
}

export function setOpenRun(runId: string | null): void {
  if (runId === openRunId) return; // see setFilters
  openRunId = runId;
  if (runId === null) openRun = null;
  void load({ background: true });
}

export function setPaused(value: boolean): void {
  paused = value;
}

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** The run list, the chip counts and executor health — one request. */
async function loadList({ background }: { background: boolean }): Promise<void> {
  listInFlight?.abort();
  const ctrl = new AbortController();
  listInFlight = ctrl;
  if (background) refreshing = true;
  else loading = true;
  try {
    const page = await listRuns({ statuses: filters, limit }, { signal: ctrl.signal });
    if (listInFlight !== ctrl) return; // a newer request already won
    runs = page.runs;
    executors = page.executors;
    counts = page.status_counts;
    total = page.total;
    offsetMs = clockOffset(page.server_now, Date.now());
    error = null;
    lastLoadedAt = new Date();
  } catch (err) {
    if (isAbortError(err)) return;
    // Keep the last known rows and say the refresh failed. Blanking the table
    // is strictly worse than showing numbers with a warning.
    error = describe(err);
  } finally {
    if (listInFlight === ctrl) {
      refreshing = false;
      loading = false;
    }
  }
}

/** One run's units and timings. */
async function loadDetail({ background }: { background: boolean }): Promise<void> {
  const runId = openRunId;
  if (!runId) return;
  detailInFlight?.abort();
  const ctrl = new AbortController();
  detailInFlight = ctrl;
  if (background) refreshing = true;
  else loading = true;
  try {
    const detail = await getRunDetail(runId, { signal: ctrl.signal });
    if (detailInFlight !== ctrl || openRunId !== runId) return;
    openRun = detail;
    offsetMs = clockOffset(detail.server_now, Date.now());
    error = null;
    lastLoadedAt = new Date();
  } catch (err) {
    if (isAbortError(err)) return;
    error = describe(err);
  } finally {
    if (detailInFlight === ctrl) {
      refreshing = false;
      loading = false;
    }
  }
}

/** What a poll tick reads: whichever view is open. */
async function load({ background }: { background: boolean }): Promise<void> {
  await (openRunId ? loadDetail({ background }) : loadList({ background }));
}

export async function boot(): Promise<void> {
  // The list unconditionally, even when the page opened straight into a run.
  //
  // Executor health and the in-flight tallies ride on the list response, and
  // the health strip is chrome on every route. Booting into a detail URL
  // without this left the strip claiming "no executor has ever reported" while
  // a worker was heartbeating fine — the precise false alarm the strip exists
  // to prevent, shown at the moment someone is deep-linked into a stuck run.
  await Promise.all([loadList({ background: false }), loadDetail({ background: false })]);
}

/**
 * Start the visibility-gated poll loop.
 *
 * Returns `{ stop, refreshNow }` rather than a bare teardown: after a mutation
 * the page must re-read immediately, and doing that by calling `load` directly
 * would leave the armed timer to fire microseconds later and leave the backoff
 * where it was.
 *
 * Polling rather than SSE for the same reason the catalog gives: the existing
 * `/jobs/{run_id}/events` stream is scoped to one run, pins a daemon thread per
 * subscriber and caps at five minutes — the wrong consumer for a dashboard left
 * open all day.
 */
export function startPolling(): { stop: () => void; refreshNow: () => void } {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let delay = POLL_MS;
  let fails = 0;
  let stopped = false;

  const clear = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };

  const schedule = () => {
    if (stopped) return;
    clear();
    timer = setTimeout(() => void tick(), delay);
  };

  const tick = async () => {
    if (stopped || document.hidden || paused) return schedule();
    // Nothing about a finished run can change; stop re-reading it. The list
    // keeps polling underneath.
    if (openRunId && openRun && isRunTerminal(openRun.run.status))
      return schedule();
    await load({ background: true });
    // Count consecutive failures rather than comparing messages. `ApiError`
    // embeds the status line, so a backend flapping 502 → 504 produces a
    // different string each tick and a message compare never backs off at all.
    fails = error ? fails + 1 : 0;
    delay =
      fails > 1
        ? Math.min(POLL_MS * 2 ** (fails - 1), BACKOFF_MAX_MS)
        : POLL_MS;
    schedule();
  };

  const refreshNow = () => {
    // Deliberately not `tick()`: that returns early when the tab is hidden,
    // and someone pressing Refresh — or an action that just mutated a run — is
    // by definition present. Load directly, then resume the normal cadence.
    clear();
    delay = POLL_MS;
    fails = 0;
    void load({ background: true }).then(schedule);
  };

  const onVisibility = () => {
    if (document.hidden) return;
    refreshNow();
  };

  document.addEventListener("visibilitychange", onVisibility);
  schedule();

  return {
    stop: () => {
      stopped = true;
      clear();
      document.removeEventListener("visibilitychange", onVisibility);
      listInFlight?.abort();
      detailInFlight?.abort();
    },
    refreshNow,
  };
}

let aois = $state.raw<AOIRow[]>([]);

export function aoiOptions(): AOIRow[] {
  return aois;
}

export async function loadAois(): Promise<void> {
  try {
    aois = await listAois();
  } catch {
    // A missing AOI list is not fatal — a run can be submitted unscoped, and
    // the form says so. Failing the whole panel over the picker would be worse.
    aois = [];
  }
}

/**
 * Submit a run and return where it went.
 *
 * `noop` is a success, not a failure: the dirty set was empty, so nothing was
 * persisted and `run_id` is deliberately null. Navigating to it would 404.
 */
export async function launchRun(
  body: SubmitRunBody,
  refreshNow: () => void,
): Promise<{ status: string; runId: string | null }> {
  const release = beginAction("launch");
  try {
    const res = await submitRun(body);
    refreshNow();
    return { status: res.status, runId: res.run_id };
  } finally {
    release();
  }
}

/**
 * Cancel a run. Returns the error message, or null on success.
 *
 * Errors are returned rather than thrown so the caller can render the API's own
 * `detail` — "already succeeded" is a useful sentence, and a bare status code
 * is not.
 */
export async function cancel(runId: string, refreshNow: () => void) {
  return await _action(`${runId}:cancel`, () => cancelRun(runId), refreshNow);
}

/** Rerun a finished run's non-succeeded indicators as a new run. */
export async function rerun(
  runId: string,
  refreshNow: () => void,
): Promise<{ error: string | null; newRunId?: string }> {
  const release = beginAction(`${runId}:rerun`);
  try {
    const res = await rerunRun(runId);
    refreshNow();
    return { error: null, newRunId: res.run_id ?? undefined };
  } catch (err) {
    return { error: await describeApiError(err) };
  } finally {
    release();
  }
}

async function _action(
  key: string,
  call: () => Promise<unknown>,
  refreshNow: () => void,
): Promise<string | null> {
  const release = beginAction(key);
  try {
    await call();
    refreshNow();
    return null;
  } catch (err) {
    return await describeApiError(err);
  } finally {
    release();
  }
}

/** Prefer FastAPI's `detail` over the status line `ApiError` carries. */
async function describeApiError(err: unknown): Promise<string> {
  const response = (err as { response?: Response })?.response;
  if (response) {
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") return body.detail;
    } catch {
      // Fall through to the message below.
    }
  }
  return err instanceof Error ? err.message : String(err);
}

/** Mark an action in flight; returns a release for a `finally`. */
export function beginAction(key: string): () => void {
  busy = new Set(busy).add(key);
  return () => {
    const next = new Set(busy);
    next.delete(key);
    busy = next;
  };
}

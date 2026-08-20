/**
 * Pure derivations for the Ops page.
 *
 * No Svelte, no `document`, no clock: every function that needs "now" takes it
 * as an argument. That is what makes the whole file testable under Vitest's
 * default node environment, and it is also what forces the clock question into
 * the open — see `serverNow`.
 */
import type { Tone } from "../lib/components/ui/tone";
import type { RunRow, RunStatus, UnitStatus } from "./api";

/**
 * Statuses to tones, exhaustively.
 *
 * `Record<RunStatus, Tone>` is the point: the compiler rejects a missing case,
 * so the day the backend adds a status `check:ops` fails rather than the page
 * silently rendering it grey. Stronger than a runtime totality test, which is
 * why there isn't one.
 */
export const runTone: Record<RunStatus, Tone> = {
  queued: "neutral",
  running: "accent",
  succeeded: "fresh",
  partial: "partial",
  failed: "error",
  cancelling: "stale",
  cancelled: "stale",
};

export const unitTone: Record<UnitStatus, Tone> = {
  queued: "neutral",
  running: "accent",
  succeeded: "fresh",
  failed: "error",
  skipped: "neutral",
  cancelled: "stale",
};

/** Run statuses from which nothing further happens. */
const RUN_TERMINAL: RunStatus[] = [
  "succeeded",
  "partial",
  "failed",
  "cancelled",
];

export function isRunTerminal(status: RunStatus): boolean {
  return RUN_TERMINAL.includes(status);
}

/**
 * The server's clock, as a millisecond epoch.
 *
 * Every elapsed time on this page is a server timestamp minus "now", and the
 * browser's clock is not the server's. Taking the offset from the response that
 * delivered the rows keeps a skewed client from inventing negative durations or
 * suppressing the stalled badge outright.
 */
export function serverNow(clientNow: number, offsetMs: number): number {
  return clientNow + offsetMs;
}

export function clockOffset(serverIso: string, clientNow: number): number {
  const server = Date.parse(serverIso);
  return Number.isNaN(server) ? 0 : server - clientNow;
}

export interface Progress {
  done: number;
  failed: number;
  total: number;
  pct: number;
}

export function progressOf(run: RunRow): Progress {
  const total = run.units_total;
  const done = run.units_terminal;
  // A run whose plan was empty has no units; 0/0 is 0 % here, never NaN.
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  return { done, failed: run.units_failed, total, pct };
}

/**
 * Elapsed milliseconds, or null when there is nothing honest to show.
 *
 * Clamped at zero: a clock disagreement must render as `—` rather than as a
 * negative duration, which reads as a bug in the run rather than in the clock.
 */
export function durationMs(
  startIso: string | null,
  endIso: string | null,
  now: number,
): number | null {
  if (!startIso) return null;
  const start = Date.parse(startIso);
  if (Number.isNaN(start)) return null;
  const end = endIso ? Date.parse(endIso) : now;
  if (Number.isNaN(end)) return null;
  return Math.max(0, end - start);
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return "—";
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(Math.floor(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** Grace before a run that looks stalled is called stalled. See `isStalled`. */
export const STALL_GRACE_MS = 15_000;

/**
 * A run whose units all finished but which never closed.
 *
 * That is the signature of a workflow that died past its last step: `s_finalize`
 * and `s_release_plan` sit on `run_workflow`'s normal return path only, so any
 * exceptional exit leaves the envelope `running` forever.
 *
 * The grace period is not optional. `job_unit` reaches all-terminal at the
 * child's own write, and the parent still has to notice, settle and finalise —
 * a window a two-second poll lands in often enough that without the grace the
 * badge would flash on roughly one healthy run in ten, at its final tick.
 *
 * Not "no unit is running": `s_open_run` writes the envelope and inserts every
 * unit as `queued` in one step, and the units queue holds eight per worker, so a
 * healthy run waiting for a slot looks identical.
 */
export function isStalled(run: RunRow, now: number): boolean {
  if (run.status !== "running" || run.completed_at) return false;
  if (run.units_total === 0 || run.units_terminal !== run.units_total)
    return false;
  if (!run.units_last_terminal_at) return false;
  const last = Date.parse(run.units_last_terminal_at);
  return !Number.isNaN(last) && now - last > STALL_GRACE_MS;
}

/** Chip counts, zero-filled — `GROUP BY` omits statuses with no rows. */
export function statusCounts(
  raw: Record<string, number>,
  statuses: RunStatus[],
): Record<RunStatus, number> {
  const out = {} as Record<RunStatus, number>;
  for (const s of statuses) out[s] = raw[s] ?? 0;
  return out;
}

// ── Hash routing ────────────────────────────────────────────────────────────
//
// `#/r/<run_id>` for a run, `#/?status=failed,partial` for a filtered index.
// The `r/` prefix is deliberate: a bare `#/<id>` cannot be told apart from a
// filtered index route, which is the collision the catalog's `#/i/` avoids.

export function runIdFromHash(hash: string): string | null {
  const m = /^#\/r\/([^?]+)/.exec(hash);
  return m ? decodeURIComponent(m[1]) : null;
}

export function statusesFromHash(
  hash: string,
  known: RunStatus[],
): RunStatus[] {
  const q = hash.indexOf("?");
  if (q === -1) return [];
  const raw = new URLSearchParams(hash.slice(q + 1)).get("status");
  if (!raw) return [];
  const wanted = new Set(raw.split(","));
  return known.filter((s) => wanted.has(s));
}

export function statusesToHash(statuses: RunStatus[]): string {
  return statuses.length ? `#/?status=${statuses.join(",")}` : "#/";
}

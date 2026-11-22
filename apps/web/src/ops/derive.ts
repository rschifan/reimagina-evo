/**
 * Pure derivations for the Ops page.
 *
 * No Svelte, no `document`, no clock: every function that needs "now" takes it
 * as an argument. That is what makes the whole file testable under Vitest's
 * default node environment, and it is also what forces the clock question into
 * the open — see `serverNow`.
 */
import { decodeOrNull } from "../lib/utils";
import type { Tone } from "../lib/components/ui/tone";
import type { RunRow, RunStatus } from "./api";

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

/** The server stamped its clock somewhere inside the request; the midpoint of
 *  send and receive is the best guess. Arrival time alone counted the whole
 *  round trip as skew, so every slow response nudged "now" forward. */
export function clockOffset(serverIso: string, sentAt: number, receivedAt: number): number {
  const server = Date.parse(serverIso);
  return Number.isNaN(server) ? 0 : server - (sentAt + receivedAt) / 2;
}

/** The displayed clock never steps back by less than `toleranceMs`: offsets
 *  re-estimated from each response jitter, and a bar that shrinks on a tick
 *  reads as the run going backward. A larger correction is real and taken. */
export function monotonicNow(prev: number, next: number, toleranceMs = 1_000): number {
  return next < prev && prev - next < toleranceMs ? prev : next;
}

export interface Progress {
  done: number;
  failed: number;
  total: number;
  pct: number;
  /** Percentage widths that sum to <= 100, in render order. */
  segments: ProgressSegments;
  /** One line describing the whole bar, for assistive technology. */
  label: string;
}

/**
 * The bar's four parts, as percentages of ``total``.
 *
 * ⚠ This exists because a single-value bar cannot tell the truth about a
 * run. The component rendered ``<progress value={done} max={total}>`` where
 * ``done`` was ``units_terminal`` — *finished*, not *succeeded* — so a run
 * whose only unit failed drew a **full green bar** beside a red `failed`
 * badge. Observed on 25 of 27 runs in the dashboard, every one of them a
 * failure.
 *
 * The bar is the largest, most saturated element in the row, and green means
 * success in every convention a dashboard inherits. Where a glance and a read
 * disagree, the glance wins.
 */
export interface ProgressSegments {
  succeeded: number;
  failed: number;
  running: number;
  /** Neither finished nor started: the part still to come. */
  pending: number;
}

export function progressOf(run: RunRow): Progress {
  const total = run.units_total;
  const done = run.units_terminal;
  // A run whose plan was empty has no units; 0/0 is 0 % here, never NaN.
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  // Clamped and ordered so the four segments can never sum past 100 %, even
  // if the server's counters disagree with each other mid-transition — a
  // unit can be observed as both terminal and running across two queries.
  // A bar that overflows its track is a worse failure than one that is
  // briefly a percent short.
  const share = (n: number): number =>
    total > 0 ? Math.max(0, Math.min(100, (n / total) * 100)) : 0;
  const succeeded = share(run.units_succeeded);
  const failed = share(run.units_failed);
  const running = Math.min(share(run.units_running), 100 - succeeded - failed);
  // Zero units means zero of everything, not "100 % pending". The bar is
  // not rendered in that case, but a `Progress` that claims a full track of
  // pending work for a run that planned none would be wrong wherever it is
  // read next.
  const pending =
    total > 0 ? Math.max(0, 100 - succeeded - failed - running) : 0;

  return {
    done,
    failed: run.units_failed,
    total,
    pct,
    segments: { succeeded, failed, running: Math.max(0, running), pending },
    label: progressLabel(run),
  };
}

/**
 * The bar in one sentence, for a screen reader and for a tooltip.
 *
 * Spelled out rather than "60 %": a percentage of *what* is exactly the
 * ambiguity that made the old bar wrong.
 */
export function progressLabel(run: RunRow): string {
  if (run.units_total === 0) return "no units";
  const parts: string[] = [];
  if (run.units_succeeded) parts.push(`${run.units_succeeded} succeeded`);
  if (run.units_failed) parts.push(`${run.units_failed} failed`);
  if (run.units_running) parts.push(`${run.units_running} running`);
  const accounted =
    run.units_succeeded + run.units_failed + run.units_running;
  const pending = Math.max(0, run.units_total - accounted);
  if (pending) parts.push(`${pending} pending`);
  return `${run.units_total} unit${run.units_total === 1 ? "" : "s"}: ${
    parts.length ? parts.join(", ") : "none started"
  }`;
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
/** Cancel is offered only while the run can still change. */
export function canCancel(run: { status: RunStatus }): boolean {
  return !isRunTerminal(run.status);
}

/**
 * Rerun is offered on a finished run that still has work left undone.
 *
 * Not on a live one: it would race the run's own remaining units for the same
 * indicators. Not on a wholly successful one: there is nothing to redo.
 */
export function canRerun(run: {
  status: RunStatus;
  units_total: number;
  units_succeeded: number;
}): boolean {
  return (
    isRunTerminal(run.status) &&
    run.units_total > 0 &&
    run.units_succeeded < run.units_total
  );
}

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
  return m ? decodeOrNull(m[1]) : null;
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

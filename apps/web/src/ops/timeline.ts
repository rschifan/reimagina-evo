/**
 * The run page's execution timeline, as data. `RunTimeline.svelte` draws this
 * and decides nothing; every rule the picture follows is here, under test.
 *
 * Two invariants the design rests on (docs/superpowers/specs/
 * 2026-11-18-run-execution-view-design.md):
 * - Rows never move: order is dependency depth, then slug — never start time.
 * - Nothing drawn moves left: bars grow left to right on an axis that starts
 *   at 10 s and doubles each time the run reaches its edge, so everything
 *   shrinks at once and the run continues. Nothing is predicted.
 */
import type { Tone } from "../lib/components/ui/tone";
import type { RunDetailResponse, UnitDetail } from "./api";
import { formatDuration } from "./derive";

export type RowState =
  | "succeeded"
  | "failed"
  | "cancelled"
  | "skipped"
  | "running"
  | "ready"
  | "blocked";

/** The one place a state gets a colour. Bars, dots and the legend read it. */
export const UNIT_VISUAL: Record<
  RowState,
  { label: string; bar: string | null; dot: string }
> = {
  running: { label: "running", bar: "bg-state-partial", dot: "bg-state-partial" },
  succeeded: { label: "done", bar: "bg-state-fresh", dot: "bg-state-fresh" },
  failed: { label: "failed", bar: "bg-state-error", dot: "bg-state-error" },
  cancelled: { label: "cancelled", bar: "bg-line", dot: "bg-line" },
  skipped: { label: "skipped", bar: null, dot: "border-2 border-line" },
  ready: {
    label: "next, waits for a free slot",
    bar: null,
    dot: "border-2 border-state-stale",
  },
  blocked: {
    label: "waits on prerequisites",
    bar: null,
    dot: "border-2 border-dashed border-muted-foreground",
  },
};

const ENDED = new Set(["failed", "cancelled", "skipped"]);
const t = (iso: string | null) => (iso ? Date.parse(iso) : null);

function prerequisites(unit: UnitDetail, by: Map<string, UnitDetail>): UnitDetail[] {
  return unit.depends_on.flatMap((d) => by.get(d) ?? []);
}

/** The first prerequisite, at any depth, that ended without succeeding. */
function deadAncestor(
  unit: UnitDetail,
  by: Map<string, UnitDetail>,
  seen = new Set<string>(),
): UnitDetail | null {
  for (const p of prerequisites(unit, by)) {
    if (seen.has(p.indicator)) continue;
    seen.add(p.indicator);
    if (ENDED.has(p.status)) return p;
    const dead = deadAncestor(p, by, seen);
    if (dead) return dead;
  }
  return null;
}

export function rowState(unit: UnitDetail, by: Map<string, UnitDetail>): RowState {
  if (unit.status !== "queued") return unit.status;
  return prerequisites(unit, by).every((p) => p.status === "succeeded")
    ? "ready"
    : "blocked";
}

export function rowOrder(units: UnitDetail[]): UnitDetail[] {
  const by = new Map(units.map((u) => [u.indicator, u]));
  const depth = new Map<string, number>();
  const depthOf = (u: UnitDetail, seen: Set<string>): number => {
    const known = depth.get(u.indicator);
    if (known !== undefined) return known;
    if (seen.has(u.indicator)) return 0; // a cycle; the registry forbids one
    seen.add(u.indicator);
    const d = Math.max(-1, ...prerequisites(u, by).map((p) => depthOf(p, seen))) + 1;
    depth.set(u.indicator, d);
    return d;
  };
  units.forEach((u) => depthOf(u, new Set()));
  return [...units].sort(
    (a, b) =>
      depth.get(a.indicator)! - depth.get(b.indicator)! ||
      a.indicator.localeCompare(b.indicator),
  );
}

/** Live: 10 s, doubled whenever the run reaches 95 % of it. Finished: the run. */
export function axisMs(elapsed: number, live: boolean): number {
  if (!live) return Math.max(elapsed, 1);
  let axis = 10_000;
  while (elapsed > 0.95 * axis) axis *= 2;
  return axis;
}

const TICK_STEPS_S = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600];

/** Sub-second steps get hundredths (0.25s), whole seconds otherwise. */
function tickLabel(ms: number, step: number): string {
  if (step < 1000) return `${+(ms / 1000).toFixed(2)}s`;
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

/** At most six intervals; the axis end is always the last tick, at 100 %. */
export function ticks(axis: number): { pct: number; label: string }[] {
  const step =
    (TICK_STEPS_S.find((s) => axis / (s * 1000) <= 6) ?? TICK_STEPS_S.at(-1)!) * 1000;
  const out: { pct: number; label: string }[] = [];
  for (let v = 0; v < axis - step / 2; v += step) {
    out.push({ pct: (v / axis) * 100, label: tickLabel(v, step) });
  }
  out.push({ pct: 100, label: tickLabel(axis, step) });
  return out;
}

export interface Row {
  unit: UnitDetail;
  state: RowState;
  leftPct: number;
  /** 0 when the unit has no bar. */
  widthPct: number;
  numbers: string;
  note: string | null;
}

export interface TimelineModel {
  rows: Row[];
  axis: number;
  elapsedMs: number;
  /** Null once the run has finished. */
  nowPct: number | null;
  ticks: { pct: number; label: string }[];
  /** Running-unit count at each start/finish, for the parallelism strip. */
  strip: { pct: number; running: number }[];
  /** The strip's full height: the per-worker slots, or the peak if higher —
   *  several workers can run more of one run's units at once. */
  stripMax: number;
  chips: { text: string; tone: Tone }[];
}

function note(
  unit: UnitDetail,
  state: RowState,
  by: Map<string, UnitDetail>,
  live: boolean,
): string | null {
  // A finished run's queued units will never run: not "next", not "after".
  if (!live && unit.status === "queued") return "never started";
  if (state === "cancelled" && !unit.started_at) return "cancelled before it started";
  if (state === "ready") return "next · waits for a free slot";
  if (state === "skipped") return unit.detail ? `skipped · ${unit.detail}` : "skipped";
  if (state !== "blocked") return null;
  const dead = deadAncestor(unit, by);
  if (dead) return `won't start: ${dead.indicator} ${dead.status}`;
  const pre = prerequisites(unit, by);
  return `after ${pre
    .filter((p) => p.status !== "succeeded")
    .map((p) => p.indicator)
    .join(", ")}`;
}

export function timelineModel(
  units: UnitDetail[],
  slots: number,
  now: number,
  live: boolean,
): TimelineModel {
  const by = new Map(units.map((u) => [u.indicator, u]));
  const t0 = Math.min(...units.map((u) => t(u.queued_at)!));
  const lastEnd = Math.max(t0, ...units.map((u) => t(u.completed_at) ?? t0));
  // A live run whose units have all ended is stalled (the run row never
  // closed): its time stops at the last finish instead of growing the axis.
  const settled = units.every((u) => ENDED.has(u.status) || u.status === "succeeded");
  const elapsedMs = units.length ? Math.max(0, (live && !settled ? now : lastEnd) - t0) : 0;
  const axis = axisMs(elapsedMs, live);
  const pct = (ms: number) => Math.min(100, Math.max(0, (ms / axis) * 100));

  const spans = units.flatMap((u) => {
    const start = t(u.started_at);
    if (start === null) return [];
    const end = t(u.completed_at) ?? (u.status === "running" ? Math.max(now, start) : start);
    return [{ unit: u, start: start - t0, end: end - t0 }];
  });
  const span = new Map(spans.map((s) => [s.unit.indicator, s]));

  const rows: Row[] = rowOrder(units).map((unit) => {
    // Queued in a finished run means it never ran: drawn and counted as skipped.
    const state = !live && unit.status === "queued" ? "skipped" : rowState(unit, by);
    const s = span.get(unit.indicator);
    return {
      unit,
      state,
      leftPct: s ? pct(s.start) : 0,
      widthPct: s ? pct(s.end) - pct(s.start) : 0,
      numbers: s ? formatDuration(s.end - s.start) : "–",
      note: note(unit, state, by, live),
    };
  });

  const events = [...new Set([0, ...spans.flatMap((s) => [s.start, s.end]), elapsedMs])]
    .filter((x) => x <= elapsedMs)
    .sort((a, b) => a - b);
  const strip = units.length
    ? events.map((x) => ({
        pct: pct(x),
        running: spans.filter((s) => s.start <= x && x < s.end).length,
      }))
    : [];

  const count = (st: RowState) => rows.filter((r) => r.state === st).length;
  const ended = count("cancelled") + count("skipped");
  const chips: { text: string; tone: Tone }[] = [];
  if (units.length) {
    if (live) chips.push({ text: `${formatDuration(elapsedMs)} elapsed`, tone: "neutral" });
    if (count("running")) chips.push({ text: `${count("running")} running`, tone: "partial" });
    // "done" is what the legend calls done: succeeded, in its green.
    chips.push({ text: `${count("succeeded")} of ${rows.length} done`, tone: "fresh" });
    if (count("failed")) chips.push({ text: `${count("failed")} failed`, tone: "error" });
    if (ended) chips.push({ text: `${ended} cancelled or skipped`, tone: "neutral" });
    if (count("ready")) chips.push({ text: `${count("ready")} next in line`, tone: "stale" });
    if (count("blocked"))
      chips.push({ text: `${count("blocked")} waiting on prerequisites`, tone: "neutral" });
    if (!live) chips.push({ text: `finished in ${formatDuration(elapsedMs)}`, tone: "neutral" });
  }

  return {
    rows,
    axis,
    elapsedMs,
    nowPct: live ? pct(elapsedMs) : null,
    ticks: ticks(axis),
    strip,
    stripMax: Math.max(slots, ...strip.map((x) => x.running)),
    chips,
  };
}

/**
 * The model at the response's own time, not the wall clock. A unit's finish
 * reaches the page one poll late; drawing running bars to the wall clock grew
 * them past `completed_at` and snapped them back when it arrived. At the
 * data's time a finish lands at or after the edge already drawn, so no bar
 * ever shortens. `RunTimeline.svelte` animates between polls with CSS.
 */
export function timelineAt(detail: RunDetailResponse, live: boolean): TimelineModel {
  return timelineModel(detail.units, detail.unit_slots, Date.parse(detail.server_now), live);
}

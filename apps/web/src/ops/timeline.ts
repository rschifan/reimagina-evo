/**
 * The run page's execution timeline, as data. `RunTimeline.svelte` draws this
 * and decides nothing; every rule the picture follows is here, under test.
 *
 * Two invariants the design rests on (docs/superpowers/specs/
 * 2026-11-18-run-execution-view-design.md):
 * - Rows never move: order is dependency depth, then slug — never start time.
 * - Nothing drawn shrinks while the run is live: the axis is the forecast
 *   finish and grows only when the run passes it (A1), or doubles from 10 s
 *   when there is no forecast (A2). Positions are fractions of a fixed axis.
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

/** List-scheduling simulation of the run over last-run durations. Null without
 *  an estimate for every unit: a partial forecast would be a confident guess. */
export function forecastMs(units: UnitDetail[], slots: number): number | null {
  if (!units.length || units.some((u) => u.expected_ms === null)) return null;
  const ids = new Set(units.map((u) => u.indicator));
  const finished = new Set<string>();
  const pending = rowOrder(units);
  const running: { id: string; end: number }[] = [];
  let now = 0;
  while (pending.length || running.length) {
    for (let i = 0; i < pending.length && running.length < slots; ) {
      const u = pending[i];
      if (u.depends_on.every((d) => !ids.has(d) || finished.has(d))) {
        running.push({ id: u.indicator, end: now + u.expected_ms! });
        pending.splice(i, 1);
      } else i++;
    }
    if (!running.length) return null; // nothing can start: a cycle
    running.sort((a, b) => a.end - b.end);
    const next = running.shift()!;
    now = next.end;
    finished.add(next.id);
  }
  return now;
}

export function axisMs(elapsed: number, forecast: number | null, live: boolean): number {
  if (!live) return Math.max(elapsed, forecast ?? 0, 1);
  let axis = forecast && forecast > 0 ? forecast : 10_000;
  const [grow, headroom] = forecast && forecast > 0 ? [1.25, 0.98] : [2, 0.95];
  while (elapsed > headroom * axis) axis *= grow;
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
  /** Running only: the last run's duration, as an outline. */
  expectedPct: number | null;
  numbers: string;
  note: string | null;
  over: boolean;
}

export interface TimelineModel {
  rows: Row[];
  axis: number;
  elapsedMs: number;
  /** Only fixes the axis; not drawn. */
  forecast: number | null;
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

function note(unit: UnitDetail, state: RowState, by: Map<string, UnitDetail>): string | null {
  if (state === "ready") return "next · waits for a free slot";
  if (state === "skipped") return unit.detail ? `skipped · ${unit.detail}` : "skipped";
  if (state !== "blocked") return null;
  const pre = prerequisites(unit, by);
  const dead = pre.find((p) => ENDED.has(p.status));
  if (dead) return `won't start: ${dead.indicator} ${dead.status}`;
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
  const elapsedMs = units.length ? Math.max(0, (live ? now : lastEnd) - t0) : 0;
  const forecast = forecastMs(units, slots);
  const axis = axisMs(elapsedMs, forecast, live);
  const pct = (ms: number) => Math.min(100, Math.max(0, (ms / axis) * 100));

  const spans = units.flatMap((u) => {
    const start = t(u.started_at);
    if (start === null) return [];
    const end = t(u.completed_at) ?? (u.status === "running" ? Math.max(now, start) : start);
    return [{ unit: u, start: start - t0, end: end - t0 }];
  });
  const span = new Map(spans.map((s) => [s.unit.indicator, s]));

  const rows: Row[] = rowOrder(units).map((unit) => {
    const state = rowState(unit, by);
    const s = span.get(unit.indicator);
    const exp = unit.expected_ms;
    let numbers = "–";
    let over = false;
    if (s && state === "running") {
      const el = s.end - s.start;
      if (exp === null) numbers = formatDuration(el);
      else if (el <= exp) numbers = `${formatDuration(el)} · ${Math.round((el / exp) * 100)}%`;
      else [numbers, over] = [`+${formatDuration(el - exp)} over`, true];
    } else if (s) numbers = formatDuration(s.end - s.start);
    return {
      unit,
      state,
      leftPct: s ? pct(s.start) : 0,
      widthPct: s ? pct(s.end) - pct(s.start) : 0,
      // Clamped: on a fallback axis a long estimate would run off the track.
      expectedPct:
        state === "running" && exp !== null && s
          ? Math.min((exp / axis) * 100, 100 - pct(s.start))
          : null,
      numbers,
      note: note(unit, state, by),
      over,
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
    forecast,
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

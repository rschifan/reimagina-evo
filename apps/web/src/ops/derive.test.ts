import { describe, expect, it } from "vitest";

import type { RunRow } from "./api";
import { RUN_STATUSES } from "./api";
import {
  clockOffset,
  durationMs,
  formatDuration,
  isStalled,
  progressOf,
  runIdFromHash,
  statusCounts,
  statusesFromHash,
  statusesToHash,
  niceSpan,
  timelineOf,
  STALL_GRACE_MS,
} from "./derive";

function run(overrides: Partial<RunRow> = {}): RunRow {
  return {
    run_id: "06baed8c-a6c9-47c1-ab0c-cc38a77f260c",
    status: "running",
    scenario_id: null,
    aoi_id: null,
    code_version: "test",
    submitted_at: "2026-08-20T10:00:00+00:00",
    completed_at: null,
    units_total: 3,
    units_terminal: 3,
    units_succeeded: 3,
    units_failed: 0,
    units_running: 0,
    units_last_terminal_at: "2026-08-20T10:00:09+00:00",
    ...overrides,
  };
}

describe("progressOf", () => {
  it("is 0% for a run with no units rather than NaN", () => {
    const p = progressOf(run({ units_total: 0, units_terminal: 0 }));
    expect(p).toEqual({ done: 0, failed: 0, total: 0, pct: 0 });
  });

  it("counts failures alongside the completed total", () => {
    const p = progressOf(
      run({ units_total: 4, units_terminal: 3, units_failed: 1 }),
    );
    expect(p.pct).toBe(75);
    expect(p.failed).toBe(1);
  });
});

describe("durationMs", () => {
  const now = Date.parse("2026-08-20T10:00:30+00:00");

  it("measures a still-running unit against the injected now", () => {
    expect(durationMs("2026-08-20T10:00:00+00:00", null, now)).toBe(30_000);
  });

  it("clamps a clock-skewed negative to zero rather than showing -00:04", () => {
    expect(durationMs("2026-08-20T10:00:40+00:00", null, now)).toBe(0);
  });

  it("is null when the unit never started", () => {
    expect(durationMs(null, null, now)).toBeNull();
  });

  it("reads an offset-bearing timestamp as an absolute instant", () => {
    // Same instant, two notations: a naive parse would treat one as local time.
    const utc = durationMs(
      "2026-08-20T10:00:00+00:00",
      "2026-08-20T10:00:10+00:00",
      now,
    );
    const offset = durationMs(
      "2026-08-20T12:00:00+02:00",
      "2026-08-20T12:00:10+02:00",
      now,
    );
    expect(utc).toBe(offset);
  });
});

describe("formatDuration", () => {
  it("renders an em dash for nothing to show", () => {
    expect(formatDuration(null)).toBe("—");
  });

  it("rolls over at a minute and at an hour", () => {
    expect(formatDuration(1_400)).toBe("1.4s");
    expect(formatDuration(123_000)).toBe("2m 03s");
    expect(formatDuration(4_320_000)).toBe("1h 12m");
  });
});

describe("isStalled", () => {
  const lastTerminal = Date.parse("2026-08-20T10:00:09+00:00");

  it("does not fire inside the grace period", () => {
    // The window between the last child's write and the parent's finalise. A
    // healthy run passes through this on every successful completion.
    expect(isStalled(run(), lastTerminal + STALL_GRACE_MS - 1)).toBe(false);
  });

  it("fires once the run has been all-terminal but open past the grace", () => {
    expect(isStalled(run(), lastTerminal + STALL_GRACE_MS + 1)).toBe(true);
  });

  it("ignores a run that still has units to finish", () => {
    const r = run({ units_terminal: 2, units_running: 1 });
    expect(isStalled(r, lastTerminal + 60_000)).toBe(false);
  });

  it("ignores a closed run", () => {
    const r = run({
      completed_at: "2026-08-20T10:00:10+00:00",
      status: "succeeded",
    });
    expect(isStalled(r, lastTerminal + 60_000)).toBe(false);
  });

  it("ignores a run whose envelope exists but has no units yet", () => {
    const r = run({
      units_total: 0,
      units_terminal: 0,
      units_last_terminal_at: null,
    });
    expect(isStalled(r, lastTerminal + 60_000)).toBe(false);
  });
});

describe("clock correction", () => {
  it("recovers the server offset from the response", () => {
    const clientNow = Date.parse("2026-08-20T10:00:00+00:00");
    const offset = clockOffset("2026-08-20T10:00:20+00:00", clientNow);
    expect(offset).toBe(20_000);
  });

  it("a client clock behind the server does not suppress a stalled run", () => {
    // Client is 60s slow. Uncorrected, `now - last_terminal` stays under the
    // grace and the badge never appears — the failure this correction exists for.
    const clientNow = lastTerminalPlus(20_000) - 60_000;
    const offset = clockOffset("2026-08-20T10:00:29+00:00", clientNow);
    expect(isStalled(run(), clientNow + offset)).toBe(true);
    expect(isStalled(run(), clientNow)).toBe(false);
  });

  function lastTerminalPlus(ms: number): number {
    return Date.parse("2026-08-20T10:00:09+00:00") + ms;
  }
});

describe("hash routing", () => {
  it("round-trips a status filter", () => {
    const hash = statusesToHash(["failed", "partial"]);
    expect(statusesFromHash(hash, RUN_STATUSES)).toEqual(["partial", "failed"]);
  });

  it("drops unknown statuses from a hand-edited URL", () => {
    expect(statusesFromHash("#/?status=failed,bogus", RUN_STATUSES)).toEqual([
      "failed",
    ]);
  });

  it("tells a run route apart from a filtered index", () => {
    expect(runIdFromHash("#/r/abc-123")).toBe("abc-123");
    expect(runIdFromHash("#/?status=failed")).toBeNull();
    expect(runIdFromHash("#/")).toBeNull();
  });
});

describe("statusCounts", () => {
  it("zero-fills statuses the GROUP BY omitted", () => {
    const counts = statusCounts({ succeeded: 2 }, RUN_STATUSES);
    expect(counts.succeeded).toBe(2);
    expect(counts.failed).toBe(0);
    expect(Object.keys(counts)).toHaveLength(RUN_STATUSES.length);
  });
});

describe("timelineOf", () => {
  const unit = (
    indicator: string,
    queued: string,
    started: string | null,
    completed: string | null,
    depends_on: string[] = [],
  ) => ({
    indicator,
    status: "succeeded" as const,
    depends_on,
    queued_at: `2026-08-20T10:00:${queued}+00:00`,
    started_at: started ? `2026-08-20T10:00:${started}+00:00` : null,
    completed_at: completed ? `2026-08-20T10:00:${completed}+00:00` : null,
  });

  const now = Date.parse("2026-08-20T10:00:30+00:00");

  it("reports the parallelism actually achieved, not the DAG's width", () => {
    // Two overlap 00-10; the third only starts at 10.
    const t = timelineOf(
      [
        unit("a", "00", "00", "10"),
        unit("b", "00", "00", "08"),
        unit("c", "00", "10", "20", ["a"]),
      ],
      now,
    );
    expect(t.peak).toBe(2);
  });

  it("does not count a unit that ended exactly as another began", () => {
    const t = timelineOf([unit("a", "00", "00", "10"), unit("b", "00", "10", "20")], now);
    expect(t.peak).toBe(1);
  });

  it("measures a still-running unit to now", () => {
    const t = timelineOf([unit("a", "00", "10", null)], now);
    const row = t.rows[0];
    expect(row.live).toBe(true);
    expect(row.durationMs).toBe(20_000);
  });

  it("places bars proportionally across the run's span", () => {
    const t = timelineOf([unit("a", "00", "00", "10"), unit("b", "00", "10", "20")], now);
    expect(t.spanMs).toBe(20_000);
    expect(t.rows[0].runLeft).toBe(0);
    expect(t.rows[0].runWidth).toBe(50);
    expect(t.rows[1].runLeft).toBe(50);
  });

  it("survives a run where every timestamp is identical", () => {
    // Zero span would divide by zero and put every bar at NaN%.
    const t = timelineOf([unit("a", "00", "00", "00")], now);
    expect(Number.isFinite(t.rows[0].runWidth)).toBe(true);
  });

  it("is empty for a run with no units", () => {
    expect(timelineOf([], now)).toEqual({
      rows: [],
      spanMs: 0,
      elapsedMs: 0,
      ticks: [],
      peak: 0,
    });
  });

  it("shows the wait before a prerequisite cleared as queued time", () => {
    const t = timelineOf([unit("c", "00", "10", "20", ["a"])], now);
    expect(t.rows[0].queuedWidth).toBeGreaterThan(0);
    expect(t.rows[0].depends_on).toEqual(["a"]);
  });
});

describe("timeline axis stability", () => {
  const at = (s: string) => `2026-08-20T10:00:${s}+00:00`;
  const finished = {
    indicator: "a",
    status: "succeeded" as const,
    depends_on: [],
    queued_at: at("00"),
    started_at: at("00"),
    completed_at: at("05"),
  };
  const running = {
    indicator: "b",
    status: "running" as const,
    depends_on: [],
    queued_at: at("00"),
    started_at: at("00"),
    completed_at: null,
  };

  it("holds a finished bar still while another unit runs on", () => {
    // The bug: the axis grew with `now`, so a completed bar's percentage width
    // shrank every tick and its right edge crawled leftward.
    const t1 = timelineOf([finished, running], Date.parse(at("06")));
    const t2 = timelineOf([finished, running], Date.parse(at("09")));

    expect(t2.rows[0].runWidth).toBe(t1.rows[0].runWidth);
    expect(t2.rows[0].runLeft).toBe(t1.rows[0].runLeft);
  });

  it("still grows the running bar as time passes", () => {
    const t1 = timelineOf([finished, running], Date.parse(at("06")));
    const t2 = timelineOf([finished, running], Date.parse(at("09")));

    expect(t2.rows[1].runWidth).toBeGreaterThan(t1.rows[1].runWidth);
  });

  it("reports elapsed time, not the padded axis", () => {
    const t = timelineOf([finished, running], Date.parse(at("06")));

    expect(t.elapsedMs).toBe(6_000);
    expect(t.spanMs).toBe(10_000); // quantised
  });

  it("uses the exact span once nothing can grow", () => {
    const t = timelineOf([finished], Date.parse(at("30")));

    expect(t.spanMs).toBe(5_000);
    expect(t.rows[0].runWidth).toBe(100);
  });

  it("rounds a long run up to whole hours rather than inventing steps", () => {
    expect(niceSpan(3_600_001)).toBe(7_200_000);
    expect(niceSpan(1_000)).toBe(5_000);
  });
});

describe("axis ticks", () => {
  it("labels five marks across the axis in elapsed time", () => {
    const t = timelineOf(
      [
        {
          indicator: "a",
          status: "succeeded" as const,
          depends_on: [],
          queued_at: "2026-08-20T10:00:00+00:00",
          started_at: "2026-08-20T10:00:00+00:00",
          completed_at: "2026-08-20T10:00:20+00:00",
        },
      ],
      Date.parse("2026-08-20T10:00:20+00:00"),
    );

    expect(t.ticks.map((x) => x.atPct)).toEqual([0, 25, 50, 75, 100]);
    // The axis is what tells a reader the x-position means time rather than
    // a proportion filled — without it a bar that starts late looks reversed.
    expect(t.ticks[0].label).toBe("0.0s");
    expect(t.ticks[4].label).toBe("20.0s");
  });

  it("has no ticks when there is nothing to draw", () => {
    expect(timelineOf([], Date.now()).ticks).toEqual([]);
  });
});

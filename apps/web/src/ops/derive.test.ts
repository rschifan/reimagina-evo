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
  timelineOf,
  tickStepSeconds,
  fitPxPerSecond,
  PX_PER_SECOND,
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
    expect(t.rows[0].live).toBe(true);
    expect(t.rows[0].durationMs).toBe(20_000);
  });

  it("draws a second as a constant width, whatever the run's length", () => {
    // The whole point of pixels: ten seconds is the same size in a short run
    // and a long one, so two runs are comparable and nothing rescales.
    const short = timelineOf([unit("a", "00", "00", "10")], now);
    const long = timelineOf(
      [unit("a", "00", "00", "10"), unit("b", "00", "10", "30")],
      now,
    );

    expect(short.rows[0].runWidthPx).toBe(10 * PX_PER_SECOND);
    expect(long.rows[0].runWidthPx).toBe(short.rows[0].runWidthPx);
    expect(long.rows[0].runLeftPx).toBe(short.rows[0].runLeftPx);
  });

  it("is empty for a run with no units", () => {
    expect(timelineOf([], now)).toEqual({
      rows: [],
      elapsedMs: 0,
      widthPx: 0,
      ticks: [],
      peak: 0,
    });
  });

  it("shows the wait before a prerequisite cleared", () => {
    const t = timelineOf([unit("c", "00", "10", "20", ["a"])], now);
    expect(t.rows[0].queuedWidthPx).toBe(10 * PX_PER_SECOND);
    expect(t.rows[0].depends_on).toEqual(["a"]);
  });
});

describe("nothing already drawn ever moves", () => {
  const u = (
    indicator: string,
    started: string,
    completed: string | null,
  ) => ({
    indicator,
    status: completed ? ("succeeded" as const) : ("running" as const),
    depends_on: [],
    queued_at: "2026-08-20T10:00:00+00:00",
    started_at: `2026-08-20T10:00:${started}+00:00`,
    completed_at: completed ? `2026-08-20T10:00:${completed}+00:00` : null,
  });

  const finished = u("a", "00", "05");
  const running = u("b", "02", null);

  it("holds a finished bar exactly still while the run continues", () => {
    // The defect this layout exists to remove: with percentages, `span` grew
    // every tick and each bar's left edge slid leftward.
    const early = timelineOf([finished, running], Date.parse("2026-08-20T10:00:06+00:00"));
    const later = timelineOf([finished, running], Date.parse("2026-08-20T10:00:25+00:00"));

    expect(later.rows[0].runLeftPx).toBe(early.rows[0].runLeftPx);
    expect(later.rows[0].runWidthPx).toBe(early.rows[0].runWidthPx);
  });

  it("grows a running bar to the right, from a fixed left edge", () => {
    const early = timelineOf([finished, running], Date.parse("2026-08-20T10:00:06+00:00"));
    const later = timelineOf([finished, running], Date.parse("2026-08-20T10:00:25+00:00"));

    expect(later.rows[1].runLeftPx).toBe(early.rows[1].runLeftPx);
    expect(later.rows[1].runWidthPx).toBeGreaterThan(early.rows[1].runWidthPx);
  });

  it("does not move anything when the last unit lands", () => {
    const at6 = Date.parse("2026-08-20T10:00:06+00:00");
    const stillRunning = timelineOf([finished, running], at6);
    const justFinished = timelineOf([finished, u("b", "02", "06")], at6);

    expect(justFinished.rows[0].runWidthPx).toBe(stillRunning.rows[0].runWidthPx);
    expect(justFinished.rows[1].runWidthPx).toBe(stillRunning.rows[1].runWidthPx);
  });
});

describe("axis ticks", () => {
  const oneUnit = (completedAt: string) => [
    {
      indicator: "a",
      status: "succeeded" as const,
      depends_on: [],
      queued_at: "2026-08-20T10:00:00+00:00",
      started_at: "2026-08-20T10:00:00+00:00",
      completed_at: completedAt,
    },
  ];

  it("places marks in pixels on the same scale as the bars", () => {
    const t = timelineOf(oneUnit("2026-08-20T10:00:20+00:00"), Date.parse("2026-08-20T10:00:20+00:00"));

    expect(t.ticks[0]).toEqual({ atPx: 0, label: "0s" });
    expect(t.ticks[1].atPx).toBe(
      Number(t.ticks[1].label.replace("s", "")) * PX_PER_SECOND,
    );
  });

  it("widens the tick step so labels stay readable on a long run", () => {
    expect(tickStepSeconds(8_000)).toBe(1);
    expect(tickStepSeconds(60_000)).toBe(10);
    expect(tickStepSeconds(600_000)).toBe(120);
  });

  it("has no ticks when there is nothing to draw", () => {
    expect(timelineOf([], Date.now()).ticks).toEqual([]);
  });
});

describe("fitPxPerSecond", () => {
  it("scales a finished run to fill the track exactly", () => {
    // 6 s run in a 740 px panel: the last bar must end at the right-hand edge
    // rather than 90 px in, which is what a fixed scale did to a short run.
    const scale = fitPxPerSecond(6_000, 740);
    const t = timelineOf(
      [
        {
          indicator: "a",
          status: "succeeded" as const,
          depends_on: [],
          queued_at: "2026-08-20T10:00:00+00:00",
          started_at: "2026-08-20T10:00:00+00:00",
          completed_at: "2026-08-20T10:00:06+00:00",
        },
      ],
      Date.parse("2026-08-20T10:00:06+00:00"),
      scale,
    );

    expect(t.widthPx).toBeCloseTo(740, 5);
    expect(t.rows[0].runWidthPx).toBeCloseTo(740, 5);
  });

  it("falls back to the fixed scale before the track has been measured", () => {
    // First render, clientWidth still 0 — dividing by it would be Infinity.
    expect(fitPxPerSecond(6_000, 0)).toBe(PX_PER_SECOND);
    expect(fitPxPerSecond(0, 740)).toBe(PX_PER_SECOND);
  });
});

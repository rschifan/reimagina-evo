import { describe, expect, it } from "vitest";

import type { RunRow } from "./api";
import { RUN_STATUSES } from "./api";
import {
  clockOffset,
  monotonicNow,
  durationMs,
  formatDuration,
  isStalled,
  progressOf,
  runIdFromHash,
  statusCounts,
  statusesFromHash,
  statusesToHash,
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
    expect(p.pct).toBe(0);
    expect(p.total).toBe(0);
    expect(p.segments).toEqual({
      succeeded: 0,
      failed: 0,
      running: 0,
      pending: 0,
    });
  });

  it("counts failures alongside the completed total", () => {
    const p = progressOf(
      run({ units_total: 4, units_terminal: 3, units_failed: 1 }),
    );
    expect(p.pct).toBe(75);
    expect(p.failed).toBe(1);
  });

  // ⚠ The regression. `<progress value={units_terminal} max={units_total}>`
  // drew a full **green** bar for a run whose only unit failed, beside a red
  // `failed` badge — observed on 25 of 27 runs in the dashboard, every one a
  // failure. The bar is the largest, most saturated element in the row, and
  // green means success in every convention a dashboard inherits.
  it("gives a wholly-failed run no green at all", () => {
    const p = progressOf(
      run({
        status: "failed",
        units_total: 1,
        units_terminal: 1,
        units_succeeded: 0,
        units_failed: 1,
        units_running: 0,
      }),
    );
    expect(p.segments.succeeded).toBe(0);
    expect(p.segments.failed).toBe(100);
    expect(p.segments.pending).toBe(0);
  });

  it("splits a partial run by outcome rather than by completion", () => {
    const p = progressOf(
      run({
        status: "partial",
        units_total: 4,
        units_terminal: 4,
        units_succeeded: 3,
        units_failed: 1,
        units_running: 0,
      }),
    );
    expect(p.segments.succeeded).toBe(75);
    expect(p.segments.failed).toBe(25);
    expect(p.segments.pending).toBe(0);
  });

  it("shows work still to come as track rather than as progress", () => {
    const p = progressOf(
      run({
        units_total: 4,
        units_terminal: 1,
        units_succeeded: 1,
        units_failed: 0,
        units_running: 1,
      }),
    );
    expect(p.segments.succeeded).toBe(25);
    expect(p.segments.running).toBe(25);
    expect(p.segments.pending).toBe(50);
  });

  it("never overflows the track when the counters disagree", () => {
    // A unit can be observed as both terminal and running across two
    // queries mid-transition. A bar that overflows its own track is a
    // worse failure than one that is briefly a percent short.
    const p = progressOf(
      run({
        units_total: 2,
        units_terminal: 2,
        units_succeeded: 2,
        units_failed: 0,
        units_running: 2,
      }),
    );
    const { succeeded, failed, running, pending } = p.segments;
    expect(succeeded + failed + running + pending).toBeLessThanOrEqual(100);
    expect(running).toBeGreaterThanOrEqual(0);
  });

  it("describes the whole bar in one line for assistive technology", () => {
    const p = progressOf(
      run({
        units_total: 4,
        units_terminal: 3,
        units_succeeded: 2,
        units_failed: 1,
        units_running: 1,
      }),
    );
    expect(p.label).toBe("4 units: 2 succeeded, 1 failed, 1 running");
  });

  it("says so when nothing has started", () => {
    const p = progressOf(
      run({
        units_total: 3,
        units_terminal: 0,
        units_succeeded: 0,
        units_failed: 0,
        units_running: 0,
      }),
    );
    expect(p.label).toBe("3 units: 3 pending");
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
    const offset = clockOffset("2026-08-20T10:00:20+00:00", clientNow, clientNow);
    expect(offset).toBe(20_000);
  });

  it("measures against the request's midpoint, not its arrival", () => {
    // Sent at 18 s, answered at 20 s: the server stamped it around 19 s.
    const at = (s: number) => Date.parse("2026-08-20T10:00:00Z") + s * 1000;
    expect(clockOffset("2026-08-20T10:00:20Z", at(18), at(20))).toBe(1_000);
  });

  it("an unparseable stamp corrects nothing", () => {
    expect(clockOffset("nope", 0, 10)).toBe(0);
  });

  it("now never steps back on a jittery sample, but takes a real correction", () => {
    expect(monotonicNow(10_000, 9_600)).toBe(10_000);
    expect(monotonicNow(10_000, 10_100)).toBe(10_100);
    expect(monotonicNow(10_000, 5_000)).toBe(5_000);
  });

  it("a client clock behind the server does not suppress a stalled run", () => {
    // Client is 60s slow. Uncorrected, `now - last_terminal` stays under the
    // grace and the badge never appears — the failure this correction exists for.
    const clientNow = lastTerminalPlus(20_000) - 60_000;
    const offset = clockOffset("2026-08-20T10:00:29+00:00", clientNow, clientNow);
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

  // A hand-edited or truncated link threw URIError inside the route effect.
  it("a malformed escape reads as no run", () => {
    expect(runIdFromHash("#/r/%E0")).toBeNull();
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

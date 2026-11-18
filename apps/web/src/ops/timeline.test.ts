import { describe, expect, test } from "vitest";
import type { UnitDetail } from "./api";
import fixture from "./fixtures/run-12-units.json";
import {
  axisMs,
  forecastMs,
  rowOrder,
  rowState,
  ticks,
  timelineModel,
} from "./timeline";

const RECORDED = fixture.units as UnitDetail[];
const ms = (iso: string | null) => (iso ? Date.parse(iso) : null);
const T0 = Math.min(...RECORDED.map((u) => ms(u.queued_at)!));
const END = Math.max(...RECORDED.map((u) => ms(u.completed_at)!));
const actual = (u: UnitDetail) => ms(u.completed_at)! - ms(u.started_at)!;

/** The recorded run as it stood at absolute time `at`. */
function frameAt(units: UnitDetail[], at: number): UnitDetail[] {
  return units.map((u) => {
    const started = ms(u.started_at);
    const completed = ms(u.completed_at);
    if (started === null || started > at)
      return { ...u, status: "queued", started_at: null, completed_at: null };
    if (completed !== null && completed <= at) return u;
    return { ...u, status: "running", completed_at: null };
  });
}

function unit(indicator: string, over: Partial<UnitDetail> = {}): UnitDetail {
  return {
    indicator,
    status: "queued",
    detail: null,
    queued_at: "2026-08-20T10:00:00Z",
    started_at: null,
    completed_at: null,
    depends_on: [],
    expected_ms: null,
    ...over,
  };
}
const at = (s: number) => Date.parse("2026-08-20T10:00:00Z") + s * 1000;
const iso = (s: number) => new Date(at(s)).toISOString();

describe("row state", () => {
  const byOf = (us: UnitDetail[]) => new Map(us.map((u) => [u.indicator, u]));

  test("ready, blocked, and won't start", () => {
    const a = unit("a", { status: "succeeded", started_at: iso(0), completed_at: iso(1) });
    const b = unit("b", { status: "running", started_at: iso(0) });
    const f = unit("f", { status: "failed", started_at: iso(0), completed_at: iso(1) });
    const ready = unit("r", { depends_on: ["a"] });
    const blocked = unit("x", { depends_on: ["a", "b"] });
    const doomed = unit("d", { depends_on: ["f"] });
    const all = [a, b, f, ready, blocked, doomed];
    expect(rowState(ready, byOf(all))).toBe("ready");
    expect(rowState(blocked, byOf(all))).toBe("blocked");
    expect(rowState(doomed, byOf(all))).toBe("blocked");

    const notes = new Map(
      timelineModel(all, 8, at(2), true).rows.map((r) => [r.unit.indicator, r.note]),
    );
    expect(notes.get("r")).toBe("next · waits for a free slot");
    expect(notes.get("x")).toBe("after b");
    expect(notes.get("d")).toBe("won't start: f failed");
  });
});

describe("row order", () => {
  test("depth then slug, unchanged for the whole run", () => {
    const final = rowOrder(RECORDED).map((u) => u.indicator);
    expect(final.slice(-2)).toEqual(["livability_block_score", "urban_form_block_composite"]);
    for (let t = T0; t <= END; t += 100) {
      expect(rowOrder(frameAt(RECORDED, t)).map((u) => u.indicator)).toEqual(final);
    }
  });
});

describe("forecast", () => {
  test("reproduces the recorded run within 1 s when expected = actual", () => {
    const units = RECORDED.map((u) => ({ ...u, expected_ms: actual(u) }));
    expect(Math.abs(forecastMs(units, 8)! - (END - T0))).toBeLessThan(1_000);
  });

  test("is null when any unit lacks expected_ms", () => {
    const units = RECORDED.map((u, i) => ({ ...u, expected_ms: i ? actual(u) : null }));
    expect(forecastMs(units, 8)).toBeNull();
    expect(forecastMs([], 8)).toBeNull();
  });

  test("respects the slot count", () => {
    const units = ["a", "b", "c"].map((s) => unit(s, { expected_ms: 1_000 }));
    expect(forecastMs(units, 1)).toBe(3_000);
    expect(forecastMs(units, 3)).toBe(1_000);
  });
});

describe("axis", () => {
  test("forecast axis holds, then grows x1.25 past 98 %", () => {
    expect(axisMs(20_000, 26_500, true)).toBe(26_500);
    expect(axisMs(26_000, 26_500, true)).toBe(33_125);
  });

  test("doubling axis without a forecast", () => {
    expect(axisMs(9_000, null, true)).toBe(10_000);
    expect(axisMs(9_600, null, true)).toBe(20_000);
  });

  test("finished run fits the run", () => {
    expect(axisMs(29_800, 26_500, false)).toBe(29_800);
    expect(axisMs(20_000, 26_500, false)).toBe(26_500);
  });

  test("last tick sits at 100 %", () => {
    expect(ticks(30_000).at(-1)!.pct).toBe(100);
    expect(ticks(33_125).at(-1)!.pct).toBe(100);
    expect(ticks(30_000)[0]).toEqual({ pct: 0, label: "0s" });
  });
});

describe("nothing drawn moves left or shrinks while live", () => {
  test("replay of the recorded run every 100 ms, with one overrun", () => {
    const units = RECORDED.map((u) => ({ ...u, expected_ms: Math.round(actual(u) * 0.9) }));
    let prev: ReturnType<typeof timelineModel> | null = null;
    let rescales = 0;
    for (let t = T0; t < END; t += 100) {
      const m = timelineModel(frameAt(units, t), 8, t, true);
      if (prev) {
        expect(m.axis).toBeGreaterThanOrEqual(prev.axis);
        if (m.axis !== prev.axis) rescales++;
        else
          m.rows.forEach((row, i) => {
            const was = prev!.rows[i];
            expect(row.unit.indicator).toBe(was.unit.indicator);
            if (was.widthPct > 0) {
              expect(row.leftPct).toBeCloseTo(was.leftPct, 9);
              expect(row.leftPct + row.widthPct).toBeGreaterThanOrEqual(
                was.leftPct + was.widthPct - 1e-9,
              );
            }
          });
      }
      prev = m;
    }
    expect(rescales).toBe(1);
  });
});

describe("numbers", () => {
  const running = (expected: number | null) =>
    timelineModel([unit("a", { status: "running", started_at: iso(0), expected_ms: expected })], 8, at(4), true)
      .rows[0];

  test("percent of expected", () => {
    expect(running(10_000).numbers).toBe("4.0s · 40%");
    expect(running(10_000).over).toBe(false);
  });

  test("past expected", () => {
    expect(running(3_000).numbers).toBe("+1.0s over");
    expect(running(3_000).over).toBe(true);
  });

  test("no estimate", () => {
    expect(running(null).numbers).toBe("4.0s");
    expect(running(null).expectedPct).toBeNull();
  });
});

describe("chips", () => {
  const texts = (m: ReturnType<typeof timelineModel>) => m.chips.map((c) => c.text);
  const one = (over: Partial<UnitDetail>) => [unit("a", { expected_ms: 10_000, ...over })];

  test("live, inside the forecast", () => {
    const m = timelineModel(one({ status: "running", started_at: iso(0) }), 8, at(4), true);
    expect(texts(m)).toEqual(["4.0s elapsed", "1 running", "0 of 1 done", "expected finish in ~6.0s"]);
  });

  test("live, past the forecast", () => {
    const m = timelineModel(one({ status: "running", started_at: iso(0) }), 8, at(12), true);
    expect(texts(m)).toContain("2.0s past forecast");
    expect(m.chips.find((c) => c.text.endsWith("past forecast"))!.tone).toBe("stale");
  });

  test("finished", () => {
    const m = timelineModel(
      one({ status: "succeeded", started_at: iso(0), completed_at: iso(12) }),
      8,
      at(30),
      false,
    );
    expect(texts(m)).toEqual(["1 of 1 done", "finished in 12.0s · forecast was 10.0s"]);
  });
});

describe("degenerate input", () => {
  test("zero units", () => {
    const m = timelineModel([], 8, at(0), true);
    expect(m.rows).toEqual([]);
    expect(m.chips).toEqual([]);
  });

  test("pre-envelope units: queued, no timings, no prerequisites", () => {
    const m = timelineModel([unit("a"), unit("b")], 8, at(1), true);
    expect(m.rows.map((r) => r.state)).toEqual(["ready", "ready"]);
    expect(m.forecast).toBeNull();
    expect(m.axis).toBe(10_000);
  });
});

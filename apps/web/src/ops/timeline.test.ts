import { describe, expect, test } from "vitest";
import type { UnitDetail } from "./api";
import fixture from "./fixtures/run-12-units.json";
import {
  axisMs,
  rowOrder,
  rowState,
  ticks,
  timelineAt,
  timelineModel,
  UNIT_VISUAL,
} from "./timeline";

const RECORDED = fixture.units as UnitDetail[];
const ms = (iso: string | null) => (iso ? Date.parse(iso) : null);
const T0 = Math.min(...RECORDED.map((u) => ms(u.queued_at)!));
const END = Math.max(...RECORDED.map((u) => ms(u.completed_at)!));

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

describe("axis", () => {
  // No prediction: the live axis starts at 10 s and doubles each time the
  // run reaches its edge, so everything drawn shrinks and the run continues.
  test("live axis starts at 10 s and doubles at the edge", () => {
    expect(axisMs(0, true)).toBe(10_000);
    expect(axisMs(9_000, true)).toBe(10_000);
    expect(axisMs(9_600, true)).toBe(20_000);
    expect(axisMs(39_000, true)).toBe(80_000);
  });

  test("finished run fits the run", () => {
    expect(axisMs(29_800, false)).toBe(29_800);
    expect(axisMs(0, false)).toBe(1);
  });

  test("last tick sits at 100 %", () => {
    expect(ticks(30_000).at(-1)!.pct).toBe(100);
    expect(ticks(33_125).at(-1)!.pct).toBe(100);
    expect(ticks(30_000)[0]).toEqual({ pct: 0, label: "0s" });
  });
});

describe("nothing drawn moves left or shrinks while live", () => {
  test("replay of the recorded run every 100 ms", () => {
    const units = RECORDED;
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
    // Each rescale is a doubling: the run outgrew the edge, nothing else.
    expect(rescales).toBeGreaterThanOrEqual(1);
    expect(prev!.axis % 10_000).toBe(0);
  });
});

describe("numbers", () => {
  test("a running unit shows its elapsed time and nothing predicted", () => {
    const row = timelineModel(
      [unit("a", { status: "running", started_at: iso(0) })], 8, at(4), true,
    ).rows[0];
    expect(row.numbers).toBe("4.0s");
    expect(row).not.toHaveProperty("expectedPct");
    expect(row).not.toHaveProperty("over");
  });
});

describe("chips", () => {
  const texts = (m: ReturnType<typeof timelineModel>) => m.chips.map((c) => c.text);
  const one = (over: Partial<UnitDetail>) => [unit("a", over)];

  test("live", () => {
    const m = timelineModel(one({ status: "running", started_at: iso(0) }), 8, at(4), true);
    expect(texts(m)).toEqual(["4.0s elapsed", "1 running", "0 of 1 done"]);
  });

  test("live, past the first axis", () => {
    const m = timelineModel(one({ status: "running", started_at: iso(0) }), 8, at(12), true);
    expect(texts(m)).toEqual(["12.0s elapsed", "1 running", "0 of 1 done"]);
  });

  test("finished", () => {
    const m = timelineModel(
      one({ status: "succeeded", started_at: iso(0), completed_at: iso(12) }),
      8,
      at(30),
      false,
    );
    expect(texts(m)).toEqual(["1 of 1 done", "finished in 12.0s"]);
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
    expect(m.axis).toBe(10_000);
  });
});

describe("review fixes", () => {
  test("drawn at the response's own time, so a finish between polls never shortens a bar", () => {
    const run = fixture.run as never;
    const poll = (units: UnitDetail[], s: number) =>
      timelineAt({ run, units, server_now: iso(s), unit_slots: 8 }, true);
    const before = poll([unit("a", { status: "running", started_at: iso(0) })], 5);
    const after = poll(
      [unit("a", { status: "succeeded", started_at: iso(0), completed_at: iso(5.4) })],
      6,
    );
    const right = (m: ReturnType<typeof timelineAt>) => m.rows[0].leftPct + m.rows[0].widthPct;
    expect(right(before)).toBeCloseTo((5 / before.axis) * 100e3, 6);
    expect(right(after)).toBeGreaterThanOrEqual(right(before));
  });

  test("'done' counts what the legend calls done", () => {
    const m = timelineModel(
      [
        unit("s", { status: "succeeded", started_at: iso(0), completed_at: iso(1) }),
        unit("f", { status: "failed", started_at: iso(0), completed_at: iso(1) }),
        unit("c", { status: "cancelled" }),
        unit("k", { status: "skipped" }),
      ],
      8,
      at(5),
      false,
    );
    expect(m.chips.slice(0, 3)).toEqual([
      { text: "1 of 4 done", tone: "fresh" },
      { text: "1 failed", tone: "error" },
      { text: "2 cancelled or skipped", tone: "neutral" },
    ]);
  });

  test("every state has its own dot", () => {
    const dots = Object.values(UNIT_VISUAL).map((v) => v.dot);
    expect(new Set(dots).size).toBe(dots.length);
  });
});

describe("follow-ups", () => {
  test("a sub-second axis keeps its zero tick and labels its end", () => {
    const tk = ticks(400);
    expect(tk[0]).toEqual({ pct: 0, label: "0s" });
    expect(tk.at(-1)).toEqual({ pct: 100, label: "0.4s" });
    expect(tk.length).toBeGreaterThan(2);
    expect(new Set(tk.map((x) => x.label)).size).toBe(tk.length);
  });

  test("the strip scale reaches the observed peak above the per-worker slots", () => {
    const running = (i: string) =>
      unit(i, { status: "running", started_at: "2026-08-20T10:00:00Z" });
    const now = Date.parse("2026-08-20T10:00:05Z");
    const m = timelineModel([running("a"), running("b"), running("c")], 2, now, true);
    expect(Math.max(...m.strip.map((s) => s.running))).toBe(3);
    expect(m.stripMax).toBe(3);
    expect(timelineModel([running("a")], 2, now, true).stripMax).toBe(2);
  });
});

describe("edge cases", () => {
  test("every unit downstream of a failure says it won't start", () => {
    const f = unit("f", { status: "failed", started_at: iso(0), completed_at: iso(1) });
    const child = unit("c", { depends_on: ["f"] });
    const grandchild = unit("g", { depends_on: ["c"] });
    const notes = new Map(
      timelineModel([f, child, grandchild], 8, at(2), true).rows.map((r) => [
        r.unit.indicator,
        r.note,
      ]),
    );
    expect(notes.get("c")).toBe("won't start: f failed");
    expect(notes.get("g")).toBe("won't start: f failed");
  });

  test("a stalled run (every unit ended, run still open) stops growing", () => {
    const done = [
      unit("a", { status: "succeeded", started_at: iso(0), completed_at: iso(5) }),
      unit("b", { status: "failed", started_at: iso(0), completed_at: iso(8) }),
    ];
    const early = timelineModel(done, 8, at(10), true);
    const late = timelineModel(done, 8, at(600), true);
    expect(late.elapsedMs).toBe(8_000);
    expect(late.axis).toBe(early.axis);
    expect(late.nowPct).toBe(early.nowPct);
  });
});

describe("finished runs", () => {
  test("a unit that never started says so, not 'next'", () => {
    const a = unit("a", { status: "succeeded", started_at: iso(0), completed_at: iso(2) });
    const leftover = unit("b", { depends_on: ["a"] });
    const cancelled = unit("c", { status: "cancelled", detail: "run cancelled" });
    const m = timelineModel([a, leftover, cancelled], 8, at(30), false);
    const row = (id: string) => m.rows.find((r) => r.unit.indicator === id)!;
    expect(row("b").note).toBe("never started");
    expect(row("b").state).not.toBe("ready");
    expect(row("c").note).toBe("cancelled before it started");
    expect(m.chips.map((c) => c.text).join()).not.toMatch(/next in line|waiting on/);
  });
});

// The run follower and the rule removal, out of the DOM. These pin what
// tests of the old sidebar panel pinned through its DOM: each case
// here exists because the ordering of awaits and timers once went wrong.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ getRun: vi.fn(), removeScenarioEdit: vi.fn() }));
vi.mock("../api", () => api);

import {
  MAX_RUN_POLLS,
  MAX_RUN_READ_FAILURES,
  RUN_POLL_MS,
  followRun,
  type FollowEvents,
} from "./follow";

const run = (status: string, units: Array<{ status: string }> = []) => ({
  run_id: "run-aaaaaaaa",
  status,
  units: units.map((u) => ({ indicator: "x", detail: null, ...u })),
});

function events() {
  return {
    progress: vi.fn(),
    landed: vi.fn(),
    failed: vi.fn(),
    unreadable: vi.fn(),
    handedOver: vi.fn(),
  } satisfies FollowEvents;
}

const tick = () => vi.advanceTimersByTimeAsync(RUN_POLL_MS);

beforeEach(() => {
  vi.useFakeTimers();
  api.getRun.mockReset();
  api.removeScenarioEdit.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("followRun", () => {
  it("fires nothing after cancel, even from a tick in flight", async () => {
    let release!: (value: unknown) => void;
    api.getRun.mockReturnValue(new Promise((r) => (release = r)));
    const on = events();
    const cancel = followRun("run-aaaaaaaa", on);
    await tick(); // the first poll is now awaiting getRun
    cancel();
    release(run("succeeded"));
    await vi.advanceTimersByTimeAsync(0);
    expect(Object.values(on).every((fn) => fn.mock.calls.length === 0)).toBe(true);
  });

  it("keeps polling while the run is running, then lands", async () => {
    api.getRun.mockResolvedValueOnce(run("running")).mockResolvedValueOnce(run("succeeded"));
    const on = events();
    followRun("run-aaaaaaaa", on);
    await tick();
    expect(on.progress).toHaveBeenCalledWith("running");
    await tick();
    expect(on.landed).toHaveBeenCalledTimes(1);
  });

  it("survives one unreadable poll and lands on the next", async () => {
    api.getRun.mockRejectedValueOnce(new Error("blip")).mockResolvedValueOnce(run("partial"));
    const on = events();
    followRun("run-aaaaaaaa", on);
    await tick();
    await tick();
    expect(on.unreadable).not.toHaveBeenCalled();
    expect(on.landed).toHaveBeenCalledTimes(1);
  });

  it("hands over only after the read failures reach the cap", async () => {
    api.getRun.mockRejectedValue(new Error("down"));
    const on = events();
    followRun("run-aaaaaaaa", on);
    for (let i = 0; i < MAX_RUN_READ_FAILURES - 1; i += 1) await tick();
    expect(on.unreadable).not.toHaveBeenCalled();
    await tick();
    expect(on.unreadable).toHaveBeenCalledTimes(1);
    await tick();
    expect(api.getRun).toHaveBeenCalledTimes(MAX_RUN_READ_FAILURES);
  });

  it("reports a failed run as failed, never as landed", async () => {
    api.getRun.mockResolvedValue(run("failed"));
    const on = events();
    followRun("run-aaaaaaaa", on);
    await tick();
    expect(on.failed).toHaveBeenCalledTimes(1);
    expect(on.landed).not.toHaveBeenCalled();
  });

  it("hands over a run still running after the poll cap", async () => {
    api.getRun.mockResolvedValue(run("running"));
    const on = events();
    followRun("run-aaaaaaaa", on);
    for (let i = 0; i < MAX_RUN_POLLS; i += 1) await tick();
    expect(on.handedOver).toHaveBeenCalledWith("running");
    await tick();
    expect(api.getRun).toHaveBeenCalledTimes(MAX_RUN_POLLS);
  });
});

describe("how often a run is checked", () => {
  // A two-second poll added up to two seconds after every run had already
  // landed — on a recompute that now takes a few seconds, a third of the wait.
  it("checks twice a second, and still follows for two minutes", () => {
    expect(RUN_POLL_MS).toBeLessThanOrEqual(500);
    expect(RUN_POLL_MS * MAX_RUN_POLLS).toBe(120_000);
  });
});

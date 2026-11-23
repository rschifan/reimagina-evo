// @vitest-environment jsdom
import { flushSync, mount, unmount } from "svelte";
import { expect, it, vi } from "vitest";

import fixture from "./fixtures/run-12-units.json";
import type { RunDetailResponse, RunListResponse } from "./api";

vi.mock("./api", async (orig) => ({
  ...(await orig<typeof import("./api")>()),
  listRuns: vi.fn(),
  getRunDetail: vi.fn(),
  listAois: vi.fn(async () => []),
}));

const api = await import("./api");
const { default: App } = await import("./App.svelte");

const settle = () => new Promise((r) => setTimeout(r, 5));

// The boot `$effect` once read `filters`, `limit`, `openRunId` and `openRun`
// through `boot()`'s synchronous prefix, so every detail response of a
// running run re-ran it: a request storm at network speed instead of one
// read per poll tick.
it("an open running run is read once on boot, not in a loop", async () => {
  const page = {
    runs: [],
    executors: [],
    status_counts: {},
    total: 0,
    server_now: new Date().toISOString(),
  } as unknown as RunListResponse;
  const detail = fixture as unknown as RunDetailResponse;
  // Past a cap, never answer: a loop then stalls instead of exhausting the heap.
  const never = new Promise<never>(() => {});
  vi.mocked(api.listRuns).mockImplementation(() =>
    vi.mocked(api.listRuns).mock.calls.length > 20 ? never : Promise.resolve(page),
  );
  vi.mocked(api.getRunDetail).mockImplementation(() =>
    vi.mocked(api.getRunDetail).mock.calls.length > 20
      ? never
      : Promise.resolve({
          ...detail,
          run: { ...detail.run, status: "running" as const },
          server_now: page.server_now,
        }),
  );
  location.hash = `#/r/${detail.run.run_id}`;
  const target = document.body.appendChild(document.createElement("div"));
  const app = mount(App, { target });
  for (let i = 0; i < 10; i++) {
    await settle();
    flushSync();
  }
  unmount(app);
  // Boot plus the hash sync may each read once; a loop reads dozens of times.
  expect(vi.mocked(api.getRunDetail).mock.calls.length).toBeLessThanOrEqual(2);
  expect(vi.mocked(api.listRuns).mock.calls.length).toBeLessThanOrEqual(2);
});

// `ops.stale` compared `Date.now()`, which is not reactive, with
// `lastLoadedAt`, which changes only on a success: during an outage nothing
// re-evaluated it, so the badge written for that case never showed.
it("an outage of over a minute shows the stale badge", async () => {
  vi.useFakeTimers();
  try {
    location.hash = "#/";
    const page = {
      runs: [],
      executors: [],
      status_counts: {},
      total: 0,
      server_now: new Date().toISOString(),
    } as unknown as RunListResponse;
    vi.mocked(api.listRuns).mockReset().mockResolvedValueOnce(page).mockRejectedValue(new Error("Failed to fetch"));
    const target = document.body.appendChild(document.createElement("div"));
    const app = mount(App, { target });
    await vi.advanceTimersByTimeAsync(0);
    flushSync();
    expect(target.textContent).toContain("updated");
    expect(target.textContent).not.toContain("stale");
    await vi.advanceTimersByTimeAsync(65_000);
    flushSync();
    expect(target.textContent).toContain("stale");
    unmount(app);
  } finally {
    vi.useRealTimers();
  }
});

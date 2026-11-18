import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RunDetailResponse, RunListResponse } from "./api";

vi.mock("./api", async (orig) => ({
  ...(await orig<typeof import("./api")>()),
  listRuns: vi.fn(),
  getRunDetail: vi.fn(),
}));

const api = await import("./api");
const { loadMore, ops, setOpenRun } = await import("./store.svelte");

const listRuns = vi.mocked(api.listRuns);
const getRunDetail = vi.mocked(api.getRunDetail);
const page = {
  runs: [],
  executors: [],
  status_counts: {},
  total: 0,
  server_now: new Date().toISOString(),
} as unknown as RunListResponse;
const detail = (run_id: string, status: string) =>
  ({ run: { run_id, status }, units: [], server_now: page.server_now }) as unknown as RunDetailResponse;
const settle = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  listRuns.mockReset().mockResolvedValue(page);
  getRunDetail.mockReset();
});

describe("ops store polling", () => {
  it("keeps reading the list while a run is open, and stops re-reading a finished run", async () => {
    getRunDetail.mockResolvedValue(detail("A", "succeeded"));
    setOpenRun("A");
    await settle();
    // The health strip rides on the list; a detail route must not freeze it.
    expect(listRuns).toHaveBeenCalledTimes(1);
    expect(getRunDetail).toHaveBeenCalledTimes(1);

    loadMore(); // any re-load
    await settle();
    expect(listRuns).toHaveBeenCalledTimes(2);
    expect(getRunDetail).toHaveBeenCalledTimes(1);
  });

  it("does not show the previous run when the next run's detail fails", async () => {
    getRunDetail.mockRejectedValue(new Error("500 on /jobs/B/detail"));
    setOpenRun("B");
    await settle();
    expect(ops.openRun).toBeNull();
    // The list succeeded in the same tick; it must not erase the detail failure.
    expect(ops.error).toBe("500 on /jobs/B/detail");
    setOpenRun(null);
  });

  it("never asks for more than the route's limit ceiling", async () => {
    for (let i = 0; i < 6; i++) loadMore();
    await settle();
    expect(ops.limit).toBe(api.MAX_RUN_LIMIT);
    expect(listRuns.mock.lastCall?.[0]?.limit).toBe(api.MAX_RUN_LIMIT);
  });
});

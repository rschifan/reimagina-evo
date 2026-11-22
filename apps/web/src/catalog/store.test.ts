import { expect, it, vi } from "vitest";

vi.mock("./api", async (orig) => ({
  ...(await orig<typeof import("./api")>()),
  getStats: vi.fn(),
}));

const api = await import("./api");
const { statsFor } = await import("./store.svelte");

// Review: one 502 was cached for the life of the page, so the detail view of
// that indicator showed the error until a full reload.
it("a failed stats request is not cached", async () => {
  const stats = { count: 1 } as unknown as Awaited<ReturnType<typeof api.getStats>>;
  vi.mocked(api.getStats).mockRejectedValueOnce(new Error("502")).mockResolvedValue(stats);
  await expect(statsFor("x", "block")).rejects.toThrow("502");
  await expect(statsFor("x", "block")).resolves.toBe(stats);
});

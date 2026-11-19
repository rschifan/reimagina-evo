// The workspace store: what reaches the screen when requests overlap, fail,
// or answer for a selection that has since moved. Each test loads a fresh
// store, because the store is module state, as the Runs page's is.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listAois: vi.fn(),
  listReachableIndicators: vi.fn(),
  getIndicator: vi.fn(),
  listScenarios: vi.fn(),
  getIndicatorStats: vi.fn(),
  createScenario: vi.fn(),
  getScenario: vi.fn(),
  addScenarioEdits: vi.fn(),
  recomputeScenario: vi.fn(),
  deleteScenario: vi.fn(),
  getComparison: vi.fn(),
  getRun: vi.fn(),
  removeScenarioEdit: vi.fn(),
}));
vi.mock("../api", () => api);

const aoi = vi.hoisted(() => {
  const listeners = new Set<(row: unknown) => void>();
  let current: unknown = null;
  return {
    listeners,
    emit(row: unknown) {
      current = row;
      for (const fn of listeners) fn(row);
    },
    module: {
      setKnownAois: vi.fn(),
      getActiveAoiId: () => (current as { aoi_id?: string } | null)?.aoi_id ?? null,
      subscribe(fn: (row: unknown) => void) {
        listeners.add(fn);
        fn(current);
        return () => listeners.delete(fn);
      },
    },
  };
});
vi.mock("../aoi", () => aoi.module);

const PILOT = { aoi_id: "aoi-1", project_id: "proj-1", name: "Pilot" };
const OTHER_PILOT = { aoi_id: "aoi-2", project_id: "proj-1", name: "Other" };

const indicator = (slug: string, statistic: "sum" | "median") => ({
  slug,
  name: slug,
  unit: statistic === "sum" ? "units" : "floors",
  applies_to: ["block"],
  valid_min: 0,
  valid_max: 100,
  statistic,
});
const INDICATORS = [indicator("units", "sum"), indicator("floors", "median")];

const row = (id: string, o: Record<string, unknown> = {}) => ({
  id,
  project_id: "proj-1",
  aoi_id: "aoi-1",
  name: id,
  description: null,
  created_at: "2026-11-18T10:00:00Z",
  updated_at: "2026-11-18T10:00:00Z",
  materialized_at: "2026-11-18T11:00:00Z",
  edit_count: 4,
  latest_run: null,
  ...o,
});

const stats = (sum: number, p50: number) => ({
  count: 3, min: 0, p05: 0, p50, p95: 0, max: 0, sum, histogram: null,
});

const settle = () => vi.advanceTimersByTimeAsync(0);

type Store = typeof import("./store.svelte");
let store: Store;

async function load(): Promise<Store> {
  vi.resetModules();
  aoi.listeners.clear();
  aoi.emit(null);
  const mod = await import("./store.svelte");
  await mod.boot();
  aoi.emit(PILOT);
  await settle();
  return mod;
}

beforeEach(async () => {
  vi.useFakeTimers();
  vi.spyOn(console, "warn").mockImplementation(() => {});
  for (const fn of Object.values(api)) fn.mockReset();
  api.listAois.mockResolvedValue([PILOT, OTHER_PILOT]);
  api.listReachableIndicators.mockResolvedValue(INDICATORS);
  api.getIndicator.mockResolvedValue({ valid_max: 2000 });
  api.listScenarios.mockResolvedValue([row("A"), row("B")]);
  api.getIndicatorStats.mockImplementation(async (_slug: string, o: { scenario_id?: string }) =>
    o.scenario_id ? stats(10, 2) : stats(1, 1),
  );
  api.getScenario.mockResolvedValue({ edits: [] });
  store = await load();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("the table", () => {
  it("boots on the active pilot: indicators, scenarios, then one stats call per cell", () => {
    expect(store.ws.indicators.map((i) => i.slug)).toEqual(["units", "floors"]);
    expect(store.ws.scenarios.map((s) => s.id)).toEqual(["A", "B"]);
    // baseline + 2 scenarios, × 2 indicators
    expect(api.getIndicatorStats).toHaveBeenCalledTimes(6);
    expect(store.ws.cells.A.units).toEqual({ value: 10, error: null });
    expect(store.ws.cells.baseline.floors).toEqual({ value: 1, error: null });
  });

  it("opens on the first indicator a rule reaches, not on an empty map", () => {
    // An overlay with nothing picked is the one view indistinguishable from a
    // broken map — the map page learned that before this page existed.
    expect(store.ws.selectedId).toBeNull();
    expect(store.ws.slug).toBe("units");
  });

  it("cells are fetched with sum or median per the indicator's statistic", () => {
    expect(store.ws.cells.B.units.value).toBe(10); // sum
    expect(store.ws.cells.B.floors.value).toBe(2); // p50
  });

  it("with no active pilot it lists nothing and asks for no stats", async () => {
    api.listScenarios.mockClear();
    api.getIndicatorStats.mockClear();
    aoi.emit(null);
    await settle();
    expect(store.ws.scenarios).toEqual([]);
    expect(api.listScenarios).not.toHaveBeenCalled();
    expect(api.getIndicatorStats).not.toHaveBeenCalled();
    expect(store.ws.projectId).toBeNull();
  });

  it("a failed cell keeps its error and the rest of the table", async () => {
    api.getIndicatorStats.mockImplementation(async (slug: string, o: { scenario_id?: string }) => {
      if (o.scenario_id === "A" && slug === "units") throw new Error("boom");
      return stats(10, 2);
    });
    await store.refreshTable();
    expect(store.ws.cells.A.units).toEqual({ value: null, error: "boom" });
    expect(store.ws.cells.A.floors).toEqual({ value: 2, error: null });
    expect(store.ws.cells.B.units).toEqual({ value: 10, error: null });
  });
});

describe("a link opened cold", () => {
  it("keeps its route until the pilot's scenarios are known, then applies it", async () => {
    vi.resetModules();
    aoi.listeners.clear();
    aoi.emit(null);
    const cold = await import("./store.svelte");
    await cold.boot();
    cold.applyRoute({ scenarioId: "B", slug: "floors" });
    // Not yet applied: nothing may write the URL back from an empty selection.
    expect(cold.ws.routeReady).toBe(false);
    aoi.emit(PILOT);
    await settle();
    expect(cold.ws.selectedId).toBe("B");
    expect(cold.ws.slug).toBe("floors");
    expect(cold.ws.routeReady).toBe(true);
    // and the table still fills: every row, not only the one the link named
    expect(cold.ws.cells.baseline?.units).toEqual({ value: 1, error: null });
    expect(cold.ws.cells.A?.units).toEqual({ value: 10, error: null });
  });
});

describe("selection", () => {
  it("a route naming an unknown scenario selects nothing and says so", () => {
    store.applyRoute({ scenarioId: "gone", slug: null });
    expect(store.ws.selectedId).toBeNull();
    expect(store.ws.message?.tone).toBe("bad");
  });

  it("a route naming a known scenario selects it and its indicator", () => {
    store.applyRoute({ scenarioId: "B", slug: "floors" });
    expect(store.ws.selectedId).toBe("B");
    expect(store.ws.slug).toBe("floors");
  });

  it("switching scenario drops a late rule load for the previous one", async () => {
    let releaseA!: (v: unknown) => void;
    api.getScenario.mockImplementation((id: string) =>
      id === "A" ? new Promise((r) => (releaseA = r)) : Promise.resolve({ edits: [] }),
    );
    store.select("A");
    store.select("B");
    await settle();
    releaseA({
      edits: [
        { id: "e1", ordinal: 1, type_id: "t", attribute_key: "floors", value_numeric: 6,
          geometry_wkt: "POLYGON((0 0,1 0,1 1,0 0))", created_at: "" },
      ],
    });
    await settle();
    expect(store.ws.selectedId).toBe("B");
    expect(store.ws.rules).toEqual([]);
  });

  it("changing pilot clears the selection", async () => {
    store.select("A");
    aoi.emit(OTHER_PILOT);
    await settle();
    expect(store.ws.selectedId).toBeNull();
  });
});

describe("recompute", () => {
  const recomputed = (runId: string | null) => ({
    scenario_id: "A", matched_per_edit: { e1: 4 }, overrides_written: 4,
    overrides_removed: 0, results_superseded: 0, run_id: runId,
    run_status: "queued", deduplicated: false,
  });

  it("a run keeps being followed after a switch, and nothing about it reaches the selection", async () => {
    api.recomputeScenario.mockResolvedValue(recomputed("run-1"));
    api.getRun.mockResolvedValue({ run_id: "run-1", status: "succeeded", units: [] });
    const refreshed = vi.fn();
    store.onMapRefresh(refreshed);
    store.select("A");
    await store.recompute();
    expect(store.ws.following.A?.runId).toBe("run-1");
    store.select("B");
    store.ws.comparison = { counts: "B's", denominator: "d", shift: "s" };
    store.ws.message = { text: "about B", tone: "ok" };
    api.getIndicatorStats.mockClear();
    await vi.advanceTimersByTimeAsync(2_000);
    // Followed to the end: A's row is refreshed from its results ...
    expect(api.getRun).toHaveBeenCalled();
    expect(store.ws.following.A).toBeUndefined();
    expect(api.getIndicatorStats.mock.calls.map((c) => c[1].scenario_id)).toEqual(["A", "A"]);
    // ... and B, on screen, hears nothing of it.
    expect(refreshed).not.toHaveBeenCalled();
    expect(store.ws.comparison?.counts).toBe("B's");
    expect(store.ws.message?.text).toBe("about B");
  });

  it("a second recompute replaces the first run's follower", async () => {
    api.recomputeScenario
      .mockResolvedValueOnce(recomputed("run-1"))
      .mockResolvedValueOnce(recomputed("run-2"));
    api.getRun.mockImplementation(async (id: string) => ({ run_id: id, status: "running", units: [] }));
    store.select("A");
    await store.recompute();
    await store.recompute();
    api.getRun.mockClear();
    await vi.advanceTimersByTimeAsync(6_000);
    const followed = new Set(api.getRun.mock.calls.map((c) => c[0]));
    expect(followed).toEqual(new Set(["run-2"]));
    expect(store.ws.following.A?.runId).toBe("run-2");
  });

  it("a landed run refreshes that row, clears the comparison and fires onMapRefresh", async () => {
    api.recomputeScenario.mockResolvedValue(recomputed("run-1"));
    api.getRun.mockResolvedValue({ run_id: "run-1", status: "succeeded", units: [] });
    const refreshed = vi.fn();
    store.onMapRefresh(refreshed);
    store.select("A");
    store.ws.comparison = { counts: "c", denominator: "d", shift: "s" };
    await store.recompute();
    api.getIndicatorStats.mockClear();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(refreshed).toHaveBeenCalledTimes(1);
    expect(store.ws.comparison).toBeNull();
    expect(store.ws.following.A).toBeUndefined();
    const asked = api.getIndicatorStats.mock.calls.map((c) => c[1].scenario_id);
    expect(asked).toEqual(["A", "A"]);
  });

  it("a failed run leaves the overrides standing and says so", async () => {
    api.recomputeScenario.mockResolvedValue(recomputed("run-1"));
    api.getRun.mockResolvedValue({ run_id: "run-1", status: "failed", units: [] });
    store.select("A");
    await store.recompute();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(store.ws.message?.tone).toBe("bad");
    expect(store.ws.message?.text).toMatch(/overrides stand/);
  });
});

describe("authoring", () => {
  const AREA = {
    type: "Feature" as const,
    properties: {},
    geometry: { type: "Polygon" as const, coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] },
  };

  it("applyRule refuses locally with validateEdit's text before any request", async () => {
    store.select("A");
    await store.applyRule(AREA, { levels: -1, unitSize: 60, peoplePerHousehold: 3.6 });
    expect(api.addScenarioEdits).not.toHaveBeenCalled();
    expect(store.ws.message?.text).toMatch(/levels/);
  });

  it("removing a rule sends every DELETE to the scenario it belongs to, even after a switch", async () => {
    let releaseFirst!: () => void;
    api.removeScenarioEdit
      .mockReturnValueOnce(new Promise<void>((r) => (releaseFirst = r)))
      .mockResolvedValue(undefined);
    store.select("A");
    const removal = store.removeRuleGroup(["e1", "e2", "e3"]);
    store.select("B");
    releaseFirst();
    await removal;
    expect(api.removeScenarioEdit.mock.calls).toEqual([
      ["A", "e1"],
      ["A", "e2"],
      ["A", "e3"],
    ]);
  });
});

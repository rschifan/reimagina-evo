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
  updateRule: vi.fn(),
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

describe("refusals and scope", () => {
  const AREA = {
    type: "Feature" as const,
    properties: {},
    geometry: { type: "Polygon" as const, coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] },
  };

  it("apply refuses locally with validateEdit's text before any request", async () => {
    store.select("A");
    const ok = await store.applyNewRule(AREA, { levels: -1, unitSize: 60, peoplePerHousehold: 3.6, lotCoverage: 0.6 });
    expect(ok).toBe(false);
    expect(api.addScenarioEdits).not.toHaveBeenCalled();
    expect(store.ws.boxError).toEqual({ field: "levels", text: expect.stringMatching(/levels/) });
  });

  it("deleting a rule sends every DELETE to the scenario it belongs to, even after a switch", async () => {
    let releaseFirst!: () => void;
    api.removeScenarioEdit
      .mockReturnValueOnce(new Promise<void>((r) => (releaseFirst = r)))
      .mockResolvedValue(undefined);
    api.recomputeScenario.mockResolvedValue({
      scenario_id: "A", matched_per_edit: {}, overrides_written: 0, overrides_removed: 4,
      results_superseded: 0, run_id: null, run_status: "", deduplicated: false,
    });
    store.select("A");
    const removal = store.deleteRule({
      areaWkt: "POLYGON((0 0,1 0,1 1,0 0))", levels: 6, unitSize: 60, peoplePerHousehold: 3.6,
      lotCoverage: null, editIds: ["e1", "e2", "e3"], complete: true, ordinal: 1,
    });
    store.select("B");
    releaseFirst();
    await removal;
    expect(api.removeScenarioEdit.mock.calls).toEqual([["A", "e1"], ["A", "e2"], ["A", "e3"]]);
    expect(api.recomputeScenario).toHaveBeenCalledWith("A");
  });
});


describe("the default view", () => {
  it("is the change when the scenario has rules, the values otherwise", async () => {
    api.getScenario.mockResolvedValueOnce({
      edits: [{ id: "e1", ordinal: 1, type_id: "t", attribute_key: "num_levels", value_numeric: 3, geometry_wkt: "POLYGON((0 0,1 0,1 1,0 0))", created_at: "" }],
    });
    store.select("A");
    await settle();
    expect(store.ws.mode).toBe("delta");
    api.getScenario.mockResolvedValueOnce({ edits: [] });
    store.select("B");
    await settle();
    expect(store.ws.mode).toBe("value");
  });
});


describe("editing on the map", () => {
  const AREA = {
    type: "Feature" as const,
    properties: {},
    geometry: { type: "Polygon" as const, coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] },
  };
  const VALUES = { levels: 3, unitSize: 66, peoplePerHousehold: 3.6, lotCoverage: 0.6 };
  const written = (ids: string[]) => ({
    scenario_id: "A",
    feature_type: "MEX:block",
    edits: ids.map((id) => ({ id, attribute_key: "k", value_numeric: 1 })),
  });
  const recomputed = (matched: Record<string, number>, runId: string | null = "run-1") => ({
    scenario_id: "A", matched_per_edit: matched, overrides_written: 5,
    overrides_removed: 0, results_superseded: 0, run_id: runId,
    run_status: "queued", deduplicated: false,
  });
  // `load()` resets the module registry, so the class the store checks
  // `instanceof` against is the one imported after it, not one from the top
  // of this file.
  let ApiError: typeof import("../http").ApiError;
  beforeEach(async () => {
    ({ ApiError } = await import("../http"));
  });
  const ceiling = () => {
    const response = new Response(
      JSON.stringify({ detail: { error: "ceiling_exceeded", message: "housing units over 200000" } }),
      { status: 422 },
    );
    return new ApiError("422 Unprocessable Entity on /recompute", response);
  };
  const GROUP = {
    areaWkt: "POLYGON((0 0,1 0,1 1,0 0))",
    levels: 6,
    unitSize: 60,
    peoplePerHousehold: 3.6,
    lotCoverage: null,
    editIds: ["e1", "e2", "e3", "e4"],
    complete: true,
    ordinal: 1,
  };

  beforeEach(() => {
    api.addScenarioEdits.mockResolvedValue(written(["n1", "n2", "n3", "n4", "n5"]));
    api.recomputeScenario.mockResolvedValue(recomputed({ n1: 7, n2: 7, n3: 7, n4: 7, n5: 7 }));
    api.getRun.mockResolvedValue({ run_id: "run-1", status: "succeeded", units: [] });
    api.removeScenarioEdit.mockResolvedValue(undefined);
    api.updateRule.mockResolvedValue(written(GROUP.editIds));
  });

  const settleRun = async <T,>(p: Promise<T>): Promise<T> => {
    await vi.advanceTimersByTimeAsync(2_500);
    return p;
  };

  it("apply on the baseline creates the scenario, then the rule, then recomputes", async () => {
    api.createScenario.mockResolvedValue({ id: "N", name: "Scenario 3" });
    api.listScenarios.mockResolvedValue([row("A"), row("B"), row("N")]);
    store.select(null);
    const order: string[] = [];
    api.createScenario.mockImplementation(async () => (order.push("create"), { id: "N", name: "S" }));
    api.addScenarioEdits.mockImplementation(async () => (order.push("edits"), written(["n1"])));
    api.recomputeScenario.mockImplementation(async () => (order.push("recompute"), recomputed({ n1: 3 })));

    const ok = await settleRun(store.applyNewRule(AREA, VALUES, "Scenario 3"));

    expect(ok).toBe(true);
    expect(order).toEqual(["create", "edits", "recompute"]);
    expect(api.addScenarioEdits).toHaveBeenCalledWith("N", expect.objectContaining({ lot_coverage: 0.6, levels: 3 }));
    expect(store.ws.selectedId).toBe("N");
    expect(store.ws.mode).toBe("delta");
    expect(store.ws.busy).toBe(false);
  });

  it("a refused recompute undoes the new rule", async () => {
    store.select("A");
    api.recomputeScenario.mockRejectedValue(ceiling());

    const ok = await settleRun(store.applyNewRule(AREA, VALUES));

    expect(ok).toBe(false);
    expect(api.removeScenarioEdit.mock.calls.map((c) => c[1])).toEqual(["n1", "n2", "n3", "n4", "n5"]);
    expect(store.ws.boxError?.text).toMatch(/200000|ceiling/);
  });

  it("a refused recompute after creating the scenario deletes the scenario", async () => {
    store.select(null);
    api.createScenario.mockResolvedValue({ id: "N", name: "S" });
    api.recomputeScenario.mockRejectedValue(ceiling());
    api.deleteScenario.mockResolvedValue({ overrides: 0, results_superseded: 0 });

    await settleRun(store.applyNewRule(AREA, VALUES, "S"));

    expect(api.deleteScenario).toHaveBeenCalledWith("N");
    expect(store.ws.selectedId).toBeNull();
  });

  it("a rule that catches no block is undone and said so", async () => {
    store.select("A");
    api.recomputeScenario.mockResolvedValue(recomputed({ n1: 0, n2: 0, n3: 0, n4: 0, n5: 0 }, null));

    const ok = await settleRun(store.applyNewRule(AREA, VALUES));

    expect(ok).toBe(false);
    expect(api.removeScenarioEdit).toHaveBeenCalledTimes(5);
    expect(store.ws.boxError?.text).toMatch(/No blocks fall inside this area/);
  });

  it("save keeps the rule's edit ids", async () => {
    store.select("A");
    api.recomputeScenario.mockResolvedValue(recomputed({ e1: 4, e2: 4, e3: 4, e4: 4 }));
    const ok = await settleRun(store.saveRule(GROUP, AREA, VALUES));
    expect(ok).toBe(true);
    expect(api.updateRule).toHaveBeenCalledWith("A", expect.objectContaining({
      edit_ids: GROUP.editIds, area: AREA, levels: 3, lot_coverage: 0.6,
    }));
    expect(api.addScenarioEdits).not.toHaveBeenCalled();
  });

  it("a refused save writes the old numbers and area back", async () => {
    store.select("A");
    api.recomputeScenario.mockRejectedValue(ceiling());
    await settleRun(store.saveRule(GROUP, AREA, VALUES));
    expect(api.updateRule).toHaveBeenCalledTimes(2);
    const undo = api.updateRule.mock.calls[1][1];
    expect(undo).toMatchObject({ levels: 6, unit_size: 60, people_per_household: 3.6 });
    // The rule did not set coverage before, so the restore does not either.
    expect(undo).not.toHaveProperty("lot_coverage");
    expect(undo.area.type).toBe("Polygon");
    expect(store.ws.boxError).not.toBeNull();
  });

  it("delete removes every edit then recomputes", async () => {
    store.select("A");
    api.recomputeScenario.mockResolvedValue(recomputed({}, "run-1"));
    const ok = await settleRun(store.deleteRule(GROUP));
    expect(ok).toBe(true);
    expect(api.removeScenarioEdit.mock.calls.map((c) => c[1])).toEqual(GROUP.editIds);
    expect(api.recomputeScenario).toHaveBeenCalledWith("A");
  });

  it("a second apply while busy is ignored", async () => {
    store.select("A");
    const first = store.applyNewRule(AREA, VALUES);
    const second = store.applyNewRule(AREA, VALUES);
    expect(await second).toBe(false);
    await settleRun(first);
    expect(api.addScenarioEdits).toHaveBeenCalledTimes(1);
  });

  // Review: a switch mid-gesture left the box open on B with A's shape and
  // error, and the next Apply wrote to B. A gesture now holds the selection.
  it("the selection holds until the gesture ends", async () => {
    store.select("A");
    let release!: () => void;
    api.addScenarioEdits.mockImplementation(
      () => new Promise((r) => (release = () => r(written(["n1"])))),
    );
    api.recomputeScenario.mockResolvedValue(recomputed({ n1: 2 }));
    const pending = store.applyNewRule(AREA, VALUES);
    store.select("B");
    expect(store.ws.selectedId).toBe("A");
    release();
    await settleRun(pending);
    expect(api.recomputeScenario.mock.calls.every((c) => c[0] === "A")).toBe(true);
    store.select("B");
    expect(store.ws.selectedId).toBe("B");
  });

  // Review: the recompute had already withdrawn the rule's overrides at its
  // old place; writing the area back without recomputing left the data
  // saying the rule did nothing while its outline was back on the map.
  it("a moved rule that catches no block is put back and recomputed", async () => {
    store.select("A");
    api.recomputeScenario
      .mockResolvedValueOnce(recomputed({ e1: 0, e2: 0, e3: 0, e4: 0 }))
      .mockResolvedValueOnce(recomputed({ e1: 4, e2: 4, e3: 4, e4: 4 }));
    const ok = await settleRun(store.saveRule(GROUP, AREA, VALUES));
    expect(ok).toBe(false);
    expect(api.updateRule).toHaveBeenCalledTimes(2);
    expect(api.recomputeScenario).toHaveBeenCalledTimes(2);
    expect(store.ws.boxError?.text).toMatch(/No blocks fall inside this area/);
  });

  // Review: a failed undo was a console warning, leaving a refused rule half
  // in the database and undrawn.
  it("a refused rule whose undo fails says what is left and shows it", async () => {
    store.select("A");
    api.recomputeScenario.mockRejectedValue(ceiling());
    api.removeScenarioEdit
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error("network"));
    api.getScenario.mockClear();
    await settleRun(store.applyNewRule(AREA, VALUES));
    expect(store.ws.boxError?.text).toMatch(/1 of 5 .*removed.*delete it/i);
    expect(api.getScenario).toHaveBeenCalledWith("A");
  });

  it("a refused save whose restore fails says so and shows the rule", async () => {
    store.select("A");
    api.recomputeScenario.mockRejectedValue(ceiling());
    api.updateRule
      .mockResolvedValueOnce(written(GROUP.editIds))
      .mockRejectedValueOnce(new Error("network"));
    api.getScenario.mockClear();
    await settleRun(store.saveRule(GROUP, AREA, VALUES));
    expect(store.ws.boxError?.text).toMatch(/could not be put back/);
    expect(api.getScenario).toHaveBeenCalledWith("A");
  });

  it("a rule sends only the numbers it sets", async () => {
    store.select("A");
    api.addScenarioEdits.mockResolvedValue(written(["n1"]));
    api.recomputeScenario.mockResolvedValue(recomputed({ n1: 3 }));
    await settleRun(
      store.applyNewRule(AREA, { levels: null, unitSize: null, peoplePerHousehold: 5, lotCoverage: null }),
    );
    const body = api.addScenarioEdits.mock.calls[0][1];
    expect(body).toEqual({ area: AREA, people_per_household: 5 });
  });



  it("the headline reads homes and people from the table's cells", () => {
    store.ws.cells = {
      baseline: { edificability_block_housing_units: { value: 100, error: null }, edificability_block_population: { value: 300, error: null } },
      A: { edificability_block_housing_units: { value: 150, error: null } },
    };
    expect(store.headline("A")).toEqual({
      homes: { base: 100, now: 150 },
      people: { base: 300, now: null },
    });
    expect(store.headline(null).homes).toEqual({ base: 100, now: 100 });
  });
});

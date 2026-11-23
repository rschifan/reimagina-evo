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
  addRule: vi.fn(),
  recomputeScenario: vi.fn(),
  deleteScenario: vi.fn(),
  getComparison: vi.fn(),
  getRun: vi.fn(),
  deleteRule: vi.fn(),
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
      setActiveAoiId: vi.fn(),
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
  rule_count: 2,
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
  api.getScenario.mockResolvedValue({ rules: [] });
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

const RULE_ROW = (id: string, ordinal: number, inputs: Record<string, number> = { levels: 6 }) => ({
  id, ordinal, type_id: "t", inputs, geometry_wkt: "POLYGON((0 0,1 0,1 1,0 0))",
  created_at: "", updated_at: "",
});

describe("selection", () => {
  it("a route naming a scenario that is gone selects nothing and says so", async () => {
    api.getScenario.mockRejectedValueOnce(new Error("404"));
    store.applyRoute({ scenarioId: "gone", slug: null });
    await settle();
    expect(store.ws.selectedId).toBeNull();
    expect(store.ws.message?.tone).toBe("bad");
  });

  it("a route naming another pilot's scenario switches to that pilot and selects it", async () => {
    // A shared link carries the scenario, not the reader's last pilot.
    api.getScenario.mockResolvedValueOnce({ id: "C", aoi_id: "aoi-2", rules: [] });
    api.listScenarios.mockResolvedValue([row("C", { aoi_id: "aoi-2" })]);
    store.applyRoute({ scenarioId: "C", slug: null });
    await settle();
    expect(aoi.module.setActiveAoiId).toHaveBeenCalledWith("aoi-2");
    aoi.emit(OTHER_PILOT);
    await settle();
    expect(store.ws.selectedId).toBe("C");
  });

  it("a route naming a known scenario selects it and its indicator", () => {
    store.applyRoute({ scenarioId: "B", slug: "floors" });
    expect(store.ws.selectedId).toBe("B");
    expect(store.ws.slug).toBe("floors");
  });

  it("switching scenario drops a late rule load for the previous one", async () => {
    let releaseA!: (v: unknown) => void;
    api.getScenario.mockImplementation((id: string) =>
      id === "A" ? new Promise((r) => (releaseA = r)) : Promise.resolve({ rules: [] }),
    );
    store.select("A");
    store.select("B");
    await settle();
    releaseA({ rules: [RULE_ROW("r1", 1)] });
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


describe("deleting a scenario", () => {
  const DELETED = {
    scenario_id: "B", overrides: 4, versions_deleted: 1, results_deleted: 9,
    job_runs_deleted: 1, materializations: 1, runs_deleted: 2,
  };

  it("deletes the row it names, not only the selected one, and keeps the selection", async () => {
    api.deleteScenario.mockResolvedValue(DELETED);
    store.select("A");
    api.listScenarios.mockResolvedValue([row("A")]);
    await store.removeScenario("B");
    expect(api.deleteScenario).toHaveBeenCalledWith("B");
    expect(store.ws.selectedId).toBe("A");
    expect(store.ws.scenarios.map((s) => s.id)).toEqual(["A"]);
  });

  it("deleting the selected scenario goes back to the baseline", async () => {
    api.deleteScenario.mockResolvedValue({ ...DELETED, scenario_id: "A" });
    store.select("A");
    await store.removeScenario("A");
    expect(store.ws.selectedId).toBeNull();
  });
});

describe("the default view", () => {
  it("is the change when the scenario has rules, the values otherwise", async () => {
    api.getScenario.mockResolvedValueOnce({ rules: [RULE_ROW("r1", 1)] });
    store.select("A");
    await settle();
    expect(store.ws.mode).toBe("delta");
    expect(store.ws.rules.map((r) => r.id)).toEqual(["r1"]);
    api.getScenario.mockResolvedValueOnce({ rules: [] });
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
  const VALUES = { levels: 3, unit_size: 66, people_per_household: 3.6, lot_coverage: 0.6 };
  const changed = (runId: string | null = "run-1", ruleId: string | null = "r9") => ({
    scenario_id: "A", matched_per_rule: ruleId ? { [ruleId]: 4 } : {}, overrides_written: 5,
    overrides_removed: 0, results_superseded: 0, run_id: runId,
    run_status: "queued", deduplicated: false,
    rule: ruleId ? RULE_ROW(ruleId, 3) : null,
  });
  // `load()` resets the module registry, so the class the store checks
  // `instanceof` against is the one imported after it, not one from the top
  // of this file.
  let ApiError: typeof import("../http").ApiError;
  beforeEach(async () => {
    ({ ApiError } = await import("../http"));
  });
  const refusal = (status: number, detail: Record<string, unknown>) =>
    new ApiError(`${status} on /rules`, new Response(JSON.stringify({ detail }), { status }));
  const ceiling = () =>
    refusal(422, {
      error: "ceiling_exceeded", breach_count: 2,
      message: "Rule 3 is too much for 2 blocks: at most 5 storeys fit.",
    });
  const RULE = {
    id: "r1", ordinal: 1, areaWkt: "POLYGON((0 0,1 0,1 1,0 0))",
    inputs: { levels: 6, unit_size: 60, people_per_household: 3.6 },
  };

  beforeEach(() => {
    api.addRule.mockResolvedValue(changed());
    api.updateRule.mockResolvedValue(changed("run-1", "r1"));
    api.deleteRule.mockResolvedValue(changed("run-1", null));
    api.recomputeScenario.mockResolvedValue(changed("run-1", null));
    api.getRun.mockResolvedValue({ run_id: "run-1", status: "succeeded", units: [] });
  });

  const settleRun = async <T,>(p: Promise<T>): Promise<T> => {
    await vi.advanceTimersByTimeAsync(2_500);
    return p;
  };

  it("apply refuses locally with validateEdit's text before any request", async () => {
    store.select("A");
    const ok = await store.applyNewRule(AREA, { ...VALUES, levels: -1 });
    expect(ok).toBe(false);
    expect(api.addRule).not.toHaveBeenCalled();
    expect(store.ws.boxError).toEqual({ field: "levels", text: expect.stringMatching(/levels/) });
  });

  it("apply on the baseline creates the scenario with its rule in one request, then follows its run", async () => {
    api.listScenarios.mockResolvedValue([row("A"), row("B"), row("N")]);
    store.select(null);
    api.createScenario.mockResolvedValue({
      id: "N", name: "S", rules: [], first_rule: { ...changed(), scenario_id: "N" },
    });

    const ok = await settleRun(store.applyNewRule(AREA, VALUES, "Scenario 3"));

    expect(ok).toBe(true);
    expect(api.createScenario).toHaveBeenCalledWith(
      expect.objectContaining({
        aoi_id: "aoi-1",
        name: "Scenario 3",
        rule: {
          area: AREA,
          inputs: { levels: 3, unit_size: 66, people_per_household: 3.6, lot_coverage: 0.6 },
          units: { levels: "set", unit_size: "set", people_per_household: "set", lot_coverage: "set" },
        },
      }),
    );
    expect(api.addRule).not.toHaveBeenCalled();
    expect(api.getRun).toHaveBeenCalledWith("run-1");
    expect(store.ws.selectedId).toBe("N");
    expect(store.ws.mode).toBe("delta");
    expect(store.ws.busy).toBe(false);
  });

  it("a refused rule stored nothing, so nothing is undone and the box says why", async () => {
    store.select("A");
    api.addRule.mockRejectedValue(ceiling());

    const ok = await settleRun(store.applyNewRule(AREA, VALUES));

    expect(ok).toBe(false);
    expect(api.deleteRule).not.toHaveBeenCalled();
    expect(api.recomputeScenario).not.toHaveBeenCalled();
    expect(store.ws.boxError?.text).toMatch(/Not saved.*Rule 3 is too much for 2 blocks: at most 5 storeys/);
  });

  it("the field a refusal names is the one the box marks", async () => {
    store.select("A");
    api.addRule.mockRejectedValue(
      refusal(400, { error: "invalid_value", field: "unit_size", message: "unit_size must be more than 15" }),
    );
    await settleRun(store.applyNewRule(AREA, VALUES));
    expect(store.ws.boxError?.field).toBe("unit_size");
  });

  it("a refused first rule left no scenario, so nothing is deleted and the box says why", async () => {
    store.select(null);
    api.createScenario.mockRejectedValue(ceiling());

    const ok = await settleRun(store.applyNewRule(AREA, VALUES, "S"));

    expect(ok).toBe(false);
    expect(api.deleteScenario).not.toHaveBeenCalled();
    expect(store.ws.selectedId).toBeNull();
    expect(store.ws.boxError?.text).toMatch(/2 blocks/);
  });

  // B4: a 500 after the commit (or a lost response) used to delete the rule
  // from the map while its values stayed in the database.
  it("a rule whose recompute was not queued stands, and the page says so", async () => {
    store.select("A");
    await settle();
    api.addRule.mockRejectedValue(
      refusal(500, {
        error: "recompute_not_enqueued",
        materialized: { overrides_written: 8 },
        message: "materialised and committed",
      }),
    );
    api.getScenario.mockClear();
    api.getScenario.mockResolvedValue({ rules: [RULE_ROW("r9", 3)] });

    const ok = await settleRun(store.applyNewRule(AREA, VALUES));

    expect(ok).toBe(true);
    expect(api.deleteRule).not.toHaveBeenCalled();
    expect(api.getScenario).toHaveBeenCalledWith("A");
    expect(store.ws.rules.map((r) => r.id)).toEqual(["r9"]);
    expect(store.ws.message?.text).toMatch(/8 overrides were written.*Recompute/);
  });

  it("a written, saved or deleted rule is spliced from the response, not read back", async () => {
    api.getScenario.mockResolvedValue({ rules: [RULE_ROW("r1", 1)] });
    store.select("A");
    await settle();
    api.getScenario.mockClear();
    api.listScenarios.mockClear();

    await settleRun(store.applyNewRule(AREA, VALUES));
    expect(store.ws.rules.map((r) => r.id)).toEqual(["r1", "r9"]);

    api.updateRule.mockResolvedValue({ ...changed("run-1", null), rule: RULE_ROW("r1", 1, { levels: 4 }) });
    await settleRun(store.saveRule(RULE, null, { ...VALUES, levels: 4 }));
    expect(store.ws.rules.map((r) => [r.id, r.inputs.levels])).toEqual([["r1", 4], ["r9", 6]]);

    await settleRun(store.deleteRule(RULE));
    expect(store.ws.rules.map((r) => r.id)).toEqual(["r9"]);

    expect(api.getScenario).not.toHaveBeenCalled();
    // One list read per gesture: when its run lands.
    expect(api.listScenarios).toHaveBeenCalledTimes(3);
  });

  it("a change rule is sent with each number's unit, and its negative numbers are not refused locally", async () => {
    store.select("A");
    await settleRun(
      store.applyNewRule(AREA, { levels: -1, unit_size: -10 }, undefined, { levels: "abs", unit_size: "pct" }),
    );
    expect(api.addRule.mock.calls[0][1]).toEqual({
      area: AREA,
      inputs: { levels: -1, unit_size: -10 },
      units: { levels: "abs", unit_size: "pct" },
    });
  });

  it("save sends the rule's id and its whole set of numbers", async () => {
    store.select("A");
    const ok = await settleRun(store.saveRule(RULE, AREA, VALUES));
    expect(ok).toBe(true);
    // Every number's unit travels with an edit: "set" turns a change back.
    expect(api.updateRule).toHaveBeenCalledWith("A", "r1", {
      area: AREA,
      inputs: { levels: 3, unit_size: 66, people_per_household: 3.6, lot_coverage: 0.6 },
      units: { levels: "set", unit_size: "set", people_per_household: "set", lot_coverage: "set" },
    });
  });

  it("a refused save changed nothing: one request, and the box says why", async () => {
    store.select("A");
    api.updateRule.mockRejectedValue(ceiling());
    const ok = await settleRun(store.saveRule(RULE, AREA, VALUES));
    expect(ok).toBe(false);
    expect(api.updateRule).toHaveBeenCalledTimes(1);
    expect(store.ws.boxError).not.toBeNull();
  });

  it("delete is one request for the rule, to the scenario it belongs to", async () => {
    store.select("A");
    const ok = await settleRun(store.deleteRule(RULE));
    expect(ok).toBe(true);
    expect(api.deleteRule.mock.calls).toEqual([["A", "r1"]]);
  });

  it("a delete the server refuses leaves the rule and says why", async () => {
    store.select("A");
    api.deleteRule.mockRejectedValue(ceiling());
    const ok = await settleRun(store.deleteRule(RULE));
    expect(ok).toBe(false);
    expect(store.ws.boxError?.text).toMatch(/too dense/);
  });

  // B6: the table's row was read once, while the run was still queued, and
  // kept saying "Computing" (or the last run's "Failed") after it landed.
  it("the scenario list is read again when the run lands", async () => {
    store.select("A");
    await settle();
    api.listScenarios.mockClear();
    let land!: () => void;
    api.getRun.mockImplementation(
      () => new Promise((r) => (land = () => r({ run_id: "run-1", status: "succeeded", units: [] }))),
    );
    const pending = store.applyNewRule(AREA, VALUES);
    await vi.advanceTimersByTimeAsync(600);
    const before = api.listScenarios.mock.calls.length;
    land();
    await settleRun(pending);
    expect(api.listScenarios.mock.calls.length).toBeGreaterThan(before);
  });

  it("a partial run is not reported as a clean success", async () => {
    store.select("A");
    api.getRun.mockResolvedValue({ run_id: "run-1", status: "partial", units: [] });
    await settleRun(store.applyNewRule(AREA, VALUES));
    expect(store.ws.message?.tone).toBe("bad");
    expect(store.ws.message?.text).toMatch(/Some indicators failed/);
  });

  it("recompute posts once and follows the run", async () => {
    store.select("A");
    await settleRun(store.recomputeSelected());
    expect(api.recomputeScenario).toHaveBeenCalledWith("A");
    expect(api.getRun).toHaveBeenCalledWith("run-1");
    expect(store.ws.busy).toBe(false);
  });

  it("a second apply while busy is ignored", async () => {
    store.select("A");
    const first = store.applyNewRule(AREA, VALUES);
    const second = store.applyNewRule(AREA, VALUES);
    expect(await second).toBe(false);
    await settleRun(first);
    expect(api.addRule).toHaveBeenCalledTimes(1);
  });

  // Review: a switch mid-gesture left the box open on B with A's shape and
  // error, and the next Apply wrote to B. A gesture now holds the selection.
  it("the selection holds until the gesture ends", async () => {
    store.select("A");
    let release!: () => void;
    api.addRule.mockImplementation(() => new Promise((r) => (release = () => r(changed()))));
    const pending = store.applyNewRule(AREA, VALUES);
    store.select("B");
    expect(store.ws.selectedId).toBe("A");
    release();
    await settleRun(pending);
    expect(api.addRule.mock.calls.every((c) => c[0] === "A")).toBe(true);
    store.select("B");
    expect(store.ws.selectedId).toBe("B");
  });

  // Review: a pilot switch mid-recompute said "Scenario cleared" but kept A
  // selected, so A's rules landed on the other pilot's map and the next rule
  // was written into A. A pilot switch clears even a gesture's selection.
  it("a pilot switch mid-recompute clears the selection", async () => {
    store.select("A");
    await settle();
    const pending = store.applyNewRule(AREA, VALUES);
    await settle();
    expect(store.ws.busy).toBe(true);
    aoi.emit(OTHER_PILOT);
    await settle();
    expect(store.ws.selectedId).toBeNull();
    await settleRun(pending);
    expect(store.ws.selectedId).toBeNull();
  });

  it("a rule sends only the numbers it sets", async () => {
    store.select("A");
    await settleRun(
      store.applyNewRule(AREA, { people_per_household: 5 }),
    );
    expect(api.addRule.mock.calls[0][1]).toEqual({
      area: AREA,
      inputs: { people_per_household: 5 },
      units: { people_per_household: "set" },
    });
  });
});

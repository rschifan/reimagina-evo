// @vitest-environment jsdom
//
// The panel's run polling, driven through the real DOM with the API, the AOI
// store and the selection controller replaced. `scenario.test.ts` covers the
// pure helpers; what is here is behaviour that only exists across awaits and
// timers, where an ordering mistake is invisible to a unit test of a function.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  addScenarioEdits: vi.fn(),
  createScenario: vi.fn(),
  deleteScenario: vi.fn(),
  getComparison: vi.fn(),
  getIndicator: vi.fn(),
  getRun: vi.fn(),
  getScenario: vi.fn(),
  listScenarios: vi.fn(),
  recomputeScenario: vi.fn(),
  removeScenarioEdit: vi.fn(),
}));
vi.mock("./api", () => api);
vi.mock("./aoi", () => ({
  subscribe: (listener: (aoi: unknown) => void) => {
    listener({ aoi_id: "aoi-1", project_id: "proj-1" });
    return () => {};
  },
}));
vi.mock("./selection", () => ({
  enableSelection: () => ({ subscribe: () => () => {} }),
}));

import { enableScenarioPanel } from "./scenario";

const scenarioRow = (id: string, name: string) => ({
  id,
  project_id: "proj-1",
  aoi_id: "aoi-1",
  name,
  description: null,
  created_at: "",
  updated_at: "",
  materialized_at: null,
});

const recomputed = (runId: string | null = "run-aaaaaaaa") => ({
  scenario_id: "A",
  matched_per_edit: { e1: 4 },
  overrides_written: 4,
  overrides_removed: 0,
  results_superseded: 0,
  run_id: runId,
  run_status: "queued",
  deduplicated: false,
});

const run = (status: string) => ({ id: "run-aaaaaaaa", status, units: [] });

const POLL_MS = 2_000;

let host: HTMLElement;
let onScenarioChange: ReturnType<typeof vi.fn<(scenarioId: string | null) => void>>;
let dispose: () => void;

const part = (name: string) => host.querySelector(`[data-part="${name}"]`) as HTMLElement;
const click = (action: string) =>
  (host.querySelector(`[data-action="${action}"]`) as HTMLButtonElement).click();
const message = () => part("message").textContent ?? "";

/** Let queued promise callbacks run without moving the clock. */
const settle = () => vi.advanceTimersByTimeAsync(0);

function pick(id: string): void {
  const select = host.querySelector('[data-field="scenario"]') as HTMLSelectElement;
  select.value = id;
  select.dispatchEvent(new Event("change"));
}

beforeEach(async () => {
  vi.useFakeTimers();
  vi.spyOn(console, "warn").mockImplementation(() => {});
  for (const fn of Object.values(api)) fn.mockReset();
  api.getIndicator.mockResolvedValue({ valid_max: 2000 });
  api.listScenarios.mockResolvedValue([scenarioRow("A", "Alpha"), scenarioRow("B", "Beta")]);
  api.getScenario.mockResolvedValue({ edits: [] });
  host = document.createElement("div");
  document.body.appendChild(host);
  onScenarioChange = vi.fn<(scenarioId: string | null) => void>();
  const controller = enableScenarioPanel({} as never, {
    onScenarioChange,
    host,
    activeIndicatorSlug: () => "some_indicator",
    activeIndicatorUnit: () => null,
  });
  dispose = controller.dispose;
  await settle();
  pick("A");
  await settle();
  onScenarioChange.mockClear();
});

afterEach(() => {
  dispose();
  host.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("recompute polling", () => {
  // ⚠ Without the guard the old scenario's run is followed, and when it lands
  // the panel redraws the map for whichever scenario is showing by then.
  it("does not follow a run whose scenario is no longer the active one", async () => {
    let resolveRecompute!: (value: unknown) => void;
    api.recomputeScenario.mockReturnValue(new Promise((r) => (resolveRecompute = r)));
    api.getRun.mockResolvedValue(run("succeeded"));

    click("recompute");
    await settle();
    pick("B");
    await settle();
    onScenarioChange.mockClear();
    const before = message();

    resolveRecompute(recomputed());
    await vi.advanceTimersByTimeAsync(POLL_MS * 3);

    expect(api.getRun).not.toHaveBeenCalled();
    expect(onScenarioChange).not.toHaveBeenCalled();
    expect(message()).toBe(before);
  });

  it("keeps following the run when the scenario has not changed", async () => {
    api.recomputeScenario.mockResolvedValue(recomputed());
    api.getRun.mockResolvedValue(run("succeeded"));

    click("recompute");
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(api.getRun).toHaveBeenCalledWith("run-aaaaaaaa");
    expect(onScenarioChange).toHaveBeenCalledWith("A");
  });

  // One network blip is not a lost run.
  it("survives one unreadable poll and redraws when the next one lands", async () => {
    api.recomputeScenario.mockResolvedValue(recomputed());
    api.getRun
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValue(run("succeeded"));

    click("recompute");
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(onScenarioChange).not.toHaveBeenCalled();
    expect(message()).not.toMatch(/could not be read/);

    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(api.getRun).toHaveBeenCalledTimes(2);
    expect(onScenarioChange).toHaveBeenCalledWith("A");
    expect(message()).toMatch(/succeeded/);
  });

  it("hands over to the dashboard only after the polls keep failing", async () => {
    api.recomputeScenario.mockResolvedValue(recomputed());
    api.getRun.mockRejectedValue(new TypeError("Failed to fetch"));

    click("recompute");
    await vi.advanceTimersByTimeAsync(POLL_MS * 10);

    expect(api.getRun.mock.calls.length).toBeGreaterThan(1);
    expect(api.getRun.mock.calls.length).toBeLessThanOrEqual(5);
    expect(message()).toMatch(/could not be read.*run dashboard/);
    expect(onScenarioChange).not.toHaveBeenCalled();
    // The overrides were written; the message must not say the recompute failed.
    expect(message()).toMatch(/4 overrides written/);
  });

  it("reports a failed run as a failed run, with the overrides standing", async () => {
    api.recomputeScenario.mockResolvedValue(recomputed());
    api.getRun.mockResolvedValue(run("failed"));

    click("recompute");
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(message()).toMatch(/The overrides stand/);
    expect(part("message").dataset.tone).toBe("bad");
    expect(onScenarioChange).not.toHaveBeenCalled();
  });

  // The strip says "N changed" about the previous run's numbers. Beside fresh
  // results it reads as current.
  it("clears a previous comparison when a new run lands", async () => {
    api.getComparison.mockResolvedValue({
      scenario_id: "A",
      indicator_slug: "some_indicator",
      aoi_id: "aoi-1",
      summary: { changed: 16, unchanged: 0, new: 0, mean_delta: 1, higher_is_better: true },
      row_count: 16,
      rows: [],
      rows_truncated: false,
    });
    click("compare");
    await settle();
    expect(part("comparison").hidden).toBe(false);
    expect(part("comparison").textContent).toMatch(/16 changed/);

    api.recomputeScenario.mockResolvedValue(recomputed());
    api.getRun.mockResolvedValue(run("succeeded"));
    click("recompute");
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(part("comparison").hidden).toBe(true);
    expect(part("comparison").textContent).toBe("");
  });
});

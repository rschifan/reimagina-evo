import { describe, expect, it } from "vitest";

import type { IndicatorValueStats, ReachableIndicator, ScenarioListRow } from "../api";
import {
  cellValue,
  formatCell,
  noValuesNote,
  formatHash,
  parseHash,
  statusFor,
  statusLabel,
} from "./derive";

const row = (o: Partial<ScenarioListRow> = {}): ScenarioListRow => ({
  id: "A",
  project_id: "p",
  aoi_id: "a",
  name: "A",
  description: null,
  created_at: "2026-11-18T10:00:00Z",
  updated_at: "2026-11-18T10:00:00Z",
  materialized_at: null,
  edit_count: 4,
  latest_run: null,
  ...o,
});

const stats = (o: Partial<IndicatorValueStats> = {}): IndicatorValueStats => ({
  count: 0,
  min: null,
  p05: null,
  p50: null,
  p95: null,
  max: null,
  sum: null,
  histogram: null,
  ...o,
});

const indicator = (statistic: "sum" | "median"): ReachableIndicator => ({
  slug: "x",
  name: "X",
  description: null,
  model_family: null,
  unit: statistic === "sum" ? "units" : "units_per_ha",
  applies_to: ["block"],
  valid_min: 0,
  valid_max: 1,
  cap_value: null,
  statistic,
});

const DONE = "2026-11-18T11:00:00Z";

describe("statusFor", () => {
  it("no rules beats everything", () => {
    expect(statusFor(row({ edit_count: 0 }), null)).toBe("no-rules");
  });

  it("never materialised is not computed", () => {
    expect(statusFor(row(), null)).toBe("not-computed");
  });

  it("rules edited after materialising are stale", () => {
    expect(statusFor(row({ materialized_at: "2026-11-18T09:00:00Z" }), null)).toBe("stale");
  });

  it("a failed latest run is failed even though materialised", () => {
    expect(
      statusFor(row({ materialized_at: DONE, latest_run: { id: "r", status: "failed" } }), null),
    ).toBe("failed");
  });

  it("the followed run wins over the row's latest run", () => {
    expect(
      statusFor(row({ materialized_at: DONE, latest_run: { id: "r", status: "failed" } }), {
        status: "running",
      }),
    ).toBe("computing");
  });

  it("a queued followed run is queued", () => {
    expect(statusFor(row({ materialized_at: DONE }), { status: "queued" })).toBe("queued");
  });

  it("a running latest run is computing after a reload", () => {
    expect(
      statusFor(row({ materialized_at: DONE, latest_run: { id: "r", status: "running" } }), null),
    ).toBe("computing");
  });

  it("materialised with a good run is up to date", () => {
    expect(
      statusFor(row({ materialized_at: DONE, latest_run: { id: "r", status: "succeeded" } }), null),
    ).toBe("up-to-date");
  });

  it("a partial latest run says some indicators failed, not up to date", () => {
    expect(
      statusFor(row({ materialized_at: DONE, latest_run: { id: "r", status: "partial" } }), null),
    ).toBe("partial");
    expect(statusLabel("partial")).toBe("Partly failed");
  });

  it("a run being cancelled is still in flight", () => {
    expect(
      statusFor(row({ materialized_at: DONE, latest_run: { id: "r", status: "cancelling" } }), null),
    ).toBe("computing");
  });

  it("every status has a label", () => {
    expect(statusLabel("stale")).toBe("Rules changed — recompute");
    expect(statusLabel("no-rules")).toBe("No rules yet");
  });
});

describe("cells", () => {
  it("a cell with no values is null, not zero", () => {
    expect(cellValue(stats(), "sum")).toBeNull();
    expect(cellValue(stats(), "median")).toBeNull();
    expect(cellValue(null, "sum")).toBeNull();
  });

  it("sum and median read different fields", () => {
    const s = stats({ count: 3, sum: 30, p50: 7 });
    expect(cellValue(s, "sum")).toBe(30);
    expect(cellValue(s, "median")).toBe(7);
  });

  it("noValuesNote names the missing inputs on the baseline", () => {
    // Every indicator this page offers is computed from these three; a blank
    // baseline means they are absent, not that anything failed.
    expect(noValuesNote("Planned Floors", false)).toBe(
      "Planned Floors has no baseline values here: these blocks carry no storeys, " +
        "dwelling size or household size. A scenario's rules supply them inside the " +
        "areas they cover.",
    );
    expect(noValuesNote("Planned Floors", true)).toBe(
      "Planned Floors has no values in this scenario here yet.",
    );
  });

  it("formatCell shows a dash for null", () => {
    expect(formatCell(null, indicator("median"))).toBe("—");
  });

  it("a total is a whole number", () => {
    expect(formatCell(12400.4, indicator("sum"))).toBe((12400).toLocaleString());
  });
});

describe("the hash", () => {
  it("round-trips", () => {
    expect(parseHash(formatHash("A", "edificability_block_density"))).toEqual({
      scenarioId: "A",
      slug: "edificability_block_density",
    });
  });

  it("an empty hash selects nothing", () => {
    expect(parseHash("")).toEqual({ scenarioId: null, slug: null });
    expect(parseHash("#/")).toEqual({ scenarioId: null, slug: null });
  });

  it("a scenario without an indicator", () => {
    expect(parseHash(formatHash("A", null))).toEqual({ scenarioId: "A", slug: null });
  });
});

describe("statusTone", () => {
  it("a failed scenario reads as an error, a current one as fresh", async () => {
    const { statusTone } = await import("./derive");
    expect(statusTone("failed")).toBe("error");
    expect(statusTone("up-to-date")).toBe("fresh");
    expect(statusTone("partial")).toBe("partial");
  });
});

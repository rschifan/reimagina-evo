import { render } from "svelte/server";
import { describe, expect, it } from "vitest";

import type { ExecutorRow } from "./api";
import HealthStrip from "./HealthStrip.svelte";

function strip(ages: number[]): string {
  const executors = ages.map(
    (heartbeat_age_s, i) =>
      ({ executor_id: `host:${i}`, role: "worker", heartbeat_age_s }) as ExecutorRow,
  );
  return render(HealthStrip, { props: { executors, runs: [] } }).body;
}

describe("HealthStrip", () => {
  it("says nothing will run when the only executor row is stale", () => {
    // A process that exited leaves its row; a row is not a live worker.
    expect(strip([120])).toContain("nothing will run until a worker is started");
    expect(strip([120])).toContain("no live executor");
    expect(strip([])).toContain("no executor has ever reported");
  });

  it("stays quiet while an executor is beating", () => {
    expect(strip([5])).not.toContain("nothing will run");
  });
});

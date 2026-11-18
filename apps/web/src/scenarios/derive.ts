/**
 * What the scenario workspace shows, derived from what the API answers.
 *
 * Pure: no DOM, no fetch, no runes. The store calls these and the components
 * render their results, so every rule about what a planner sees — which
 * status a scenario is in, what a table cell says, where a link points — is
 * tested here without a browser.
 */
import type { IndicatorValueStats, ReachableIndicator, ScenarioListRow } from "../api";
import { fmtNumber } from "../format";

export type ScenarioStatus =
  | "no-rules"
  | "not-computed"
  | "stale"
  | "queued"
  | "computing"
  | "failed"
  | "up-to-date";

const LABELS: Record<ScenarioStatus, string> = {
  "no-rules": "No rules yet",
  "not-computed": "Not computed",
  stale: "Rules changed — recompute",
  queued: "Queued",
  computing: "Computing",
  failed: "Failed",
  "up-to-date": "Up to date",
};

export function statusLabel(status: ScenarioStatus): string {
  return LABELS[status];
}

/** A run status as the table names it, or null when it says nothing new. */
function fromRun(status: string): ScenarioStatus | null {
  if (status === "queued") return "queued";
  if (status === "running") return "computing";
  if (status === "failed" || status === "cancelled") return "failed";
  return null;
}

/**
 * The one status a scenario row shows.
 *
 * In order: a scenario with no rules has nothing else to say; a run the page
 * is following is fresher than anything the list knew; rules edited since the
 * last materialisation make every number stale; never materialised is not
 * computed. Only then does the newest run speak — and it has to, because a
 * failed recompute still sets `materialized_at`.
 */
export function statusFor(
  row: ScenarioListRow,
  followed: { status: string } | null,
): ScenarioStatus {
  if (row.edit_count === 0) return "no-rules";
  const live = followed ? fromRun(followed.status) : null;
  if (live) return live;
  if (row.materialized_at === null) return "not-computed";
  if (Date.parse(row.updated_at) > Date.parse(row.materialized_at)) return "stale";
  const last = row.latest_run ? fromRun(row.latest_run.status) : null;
  return last ?? "up-to-date";
}

/** The number a table cell shows, or null when there are no values.
 *
 *  ⚠ Null, never 0: a scenario that has not computed an indicator has no
 *  total, and a 0 would read as "this scenario allows no dwellings". */
export function cellValue(
  stats: IndicatorValueStats | null,
  statistic: "sum" | "median",
): number | null {
  if (!stats || stats.count === 0) return null;
  return statistic === "sum" ? stats.sum : stats.p50;
}

/** A total is a count of dwellings or people, so a whole number; a median
 *  keeps the adaptive precision the rest of the app uses. */
export function formatCell(value: number | null, indicator: ReachableIndicator): string {
  if (value === null) return "—";
  return indicator.statistic === "sum" ? Math.round(value).toLocaleString() : fmtNumber(value);
}

/** `#/<scenario>/<indicator>`, both optional — the Runs page keeps its open
 *  run the same way, so a reload or a shared link keeps the place. */
export function parseHash(hash: string): { scenarioId: string | null; slug: string | null } {
  const [scenario, slug] = hash.replace(/^#\/?/, "").split("/");
  return {
    scenarioId: scenario ? decodeURIComponent(scenario) : null,
    slug: slug ? decodeURIComponent(slug) : null,
  };
}

export function formatHash(scenarioId: string | null, slug: string | null): string {
  if (!scenarioId) return "#/";
  const tail = slug ? `/${encodeURIComponent(slug)}` : "";
  return `#/${encodeURIComponent(scenarioId)}${tail}`;
}

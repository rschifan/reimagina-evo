/**
 * What the scenario workspace shows, derived from what the API answers.
 *
 * Pure: no DOM, no fetch, no runes. The store calls these and the components
 * render their results, so every rule about what a planner sees — which
 * status a scenario is in, what a table cell says, where a link points — is
 * tested here without a browser.
 */
import { decodeOrNull } from "../lib/utils";
import type { IndicatorValueStats, ReachableIndicator, ScenarioListRow } from "../api";
import { fmtNumber } from "../format";
import type { Tone } from "../lib/components/ui/tone";

export type ScenarioStatus =
  | "no-rules"
  | "not-computed"
  | "stale"
  | "queued"
  | "computing"
  | "failed"
  | "partial"
  | "up-to-date";

const LABELS: Record<ScenarioStatus, string> = {
  "no-rules": "No rules yet",
  "not-computed": "Not computed",
  stale: "Rules changed — recompute",
  queued: "Queued",
  computing: "Computing",
  failed: "Failed",
  partial: "Partly failed",
  "up-to-date": "Up to date",
};

export function statusLabel(status: ScenarioStatus): string {
  return LABELS[status];
}

/** Typed as a total map so the compiler proves every status has a tone. */
const TONES: Record<ScenarioStatus, Tone> = {
  "no-rules": "neutral",
  "not-computed": "neutral",
  stale: "stale",
  queued: "accent",
  computing: "accent",
  failed: "error",
  partial: "partial",
  "up-to-date": "fresh",
};

export function statusTone(status: ScenarioStatus): Tone {
  return TONES[status];
}

/** A run status as the table names it, or null when it says nothing new. */
function fromRun(status: string): ScenarioStatus | null {
  if (status === "queued") return "queued";
  // `cancelling` is still in flight: the run has not stopped yet.
  if (status === "running" || status === "cancelling") return "computing";
  if (status === "failed" || status === "cancelled") return "failed";
  // Results landed, but some indicators failed: not "up to date".
  if (status === "partial") return "partial";
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
  if (row.rule_count === 0) return "no-rules";
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

/** The baseline has nothing for an indicator: say what is missing. Every
 *  indicator this page offers is computed from storeys, dwelling size and
 *  household size, so a blank baseline means those inputs are absent from the
 *  pilot's blocks, not that a computation failed. */
export const NO_BASELINE =
  "these blocks carry no storeys, dwelling size or household size";

export function noValuesNote(indicatorName: string, inScenario: boolean): string {
  return inScenario
    ? `${indicatorName} has no values in this scenario here yet.`
    : `${indicatorName} has no baseline values here: ${NO_BASELINE}. ` +
        "A scenario's rules supply them inside the areas they cover.";
}

/** `#/<scenario>/<indicator>`, both optional — the Runs page keeps its open
 *  run the same way, so a reload or a shared link keeps the place. */
export function parseHash(hash: string): { scenarioId: string | null; slug: string | null } {
  const [scenario, slug] = hash.replace(/^#\/?/, "").split("/");
  return {
    scenarioId: scenario ? decodeOrNull(scenario) : null,
    slug: slug ? decodeOrNull(slug) : null,
  };
}

export function formatHash(scenarioId: string | null, slug: string | null): string {
  if (!scenarioId) return "#/";
  const tail = slug ? `/${encodeURIComponent(slug)}` : "";
  return `#/${encodeURIComponent(scenarioId)}${tail}`;
}

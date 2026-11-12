/**
 * Author a scenario from the map: pick one, draw a rule on it, recompute.
 *
 * The six operations `apps/cli/scenario.py` offers at a terminal and
 * `apps/api/routers/scenarios.py` offers over HTTP, for someone looking at the
 * city. Nothing here owns a rule. The four edits one proposal writes, the
 * refusal of a degenerate number and the density precheck all live in
 * `reimagina.scenarios.proposal`; what this module adds is the one thing a
 * form has that a request body does not — the chance to say no before the
 * round trip, and the obligation to say *why* in the user's own words.
 *
 * ## What the client-side check is, and what it is not
 *
 * `validateEdit` mirrors `max_levels_for` — the density bound, which is the
 * only one of the three ceilings independent of block area and therefore the
 * only one checkable before a block is chosen. It is **a saved round trip, not
 * a guarantee**: `check_ceilings` deliberately omits `floors`, and the
 * housing-unit and population bounds scale with each block's own area, so a
 * proposal that passes here can still be refused per block at
 * materialisation. {@link CLIENT_PRECHECK_NOTE} is what the form says so, and
 * the 422 path is wired and exercised rather than assumed unreachable.
 *
 * ⚠ **The bound is never compiled in.** It is read from the catalog the API
 * serves (`valid_max` of `edificability_block_density`), because that is where
 * `ceilings._bound_for` reads it too. A frontend carrying its own 2 000 would
 * agree with the server until the day the catalog moved, and then refuse
 * proposals the platform accepts — or, worse, wave through proposals that
 * abort a city-scale compute inside a plpgsql trigger. When the catalog could
 * not be read the check is skipped rather than guessed: the server still
 * refuses, with the numbers.
 *
 * ## What is deliberately not here
 *
 * The **delta tile**. r049 gave `serving_geo.indicator_overlay_tile` a `delta`
 * parameter that swaps `value_numeric` for `delta` + `state`; reading it is
 * the next task's, and {@link tileUrlFor} must not ask for it, because the
 * choropleth paints `value_numeric` and would come out blank.
 *
 * A **total** on the comparison strip. See {@link comparisonHeadline}.
 */

import type { Feature, Polygon } from "geojson";
import type { Map as MapLibreMap } from "maplibre-gl";

import type {
  ComparisonResponse,
  EditRow,
  IndicatorSummary,
  ScenarioRow,
} from "./api";
import {
  addScenarioEdits,
  createScenario,
  deleteScenario,
  getComparison,
  getIndicator,
  getRun,
  getScenario,
  listScenarios,
  recomputeScenario,
  removeScenarioEdit,
} from "./api";
import { subscribe as subscribeToAoi } from "./aoi";
import { ApiError } from "./http";
import { enableSelection } from "./selection";

/** The indicator whose `valid_max` *is* the density ceiling (`ceilings.py`). */
export const DENSITY_INDICATOR_SLUG = "edificability_block_density";

/** `ceilings._M2_PER_HECTARE`, and the reason the bound is per hectare. */
const M2_PER_HECTARE = 10_000;

/** How often, and for how long, the panel follows the run it enqueued.
 *  Two minutes covers a synthetic territory and a small pilot; a Mexicali-
 *  scale recompute outlives it, and the message hands over to the dashboard
 *  rather than pretending the run is lost. */
const RUN_POLL_MS = 2_000;
const MAX_RUN_POLLS = 60;

/**
 * What the form says about its own check, printed next to the inputs.
 *
 * The wording is the whole of the guarantee, so it is asserted in the tests:
 * a planner who gets past this must not read it as "this proposal is safe".
 */
export const CLIENT_PRECHECK_NOTE =
  "Checked here: the density ceiling, which does not depend on block area. " +
  "The housing-unit and population ceilings do, so the server checks those " +
  "block by block when the scenario is recomputed.";


// ── the tile URL ────────────────────────────────────────────────────────

export interface TileUrlOptions {
  /** `source_config.tiles_url_template` from `/tile-sources`, `{z}/{x}/{y}`
   *  placeholders intact. */
  template: string;
  slug: string;
  /** Feature type to draw at, when the indicator has more than one grain. */
  grain?: string | null;
  aoiId?: string | null;
  /** The active scenario, or null/absent for the baseline. */
  scenarioId?: string | null;
}


/**
 * The overlay's tile URL, with `scenario_id` only when a scenario is active.
 *
 * ⚠ **The baseline URL is byte-identical to the one that existed before
 * scenarios.** MapLibre's tile cache is keyed by URL, so adding a parameter to
 * the baseline — or merely reordering the three it has — invalidates every
 * cached tile of every indicator on every AOI, for no gain. `scenario_id` is
 * therefore appended last and only when there is one; `null` and absent are
 * the same statement, because the panel's getter returns either while the page
 * is booting.
 *
 * `{z}/{x}/{y}` in the template is literal and must survive query-string
 * composition, which is why this concatenates rather than going through
 * `new URL(template)` — that would reject the curly braces.
 */
export function tileUrlFor(options: TileUrlOptions): string {
  const params = new URLSearchParams({ indicator_slug: options.slug });
  if (options.grain) params.set("feature_type", options.grain);
  if (options.aoiId) params.set("aoi_id", options.aoiId);
  if (options.scenarioId) params.set("scenario_id", options.scenarioId);
  return `${options.template}?${params.toString()}`;
}


// ── the density precheck ────────────────────────────────────────────────

/**
 * Most storeys the density bound allows at `unitSize` m² per dwelling.
 *
 * `max_levels_for`, floored: the density indicator is
 * `10000 * levels / unit_size`, block area cancels, and half a storey is not
 * buildable. The bound is a parameter rather than a constant for the reason
 * the module docstring gives.
 */
export function maxLevelsFor(unitSize: number, densityValidMax: number): number {
  return Math.floor((densityValidMax * unitSize) / M2_PER_HECTARE);
}


export interface ProposalValues {
  levels: number;
  unitSize: number;
  peoplePerHousehold: number;
  /** `valid_max` of {@link DENSITY_INDICATOR_SLUG} from the catalog. Null or
   *  absent when `/indicators/{slug}` could not be read — the check is then
   *  the server's alone. */
  densityValidMax?: number | null;
}


/**
 * Why this proposal cannot be stored, or null when nothing here can tell.
 *
 * Two refusals, both `reimagina.scenarios.proposal`'s:
 *
 * * `require_positive` — zero, negative and non-finite. Nothing downstream
 *   would refuse them (`value_numeric` is a bare `double precision`), so they
 *   reach the compute as `NaN` and the indicators silently skip the block.
 * * `precheck_ceilings` — the density ceiling, against the catalog bound.
 *
 * The message names the **field**, never the control, so the panel can
 * highlight an input and the CLI could print the same sentence; and it never
 * says a proposal is safe, only that this check found nothing.
 */
export function validateEdit(values: ProposalValues): string | null {
  // ⚠ The message names the API's field as well as the form's control. The
  // fields are camelCase here because this is TypeScript, but `levels`,
  // `unit_size` and `people_per_household` are the names `require_positive`
  // raises about and the names the 400 carries in `field` — so a value refused
  // locally and the same value refused by the server read as the same refusal,
  // about the same thing, rather than as two unrelated complaints.
  const fields: Array<[string, string, number]> = [
    ["Storeys", "levels", values.levels],
    ["m² per dwelling", "unit_size", values.unitSize],
    ["People per household", "people_per_household", values.peoplePerHousehold],
  ];
  for (const [label, field, value] of fields) {
    if (!Number.isFinite(value) || value <= 0) {
      return `${label} (${field}) must be a positive number, got ${value}.`;
    }
  }
  const bound = values.densityValidMax;
  // ⚠ Unknown bound = no opinion. Guessing the catalog's value would make
  // this frontend the second place a ceiling lives, which is the failure the
  // module docstring describes; the server refuses with 422 either way.
  if (bound === null || bound === undefined || !Number.isFinite(bound) || bound <= 0) {
    return null;
  }
  const density = (M2_PER_HECTARE * values.levels) / values.unitSize;
  if (density > bound) {
    const allowed = maxLevelsFor(values.unitSize, bound);
    return (
      `${fmt(values.levels)} storeys at ${fmt(values.unitSize)} m² per dwelling ` +
      `is ${density.toFixed(0)} dwellings/ha, over the density ceiling of ` +
      `${fmt(bound)}. At ${fmt(values.unitSize)} m² per dwelling the ceiling ` +
      `allows ${allowed} storeys.`
    );
  }
  return null;
}


/** The density bound off a catalog row, or null when it declares none. */
export function densityBoundOf(indicator: IndicatorSummary | null): number | null {
  const bound = indicator?.valid_max;
  return typeof bound === "number" && Number.isFinite(bound) ? bound : null;
}


// ── the run a recompute enqueues ────────────────────────────────────────

/**
 * The run statuses that end a run, mirroring
 * `reimagina.orchestration.port.RUN_TERMINAL_STATES`.
 *
 * ⚠ `partial` is one of them. A run where some units failed is finished — it
 * will never move again — and a poller that waits for `succeeded` alone waits
 * forever, on exactly the runs whose results someone most wants to see.
 * `cancelling` is **not** terminal: it is the request, not the outcome.
 */
export const RUN_TERMINAL_STATES: readonly string[] = [
  "succeeded",
  "partial",
  "failed",
  "cancelled",
];


export function isTerminalRunStatus(status: string): boolean {
  return RUN_TERMINAL_STATES.includes(status);
}


/** Whether a terminal run produced results worth redrawing the map for. */
export function runProducedResults(status: string): boolean {
  return status === "succeeded" || status === "partial";
}


// ── refusals ────────────────────────────────────────────────────────────

/** Which call was refused. See {@link refusalText}. */
export type ScenarioOperation =
  | "list"
  | "create"
  | "edits"
  | "removeEdit"
  | "recompute"
  | "delete"
  | "compare";


/**
 * One refused call, in a sentence for the person who made it.
 *
 * ⚠ **The operation is a parameter because the status is not enough.**
 * `DuplicateScenarioName` and `ScenarioHasNoAOI` are both 409 with a
 * plain-string detail, and matching a substring of the server's sentence would
 * re-break the day that sentence is reworded. The caller knows which request
 * it made, so it says.
 *
 * A duplicate name is the most likely refusal in the panel — naming a scenario
 * twice is an ordinary thing to do — and "something went wrong" would send the
 * reader looking for a platform bug instead of at the name field.
 *
 * Async because `jsonRequest` throws before reading the body, deliberately:
 * the detail is JSON here and HTML from a proxy one hop away, so it is read
 * defensively and only where a sentence is wanted.
 */
export async function refusalText(
  err: unknown,
  operation: ScenarioOperation,
): Promise<string> {
  if (!(err instanceof ApiError)) {
    // A transport failure, an abort, or a defect in this module. Reporting the
    // message as itself beats a invented explanation of someone else's error.
    const message = err instanceof Error ? err.message : String(err);
    return `${capitalize(OPERATION_NOUN[operation])} failed: ${message}`;
  }
  const status = err.response.status;
  const body = await readBody(err);
  const detail = pick(body, "detail");
  const envelope = asRecord(pick(body, "error"));
  const d = asRecord(detail);
  const code = typeof d?.error === "string" ? d.error : null;
  const served = serverMessage(detail, d, envelope);

  if (status === 404) {
    if (code === "unknown_reference") {
      const field = typeof d?.field === "string" ? d.field : "reference";
      return `No record matches the ${field} in this request. Reload the page and try again.`;
    }
    return (
      "That scenario is not there any more — it may have been deleted " +
      `elsewhere. Reload the list.${suffix(served)}`
    );
  }
  if (status === 409) {
    if (operation === "create") {
      return `That name is taken. Every scenario in a project needs its own name.${suffix(served)}`;
    }
    return (
      "This scenario has no area of interest, so there is no bound on what " +
      `its edits would select. Set one before recomputing.${suffix(served)}`
    );
  }
  if (status === 400) {
    if (code === "invalid_geometry") {
      return `The drawn area is not a polygon.${suffix(served)}`;
    }
    const field = typeof d?.field === "string" ? d.field : null;
    return field
      ? `${field} was refused.${suffix(served)}`
      : `The proposal was refused.${suffix(served)}`;
  }
  if (status === 422) {
    if (code === "ceiling_precheck_failed") {
      const allowed = typeof d?.allowed_levels === "number" ? d.allowed_levels : null;
      const head =
        allowed === null
          ? "Too dense for the indicators to store."
          : `Too dense: the ceiling allows ${allowed} storeys at this unit size.`;
      return `${head}${suffix(served)}`;
    }
    if (code === "ceiling_exceeded") {
      const count = typeof d?.breach_count === "number" ? d.breach_count : null;
      const head =
        count === null
          ? "Some blocks would compute a value the indicators cannot store."
          : `${count} block${count === 1 ? "" : "s"} would compute a value the ` +
            "indicators cannot store, so nothing was materialised.";
      return `${head}${suffix(served)}`;
    }
    return `The request was refused before it reached the database.${suffix(served)}`;
  }
  if (status === 500 && code === "recompute_not_enqueued") {
    // ⚠ The one refusal that must not read as "nothing happened". The
    // overrides are written and committed by this point; a planner told the
    // recompute failed would draw the same rule a second time.
    const m = asRecord(d?.materialized);
    const written = typeof m?.overrides_written === "number" ? m.overrides_written : null;
    const stands =
      written === null
        ? "The edits were written and committed"
        : `${written} override${written === 1 ? "" : "s"} were written and committed`;
    return (
      `${stands}; only the recompute was not queued. Retry — ` +
      "materialising a scenario twice is the same as materialising it once."
    );
  }
  if (status === 503) {
    // Deliberately claims nothing about what was written: on `recompute` this
    // outage can arrive either side of the commit, which is exactly why the
    // router refuses to answer the enqueue half with a different code.
    return `The database was unreachable. Try again in a moment.${suffix(served)}`;
  }
  const requestId =
    typeof envelope?.request_id === "string" ? ` (request ${envelope.request_id})` : "";
  return (
    `${capitalize(OPERATION_NOUN[operation])} failed: ` +
    `${status}${err.response.statusText ? ` ${err.response.statusText}` : ""}` +
    `${requestId}.${suffix(served)}`
  );
}


const OPERATION_NOUN: Record<ScenarioOperation, string> = {
  list: "reading the scenario list",
  create: "creating the scenario",
  edits: "writing the rule",
  removeEdit: "removing the rule",
  recompute: "the recompute",
  delete: "deleting the scenario",
  compare: "the comparison",
};


async function readBody(err: ApiError): Promise<unknown> {
  try {
    // `clone()` so a caller that reads the body itself is not starved of it.
    return await err.response.clone().json();
  } catch {
    // Not JSON: a proxy's HTML, or an empty body. The status is then all there
    // is, and the generic branch prints it.
    return null;
  }
}


function serverMessage(
  detail: unknown,
  d: Record<string, unknown> | null,
  envelope: Record<string, unknown> | null,
): string {
  if (typeof detail === "string") return detail;
  if (d && typeof d.message === "string") return d.message;
  if (envelope && typeof envelope.message === "string") return envelope.message;
  if (Array.isArray(detail)) {
    // FastAPI's own 422: a list of {loc, msg, type}.
    return detail
      .map((entry) => (asRecord(entry)?.msg as string | undefined) ?? "")
      .filter(Boolean)
      .join("; ");
  }
  return "";
}


function suffix(served: string): string {
  return served ? ` ${served}` : "";
}


function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}


function pick(body: unknown, key: string): unknown {
  return asRecord(body)?.[key];
}


function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}


/** Numbers as typed: 6 not 6.00, 3.6 not 3.60. */
function fmt(value: number): string {
  return Number.isInteger(value) ? String(value) : String(value);
}


// ── the comparison strip ────────────────────────────────────────────────

export interface ComparisonHeadline {
  /** "12 changed · 3 unchanged · 1 new". */
  counts: string;
  /** What those counts are *of*. See the warning below. */
  denominator: string;
  /** The mean shift and its direction, or that there is no baseline. */
  shift: string;
}


/**
 * The comparison, as three lines for a header strip.
 *
 * ⚠ **The denominator is the response's own `row_count` and nothing else.**
 * `changed + unchanged + new === row_count` always, and that sum is *less*
 * than the number of blocks in the AOI whenever the baseline indicator has
 * gaps: a feature with no numeric value on either side is absent from the
 * comparison by design, and the response carries no total for exactly that
 * reason. Taking the denominator from the AOI or from the tile layer would
 * make the arithmetic visibly fail to add up, and the first conclusion a
 * reader draws from that is that the comparison is broken. Taking it from
 * `rows.length` is the same mistake one step smaller — `rows` is whatever
 * `limit` left behind.
 *
 * So the strip says what the number is of, in words, rather than leaving the
 * reader to assume it is the territory.
 */
export function comparisonHeadline(
  response: ComparisonResponse,
  unit?: string | null,
): ComparisonHeadline {
  const { changed, unchanged, new: added, mean_delta, higher_is_better } = response.summary;
  const direction = higher_is_better ? "higher is better" : "lower is better";
  return {
    counts: `${changed} changed · ${unchanged} unchanged · ${added} new`,
    denominator:
      `of ${response.row_count} features with a value on either side ` +
      "(not the AOI's block count)",
    shift:
      mean_delta === null
        ? "no baseline to subtract from"
        : `mean ${mean_delta >= 0 ? "+" : ""}${mean_delta.toFixed(2)}` +
          `${unit ? ` ${unit}` : ""} · ${direction}`,
  };
}


// ── a scenario's rules ──────────────────────────────────────────────────

export interface EditGroup {
  /** The drawn area as the server stored it (`ST_MakeValid`'d WKT). */
  areaWkt: string;
  levels: number | null;
  unitSize: number | null;
  peoplePerHousehold: number | null;
  /** Every edit in the group, in the order they were written. The remove
   *  button passes all of them. */
  editIds: string[];
  /** True when all four edits of one proposal are present. */
  complete: boolean;
  /** Lowest ordinal in the group — what orders the list. */
  ordinal: number;
}


/**
 * The four edits of one proposal, as one row.
 *
 * ⚠ **Grouped, because a proposal is four edits and three of four is not a
 * proposal.** `edits_for` writes `floors` and `num_levels` from one number
 * precisely so planned floors cannot freeze while edificability moves;
 * `DELETE /scenarios/{id}/edits` removes one edit by id, so a list of four
 * rows with four × buttons would offer the user exactly that half-written
 * state — and the compute's answer to it is to skip the block silently.
 *
 * Keyed on the stored WKT: two strokes of one selection were unioned into one
 * area before the insert, so one drawn shape is one key.
 */
export function groupEditsByArea(edits: EditRow[]): EditGroup[] {
  const byArea = new Map<string, EditGroup>();
  const seen = new Map<string, Set<string>>();
  for (const edit of edits) {
    let group = byArea.get(edit.geometry_wkt);
    if (!group) {
      group = {
        areaWkt: edit.geometry_wkt,
        levels: null,
        unitSize: null,
        peoplePerHousehold: null,
        editIds: [],
        complete: false,
        ordinal: edit.ordinal,
      };
      byArea.set(edit.geometry_wkt, group);
      seen.set(edit.geometry_wkt, new Set());
    }
    group.editIds.push(edit.id);
    group.ordinal = Math.min(group.ordinal, edit.ordinal);
    seen.get(edit.geometry_wkt)?.add(edit.attribute_key);
    switch (edit.attribute_key) {
      case "floors":
      case "num_levels":
        // The same quantity under two names. Either one answers "how tall",
        // and `complete` below is what notices when only one was written.
        group.levels = edit.value_numeric;
        break;
      case "unit_size":
        group.unitSize = edit.value_numeric;
        break;
      case "people_per_household":
        group.peoplePerHousehold = edit.value_numeric;
        break;
      default:
        break;
    }
  }
  for (const [wkt, group] of byArea) {
    const keys = seen.get(wkt) ?? new Set<string>();
    group.complete =
      keys.has("floors") &&
      keys.has("num_levels") &&
      keys.has("unit_size") &&
      keys.has("people_per_household");
  }
  return [...byArea.values()].sort((a, b) => a.ordinal - b.ordinal);
}


// ── rendering ───────────────────────────────────────────────────────────

/**
 * Fill the scenario picker: the baseline first, then every scenario.
 *
 * The baseline is an option rather than a separate control because it is a
 * choice of the same kind — "which city am I looking at" — and because the way
 * back from a scenario has to be as reachable as the way in.
 */
export function renderScenarioOptions(
  select: HTMLSelectElement,
  scenarios: ScenarioRow[],
  activeId: string | null,
): void {
  select.replaceChildren();
  const baseline = document.createElement("option");
  baseline.value = "";
  baseline.textContent = "(baseline city)";
  select.appendChild(baseline);
  for (const scenario of scenarios) {
    const option = document.createElement("option");
    option.value = scenario.id;
    // `textContent`, not markup: a scenario name is whatever its author typed.
    option.textContent = scenario.materialized_at
      ? scenario.name
      : `${scenario.name} · not yet recomputed`;
    select.appendChild(option);
  }
  select.value = activeId ?? "";
  // A scenario deleted in another tab leaves `value` empty, which reads as the
  // baseline — true, and the list refresh that follows will show it is gone.
}


/** The scenario's rules, one row per drawn area, each removable whole. */
export function renderEditGroups(
  host: HTMLElement,
  groups: EditGroup[],
  onRemove: (editIds: string[]) => void,
): void {
  host.replaceChildren();
  if (groups.length === 0) {
    const empty = document.createElement("p");
    empty.className = "scenario-panel__empty";
    empty.textContent =
      "No rule yet. Draw an area on the map, set the numbers above, and apply.";
    host.appendChild(empty);
    return;
  }
  const list = document.createElement("ul");
  list.className = "scenario-panel__rules";
  groups.forEach((group, index) => {
    const row = document.createElement("li");
    const label = document.createElement("span");
    label.className = "scenario-panel__rule-label";
    label.textContent = describeGroup(group, index + 1);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "scenario-panel__rule-remove";
    remove.textContent = "×";
    remove.title = `Remove area ${index + 1} and all ${group.editIds.length} of its edits`;
    remove.setAttribute("aria-label", remove.title);
    remove.addEventListener("click", () => onRemove(group.editIds));
    row.append(label, remove);
    list.appendChild(row);
  });
  host.appendChild(list);
}


function describeGroup(group: EditGroup, position: number): string {
  const parts = [
    group.levels === null ? "storeys —" : `${fmt(group.levels)} storeys`,
    group.unitSize === null ? "m²/dwelling —" : `${fmt(group.unitSize)} m²/dwelling`,
    group.peoplePerHousehold === null
      ? "people/household —"
      : `${fmt(group.peoplePerHousehold)} people/household`,
  ];
  // An incomplete group is named as such rather than papered over: the
  // edificability family skips any feature missing any of the three, so a
  // partial rule computes nothing and that is invisible on the map.
  const flag = group.complete ? "" : " · incomplete, will not compute";
  return `Area ${position}: ${parts.join(" · ")}${flag}`;
}


// ── the panel ───────────────────────────────────────────────────────────

export interface ScenarioController {
  /** The active scenario, or null when the map is showing the baseline. */
  activeScenarioId(): string | null;
  /** Re-read the scenario list and the active scenario's rules. */
  refresh(): Promise<void>;
  dispose(): void;
}


export interface ScenarioPanelOptions {
  /**
   * Called when the active scenario changes, including to null. The page owns
   * what that means — the indicator list is scoped to it, the overlay's tile
   * URL carries it, and the ramp is read per scenario — so the panel states
   * the change and nothing more.
   */
  onScenarioChange: (scenarioId: string | null) => void;
  /** The indicator the map is drawing, for the comparison. Null = none. */
  activeIndicatorSlug?: () => string | null;
  /** The indicator's unit, for the mean shift. */
  activeIndicatorUnit?: () => string | null;
  /** Where the panel renders. Defaults to `#scenario-panel`. */
  host?: HTMLElement | null;
}


const PANEL_HTML = `
  <label class="scenario-panel__pick">
    <span>Active scenario</span>
    <select data-field="scenario"></select>
  </label>
  <div class="scenario-panel__create">
    <input type="text" data-field="name" placeholder="New scenario name" />
    <button type="button" data-action="create">Create</button>
  </div>
  <div data-part="authoring" hidden>
    <div class="scenario-panel__form">
      <label><span>Storeys</span>
        <input type="number" data-field="levels" min="1" step="1" value="6" /></label>
      <label><span>m² per dwelling</span>
        <input type="number" data-field="unit-size" min="1" step="5" value="60" /></label>
      <label><span>People per household</span>
        <input type="number" data-field="pph" min="0.1" step="0.1" value="3.6" /></label>
      <p class="scenario-panel__note" data-part="note"></p>
      <button type="button" data-action="apply">Apply to drawn area</button>
    </div>
    <div data-part="rules" class="scenario-panel__rules-host"></div>
    <div class="scenario-panel__actions">
      <button type="button" data-action="recompute">Recompute</button>
      <button type="button" data-action="compare">Compare to baseline</button>
      <button type="button" data-action="delete" class="scenario-panel__danger">Delete</button>
    </div>
    <div data-part="comparison" class="scenario-panel__comparison" hidden></div>
  </div>
  <p data-part="message" class="scenario-panel__message" role="status"></p>
`;


/**
 * Mount the scenario panel and return the handle the page reads.
 *
 * `map` rather than a selection controller, because `enableSelection` is an
 * idempotent per-map factory (`selection.ts` caches on the map) — asking it
 * here returns the very controller the toolbar and the metrics panel are
 * driving, so the drawn area the form reads is the one the user can see.
 *
 * The panel owns the AOI subscription itself: a scenario belongs to one AOI,
 * so switching pilots has to drop the active one rather than carry a CDMX
 * what-if into Mexicali.
 */
export function enableScenarioPanel(
  map: MapLibreMap,
  options: ScenarioPanelOptions,
): ScenarioController {
  const host = options.host ?? document.getElementById("scenario-panel");
  if (!host) {
    // The map page without the panel's host element is a valid page; an
    // embedded map has no sidebar at all.
    return { activeScenarioId: () => null, refresh: async () => {}, dispose: () => {} };
  }
  const selection = enableSelection(map);
  host.innerHTML = PANEL_HTML;

  const el = <T extends HTMLElement>(selector: string): T =>
    host.querySelector(selector) as T;
  const picker = el<HTMLSelectElement>('[data-field="scenario"]');
  const nameInput = el<HTMLInputElement>('[data-field="name"]');
  const levelsInput = el<HTMLInputElement>('[data-field="levels"]');
  const unitSizeInput = el<HTMLInputElement>('[data-field="unit-size"]');
  const pphInput = el<HTMLInputElement>('[data-field="pph"]');
  const authoring = el<HTMLElement>('[data-part="authoring"]');
  const rulesHost = el<HTMLElement>('[data-part="rules"]');
  const comparisonHost = el<HTMLElement>('[data-part="comparison"]');
  const messageHost = el<HTMLElement>('[data-part="message"]');
  const createButton = el<HTMLButtonElement>('[data-action="create"]');
  const applyButton = el<HTMLButtonElement>('[data-action="apply"]');
  const recomputeButton = el<HTMLButtonElement>('[data-action="recompute"]');
  const compareButton = el<HTMLButtonElement>('[data-action="compare"]');
  const deleteButton = el<HTMLButtonElement>('[data-action="delete"]');
  el<HTMLElement>('[data-part="note"]').textContent = CLIENT_PRECHECK_NOTE;

  let aoiId: string | null = null;
  let projectId: string | null = null;
  let activeId: string | null = null;
  let scenarios: ScenarioRow[] = [];
  let densityValidMax: number | null = null;
  let drawn: Feature<Polygon> | null = null;
  // One poll at a time, superseded by token rather than by clearing alone: a
  // tick already in flight when the next recompute starts must not report its
  // run's status over the newer one's.
  let pollTimer: number | null = null;
  let pollToken = 0;

  const say = (message: string, tone: "ok" | "bad" = "ok"): void => {
    messageHost.textContent = message;
    messageHost.dataset.tone = tone;
  };

  const refused = async (err: unknown, operation: ScenarioOperation): Promise<void> => {
    say(await refusalText(err, operation), "bad");
  };

  /** The density bound, from the catalog, once per page. */
  void getIndicator(DENSITY_INDICATOR_SLUG)
    .then((indicator) => {
      densityValidMax = densityBoundOf(indicator);
      clampLevels();
    })
    .catch((err) => {
      // Not fatal and not hidden: the form simply stops pre-checking and the
      // server's 422 becomes the only refusal. Logged because a silently
      // absent ceiling looks exactly like a working one until it is breached.
      console.warn("density ceiling unavailable; the server will check it", err);
    });

  /** Keep the storeys input's own `max` on the ceiling, when it is known. */
  function clampLevels(): void {
    const unitSize = Number(unitSizeInput.value);
    if (densityValidMax === null || !Number.isFinite(unitSize) || unitSize <= 0) {
      levelsInput.removeAttribute("max");
      return;
    }
    // The API's 422 carries `allowed_levels` so a form can clamp instead of
    // re-deriving the bound; this is the same number, before the round trip.
    levelsInput.max = String(maxLevelsFor(unitSize, densityValidMax));
  }
  unitSizeInput.addEventListener("input", clampLevels);

  function setAuthoringVisible(): void {
    authoring.hidden = activeId === null;
    applyButton.disabled = activeId === null || drawn === null;
    applyButton.title =
      drawn === null ? "Draw an area on the map first" : "Write this rule over the drawn area";
  }

  async function loadScenarios(): Promise<void> {
    if (!aoiId) {
      scenarios = [];
      renderScenarioOptions(picker, scenarios, null);
      return;
    }
    try {
      scenarios = await listScenarios({ aoiId });
    } catch (err) {
      await refused(err, "list");
      return;
    }
    renderScenarioOptions(picker, scenarios, activeId);
  }

  async function loadRules(): Promise<void> {
    if (!activeId) {
      renderEditGroups(rulesHost, [], () => {});
      return;
    }
    try {
      const detail = await getScenario(activeId);
      renderEditGroups(rulesHost, groupEditsByArea(detail.edits), (editIds) => {
        void removeGroup(editIds);
      });
    } catch (err) {
      await refused(err, "list");
    }
  }

  async function removeGroup(editIds: string[]): Promise<void> {
    if (!activeId) return;
    // Sequential, and every id of the group: three edits of four is the
    // half-written proposal `groupEditsByArea` exists to keep off the screen.
    // A failure partway is reported with what is left, because the scenario is
    // then in exactly that state and pretending otherwise would hide it.
    let removed = 0;
    for (const editId of editIds) {
      try {
        await removeScenarioEdit(activeId, editId);
        removed += 1;
      } catch (err) {
        await refused(err, "removeEdit");
        break;
      }
    }
    if (removed === editIds.length) {
      say(
        `Rule removed. Recompute to withdraw the overrides it wrote — ` +
          "removing the rule does not withdraw them on its own.",
      );
    } else if (removed > 0) {
      say(
        `${removed} of ${editIds.length} edits removed; the rule is now ` +
          "incomplete and will not compute. Remove the rest.",
        "bad",
      );
    }
    await loadRules();
  }

  function setActive(scenarioId: string | null, announce = true): void {
    if (scenarioId === activeId) return;
    activeId = scenarioId;
    stopPolling();
    comparisonHost.hidden = true;
    comparisonHost.replaceChildren();
    setAuthoringVisible();
    void loadRules();
    if (announce) options.onScenarioChange(activeId);
  }

  picker.addEventListener("change", () => {
    setActive(picker.value || null);
    say(
      activeId === null
        ? "Showing the baseline city."
        : `Showing ${scenarios.find((s) => s.id === activeId)?.name ?? "the scenario"}.`,
    );
  });

  createButton.addEventListener("click", () => {
    void (async () => {
      const name = nameInput.value.trim();
      if (!name) {
        say("Give the scenario a name.", "bad");
        return;
      }
      if (!projectId) {
        say("No AOI is active, so there is no project to create a scenario in.", "bad");
        return;
      }
      createButton.disabled = true;
      try {
        const created = await createScenario({
          project_id: projectId,
          name,
          aoi_id: aoiId,
        });
        nameInput.value = "";
        await loadScenarios();
        picker.value = created.id;
        setActive(created.id);
        say(`Created ${created.name}. Draw an area and apply the numbers.`);
      } catch (err) {
        await refused(err, "create");
      } finally {
        createButton.disabled = false;
      }
    })();
  });

  applyButton.addEventListener("click", () => {
    void (async () => {
      if (!activeId) return;
      if (!drawn) {
        say("Draw an area on the map first — a rule is an area.", "bad");
        return;
      }
      const values = {
        levels: Number(levelsInput.value),
        unitSize: Number(unitSizeInput.value),
        peoplePerHousehold: Number(pphInput.value),
        densityValidMax,
      };
      const refusal = validateEdit(values);
      if (refusal) {
        say(refusal, "bad");
        return;
      }
      applyButton.disabled = true;
      try {
        const written = await addScenarioEdits(activeId, {
          area: drawn,
          levels: values.levels,
          unit_size: values.unitSize,
          people_per_household: values.peoplePerHousehold,
        });
        say(
          `${written.edits.length} edits written over the drawn area on ` +
            `${written.feature_type}. Recompute to materialise them.`,
        );
        await loadRules();
      } catch (err) {
        await refused(err, "edits");
      } finally {
        setAuthoringVisible();
      }
    })();
  });

  function stopPolling(): void {
    pollToken += 1;
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  /**
   * Follow the run the recompute enqueued, and redraw the map when it lands.
   *
   * ⚠ Without this the panel is half a loop. The recompute answers 202 the
   * moment the overrides are committed, but the indicator results it will
   * recompute do not exist yet — so the overlay keeps painting the values from
   * before the run, under the scenario that was supposed to change them, with
   * nothing on screen saying so. The scenario's own numbers arriving while the
   * map shows the old ones is the most convincing kind of wrong.
   *
   * Polling rather than the SSE stream `/jobs/{id}/events` offers: the panel
   * needs one bit — has it landed — and a stream here would be a second
   * long-lived connection per open tab for something the user watches for a
   * few seconds. The ops dashboard is where a run is actually followed, and
   * the message says so when this gives up.
   */
  function pollRun(runId: string, materialised: string): void {
    const myToken = ++pollToken;
    let attempts = 0;
    const short = runId.slice(0, 8);
    const tick = async (): Promise<void> => {
      if (myToken !== pollToken) return;
      attempts += 1;
      let run;
      try {
        run = await getRun(runId);
      } catch (err) {
        // A poll that could not be read is not a recompute that failed, and
        // must not be reported as one: the overrides stand either way.
        console.warn("could not read run", runId, err);
        say(`${materialised} Run ${short} could not be read — follow it on the run dashboard.`);
        return;
      }
      if (myToken !== pollToken) return;
      if (isTerminalRunStatus(run.status)) {
        pollTimer = null;
        if (runProducedResults(run.status)) {
          const failed = run.units.filter((u) => u.status === "failed").length;
          say(
            `${materialised} Run ${short} ${run.status}` +
              `${failed > 0 ? ` (${failed} indicator${failed === 1 ? "" : "s"} failed)` : ""}. ` +
              "Redrawing the map on its results.",
          );
          // The map is rendered from the scenario id, so restating it is what
          // rebuilds the source — and a rebuilt source drops the tiles cached
          // before the run, which are the stale ones.
          options.onScenarioChange(activeId);
        } else {
          say(
            `${materialised} Run ${short} ${run.status}. The overrides stand; ` +
              "the indicator results did not change. The run dashboard has the detail.",
            "bad",
          );
        }
        return;
      }
      if (attempts >= MAX_RUN_POLLS) {
        say(
          `${materialised} Run ${short} is still ${run.status}. It is the run ` +
            "dashboard's now — the map will show its results once it lands and " +
            "the scenario is re-picked.",
        );
        return;
      }
      say(`${materialised} Run ${short} is ${run.status}…`);
      pollTimer = window.setTimeout(() => void tick(), RUN_POLL_MS);
    };
    pollTimer = window.setTimeout(() => void tick(), RUN_POLL_MS);
  }

  recomputeButton.addEventListener("click", () => {
    void (async () => {
      if (!activeId) return;
      recomputeButton.disabled = true;
      say("Materialising the edits and queueing the recompute…");
      try {
        const result = await recomputeScenario(activeId);
        const matched = Object.values(result.matched_per_edit);
        const caught = matched.length > 0 ? Math.max(...matched) : 0;
        const materialised =
          `${caught} feature${caught === 1 ? "" : "s"} caught, ` +
          `${result.overrides_written} overrides written, ` +
          `${result.overrides_removed} removed, ` +
          `${result.results_superseded} results superseded.`;
        if (result.run_id === null) {
          // A `noop` submit: nothing was invalidated, so no run row exists and
          // an id would 404. The materialisation's counts still stand.
          say(`${materialised} Nothing needed recomputing.`);
        } else {
          say(
            `${materialised} Run ${result.run_id.slice(0, 8)} is ` +
              `${result.run_status}${result.deduplicated ? " (already in flight)" : ""}…`,
          );
          pollRun(result.run_id, materialised);
        }
        await loadScenarios();
      } catch (err) {
        await refused(err, "recompute");
      } finally {
        recomputeButton.disabled = false;
      }
    })();
  });

  compareButton.addEventListener("click", () => {
    void (async () => {
      if (!activeId) return;
      const slug = options.activeIndicatorSlug?.() ?? null;
      if (!slug) {
        say("Pick an indicator to compare.", "bad");
        return;
      }
      compareButton.disabled = true;
      try {
        const response = await getComparison(activeId, {
          indicator_slug: slug,
          aoi_id: aoiId,
        });
        renderComparison(response, options.activeIndicatorUnit?.() ?? null);
      } catch (err) {
        await refused(err, "compare");
      } finally {
        compareButton.disabled = false;
      }
    })();
  });

  function renderComparison(response: ComparisonResponse, unit: string | null): void {
    const headline = comparisonHeadline(response, unit);
    comparisonHost.replaceChildren();
    for (const [part, text] of [
      ["counts", headline.counts],
      ["denominator", headline.denominator],
      ["shift", headline.shift],
    ] as const) {
      const line = document.createElement("div");
      line.className = `scenario-panel__comparison-${part}`;
      line.textContent = text;
      comparisonHost.appendChild(line);
    }
    comparisonHost.hidden = false;
  }

  deleteButton.addEventListener("click", () => {
    void (async () => {
      if (!activeId) return;
      const name = scenarios.find((s) => s.id === activeId)?.name ?? activeId;
      if (!window.confirm(`Delete ${name}, its overrides and its results?`)) return;
      deleteButton.disabled = true;
      try {
        const removed = await deleteScenario(activeId);
        setActive(null);
        await loadScenarios();
        say(
          `Deleted: ${removed.overrides} overrides, ` +
            `${removed.versions_retired} versions retired, ` +
            `${removed.results_superseded} results superseded, ` +
            `${removed.materializations} materialisations.`,
        );
      } catch (err) {
        await refused(err, "delete");
      } finally {
        deleteButton.disabled = false;
      }
    })();
  });

  // The drawn area, live: the form's area *is* the selection the toolbar
  // draws, so the apply button is disabled until there is one.
  const unsubscribeSelection = selection.subscribe((sel) => {
    drawn = sel?.feature ?? null;
    setAuthoringVisible();
  });

  const unsubscribeAoi = subscribeToAoi((aoi) => {
    const changed = aoi?.aoi_id !== aoiId;
    aoiId = aoi?.aoi_id ?? null;
    projectId = aoi?.project_id ?? null;
    if (changed && activeId !== null) {
      // A scenario is scoped to one AOI; carrying one across pilots would
      // scope the overlay to a scenario whose edits select nothing here.
      setActive(null);
      say("Scenario cleared — a scenario belongs to one AOI.");
    }
    void loadScenarios();
  });

  setAuthoringVisible();
  renderEditGroups(rulesHost, [], () => {});

  return {
    activeScenarioId: () => activeId,
    refresh: async () => {
      await loadScenarios();
      await loadRules();
    },
    dispose: () => {
      stopPolling();
      unsubscribeSelection();
      unsubscribeAoi();
      host.replaceChildren();
    },
  };
}

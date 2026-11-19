/**
 * The pure rules a scenario-authoring screen needs: the tile URL, the
 * client-side precheck, refusal wording, the comparison headline and the
 * grouping of a scenario's edits into rules.
 *
 * The scenario workspace (`src/scenarios/`) renders them; this module has no
 * DOM. The six operations `apps/cli/scenario.py` offers at a terminal and
 * `apps/api/routers/scenarios.py` offers over HTTP are the same operations.
 * Nothing here owns a rule. The four edits one proposal writes, the
 * refusal of a degenerate number and the density precheck all live in
 * `reimagina.scenarios.proposal`; what this module adds is the one thing a
 * form has that a request body does not — the chance to say no before the
 * round trip, and the obligation to say *why* in the user's own words.
 *
 * ## What the client-side check is, and what it is not
 *
 * `validateEdit` mirrors the two bounds independent of block area, and
 * therefore checkable before a block is chosen: density (`max_levels_for`)
 * and floors (`max_floors`). It is **a saved round trip, not a guarantee**:
 * the housing-unit and population bounds scale with each block's own area, so
 * a proposal that passes here can still be refused per block at
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
 */

import type { EditRow, IndicatorSummary } from "./api";
import { ApiError } from "./http";

/** The indicator whose `valid_max` *is* the density ceiling (`ceilings.py`). */
export const DENSITY_INDICATOR_SLUG = "edificability_block_density";

/** The indicator that reads `floors`, which `--levels` writes unchanged. Its
 *  `valid_max` caps storeys whatever the unit size. */
export const FLOORS_INDICATOR_SLUG = "urban_form_block_planned_floors";

/** `ceilings._M2_PER_HECTARE`, and the reason the bound is per hectare. */
const M2_PER_HECTARE = 10_000;

/**
 * What the form says about its own check, printed next to the inputs.
 *
 * The wording is the whole of the guarantee, so it is asserted in the tests:
 * a planner who gets past this must not read it as "this proposal is safe".
 */
export const CLIENT_PRECHECK_NOTE =
  "Checked here: the density ceiling, which does not depend on block area, " +
  "and the storey ceiling, which holds whatever the unit size. " +
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
  /** Ask for the scenario's difference from the baseline instead of its
   *  values. Meaningless without a scenario, and ignored then. */
  delta?: boolean;
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
  // ⚠ Only with a scenario: the tile function answers delta-without-scenario
  // with an empty tile (serving the baseline's own values as a "difference"
  // would paint every block unchanged), so sending it would ask for a blank
  // map by name. Last, so the value-mode URL is a prefix of the delta one and
  // the baseline URL stays what it always was. `true` is the canonical
  // spelling of a vocabulary the server reads as Postgres reads a boolean.
  if (options.scenarioId && options.delta) params.set("delta", "true");
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
export function maxLevelsFor(
  unitSize: number,
  densityValidMax: number,
  lotCoverage = 1,
): number {
  // density = coverage × levels × 10000 / unit_size, so coverage divides the
  // storeys the bound allows. 1, the whole block, is the formula without it.
  return Math.floor((densityValidMax * unitSize) / M2_PER_HECTARE / lotCoverage);
}


export interface ProposalValues {
  levels: number;
  unitSize: number;
  peoplePerHousehold: number;
  /** Share of the block built on, `0 < c ≤ 1`. Absent means 1, the whole
   *  block — what the API assumes when it is not sent. */
  lotCoverage?: number;
  /** `valid_max` of {@link DENSITY_INDICATOR_SLUG} from the catalog. Null or
   *  absent when `/indicators/{slug}` could not be read — the check is then
   *  the server's alone. */
  densityValidMax?: number | null;
  /** `valid_max` of {@link FLOORS_INDICATOR_SLUG} from the catalog; null or
   *  absent on the same terms as `densityValidMax`. */
  floorsValidMax?: number | null;
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
  const coverage = values.lotCoverage ?? 1;
  if (!Number.isFinite(coverage) || coverage <= 0 || coverage > 1) {
    return `Lot coverage (lot_coverage) must be a share between 0 and 1, got ${coverage}.`;
  }
  const floorsBound = values.floorsValidMax;
  if (
    floorsBound !== null &&
    floorsBound !== undefined &&
    Number.isFinite(floorsBound) &&
    floorsBound > 0 &&
    values.levels > floorsBound
  ) {
    return (
      `${fmt(values.levels)} storeys is over the planned-floors ceiling of ` +
      `${fmt(floorsBound)}, whatever the unit size.`
    );
  }
  const bound = values.densityValidMax;
  // ⚠ Unknown bound = no opinion. Guessing the catalog's value would make
  // this frontend the second place a ceiling lives, which is the failure the
  // module docstring describes; the server refuses with 422 either way.
  if (bound === null || bound === undefined || !Number.isFinite(bound) || bound <= 0) {
    return null;
  }
  const density = (M2_PER_HECTARE * values.levels * coverage) / values.unitSize;
  if (density > bound) {
    const allowed = maxLevelsFor(values.unitSize, bound, coverage);
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
    // ⚠ Two conditions answer 503 on `recompute`, and only one of them is an
    // outage. `ops_plane_not_provisioned` means the job service's own storage
    // was never created — the database is fine, and no amount of retrying will
    // change that. Read off the status alone, this branch told a planner the
    // database was unreachable and to try again in a moment: both false, and
    // the advice can never succeed. That is the defect the server-side 503
    // exists to remove, relocated to the client.
    //
    // The code is taken from the envelope, not from `d`, because `code` above
    // reads only FastAPI's `detail` shape and this refusal carries the
    // project's error envelope.
    if (envelope?.code === "ops_plane_not_provisioned") {
      // Safe to say the edits stand, which the outage branch below cannot: this
      // is raised by the enqueue, and the enqueue runs only after the
      // materialisation has committed.
      //
      // No command in the sentence — this module's dialect names fields, never
      // flags, because a message describing a command line is written for
      // someone this API does not have. `served` still appends the server's own
      // remedy for whoever can act on it.
      return (
        "The edits were written and committed; the job service is not ready " +
        "to recompute them yet. Nothing is wrong with your request — this " +
        `deployment needs an operator to finish setting it up.${suffix(served)}`
      );
    }
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


/** Numbers as typed: 6 not 6.00, 3.6 not 3.60 — which is what `String` does. */
function fmt(value: number): string {
  return String(value);
}


// ── the comparison strip ────────────────────────────────────────────────




// ── a scenario's rules ──────────────────────────────────────────────────

export interface EditGroup {
  /** The drawn area as the server stored it (`ST_MakeValid`'d WKT). */
  areaWkt: string;
  levels: number | null;
  unitSize: number | null;
  peoplePerHousehold: number | null;
  /** Share of the block built on. Null for a rule written before lot
   *  coverage existed, which computes at 1, the whole block. */
  lotCoverage: number | null;
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
        lotCoverage: null,
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
      case "lot_coverage":
        group.lotCoverage = edit.value_numeric;
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


export function describeGroup(group: EditGroup, position: number): string {
  const parts = [
    `Rule ${position}`,
    group.levels === null ? "st —" : `${fmt(group.levels)} st`,
    group.unitSize === null ? "m² —" : `${fmt(group.unitSize)} m²`,
    group.peoplePerHousehold === null
      ? "people —"
      : `${fmt(group.peoplePerHousehold)} people`,
  ];
  if (group.lotCoverage !== null) parts.push(`${fmt(group.lotCoverage * 100)} %`);
  // An incomplete group is named as such rather than papered over: the
  // edificability family skips any feature missing any of the three, so a
  // partial rule computes nothing and that is invisible on the map.
  const flag = group.complete ? "" : " · incomplete, will not compute";
  return `${parts.join(" · ")}${flag}`;
}

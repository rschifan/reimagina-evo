/**
 * The pure rules a scenario-authoring screen needs: the tile URL, the
 * client-side precheck, refusal wording and a scenario's rules as the page
 * shows them.
 *
 * The scenario workspace (`src/scenarios/`) renders them; this module has no
 * DOM. The six operations `apps/cli/scenario.py` offers at a terminal and
 * `apps/api/routers/scenarios.py` offers over HTTP are the same operations.
 * Nothing here owns a rule: a rule is a server row, refused or stored whole.
 * The edits its numbers write, the refusal of a degenerate number and the
 * density precheck all live in `reimagina.scenarios.proposal`; what this
 * module adds is the one thing a
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

import type { IndicatorSummary, RuleInputs, RuleMode, RuleRow, RuleUnits } from "./api";
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


/** A rule's numbers by their API names (absent or null: not set, every
 *  block keeps its own; at least one must be set), and the catalog bounds. */
export type ProposalValues = { [K in keyof RuleInputs]?: number | null } & {
  /** `valid_max` of {@link DENSITY_INDICATOR_SLUG} from the catalog. Null or
   *  absent when `/indicators/{slug}` could not be read — the check is then
   *  the server's alone. */
  densityValidMax?: number | null;
  /** `valid_max` of {@link FLOORS_INDICATOR_SLUG} from the catalog; null or
   *  absent on the same terms as `densityValidMax`. */
  floorsValidMax?: number | null;
};


/** A refusal, and the field it is about (an API name) when it is one. */
export interface EditRefusal {
  field: string | null;
  text: string;
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
 * The text names the **field**, never the control, so the panel can
 * highlight an input and the CLI could print the same sentence; and it never
 * says a proposal is safe, only that this check found nothing. `field` is
 * the API's name for it — the name the server's 400 carries — and the
 * storey and density ceilings are about `levels`.
 */
export function validateEdit(values: ProposalValues): EditRefusal | null {
  const set = (value: number | null | undefined): value is number =>
    value !== null && value !== undefined;
  const fields: Array<[string, keyof RuleInputs]> = [
    ["Storeys", "levels"],
    ["m² per dwelling", "unit_size"],
    ["People per household", "people_per_household"],
  ];
  if (RULE_NUMBERS.every(([key]) => !set(values[key]))) {
    return {
      field: null,
      text:
        "Set at least one number (levels, unit_size, people_per_household or " +
        "lot_coverage): a rule with none changes nothing.",
    };
  }
  for (const [label, field] of fields) {
    const value = values[field];
    if (set(value) && (!Number.isFinite(value) || value <= 0)) {
      return { field, text: `${label} (${field}) must be a positive number, got ${value}.` };
    }
  }
  const coverage = values.lot_coverage ?? 1;
  if (!Number.isFinite(coverage) || coverage <= 0 || coverage > 1) {
    return {
      field: "lot_coverage",
      text: `Lot coverage (lot_coverage) must be a share between 0 and 1, got ${coverage}.`,
    };
  }
  // The rest needs storeys: without them each block keeps its own, which
  // only the server can see.
  const levels = values.levels;
  if (!set(levels)) return null;
  const floorsBound = values.floorsValidMax;
  if (set(floorsBound) && Number.isFinite(floorsBound) && floorsBound > 0 && levels > floorsBound) {
    return {
      field: "levels",
      text:
        `${fmt(levels)} storeys is over the planned-floors ceiling of ` +
        `${fmt(floorsBound)}, whatever the unit size.`,
    };
  }
  const bound = values.densityValidMax;
  // ⚠ Unknown bound = no opinion. Guessing the catalog's value would make
  // this frontend the second place a ceiling lives, which is the failure the
  // module docstring describes; the server refuses with 422 either way.
  if (!set(bound) || !Number.isFinite(bound) || bound <= 0) return null;
  const unitSize = values.unit_size;
  if (!set(unitSize)) return null;
  const density = (M2_PER_HECTARE * levels * coverage) / unitSize;
  if (density > bound) {
    const allowed = maxLevelsFor(unitSize, bound, coverage);
    return {
      field: "levels",
      text:
        `${fmt(levels)} storeys at ${fmt(unitSize)} m² per dwelling ` +
        `is ${density.toFixed(0)} dwellings/ha, over the density ceiling of ` +
        `${fmt(bound)}. At ${fmt(unitSize)} m² per dwelling the ceiling ` +
        `allows ${allowed} storeys.`,
    };
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
  | "rule"
  | "deleteRule"
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
    if (operation === "rule" || operation === "deleteRule") {
      return `That rule or scenario is not there any more — it changed elsewhere. Reload the page.${suffix(served)}`;
    }
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
      "This scenario's area of interest was deleted, so its rules select " +
      `nothing. Create a new scenario on a pilot and draw them there.${suffix(served)}`
    );
  }
  if (status === 400) {
    if (code === "invalid_geometry") {
      return `The drawn area is not a polygon in longitude and latitude.${suffix(served)}`;
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
      // The server names the rule, the blocks and what fits; `breach_count`
      // counts (block, indicator) pairs, not blocks, so it is not echoed.
      return `Not saved: too dense for the indicators to store.${suffix(served)}`;
    }
    if (code === "change_has_no_baseline" && served) {
      // The server's sentence names the number and what to do instead.
      return served;
    }
    if (code === "rule_catches_nothing") {
      return "No block of this pilot falls inside this area, so the rule was not stored. Draw it over the city's blocks.";
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
      `${stands}; only the recompute was not queued. Use Recompute to ` +
      "retry — materialising a scenario twice is the same as once."
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
  rule: "writing the rule",
  deleteRule: "deleting the rule",
  recompute: "the recompute",
  delete: "deleting the scenario",
  compare: "the comparison",
};


/** A refusal's JSON body, or null when it has none. */
export async function readBody(err: ApiError): Promise<unknown> {
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


// ── a scenario's rules ──────────────────────────────────────────────────

/** One rule as the page shows it: the server's row, its numbers by their
 *  API names (absent: not set, each block keeps its own). */
export interface Rule {
  id: string;
  /** Its place in "later wins"; fixed when it was created. */
  ordinal: number;
  /** The drawn area as the server stored it (`ST_MakeValid`'d WKT). */
  areaWkt: string;
  inputs: RuleInputs;
  /** Set to (default) or change by, with each changed number's unit. */
  mode?: RuleMode;
  units?: RuleUnits;
}


/** Every number a rule can set (`proposal.RULE_INPUTS`): its API name, what
 *  the page calls it, and its unit in a rule's one-line summary. */
export const RULE_NUMBERS: ReadonlyArray<readonly [keyof RuleInputs, string, string]> = [
  ["levels", "Storeys", "st"],
  ["unit_size", "m² per home", "m²"],
  ["people_per_household", "People per home", "people"],
  ["lot_coverage", "Lot coverage", "%"],
];


/** The server's rules, already in "later wins" order. */
export function rulesFromRows(rows: RuleRow[]): Rule[] {
  return rows.map((row) => ({
    id: row.id,
    ordinal: row.ordinal,
    areaWkt: row.geometry_wkt,
    inputs: row.inputs,
    mode: row.mode ?? "set",
    units: row.units ?? {},
  }));
}


/** A share as the percent a planner typed: 0.29 is "29", not
 *  "28.999999999999996", and 0.625 stays "62.5". */
export function percentOf(share: number): number {
  return Number((share * 100).toFixed(4));
}


/** The numbers both rules set: where they overlap, the later one's win. */
export function sharedNumbers(rule: Rule, later: Rule): string[] {
  return RULE_NUMBERS.filter(
    ([key]) => rule.inputs[key] !== undefined && later.inputs[key] !== undefined,
  ).map(([, label]) => label.toLowerCase());
}


export function describeRule(rule: Rule, position: number): string {
  // Only what the rule sets; everything else is each block's own value.
  const parts = [`Rule ${position}`];
  const change = rule.mode === "change";
  for (const [key, , unit] of RULE_NUMBERS) {
    const value = rule.inputs[key];
    if (value === undefined) continue;
    if (!change) {
      parts.push(`${fmt(key === "lot_coverage" ? percentOf(value) : value)} ${unit}`);
      continue;
    }
    // A change: signed, as a percentage or in the number's own unit;
    // coverage as an amount is points of the block's area.
    const pct = rule.units?.[key] === "pct";
    const cover = key === "lot_coverage";
    const amount = cover && !pct ? percentOf(value) : value;
    const sign = amount < 0 ? "−" : "+";
    const what = pct ? `% ${cover ? "cover" : unit}` : cover ? "pts cover" : unit;
    parts.push(`${sign}${fmt(Math.abs(amount))} ${what}`);
  }
  return parts.join(" · ");
}

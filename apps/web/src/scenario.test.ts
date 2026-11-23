// `scenario.ts` is pure since the sidebar panel moved to the scenario
// workspace, so these run under vitest's default `node` environment.
import { describe, expect, it } from "vitest";

import { ApiError } from "./http";
import {
  CLIENT_PRECHECK_NOTE,
  describeRule,
  maxLevelsFor,
  percentOf,
  rulesFromRows,
  sharedNumbers,
  isTerminalRunStatus,
  refusalText,
  runProducedResults,
  tileUrlFor,
  validateEdit,
} from "./scenario";
import type { RuleRow } from "./api";

/** `validateEdit`'s sentence, or null: what most of these tests read. */
const refusalOf = (values: Parameters<typeof validateEdit>[0]): string | null =>
  validateEdit(values)?.text ?? null;

/**
 * ``valid_max`` of ``edificability_block_density`` in the shipped catalog
 * (`indicators/catalog.py`), which the API serves on ``GET /indicators`` and
 * ``GET /indicators/{slug}``.
 *
 * A **fixture**, not a constant the module is allowed to know: every bound
 * below is passed in, so a reader can see which number each expectation came
 * from, and so an implementation that hardcoded one would fail the cases that
 * use a different one.
 */
const CATALOG_DENSITY_MAX = 2000;

/** The live template `choroplethConfig` reads off `/tile-sources`. */
const TEMPLATE = "/api/tiles/indicator_overlay_tile/{z}/{x}/{y}";
const SLUG = "urban_form_block_compactness";
const AOI = "12abb5e9-52a2-4467-99e0-694dcdb36886";


// ── the tile URL ────────────────────────────────────────────────────────

describe("tileUrlFor", () => {
  it("puts scenario_id in the tile URL when a scenario is active", () => {
    expect(
      tileUrlFor({ template: TEMPLATE, slug: SLUG, aoiId: AOI, scenarioId: "s-1" }),
    ).toContain("scenario_id=s-1");
  });

  it("omits scenario_id on the baseline", () => {
    expect(
      tileUrlFor({ template: TEMPLATE, slug: SLUG, aoiId: AOI, scenarioId: null }),
    ).not.toContain("scenario_id");
  });

  it("omits scenario_id when the caller does not mention one at all", () => {
    // `undefined` and `null` are the same statement here, and the panel's
    // getter can return either while the page is still booting.
    expect(tileUrlFor({ template: TEMPLATE, slug: SLUG, aoiId: AOI })).not.toContain(
      "scenario_id",
    );
  });

  // ⚠ The byte-identity test, and the reason it asserts a whole string rather
  // than a `toContain`: MapLibre's tile cache is keyed by URL, so a baseline
  // URL that gains a parameter — or merely reorders the ones it has — busts
  // every cached tile for every indicator on every AOI at once. The three
  // parameters and their order are what `activateIndicator` built before a
  // scenario existed, taken from `choropleth.ts`.
  it("leaves the baseline tile URL byte-identical to the pre-scenario one", () => {
    expect(
      tileUrlFor({
        template: TEMPLATE,
        slug: SLUG,
        grain: "block",
        aoiId: AOI,
        scenarioId: null,
      }),
    ).toBe(
      `${TEMPLATE}?indicator_slug=${SLUG}&feature_type=block&aoi_id=${AOI}`,
    );
  });

  it("appends scenario_id after the parameters the baseline already carried", () => {
    expect(
      tileUrlFor({
        template: TEMPLATE,
        slug: SLUG,
        grain: "block",
        aoiId: AOI,
        scenarioId: "s-1",
      }),
    ).toBe(
      `${TEMPLATE}?indicator_slug=${SLUG}&feature_type=block&aoi_id=${AOI}` +
        `&scenario_id=s-1`,
    );
  });

  it("encodes what it is given rather than splicing it in raw", () => {
    const url = tileUrlFor({
      template: TEMPLATE,
      slug: "a&b=c",
      aoiId: null,
      scenarioId: null,
    });
    expect(url).toBe(`${TEMPLATE}?indicator_slug=a%26b%3Dc`);
  });

  // r049 gave `indicator_overlay_tile` a `delta` parameter. A builder that
  // asked for it unprompted would change what every tile returns (`delta` +
  // `state` in place of `value_numeric` + `quality_flag`), and the value
  // choropleth paints `value_numeric`. Asking is opt-in, and covered in
  // `delta-ramp.test.ts`.
  it("does not ask for the delta tile unless told to", () => {
    const url = tileUrlFor({
      template: TEMPLATE,
      slug: SLUG,
      grain: "block",
      aoiId: AOI,
      scenarioId: "s-1",
    });
    expect(url).not.toContain("delta");
  });
});


// ── the density precheck ────────────────────────────────────────────────

describe("maxLevelsFor", () => {
  // ⚠ Four unit sizes and two bounds, not one of each. `max_levels_for(60)`
  // under the shipped catalog is exactly 12, so a function that ignored both
  // arguments and returned 12 would pass any test written at 60 m² — and 61 m²
  // floors to 12 as well, so the obvious second case does not separate them
  // either. These do: a constant fails five of the six.
  it.each([
    [30, CATALOG_DENSITY_MAX, 6],
    [60, CATALOG_DENSITY_MAX, 12],
    [125, CATALOG_DENSITY_MAX, 25],
    // Floors: 61 × 2000 / 10 000 = 12.2 storeys, and 0.2 of a storey is not
    // buildable.
    [61, CATALOG_DENSITY_MAX, 12],
    // The bound, not just the unit size: halve the catalog's ceiling and the
    // same unit size allows half as many.
    [60, 1000, 6],
    [125, 1000, 12],
  ])("allows %i m² per dwelling under a %i bound exactly %i storeys", (unit_size, bound, allowed) => {
    expect(maxLevelsFor(unit_size, bound)).toBe(allowed);
  });
});


describe("lot coverage", () => {
  const ok = { levels: 6, unit_size: 60, people_per_household: 3.6 };

  it("must be a share of the block", () => {
    for (const bad of [0, -0.1, 1.2, Number.NaN]) {
      expect(refusalOf({ ...ok, lot_coverage: bad })).toMatch(/lot_coverage.*between 0 and 1/);
    }
    expect(refusalOf({ ...ok, lot_coverage: 0.6 })).toBeNull();
    expect(refusalOf({ ...ok, lot_coverage: 0.005 })).toBeNull();
  });

  // density = coverage × levels × 10000 / unit_size: 20 storeys at 60 m² is
  // 3333/ha at full coverage, 1666 at half.
  it("scales the density ceiling", () => {
    const at = (lot_coverage: number) =>
      refusalOf({ ...ok, levels: 20, lot_coverage, densityValidMax: 2000 });
    expect(at(1)).toMatch(/density ceiling/);
    expect(at(0.5)).toBeNull();
    expect(maxLevelsFor(60, 2000, 0.5)).toBe(24);
    expect(maxLevelsFor(60, 2000)).toBe(12);
  });
});


describe("validateEdit", () => {
  const ok = { levels: 6, unit_size: 60, people_per_household: 3.6 };

  it("refuses levels above the density ceiling before submitting", () => {
    expect(
      refusalOf({
        levels: 13,
        unit_size: 60,
        people_per_household: 3.6,
        densityValidMax: CATALOG_DENSITY_MAX,
      }),
    ).toMatch(/density/);
  });

  // Storeys are capped by the planned-floors indicator whatever the unit
  // size: 250 storeys at 2 000 m² is only 1 250 dwellings/ha, under density.
  it("refuses levels above the floors ceiling even when density is legal", () => {
    const refusal = refusalOf({
      ...ok,
      levels: 250,
      unit_size: 2000,
      densityValidMax: CATALOG_DENSITY_MAX,
      floorsValidMax: 200,
    });
    expect(refusal).toMatch(/floors/);
    expect(refusal).toContain("200");
    expect(
      refusalOf({
        ...ok,
        levels: 200,
        unit_size: 2000,
        densityValidMax: CATALOG_DENSITY_MAX,
        floorsValidMax: 200,
      }),
    ).toBeNull();
  });

  it("derives the floors ceiling from the bound given, and has no opinion without one", () => {
    expect(
      refusalOf({ ...ok, levels: 60, unit_size: 2000, floorsValidMax: 50 }),
    ).toMatch(/floors/);
    expect(
      refusalOf({ ...ok, levels: 250, unit_size: 2000, floorsValidMax: null }),
    ).toBeNull();
  });

  it("passes the tallest proposal the ceiling allows", () => {
    expect(
      refusalOf({ ...ok, levels: 12, densityValidMax: CATALOG_DENSITY_MAX }),
    ).toBeNull();
  });

  // ⚠ Three unit sizes, because one cannot tell a derivation from a constant.
  // `max_levels_for(60)` is exactly 12 under the shipped catalog, so a
  // hardcoded 12 passes every 60 m² case — the hole a constant-12 mirror of this
  // bound would otherwise hide.
  it.each([
    [30, 6],
    [60, 12],
    [125, 25],
  ])("allows %i m² per dwelling exactly %i storeys", (unit_size, allowed) => {
    expect(
      refusalOf({
        ...ok,
        unit_size,
        levels: allowed,
        densityValidMax: CATALOG_DENSITY_MAX,
      }),
    ).toBeNull();
    const refusal = refusalOf({
      ...ok,
      unit_size,
      levels: allowed + 1,
      densityValidMax: CATALOG_DENSITY_MAX,
    });
    expect(refusal).toMatch(/density/);
    // ⚠ The number the message names is the derivation's own answer. Without
    // this the refusal could name any number at all — including a constant 12
    // — and every case here would still pass, because `toMatch(/density/)`
    // only reads the word.
    expect(refusal).toContain(`allows ${allowed} storeys`);
  });

  // The other half of the same hole: the bound itself must come from the
  // catalog the API served, not from a number compiled into the frontend. At
  // 1 000 units/ha the same 60 m² proposal is capped at 6 storeys, so a module
  // carrying 2 000 of its own would wave 12 through.
  it("derives the ceiling from the bound it was given, not a compiled-in one", () => {
    expect(refusalOf({ ...ok, levels: 6, densityValidMax: 1000 })).toBeNull();
    expect(refusalOf({ ...ok, levels: 7, densityValidMax: 1000 })).toContain(
      "allows 6 storeys",
    );
    expect(refusalOf({ ...ok, levels: 12, densityValidMax: 1000 })).toMatch(
      /density/,
    );
  });

  // `require_positive`'s refusals, in the one place a round trip can be saved.
  // The expectations are the API's own field names — the ones the 400 carries
  // in `field` — so a value refused here and the same value refused by the
  // server name the same thing.
  it.each([
    [{ levels: 0 }, /levels/],
    [{ levels: -3 }, /levels/],
    [{ unit_size: 0 }, /unit_size/],
    [{ people_per_household: Number.NaN }, /people_per_household/],
    [{ levels: Number.POSITIVE_INFINITY }, /levels/],
  ])("refuses a degenerate %o, naming the field", (patch, expected) => {
    expect(
      refusalOf({ ...ok, ...patch, densityValidMax: CATALOG_DENSITY_MAX }),
    ).toMatch(expected);
  });

  // ⚠ The bound is unknown when `/indicators/edificability_block_density`
  // could not be read. The honest answer is then "the server decides", not a
  // guess at the catalog's value — and certainly not a refusal of a proposal
  // the server would have accepted.
  it("leaves the ceiling to the server when the catalog bound is unknown", () => {
    expect(refusalOf({ levels: 400, unit_size: 60, people_per_household: 3.6 })).toBeNull();
    expect(
      refusalOf({ ...ok, levels: 400, densityValidMax: null }),
    ).toBeNull();
  });
});


describe("CLIENT_PRECHECK_NOTE", () => {
  // The per-block housing-unit and population bounds need each block's area, so a passing precheck is a
  // saved round trip and not a promise. The note is what the form says so,
  // and it is asserted because the wording is the whole of the guarantee.
  it("says the per-block bounds are the server's to check", () => {
    expect(CLIENT_PRECHECK_NOTE).toMatch(/block/i);
    expect(CLIENT_PRECHECK_NOTE).toMatch(/storey/i);
    expect(CLIENT_PRECHECK_NOTE).toMatch(/whatever the unit size/i);
    expect(CLIENT_PRECHECK_NOTE).toMatch(/server|checks again|recompute/i);
  });
});


// ── refusals ───────────────────────────────────────────────────────────

/** An `ApiError` as `jsonRequest` throws one: status + unread body. */
function apiError(status: number, body: unknown, statusText = ""): ApiError {
  const response = new Response(
    typeof body === "string" ? body : JSON.stringify(body),
    { status, statusText, headers: { "Content-Type": "application/json" } },
  );
  return new ApiError(`${status} ${statusText} on /scenarios`, response);
}

describe("refusalText", () => {
  // ⚠ The most likely refusal in the whole panel: a planner names a scenario
  // twice. "Something went wrong" sends them looking for a bug in the
  // platform instead of at the name field.
  it("reads a duplicate name as a taken name", async () => {
    const err = apiError(409, { detail: "scenario 'Upzone' already exists in this project" });
    await expect(refusalText(err, "create")).resolves.toMatch(/name is taken/i);
  });

  // The same status, the other state. `ScenarioHasNoAOI` and
  // `DuplicateScenarioName` are both 409 with a plain-string detail, so the
  // operation is what tells them apart — not a substring of the server's
  // sentence, which would re-break the day the sentence is reworded.
  it("reads a 409 on recompute as the missing AOI it is", async () => {
    const err = apiError(409, {
      detail: "scenario 3f… has no area of interest, so there is no bound on what its edits would select.",
    });
    const text = await refusalText(err, "recompute");
    expect(text).toMatch(/area of interest/i);
    expect(text).not.toMatch(/name is taken/i);
    // ⚠ Without this the test passes on the generic branch too, which appends
    // the server's own sentence — and that sentence contains "area of
    // interest". What separates the two is that the generic branch reports a
    // status code, because it has nothing better to say.
    expect(text).not.toMatch(/409/);
  });

  it("reads a 404 as something that is no longer there", async () => {
    const err = apiError(404, { detail: "no scenario with id 'abc'" });
    await expect(refusalText(err, "recompute")).resolves.toMatch(/not (there|found)/i);
  });

  it("names the field an unknown reference was about", async () => {
    const err = apiError(404, {
      detail: {
        error: "unknown_reference",
        field: "project_id",
        message: "no record matches the project_id in this request",
      },
    });
    const text = await refusalText(err, "create");
    expect(text).toMatch(/^No record matches the project_id/);
    // ⚠ The generic 404 appends the server's sentence, which also contains
    // "project_id". What separates the branches is the generic wording.
    expect(text).not.toMatch(/not there any more/i);
  });

  it("carries the storeys the ceiling allows out of a 422 precheck", async () => {
    const err = apiError(422, {
      detail: {
        error: "ceiling_precheck_failed",
        message: "13 storeys at 60 m² per dwelling computes 2166.7 …",
        levels: 13,
        unit_size: 60,
        allowed_levels: 12,
      },
    });
    const text = await refusalText(err, "rule");
    expect(text).toMatch(/12 storeys/);
  });

  it("says a per-block ceiling refusal saved nothing, then the server's advice", async () => {
    const err = apiError(422, {
      detail: {
        error: "ceiling_exceeded",
        message: "Rule 1 is too much for 4 blocks: at most +24 % storeys fits.",
        breach_count: 7,
        breaches: [],
      },
    });
    const text = await refusalText(err, "recompute");
    expect(text).toMatch(/^Not saved: too dense/);
    expect(text).toMatch(/Rule 1 is too much for 4 blocks: at most \+24 % storeys fits\./);
    expect(text).not.toMatch(/7 block/);
  });

  it("passes on why a change rule changed nothing", async () => {
    const message = "This change moves no block's m² per home: the blocks have no value for it, or are already past its limit. Nothing was saved. Choose = to set it instead.";
    const err = apiError(422, {
      detail: { error: "change_has_no_baseline", field: "unit_size", message },
    });
    await expect(refusalText(err, "rule")).resolves.toBe(message);
  });

  it("names the field a 400 was about", async () => {
    const err = apiError(400, {
      detail: {
        error: "invalid_value",
        field: "levels",
        message: "levels must be a positive number, got -3.0",
      },
    });
    const text = await refusalText(err, "rule");
    expect(text).toMatch(/^levels was refused\./);
    // ⚠ The generic fall-through appends the server's sentence, which also
    // starts with "levels"; it reports a status code, this branch does not.
    expect(text).not.toMatch(/400/);
  });

  it("reads a 400 on the geometry as a drawing problem", async () => {
    const err = apiError(400, {
      detail: {
        error: "invalid_geometry",
        field: "area",
        message: "the drawn area carries no polygon: …",
      },
    });
    const text = await refusalText(err, "rule");
    expect(text).toMatch(/^The drawn area is not a polygon in longitude and latitude\./);
    // Without the geometry branch this lands on the field branch
    // ("area was refused.") or the generic one (a status code).
    expect(text).not.toMatch(/was refused|400/);
  });

  // ⚠ The one refusal that must not read as "nothing happened". The overrides
  // are written and committed by the time the enqueue fails; a planner told
  // the recompute "failed" would draw the same rule again.
  it("says the edits stand when only the enqueue failed", async () => {
    const err = apiError(500, {
      detail: {
        error: "recompute_not_enqueued",
        phase: "enqueue",
        message: "scenario 3f… was materialised and committed; …",
        materialized: {
          overrides_written: 64,
          overrides_removed: 0,
          results_superseded: 12,
        },
      },
    });
    const text = await refusalText(err, "recompute");
    expect(text).toMatch(/written|stand|committed/i);
    expect(text).toMatch(/64/);
    expect(text).toMatch(/retry/i);
  });

  it("reads a 503 as the outage it is, claiming nothing about what was written", async () => {
    const err = apiError(503, {
      error: {
        code: "database_unavailable",
        // Deliberately not the branch's own wording: the served sentence is
        // appended, and must not be what the assertion below matches.
        message: "connection pool exhausted",
        request_id: "req-7",
      },
    });
    const text = await refusalText(err, "recompute");
    expect(text).toMatch(/^The database was unreachable\. Try again/);
    expect(text).not.toMatch(/503|failed:/);
    expect(text).not.toMatch(/nothing was written/i);
  });

  it("does not read an unprovisioned ops plane as a database outage", async () => {
    // Two different conditions now answer 503 on this route, and this mapper
    // branches on the status. So the deployment-state 503 arrived wearing the
    // outage's words: "The database was unreachable. Try again in a moment."
    //
    // Both halves of that are false here. The database is reachable — the
    // materialisation just committed through it, which is why this code can
    // only be raised afterwards — and trying again in a moment will never
    // work, because what is missing is a provisioning step. Telling a planner
    // to retry something that cannot succeed is the same defect the server-side
    // 503 exists to remove, moved to the client.
    const err = apiError(503, {
      error: {
        code: "ops_plane_not_provisioned",
        message: "the ops plane's storage is missing — run `bootstrap-db`",
        request_id: "req-9",
      },
    });
    const text = await refusalText(err, "recompute");
    expect(text).not.toMatch(/database was unreachable/i);
    expect(text).not.toMatch(/try again in a moment/i);
    // The edits really are safe, and this is the one screen that must say so.
    expect(text).toMatch(/written and committed/i);
    // The server's own sentence still reaches the screen for whoever can act.
    expect(text).toMatch(/bootstrap-db/);
  });

  it("falls back to the status when the body is not JSON", async () => {
    const err = apiError(500, "<html>502 from the proxy</html>", "Internal Server Error");
    await expect(refusalText(err, "list")).resolves.toMatch(/500/);
  });

  it("reports a transport failure as itself", async () => {
    await expect(refusalText(new TypeError("Failed to fetch"), "list")).resolves.toMatch(
      /Failed to fetch/,
    );
  });
});


// ── the run a recompute enqueues ───────────────────────────────────────

describe("isTerminalRunStatus", () => {
  // The vocabulary is `orchestration.port.RUN_TERMINAL_STATES` and the
  // `orchestration_run.status` CHECK behind it. ⚠ `partial` belongs in it: a
  // run with a failed unit is finished and will never move again, so a poller
  // that waits for `succeeded` alone waits forever — on exactly the runs whose
  // results someone most wants to look at. `cancelling` does not: it is the
  // request, not the outcome.
  it.each(["succeeded", "partial", "failed", "cancelled"])(
    "treats %s as terminal",
    (status) => {
      expect(isTerminalRunStatus(status)).toBe(true);
    },
  );

  it.each(["queued", "running", "cancelling"])("keeps waiting on %s", (status) => {
    expect(isTerminalRunStatus(status)).toBe(false);
  });

  // Only two of the four leave results behind to redraw. A failed run's
  // overrides still stand — materialisation committed before the enqueue —
  // but the indicator results did not move, so redrawing would show the same
  // map with a message implying it had changed.
  it("redraws the map for the two that produced results", () => {
    expect(["succeeded", "partial"].map(runProducedResults)).toEqual([true, true]);
    expect(["failed", "cancelled"].map(runProducedResults)).toEqual([false, false]);
  });
});


// ── a scenario's rules ─────────────────────────────────────────────────

function ruleRow(id: string, ordinal: number, inputs: RuleRow["inputs"]): RuleRow {
  return {
    id,
    ordinal,
    type_id: "t-1",
    inputs,
    geometry_wkt: "POLYGON((0 0,1 0,1 1,0 1,0 0))",
    created_at: "2026-11-12T00:00:00Z",
    updated_at: "2026-11-12T00:00:00Z",
  };
}

describe("rulesFromRows", () => {
  it("is the server's rules as served, numbers by their API names", () => {
    const rules = rulesFromRows([
      ruleRow("a", 2, { levels: 6, unit_size: 60, lot_coverage: 0.6 }),
      ruleRow("b", 4, { people_per_household: 5 }),
    ]);
    expect(rules.map((r) => r.id)).toEqual(["a", "b"]);
    expect(rules[0].inputs).toEqual({ levels: 6, unit_size: 60, lot_coverage: 0.6 });
    expect(rules[1].inputs).toEqual({ people_per_household: 5 });
  });
});


describe("describeRule", () => {
  const rule = (inputs: Record<string, number> = { levels: 6, unit_size: 60, people_per_household: 3.6 }) => ({
    id: "r", ordinal: 1, areaWkt: "POLYGON((0 0,1 0,1 1,0 0))", inputs,
  });

  it("names a rule by its position and its numbers", () => {
    expect(describeRule(rule(), 2)).toBe("Rule 2 · 6 st · 60 m² · 3.6 people");
    expect(describeRule(rule({ levels: 6, unit_size: 60, people_per_household: 3.6, lot_coverage: 0.6 }), 3)).toBe(
      "Rule 3 · 6 st · 60 m² · 3.6 people · 60 %",
    );
  });

  // B18: 0.29 × 100 is 28.999999999999996 in floating point.
  it("prints coverage as typed, without float noise or rounding", () => {
    expect(describeRule(rule({ lot_coverage: 0.29 }), 1)).toBe("Rule 1 · 29 %");
    expect(percentOf(0.625)).toBe(62.5);
    for (const pct of [7, 14, 29, 55, 57, 58]) expect(percentOf(pct / 100)).toBe(pct);
  });

  it("labels a change rule by its signed changes and their units", () => {
    const change = { ...rule({ levels: 1, unit_size: -10, lot_coverage: 0.05 }), mode: "change" as const, units: { unit_size: "pct" as const } };
    expect(describeRule(change, 2)).toBe("Rule 2 · +1 st · −10 % m² · +5 pts cover");
    const pct = { ...rule({ lot_coverage: 5, people_per_household: 0.5 }), mode: "change" as const, units: { lot_coverage: "pct" as const } };
    expect(describeRule(pct, 1)).toBe("Rule 1 · +0.5 people · +5 % cover");
  });

  it("labels a mixed rule's set numbers as values and its changes as signed", () => {
    const mixed = {
      ...rule({ levels: 20, lot_coverage: 0.4 }),
      mode: "change" as const,
      units: { levels: "pct" as const, lot_coverage: "set" as const },
    };
    expect(describeRule(mixed, 1)).toBe("Rule 1 · +20 % st · 40 %");
  });

  it("is a whole rule with one number, and its label shows only that number", () => {
    const [one] = rulesFromRows([ruleRow("r", 1, { people_per_household: 5 })]);
    expect(describeRule(one, 1)).toBe("Rule 1 · 5 people");
  });

  // W7: later wins per number, so a later rule that sets only household size
  // does not override an earlier rule's storeys.
  it("names only the numbers both rules set", () => {
    const [early, late] = rulesFromRows([
      ruleRow("a", 1, { levels: 8, unit_size: 60 }),
      ruleRow("b", 2, { people_per_household: 2.5, unit_size: 90 }),
    ]);
    expect(sharedNumbers(early, late)).toEqual(["m² per home"]);
    const [storeys, people] = rulesFromRows([
      ruleRow("a", 1, { levels: 8 }),
      ruleRow("b", 2, { people_per_household: 2.5 }),
    ]);
    expect(sharedNumbers(storeys, people)).toEqual([]);
  });
});


describe("the field a local refusal is about", () => {
  it("is the API's name, and storeys for the ceilings", () => {
    expect(validateEdit({ unit_size: -1 })?.field).toBe("unit_size");
    expect(validateEdit({ lot_coverage: 2 })?.field).toBe("lot_coverage");
    expect(validateEdit({ levels: 40, unit_size: 60, densityValidMax: 2000 })?.field).toBe("levels");
    expect(validateEdit({ levels: 250, floorsValidMax: 200 })?.field).toBe("levels");
    expect(validateEdit({})?.field).toBeNull();
  });
});


describe("a rule that sets only some numbers", () => {
  // Writing all four flattened the area; a number left out keeps each
  // block's own value.
  it("validates only the numbers it sets, and needs one", () => {
    expect(refusalOf({ levels: null, unit_size: null, people_per_household: 5 })).toBeNull();
    expect(refusalOf({ levels: null, unit_size: null, people_per_household: -1 })).toMatch(
      /people_per_household/,
    );
    expect(refusalOf({ levels: null, unit_size: null, people_per_household: null })).toMatch(
      /at least one/,
    );
  });

  it("checks density only when storeys and unit size are both set", () => {
    expect(refusalOf({ levels: 40, unit_size: null, people_per_household: null, densityValidMax: 2000 })).toBeNull();
    expect(refusalOf({ levels: 40, unit_size: 60, people_per_household: null, densityValidMax: 2000 })).toMatch(
      /density ceiling/,
    );
  });

});

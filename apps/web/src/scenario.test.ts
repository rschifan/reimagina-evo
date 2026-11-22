// `scenario.ts` is pure since the sidebar panel moved to the scenario
// workspace, so these run under vitest's default `node` environment.
import { describe, expect, it } from "vitest";

import { ApiError } from "./http";
import {
  CLIENT_PRECHECK_NOTE,
  describeRule,
  maxLevelsFor,
  percentOf,
  ruleInputs,
  rulesFromRows,
  sharedNumbers,
  isTerminalRunStatus,
  refusalText,
  runProducedResults,
  tileUrlFor,
  validateEdit,
} from "./scenario";
import type { RuleRow } from "./api";

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
  ])("allows %i m² per dwelling under a %i bound exactly %i storeys", (unitSize, bound, allowed) => {
    expect(maxLevelsFor(unitSize, bound)).toBe(allowed);
  });
});


describe("lot coverage", () => {
  const ok = { levels: 6, unitSize: 60, peoplePerHousehold: 3.6 };

  it("must be a share of the block", () => {
    for (const bad of [0, -0.1, 1.2, Number.NaN]) {
      expect(validateEdit({ ...ok, lotCoverage: bad })).toMatch(/lot_coverage.*between 0 and 1/);
    }
    expect(validateEdit({ ...ok, lotCoverage: 0.6 })).toBeNull();
  });

  // density = coverage × levels × 10000 / unit_size: 20 storeys at 60 m² is
  // 3333/ha at full coverage, 1666 at half.
  it("scales the density ceiling", () => {
    const at = (lotCoverage: number) =>
      validateEdit({ ...ok, levels: 20, lotCoverage, densityValidMax: 2000 });
    expect(at(1)).toMatch(/density ceiling/);
    expect(at(0.5)).toBeNull();
    expect(maxLevelsFor(60, 2000, 0.5)).toBe(24);
    expect(maxLevelsFor(60, 2000)).toBe(12);
  });
});


describe("validateEdit", () => {
  const ok = { levels: 6, unitSize: 60, peoplePerHousehold: 3.6 };

  it("refuses levels above the density ceiling before submitting", () => {
    expect(
      validateEdit({
        levels: 13,
        unitSize: 60,
        peoplePerHousehold: 3.6,
        densityValidMax: CATALOG_DENSITY_MAX,
      }),
    ).toMatch(/density/);
  });

  // Storeys are capped by the planned-floors indicator whatever the unit
  // size: 250 storeys at 2 000 m² is only 1 250 dwellings/ha, under density.
  it("refuses levels above the floors ceiling even when density is legal", () => {
    const refusal = validateEdit({
      ...ok,
      levels: 250,
      unitSize: 2000,
      densityValidMax: CATALOG_DENSITY_MAX,
      floorsValidMax: 200,
    });
    expect(refusal).toMatch(/floors/);
    expect(refusal).toContain("200");
    expect(
      validateEdit({
        ...ok,
        levels: 200,
        unitSize: 2000,
        densityValidMax: CATALOG_DENSITY_MAX,
        floorsValidMax: 200,
      }),
    ).toBeNull();
  });

  it("derives the floors ceiling from the bound given, and has no opinion without one", () => {
    expect(
      validateEdit({ ...ok, levels: 60, unitSize: 2000, floorsValidMax: 50 }),
    ).toMatch(/floors/);
    expect(
      validateEdit({ ...ok, levels: 250, unitSize: 2000, floorsValidMax: null }),
    ).toBeNull();
  });

  it("passes the tallest proposal the ceiling allows", () => {
    expect(
      validateEdit({ ...ok, levels: 12, densityValidMax: CATALOG_DENSITY_MAX }),
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
  ])("allows %i m² per dwelling exactly %i storeys", (unitSize, allowed) => {
    expect(
      validateEdit({
        ...ok,
        unitSize,
        levels: allowed,
        densityValidMax: CATALOG_DENSITY_MAX,
      }),
    ).toBeNull();
    const refusal = validateEdit({
      ...ok,
      unitSize,
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
    expect(validateEdit({ ...ok, levels: 6, densityValidMax: 1000 })).toBeNull();
    expect(validateEdit({ ...ok, levels: 7, densityValidMax: 1000 })).toContain(
      "allows 6 storeys",
    );
    expect(validateEdit({ ...ok, levels: 12, densityValidMax: 1000 })).toMatch(
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
    [{ unitSize: 0 }, /unit_size/],
    [{ peoplePerHousehold: Number.NaN }, /people_per_household/],
    [{ levels: Number.POSITIVE_INFINITY }, /levels/],
  ])("refuses a degenerate %o, naming the field", (patch, expected) => {
    expect(
      validateEdit({ ...ok, ...patch, densityValidMax: CATALOG_DENSITY_MAX }),
    ).toMatch(expected);
  });

  // ⚠ The bound is unknown when `/indicators/edificability_block_density`
  // could not be read. The honest answer is then "the server decides", not a
  // guess at the catalog's value — and certainly not a refusal of a proposal
  // the server would have accepted.
  it("leaves the ceiling to the server when the catalog bound is unknown", () => {
    expect(validateEdit({ levels: 400, unitSize: 60, peoplePerHousehold: 3.6 })).toBeNull();
    expect(
      validateEdit({ ...ok, levels: 400, densityValidMax: null }),
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

  it("reports how many blocks breached a per-block ceiling", async () => {
    const err = apiError(422, {
      detail: {
        error: "ceiling_exceeded",
        message: "4 blocks would compute …",
        breach_count: 4,
        breaches: [],
      },
    });
    await expect(refusalText(err, "recompute")).resolves.toMatch(/4 block/);
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
  it("is the server's rules in their order, numbers left out left null", () => {
    const rules = rulesFromRows([
      ruleRow("b", 4, { people_per_household: 5 }),
      ruleRow("a", 2, { levels: 6, unit_size: 60, lot_coverage: 0.6 }),
    ]);
    expect(rules.map((r) => r.id)).toEqual(["a", "b"]);
    expect(rules[0]).toMatchObject({ levels: 6, unitSize: 60, peoplePerHousehold: null, lotCoverage: 0.6 });
    expect(rules[1]).toMatchObject({ levels: null, peoplePerHousehold: 5 });
  });

  it("sends back only the numbers set", () => {
    expect(
      ruleInputs({ levels: null, unitSize: 70, peoplePerHousehold: null, lotCoverage: 0.5 }),
    ).toEqual({ unit_size: 70, lot_coverage: 0.5 });
  });
});


describe("describeRule", () => {
  const rule = (o: Partial<ReturnType<typeof rulesFromRows>[number]> = {}) => ({
    id: "r", ordinal: 1, areaWkt: "POLYGON((0 0,1 0,1 1,0 0))",
    levels: 6, unitSize: 60, peoplePerHousehold: 3.6, lotCoverage: null as number | null, ...o,
  });

  it("names a rule by its position and its numbers", () => {
    expect(describeRule(rule(), 2)).toBe("Rule 2 · 6 st · 60 m² · 3.6 people");
    expect(describeRule(rule({ lotCoverage: 0.6 }), 3)).toBe(
      "Rule 3 · 6 st · 60 m² · 3.6 people · 60 %",
    );
  });

  // B18: 0.29 × 100 is 28.999999999999996 in floating point.
  it("prints coverage as typed, without float noise or rounding", () => {
    expect(describeRule(rule({ levels: null, unitSize: null, peoplePerHousehold: null, lotCoverage: 0.29 }), 1)).toBe(
      "Rule 1 · 29 %",
    );
    expect(percentOf(0.625)).toBe(62.5);
    for (const pct of [7, 14, 29, 55, 57, 58]) expect(percentOf(pct / 100)).toBe(pct);
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


describe("a rule that sets only some numbers", () => {
  // Writing all four flattened the area; a number left out keeps each
  // block's own value.
  it("validates only the numbers it sets, and needs one", () => {
    expect(validateEdit({ levels: null, unitSize: null, peoplePerHousehold: 5 })).toBeNull();
    expect(validateEdit({ levels: null, unitSize: null, peoplePerHousehold: -1 })).toMatch(
      /people_per_household/,
    );
    expect(validateEdit({ levels: null, unitSize: null, peoplePerHousehold: null })).toMatch(
      /at least one/,
    );
  });

  it("checks density only when storeys and unit size are both set", () => {
    expect(validateEdit({ levels: 40, unitSize: null, peoplePerHousehold: null, densityValidMax: 2000 })).toBeNull();
    expect(validateEdit({ levels: 40, unitSize: 60, peoplePerHousehold: null, densityValidMax: 2000 })).toMatch(
      /density ceiling/,
    );
  });

});

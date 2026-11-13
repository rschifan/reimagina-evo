// @vitest-environment jsdom
//
// jsdom for the whole file because two of the exports under test write into
// elements (`renderScenarioOptions`, `renderEditGroups`). The rest is pure and
// would run under `node`; one docblock is cheaper than splitting the module's
// tests across two files by the environment they happen to need.
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "./http";
import {
  CLIENT_PRECHECK_NOTE,
  comparisonHeadline,
  groupEditsByArea,
  maxLevelsFor,
  isTerminalRunStatus,
  refusalText,
  renderEditGroups,
  renderScenarioOptions,
  runProducedResults,
  tileUrlFor,
  validateEdit,
} from "./scenario";
import type { ComparisonResponse, EditRow, ScenarioRow } from "./api";

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

  it("passes the tallest proposal the ceiling allows", () => {
    expect(
      validateEdit({ ...ok, levels: 12, densityValidMax: CATALOG_DENSITY_MAX }),
    ).toBeNull();
  });

  // ⚠ Three unit sizes, because one cannot tell a derivation from a constant.
  // `max_levels_for(60)` is exactly 12 under the shipped catalog, so a
  // hardcoded 12 passes every 60 m² case — the hole Task 8 found in its own
  // mirror of this bound.
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
  // `check_ceilings` omits `floors` and the per-block housing-unit and
  // population bounds need each block's area, so a passing precheck is a
  // saved round trip and not a promise. The note is what the form says so,
  // and it is asserted because the wording is the whole of the guarantee.
  it("says the per-block bounds are the server's to check", () => {
    expect(CLIENT_PRECHECK_NOTE).toMatch(/block/i);
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
    await expect(refusalText(err, "create")).resolves.toMatch(/project_id/);
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
    const text = await refusalText(err, "edits");
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
    await expect(refusalText(err, "edits")).resolves.toMatch(/levels/);
  });

  it("reads a 400 on the geometry as a drawing problem", async () => {
    const err = apiError(400, {
      detail: {
        error: "invalid_geometry",
        field: "area",
        message: "the drawn area carries no polygon: …",
      },
    });
    await expect(refusalText(err, "edits")).resolves.toMatch(/area|polygon/i);
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
        message: "The database was unreachable.",
        request_id: "req-7",
      },
    });
    const text = await refusalText(err, "recompute");
    expect(text).toMatch(/unreachable|unavailable/i);
    expect(text).not.toMatch(/nothing was written/i);
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


// ── the comparison strip ───────────────────────────────────────────────

/**
 * A comparison as the endpoint answers it. ⚠ 12 + 3 + 1 = 16 = ``row_count``,
 * and 16 is **not** the AOI's block count: a feature with no numeric value on
 * either side is absent from the rows by design, and the response carries no
 * total for that reason.
 */
function comparison(overrides: Partial<ComparisonResponse> = {}): ComparisonResponse {
  return {
    scenario_id: "s-1",
    indicator_slug: "edificability_block_density",
    aoi_id: AOI,
    summary: {
      changed: 12,
      unchanged: 3,
      new: 1,
      mean_delta: 3.5,
      higher_is_better: true,
    },
    row_count: 16,
    rows: [],
    rows_truncated: false,
    ...overrides,
  };
}

describe("comparisonHeadline", () => {
  it("states the comparison's own row count, which the three counts sum to", () => {
    const response = comparison();
    const { changed, unchanged, new: added } = response.summary;
    expect(changed + unchanged + added).toBe(response.row_count);
    expect(comparisonHeadline(response).counts).toBe("12 changed · 3 unchanged · 1 new");
    expect(comparisonHeadline(response).denominator).toContain("16");
  });

  // ⚠ The denominator comes from `row_count`, not from the array. `rows` is
  // whatever `limit` left behind — the response says so with `rows_truncated`
  // — and a strip reading "3 changed of 2" is read as a broken comparison.
  it("takes its denominator from the row count, not from the rows it was handed", () => {
    const text = comparisonHeadline(
      comparison({
        rows: [
          { feature_id: "f1", baseline: 1, scenario: 2, delta: 1, state: "changed" },
          { feature_id: "f2", baseline: 1, scenario: 1, delta: 0, state: "unchanged" },
        ],
        rows_truncated: true,
      }),
    ).denominator;
    expect(text).toContain("16");
    expect(text).not.toMatch(/\b2\b/);
  });

  // The counts are of features that had a value on one side or the other, and
  // the response deliberately carries no total. Saying so is what stops the
  // strip from being read as "16 blocks in this AOI".
  it("says what the count is of, rather than implying the territory's size", () => {
    expect(comparisonHeadline(comparison()).denominator).toMatch(/either side/i);
    expect(comparisonHeadline(comparison()).denominator).toMatch(/not the AOI/i);
  });

  it("gives the mean shift a direction from the catalog", () => {
    expect(comparisonHeadline(comparison()).shift).toMatch(/higher is better/);
    expect(
      comparisonHeadline(
        comparison({
          summary: { changed: 12, unchanged: 3, new: 1, mean_delta: -40, higher_is_better: false },
        }),
      ).shift,
    ).toMatch(/lower is better/);
  });

  it("does not invent a mean where no row had a baseline", () => {
    const shift = comparisonHeadline(
      comparison({
        summary: { changed: 0, unchanged: 0, new: 16, mean_delta: null, higher_is_better: true },
      }),
    ).shift;
    expect(shift).toMatch(/no baseline/i);
    expect(shift).not.toMatch(/\d/);
  });
});


// ── the edit list ──────────────────────────────────────────────────────

function edit(ordinal: number, key: string, value: number, wkt: string): EditRow {
  return {
    id: `e${ordinal}`,
    ordinal,
    type_id: "t-1",
    attribute_key: key,
    value_numeric: value,
    geometry_wkt: wkt,
    created_at: "2026-11-12T00:00:00Z",
  };
}

const AREA_A = "POLYGON((0 0,1 0,1 1,0 1,0 0))";
const AREA_B = "POLYGON((5 5,6 5,6 6,5 6,5 5))";

function proposalEdits(wkt: string, from: number, levels = 6): EditRow[] {
  return [
    edit(from, "floors", levels, wkt),
    edit(from + 1, "num_levels", levels, wkt),
    edit(from + 2, "unit_size", 60, wkt),
    edit(from + 3, "people_per_household", 3.6, wkt),
  ];
}

describe("groupEditsByArea", () => {
  // One proposal is four edits over one area (`edits_for`). Listing four rows
  // per drawn shape would invite removing one of them, which is the
  // half-written proposal that function exists to prevent.
  it("groups the four edits of one proposal into one row", () => {
    const groups = groupEditsByArea(proposalEdits(AREA_A, 1));
    expect(groups).toHaveLength(1);
    expect(groups[0].levels).toBe(6);
    expect(groups[0].unitSize).toBe(60);
    expect(groups[0].peoplePerHousehold).toBe(3.6);
    expect(groups[0].editIds).toHaveLength(4);
  });

  it("keeps two drawn areas apart, in the order they were written", () => {
    const groups = groupEditsByArea([
      ...proposalEdits(AREA_A, 1, 6),
      ...proposalEdits(AREA_B, 5, 10),
    ]);
    expect(groups.map((g) => g.levels)).toEqual([6, 10]);
  });

  // An edit removed one at a time (the CLI can) leaves a group that is not a
  // proposal. Showing it as one with a missing number invented would hide
  // exactly the state that makes the compute skip the block.
  it("leaves a missing number missing rather than inventing one", () => {
    const partial = proposalEdits(AREA_A, 1).slice(0, 2);
    const [group] = groupEditsByArea(partial);
    expect(group.levels).toBe(6);
    expect(group.unitSize).toBeNull();
    expect(group.complete).toBe(false);
  });

  it("marks a whole proposal complete", () => {
    expect(groupEditsByArea(proposalEdits(AREA_A, 1))[0].complete).toBe(true);
  });
});


// ── the two renderers ──────────────────────────────────────────────────

function scenario(id: string, name: string): ScenarioRow {
  return {
    id,
    project_id: "p-1",
    aoi_id: AOI,
    name,
    description: null,
    created_at: "2026-11-12T00:00:00Z",
    updated_at: "2026-11-12T00:00:00Z",
    materialized_at: null,
  };
}

describe("renderScenarioOptions", () => {
  it("offers the baseline first and keeps it selectable", () => {
    const select = document.createElement("select");
    renderScenarioOptions(select, [scenario("s-1", "Upzone the corridor")], null);
    expect(select.options).toHaveLength(2);
    expect(select.options[0].value).toBe("");
    expect(select.value).toBe("");
  });

  it("selects the active scenario", () => {
    const select = document.createElement("select");
    renderScenarioOptions(select, [scenario("s-1", "A"), scenario("s-2", "B")], "s-2");
    expect(select.value).toBe("s-2");
  });

  // A scenario name is user input and goes in as text, not as markup.
  it("puts a name in as text", () => {
    const select = document.createElement("select");
    renderScenarioOptions(select, [scenario("s-1", "<img src=x onerror=alert(1)>")], null);
    expect(select.querySelectorAll("img")).toHaveLength(0);
    expect(select.options[1].textContent).toContain("<img");
  });
});

describe("renderEditGroups", () => {
  it("renders one row per drawn area and removes the whole proposal", () => {
    const host = document.createElement("div");
    const onRemove = vi.fn();
    renderEditGroups(host, groupEditsByArea(proposalEdits(AREA_A, 1)), onRemove);
    const buttons = host.querySelectorAll("button");
    expect(buttons).toHaveLength(1);
    buttons[0].dispatchEvent(new Event("click"));
    // Every id of the group, because three edits of four is not a proposal.
    expect(onRemove).toHaveBeenCalledWith(["e1", "e2", "e3", "e4"]);
  });

  it("says so when there is no rule yet, rather than rendering nothing", () => {
    const host = document.createElement("div");
    renderEditGroups(host, [], vi.fn());
    expect(host.textContent).toMatch(/no rule|nothing drawn|draw/i);
  });
});

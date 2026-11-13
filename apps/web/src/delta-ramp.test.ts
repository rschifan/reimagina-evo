// @vitest-environment jsdom
import { createExpression } from "@maplibre/maplibre-gl-style-spec";
import type { ExpressionSpecification } from "maplibre-gl";
import { describe, expect, it } from "vitest";

import type { ComparisonResponse, IndicatorSummary, IndicatorValueStats } from "./api";
import {
  activateIndicator,
  DELTA_NEUTRAL,
  DELTA_NEW,
  DELTA_STOPS,
  deltaDomainFromComparison,
  deltaTooltip,
  renderDeltaLegend,
  renderModeToggle,
  deltaFill,
  deltaRamp,
  deltaScopeText,
  rampDomainFromStats,
  rampScopeText,
  valueRamp,
  type RampDomain,
} from "./choropleth";
import { tileUrlFor } from "./scenario";

// ── an oracle that is not ours ─────────────────────────────────────────
//
// Every colour below is what MapLibre's own expression engine says a feature
// would be painted, not what a hand-written mirror of the expression says. A
// mirror can agree with the code under test about a mistake; the engine cannot,
// and it also refuses an expression it cannot type, so a ramp that would throw
// at `addLayer` fails here instead of on a user's map.

const FILL_SPEC = {
  type: "color",
  "property-type": "data-driven",
  expression: { interpolated: true, parameters: ["zoom", "feature"] },
} as never;

type Rgba = [number, number, number, number];

function paint(expression: ExpressionSpecification, properties: Record<string, unknown>): Rgba {
  const compiled = createExpression(expression, "layers[0].paint.fill-color", FILL_SPEC);
  if (compiled.result !== "success") {
    throw new Error(
      "MapLibre rejects this expression: " +
        compiled.value.map((e: { message: string }) => e.message).join("; "),
    );
  }
  const colour = compiled.value.evaluate({ zoom: 12 } as never, {
    type: 3,
    properties,
  } as never) as { r: number; g: number; b: number; a: number };
  return [colour.r, colour.g, colour.b, colour.a];
}

function hex(rgba: Rgba): string {
  return (
    "#" +
    rgba
      .slice(0, 3)
      .map((c) => Math.round(c * 255).toString(16).padStart(2, "0"))
      .join("")
  );
}

/** What a feature with this `delta` is painted, under a ramp. */
function colourAt(ramp: ExpressionSpecification, delta: number | null): string {
  return hex(paint(ramp, delta === null ? {} : { delta }));
}

/** Every colour literal an expression names — its palette. */
function paletteOf(expression: unknown): string[] {
  const found: string[] = [];
  const walk = (node: unknown): void => {
    if (typeof node === "string" && /^#[0-9a-f]{6}$/i.test(node)) found.push(node.toLowerCase());
    else if (Array.isArray(node)) node.forEach(walk);
  };
  walk(expression);
  return found;
}

/** What a *tile feature* is painted under the full delta fill. */
function styleFor(
  properties: { state?: string; delta?: number | null },
  limit = 40,
): Rgba {
  const props: Record<string, unknown> = {};
  if (properties.state !== undefined) props.state = properties.state;
  if (properties.delta !== undefined && properties.delta !== null) props.delta = properties.delta;
  return paint(deltaFill({ min: -limit, max: limit }), props);
}

const channelSpread = (hexColour: string): number => {
  const c = [1, 3, 5].map((i) => parseInt(hexColour.slice(i, i + 2), 16));
  return Math.max(...c) - Math.min(...c);
};

describe("the delta ramp", () => {
  it("centres the diverging ramp on zero", () => {
    const ramp = deltaRamp({ min: -40, max: 120 });
    expect(colourAt(ramp, 0)).toBe(DELTA_NEUTRAL);
  });

  // A domain of {-40, 120} is not a ramp from -40 to 120, it is a ramp from
  // -120 to 120 that happens to have seen less on one side. If it were
  // asymmetric, -40 would paint as the strongest decrease on the map and
  // read as the same size as +120.
  it("is symmetric about zero whatever the observed asymmetry", () => {
    const lopsided = deltaRamp({ min: -40, max: 120 });
    const even = deltaRamp({ min: -120, max: 120 });
    for (const delta of [-120, -40, -1, 1, 40, 120]) {
      expect(colourAt(lopsided, delta)).toBe(colourAt(even, delta));
    }
  });

  it("is neutral at zero: a grey, not a hue", () => {
    expect(channelSpread(DELTA_NEUTRAL)).toBeLessThanOrEqual(8);
  });

  it("reads blue for a decrease and red for an increase, each strongest at its end", () => {
    const ramp = deltaRamp({ min: -10, max: 10 });
    const down = colourAt(ramp, -10);
    const up = colourAt(ramp, 10);
    const [dr, , db] = [1, 3, 5].map((i) => parseInt(down.slice(i, i + 2), 16));
    const [ur, , ub] = [1, 3, 5].map((i) => parseInt(up.slice(i, i + 2), 16));
    expect(db).toBeGreaterThan(dr);
    expect(ur).toBeGreaterThan(ub);
  });

  it("moves away from neutral monotonically on both arms", () => {
    const ramp = deltaRamp({ min: -10, max: 10 });
    const dist = (a: string, b: string): number =>
      [1, 3, 5].reduce(
        (s, i) => s + (parseInt(a.slice(i, i + 2), 16) - parseInt(b.slice(i, i + 2), 16)) ** 2,
        0,
      );
    for (const sign of [-1, 1]) {
      const d = [0, 2.5, 5, 7.5, 10].map((m) => dist(colourAt(ramp, sign * m), DELTA_NEUTRAL));
      for (let i = 1; i < d.length; i++) expect(d[i]).toBeGreaterThan(d[i - 1]);
    }
  });

  it("clamps beyond the domain rather than extrapolating", () => {
    const ramp = deltaRamp({ min: -10, max: 10 });
    expect(colourAt(ramp, 500)).toBe(colourAt(ramp, 10));
    expect(colourAt(ramp, -500)).toBe(colourAt(ramp, -10));
  });

  it("survives a domain with nothing in it", () => {
    // max === min === 0 would be a divide-by-zero in the interpolation, which
    // MapLibre turns into NaN rather than an error.
    const ramp = deltaRamp({ min: 0, max: 0 });
    expect(colourAt(ramp, 0)).toBe(DELTA_NEUTRAL);
    const [first, second] = paletteOf(ramp);
    expect(first).toBeDefined();
    expect(second).toBeDefined();
  });

  it("uses a different palette from the value ramp", () => {
    const delta = new Set(paletteOf(deltaRamp({ min: -1, max: 1 })));
    const value = new Set(paletteOf(valueRamp(domain({ min: 0, max: 1 }))));
    expect(value.size).toBeGreaterThan(0);
    expect(delta).not.toEqual(value);
    // Not merely a different set: no colour in common, or a delta swatch could
    // be read as a point on the value ramp the same legend slot just showed.
    expect([...delta].filter((c) => value.has(c))).toEqual([]);
  });
});

describe("the delta fill: state decides, delta does not", () => {
  it("renders a `new` feature distinctly from an unchanged one", () => {
    expect(styleFor({ state: "new" })).not.toEqual(styleFor({ state: "unchanged", delta: 0 }));
  });

  it("paints `new` with its own fill, which is not on the ramp", () => {
    expect(hex(styleFor({ state: "new" }))).toBe(DELTA_NEW);
    expect(paletteOf(deltaRamp({ min: -40, max: 40 }))).not.toContain(DELTA_NEW);
  });

  // The tile emits a NULL delta for `new` — "from nothing to 120" is not a
  // change of 120 — and ST_AsMVT drops a NULL property, so the key is absent.
  // Absent is also what a ramp reads as zero. Keying on the missing delta is
  // how a block the scenario *created* gets painted "no change".
  it("styles on state, so a `new` block with no delta is not 'no change'", () => {
    const created = styleFor({ state: "new" });
    const nothing = styleFor({ state: "unchanged", delta: 0 });
    expect(hex(created)).not.toBe(hex(nothing));
    expect(hex(nothing)).toBe(DELTA_NEUTRAL);
  });

  it("styles on state even when a `new` feature does carry a delta", () => {
    expect(hex(styleFor({ state: "new", delta: 0 }))).toBe(DELTA_NEW);
    expect(hex(styleFor({ state: "new", delta: 30 }))).toBe(DELTA_NEW);
  });

  it("paints an unchanged block neutral whether or not it carries a delta", () => {
    expect(hex(styleFor({ state: "unchanged", delta: 0 }))).toBe(DELTA_NEUTRAL);
    expect(hex(styleFor({ state: "unchanged" }))).toBe(DELTA_NEUTRAL);
  });

  it("paints a changed block on the ramp", () => {
    expect(hex(styleFor({ state: "changed", delta: 40 }, 40))).toBe(
      colourAt(deltaRamp({ min: -40, max: 40 }), 40),
    );
    expect(hex(styleFor({ state: "changed", delta: -40 }, 40))).toBe(
      colourAt(deltaRamp({ min: -40, max: 40 }), -40),
    );
  });

  it("paints nothing for a state it does not know, rather than a wrong colour", () => {
    expect(styleFor({ state: "mystery", delta: 5 })[3]).toBe(0);
    expect(styleFor({ delta: 5 })[3]).toBe(0);
    // A `changed` block with no delta contradicts the tile contract.
    expect(styleFor({ state: "changed" })[3]).toBe(0);
  });
});

describe("the tile URL in delta mode", () => {
  const template = "http://api/tiles/indicator_overlay_tile/{z}/{x}/{y}";
  const base = { template, slug: "edificability_block_density", aoiId: "a-1" };

  it("asks for the delta tile, in the canonical spelling, when a scenario is shown as a difference", () => {
    const url = tileUrlFor({ ...base, scenarioId: "s-1", delta: true });
    expect(new URL(url.replace(/\{(\w)\}/g, "$1")).searchParams.get("delta")).toBe("true");
    expect(url).toContain("scenario_id=s-1");
  });

  it("does not ask for it in value mode", () => {
    expect(tileUrlFor({ ...base, scenarioId: "s-1", delta: false })).not.toContain("delta");
    expect(tileUrlFor({ ...base, scenarioId: "s-1" })).not.toContain("delta");
  });

  // The server answers delta-without-scenario with an empty tile, so the
  // client that sent it would be asking for a blank map by name.
  it("does not ask for a difference from nothing", () => {
    expect(tileUrlFor({ ...base, delta: true })).toBe(tileUrlFor(base));
    expect(tileUrlFor({ ...base, scenarioId: null, delta: true })).toBe(tileUrlFor(base));
  });

  it("appends it last, so the value-mode URL is a prefix of the delta-mode one", () => {
    const value = tileUrlFor({ ...base, scenarioId: "s-1" });
    expect(tileUrlFor({ ...base, scenarioId: "s-1", delta: true })).toBe(`${value}&delta=true`);
  });
});

// ── the delta domain ───────────────────────────────────────────────────

function comparison(
  rows: Array<{ delta: number | null; state: string }>,
): Pick<ComparisonResponse, "rows" | "summary"> {
  return {
    rows: rows.map((r, i) => ({
      feature_id: `f${i}`,
      baseline: r.state === "new" ? null : 1,
      scenario: 2,
      delta: r.delta,
      state: r.state,
    })),
    summary: {
      changed: rows.filter((r) => r.state === "changed").length,
      unchanged: rows.filter((r) => r.state === "unchanged").length,
      new: rows.filter((r) => r.state === "new").length,
      mean_delta: null,
      higher_is_better: true,
    },
  };
}

describe("deltaDomainFromComparison", () => {
  it("spans the largest change when there is only a handful", () => {
    const d = deltaDomainFromComparison(
      comparison([
        { delta: -40, state: "changed" },
        { delta: 120, state: "changed" },
        { delta: 0, state: "unchanged" },
      ]),
    );
    expect(d.limit).toBe(120);
    expect(d.changed).toBe(2);
    expect(d.basis).toBe("percentile");
  });

  it("takes the 95th percentile of |change|, so one outlier does not wash the rest to neutral", () => {
    const rows = Array.from({ length: 99 }, (_, i) => ({
      delta: (i % 2 ? 1 : -1) * (1 + i / 100),
      state: "changed",
    }));
    rows.push({ delta: 10_000, state: "changed" });
    const d = deltaDomainFromComparison(comparison(rows));
    expect(d.limit).toBeLessThan(3);
  });

  it("ignores `new` and `unchanged` rows: they carry no change to scale by", () => {
    const d = deltaDomainFromComparison(
      comparison([
        { delta: 5, state: "changed" },
        { delta: null, state: "new" },
        { delta: 0, state: "unchanged" },
        { delta: 900, state: "new" },
      ]),
    );
    expect(d.limit).toBe(5);
    expect(d.new).toBe(2);
  });

  it("says so when nothing changed, rather than inventing a span", () => {
    const d = deltaDomainFromComparison(comparison([{ delta: 0, state: "unchanged" }]));
    expect(d.basis).toBe("none");
    expect(d.changed).toBe(0);
    expect(deltaScopeText(d, "m")).toMatch(/no block changed/i);
  });

  it("states its basis in the legend, with the unit", () => {
    const d = deltaDomainFromComparison(comparison([{ delta: 8, state: "changed" }]));
    const text = deltaScopeText(d, "m");
    expect(text).toContain("95th percentile");
    expect(text).toMatch(/8(\.0+)? m/);
  });
});

// ── the uniform-values legend ──────────────────────────────────────────

function indicator(): IndicatorSummary {
  return {
    slug: "edificability_block_density",
    name: "Housing density",
    unit: "units/ha",
    grain: "block",
    valid_min: 0,
    valid_max: 2000,
    applies_to: ["block"],
  } as unknown as IndicatorSummary;
}

function stats(overrides: Partial<IndicatorValueStats>): IndicatorValueStats {
  return {
    count: 0,
    min: null,
    p05: null,
    p50: null,
    p95: null,
    max: null,
    histogram: null,
    ...overrides,
  };
}

function domain(over: Partial<RampDomain>): RampDomain {
  return {
    min: 0,
    max: 1,
    p50: null,
    count: 0,
    basis: "empty",
    fromStats: false,
    ...over,
  };
}

describe("a layer whose values are all identical", () => {
  // What the server actually returned for a scenario whose recompute wrote one
  // number to every block: percentile_cont over identical doubles is not
  // exactly that double. p05 and p95 differ in the 13th significant digit.
  const noisy = stats({
    count: 16,
    min: 1333,
    max: 1333,
    p05: 1333.0000000000002,
    p50: 1333,
    p95: 1333.0000000000014,
  });

  it("is uniform, not a percentile ramp over a single point", () => {
    const d = rampDomainFromStats(noisy, indicator());
    expect(d.basis).toBe("uniform");
    expect(rampScopeText(d)).toMatch(/all 16 values identical \(1333\)/);
    expect(rampScopeText(d)).not.toMatch(/p05/);
  });

  it("is uniform at the epsilon the server really produces, at any magnitude", () => {
    for (const v of [0.003, 1, 1333, 123456789]) {
      const eps = v * 2e-16 * 8;
      const d = rampDomainFromStats(
        stats({ count: 9, min: v, max: v, p05: v - eps, p50: v, p95: v + eps }),
        indicator(),
      );
      expect(d.basis).toBe("uniform");
    }
  });

  it("does not take a narrow but real spread for uniform", () => {
    // 1333 to 1333.001 is a spread of under a millionth, and it is a spread:
    // the values differ, and a ramp over it is honest.
    const d = rampDomainFromStats(
      stats({ count: 50, min: 1332.9, max: 1333.2, p05: 1333.0, p50: 1333.0005, p95: 1333.001 }),
      indicator(),
    );
    expect(d.basis).toBe("percentile");
    expect(d.min).toBe(1333.0);
    expect(d.max).toBe(1333.001);
  });

  it("falls to min-max, not uniform, when p05 = p95 within noise but min and max really differ", () => {
    const d = rampDomainFromStats(
      stats({
        count: 1200,
        min: 250,
        max: 1500,
        p05: 900,
        p50: 900,
        p95: 900.0000000000009,
      }),
      indicator(),
    );
    expect(d.basis).toBe("min-max");
  });
});


// ── the wiring: what activateIndicator hands MapLibre ──────────────────
//
// The expressions above are right in isolation and could still be unplugged:
// a fill that is never installed, or a source that never asks for the delta
// tile, leaves every unit test green and the map blank or wrong.

function fakeMap() {
  const added = { sources: [] as Array<Record<string, unknown>>, layers: [] as Array<Record<string, unknown>> };
  const present = new Set<string>();
  const map = {
    getLayer: (id: string) => (present.has(id) ? {} : undefined),
    getSource: (id: string) => (present.has(id) ? {} : undefined),
    addSource: (id: string, spec: Record<string, unknown>) => {
      present.add(id);
      added.sources.push(spec);
    },
    addLayer: (spec: Record<string, unknown>) => {
      present.add(spec.id as string);
      added.layers.push(spec);
    },
    removeLayer: (id: string) => present.delete(id),
    removeSource: (id: string) => present.delete(id),
    getCanvasContainer: () => document.createElement("div"),
    setPaintProperty: () => undefined,
    getPaintProperty: () => undefined,
    setFeatureState: () => undefined,
    removeFeatureState: () => undefined,
    on: () => undefined,
    off: () => undefined,
  };
  return { map: map as never, added };
}

const config = {
  tilesUrlTemplate: "http://api/tiles/indicator_overlay_tile/{z}/{x}/{y}",
  sourceLayer: "indicator_overlay",
  minzoom: 10,
  maxzoom: 16,
};

const deltaDomain = {
  limit: 40,
  changed: 3,
  unchanged: 1,
  new: 1,
  basis: "percentile" as const,
};

describe("activateIndicator", () => {
  const value = domain({ min: 0, max: 100, count: 5, basis: "percentile", fromStats: true });

  it("draws the delta tile on the delta fill when a scenario is shown as a difference", () => {
    const { map, added } = fakeMap();
    activateIndicator(
      map, config, indicator(), value,
      undefined, "a-1", undefined, "block", "s-1", deltaDomain,
    );
    expect(String((added.sources[0].tiles as string[])[0])).toContain("&delta=true");
    const fill = (added.layers[0].paint as Record<string, unknown>)["fill-color"];
    expect(fill).toEqual(deltaFill({ min: -40, max: 40 }));
  });

  it("draws values on the value ramp otherwise, whatever a stale delta domain says", () => {
    for (const [scenario, delta] of [["s-1", null], ["s-1", undefined], [null, deltaDomain]] as const) {
      const { map, added } = fakeMap();
      activateIndicator(
        map, config, indicator(), value,
        undefined, "a-1", undefined, "block", scenario, delta,
      );
      expect(String((added.sources[0].tiles as string[])[0])).not.toContain("delta");
      const fill = (added.layers[0].paint as Record<string, unknown>)["fill-color"];
      expect(fill).toEqual(valueRamp(value));
    }
  });
});


describe("what the difference view says in words", () => {
  const ind = { name: "Housing density", unit: "units/ha" };

  it("tells the three states apart in the tooltip, since colour alone cannot", () => {
    expect(deltaTooltip({ state: "new" }, ind)?.detail).toMatch(/new in this scenario/);
    expect(deltaTooltip({ state: "unchanged", delta: 0 }, ind)?.detail).toMatch(/no change/);
    expect(deltaTooltip({ state: "changed", delta: 12.5 }, ind)?.detail).toMatch(/^\+12\.5.*units\/ha/);
    expect(deltaTooltip({ state: "changed", delta: -3 }, ind)?.detail).toMatch(/^[-−]3.*units\/ha/);
  });

  it("says nothing for a feature it cannot describe", () => {
    expect(deltaTooltip({ state: "changed" }, ind)).toBeNull();
    expect(deltaTooltip({}, ind)).toBeNull();
  });

  it("draws the legend from the same constants as the fill, with a label for every state", () => {
    const host = document.createElement("div");
    renderDeltaLegend(host, indicator(), deltaDomain);
    const html = host.innerHTML.toLowerCase();
    for (const colour of [DELTA_NEUTRAL, DELTA_NEW, ...DELTA_STOPS]) {
      expect(html).toContain(colour);
    }
    expect(host.textContent).toMatch(/new in this scenario \(1\)/);
    expect(host.textContent).toMatch(/no change \(1\)/);
    expect(host.textContent).toMatch(/95th percentile/);
  });

  it("offers the view switch only while a scenario is active", () => {
    const host = document.createElement("div");
    renderModeToggle(host, false, "value", () => undefined);
    expect(host.hidden).toBe(true);
    expect(host.querySelectorAll("button")).toHaveLength(0);

    const picked: string[] = [];
    renderModeToggle(host, true, "value", (m) => picked.push(m));
    expect(host.hidden).toBe(false);
    const buttons = [...host.querySelectorAll<HTMLButtonElement>("button")];
    expect(buttons.map((b) => b.getAttribute("aria-pressed"))).toEqual(["true", "false"]);
    buttons[0].click(); // already active: not a change
    buttons[1].click();
    expect(picked).toEqual(["delta"]);
  });
});

import { describe, expect, it } from "vitest";

import { fuzzyScore, highlight, rankCommands, type Command } from "./commands";

function cmd(
  id: string,
  title: string,
  group = "Indicator",
  extra: Partial<Command> = {},
): Command {
  return { id, title, group, run: () => {}, ...extra };
}

const CATALOG: Command[] = [
  cmd("fit", "Fit map to the active AOI", "View"),
  cmd("panel", "Hide panel", "View"),
  cmd("rect", "Draw rectangle selection", "Select"),
  cmd("bbd", "Block Building Density"),
  cmd("bc", "Block Compactness (Polsby-Popper)"),
  cmd("liv", "Livability Score"),
  cmd("health", "Distance to health facility (network)"),
  cmd("blocks", "Hide Blocks (manzanas)", "Layer"),
];

describe("fuzzyScore", () => {
  it("matches a subsequence, not only a substring", () => {
    // What makes a palette feel fast: the user should not have to remember
    // the exact wording.
    expect(fuzzyScore("Distance to health facility", "dhf")).not.toBeNull();
  });

  it("ranks word starts above scattered letters", () => {
    const initials = fuzzyScore("Block Building Density", "bbd");
    const scattered = fuzzyScore("Blob of absurd data", "bbd");
    expect(initials!.score).toBeGreaterThan(scattered?.score ?? -Infinity);
  });

  it("ranks a contiguous run above the same letters apart", () => {
    // Holding word starts equal — both begin with `b` — so this isolates
    // adjacency. "Bloom" is b-l-o straight through; "Below" has a gap.
    const run = fuzzyScore("Bloom", "blo");
    const apart = fuzzyScore("Below", "blo");
    expect(run!.score).toBeGreaterThan(apart!.score);
  });

  it("ranks initials above a single word start", () => {
    // Word starts are the primary signal, deliberately above adjacency:
    // initials are how people abbreviate, and "bbd" for "Block Building
    // Density" has to beat any single-word partial.
    const initials = fuzzyScore("Be low on oxygen", "blo");
    const oneStart = fuzzyScore("Bloom", "blo");
    expect(initials!.score).toBeGreaterThan(oneStart!.score);
  });

  // ⚠ The gate. Subsequence matching is what finds "Distance to health
  // facility" from "dhf"; it is also why "blo" matched "Livability Score" —
  // Liva(b)ility Sc(o)re with an `l` between. Legitimate as a subsequence,
  // useless as a result, and the palette filled with rows whose highlighted
  // letters sat three words apart.
  it("rejects a scattered match with no word start and no run", () => {
    expect(fuzzyScore("Livability Score", "blo")).toBeNull();
  });

  it("still accepts a single character anywhere", () => {
    // One letter cannot be "scattered", and refusing it would make the
    // first keystroke of every query empty the list.
    expect(fuzzyScore("Livability Score", "y")).not.toBeNull();
  });

  it("returns null when a character is simply absent", () => {
    expect(fuzzyScore("Block Density", "zzz")).toBeNull();
  });

  it("ignores spaces in the query", () => {
    expect(fuzzyScore("Block Building Density", "b b d")).not.toBeNull();
  });

  it("is case-insensitive in both directions", () => {
    expect(fuzzyScore("BLOCK DENSITY", "blo")).not.toBeNull();
    expect(fuzzyScore("block density", "BLO")).not.toBeNull();
  });
});

describe("rankCommands", () => {
  it("returns everything in registration order for an empty query", () => {
    // An empty palette is a menu, and a menu's job is to show what exists.
    const ranked = rankCommands(CATALOG, "");
    expect(ranked.map((r) => r.command.id)).toEqual(CATALOG.map((c) => c.id));
  });

  it("puts the best match first", () => {
    const ranked = rankCommands(CATALOG, "blo");
    expect(ranked[0].command.id).toBe("bbd");
  });

  it("drops the noise the gate exists for", () => {
    const ids = rankCommands(CATALOG, "blo").map((r) => r.command.id);
    expect(ids).not.toContain("liv");
  });

  // ⚠ Ranking alone interleaves groups, and the renderer emits a heading
  // whenever the group changes — so a score-sorted list produced
  // "Indicator", "Layer", "Indicator" again, which reads as a rendering bug
  // and makes the list impossible to skim by section.
  it("keeps each group contiguous", () => {
    const groups = rankCommands(CATALOG, "d").map((r) => r.command.group);
    const seen = new Set<string>();
    let previous = "";
    for (const group of groups) {
      if (group !== previous) {
        expect(seen.has(group)).toBe(false);
        seen.add(group);
        previous = group;
      }
    }
  });

  it("orders the groups by their best match, not alphabetically", () => {
    const groups = rankCommands(CATALOG, "blo").map((r) => r.command.group);
    expect(groups[0]).toBe("Indicator");
  });

  it("matches on keywords without claiming to highlight them", () => {
    // A highlight the user cannot find in the row reads as a bug.
    const withSlug = [
      cmd("x", "Urban Form Composite", "Indicator", {
        keywords: "urban_form_block_composite",
      }),
    ];
    const ranked = rankCommands(withSlug, "composite");
    expect(ranked).toHaveLength(1);
    const slugOnly = rankCommands(withSlug, "urban_form_blo");
    expect(slugOnly).toHaveLength(1);
    expect(slugOnly[0].ranges).toEqual([]);
  });

  it("finds nothing rather than everything when nothing matches", () => {
    expect(rankCommands(CATALOG, "qqqq")).toEqual([]);
  });
});

describe("highlight", () => {
  it("splits a title into matched and unmatched spans", () => {
    expect(highlight("Block Density", [[0, 3]])).toEqual([
      { text: "Blo", match: true },
      { text: "ck Density", match: false },
    ]);
  });

  it("returns the whole string when nothing matched", () => {
    expect(highlight("Block", [])).toEqual([{ text: "Block", match: false }]);
  });

  it("handles a match that runs to the end", () => {
    expect(highlight("Block", [[3, 5]])).toEqual([
      { text: "Blo", match: false },
      { text: "ck", match: true },
    ]);
  });
});

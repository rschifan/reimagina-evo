/**
 * The command registry and its matcher — the data half of the palette.
 *
 * Separated from the UI so the ranking can be tested without a DOM, because
 * ranking is the part that decides whether the palette feels like a tool or
 * like a search box that usually fails.
 *
 * **Why a palette at all.** Everything this application can do has to be
 * found by looking at it: the draw tools are 28 px glyphs, switching AOI or
 * indicator means finding the right `<select>` in a 1,173 px scroll, and
 * there is no keyboard path to any of it. "Easy but powerful" is a layering
 * problem — leave the surface quiet, and put the whole command surface one
 * keystroke away — and a palette is the established answer.
 *
 * It is also the only place in the application that can *list* what the
 * application does. A user who opens it learns the feature set.
 */

export interface Command {
  /** Stable id, for tests and for remembering recent commands. */
  id: string;
  /** What the user reads. */
  title: string;
  /** Group heading — "Indicator", "Layer", "Go to", "Draw". */
  group: string;
  /** Optional right-aligned hint: a keyboard shortcut, or current state. */
  hint?: string;
  /** Extra words that should match but need not be shown. */
  keywords?: string;
  /** Marks the command as describing the current state. */
  active?: boolean;
  run: () => void | Promise<void>;
}

export interface ScoredCommand {
  command: Command;
  score: number;
  /** Index pairs to highlight in ``title``, as [start, end) ranges. */
  ranges: Array<[number, number]>;
}

/**
 * Subsequence match of ``query`` against ``text``, or ``null``.
 *
 * Subsequence rather than substring because that is what makes a palette
 * feel fast: "dhf" should find "Distance to health facility" without the
 * user knowing the exact wording. Substring matching would force them to
 * remember the phrase, which defeats the point of not having to.
 *
 * The score rewards two things, in this order:
 *
 * - **Matches at word starts.** "bd" should rank "Block Density" above
 *   "Buildings (…) <something>d", because initials are how people abbreviate.
 * - **Runs of adjacent characters.** A contiguous hit is a stronger signal
 *   than the same letters scattered across the string.
 */
export function fuzzyScore(
  text: string,
  query: string,
): { score: number; ranges: Array<[number, number]> } | null {
  if (!query) return { score: 0, ranges: [] };
  let wordStarts = 0;
  let longestRun = 1;
  const haystack = text.toLowerCase();
  const needle = query.toLowerCase();

  let score = 0;
  let at = 0;
  let runStart = -1;
  let previousIndex = -2;
  let run = 1;
  const ranges: Array<[number, number]> = [];

  for (const char of needle) {
    // Spaces in a query are separators, not characters to find.
    if (char === " ") continue;
    const found = haystack.indexOf(char, at);
    if (found === -1) return null;

    const atWordStart =
      found === 0 || /[\s\-_/(.·]/.test(haystack[found - 1] ?? "");
    if (atWordStart) {
      score += 8;
      wordStarts += 1;
    }
    if (found === previousIndex + 1) {
      score += 5;
      run += 1;
      longestRun = Math.max(longestRun, run);
    } else {
      run = 1;
      // Closing a run: record it, and charge for the gap that follows.
      if (runStart >= 0) ranges.push([runStart, previousIndex + 1]);
      runStart = found;
      score -= Math.min(6, found - at);
    }
    if (runStart < 0) runStart = found;
    previousIndex = found;
    at = found + 1;
  }
  if (runStart >= 0) ranges.push([runStart, previousIndex + 1]);

  // ⚠ A quality gate, not just a score.
  //
  // Subsequence matching is what makes "dhf" find "Distance to health
  // facility", and it is also why "blo" matched "Livability Score" —
  // Liva(b)ility Sc(o)re, with an `l` in between. Legitimate as a
  // subsequence and useless as a result: the palette filled with rows whose
  // highlighted letters were scattered three words apart, which reads as the
  // search being broken rather than generous.
  //
  // So a multi-character query has to land somewhere a human would:
  // either on word starts (how people abbreviate) or as a run of adjacent
  // characters (how people type a prefix). One or the other, not both.
  if (needle.replace(/ /g, "").length > 1 && wordStarts === 0 && longestRun < 2) {
    return null;
  }

  // Shorter titles win ties: with everything else equal, the more specific
  // command is the one with less text around the match.
  score -= haystack.length * 0.05;
  return { score, ranges };
}

/**
 * Rank ``commands`` against ``query``.
 *
 * With an empty query this returns everything in registration order, which
 * is deliberate: an empty palette is a menu, and a menu's job is to show
 * what exists rather than to guess.
 */
export function rankCommands(
  commands: Command[],
  query: string,
): ScoredCommand[] {
  const trimmed = query.trim();
  if (!trimmed) {
    return commands.map((command) => ({ command, score: 0, ranges: [] }));
  }
  const scored: ScoredCommand[] = [];
  for (const command of commands) {
    const onTitle = fuzzyScore(command.title, trimmed);
    // Keywords and the group both match, but neither highlights: showing a
    // hit the user cannot see in the row reads as a bug rather than a
    // feature, so they contribute score and no ranges.
    const elsewhere = command.keywords
      ? fuzzyScore(`${command.group} ${command.keywords}`, trimmed)
      : fuzzyScore(command.group, trimmed);
    if (!onTitle && !elsewhere) continue;
    const score = Math.max(
      onTitle ? onTitle.score : -Infinity,
      elsewhere ? elsewhere.score - 12 : -Infinity,
    );
    scored.push({ command, score, ranges: onTitle?.ranges ?? [] });
  }
  // Stable within equal scores: `Array.prototype.sort` is required to be
  // stable, so commands keep registration order when the query cannot
  // separate them, and the list does not reshuffle as the user types.
  scored.sort((a, b) => b.score - a.score);
  return groupContiguously(scored);
}

/**
 * Reorder so each group appears once, groups ordered by their best hit.
 *
 * ⚠ Ranking alone interleaves them. The renderer emits a heading whenever
 * the group changes, so a score-sorted list produced "Indicator", "Layer",
 * "Indicator" again — which reads as a rendering bug rather than as
 * relevance ordering, and makes the list impossible to skim by section.
 *
 * Relevance still decides the order *of* the groups and the order *within*
 * each one. Only the interleaving goes.
 */
function groupContiguously(scored: ScoredCommand[]): ScoredCommand[] {
  const groups = new Map<string, ScoredCommand[]>();
  for (const entry of scored) {
    const bucket = groups.get(entry.command.group);
    if (bucket) bucket.push(entry);
    else groups.set(entry.command.group, [entry]);
  }
  // `Map` preserves insertion order, and insertion order here is the order
  // each group's best-scoring member appeared — so the group containing the
  // single best match comes first.
  return [...groups.values()].flat();
}

/** Split ``text`` into matched / unmatched spans for rendering. */
export function highlight(
  text: string,
  ranges: Array<[number, number]>,
): Array<{ text: string; match: boolean }> {
  if (ranges.length === 0) return [{ text, match: false }];
  const parts: Array<{ text: string; match: boolean }> = [];
  let at = 0;
  for (const [start, end] of ranges) {
    if (start > at) parts.push({ text: text.slice(at, start), match: false });
    parts.push({ text: text.slice(start, end), match: true });
    at = end;
  }
  if (at < text.length) parts.push({ text: text.slice(at), match: false });
  return parts;
}

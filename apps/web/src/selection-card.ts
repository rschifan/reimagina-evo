/**
 * The selection summary card, bottom-right of the map.
 *
 * It must not flicker while a shape is drawn or dragged: a new selection keeps
 * the previous numbers on screen, dimmed, until the one response for the
 * settled shape replaces them in a single paint. The card never shows a
 * placeholder, and stays hidden until its first answer arrives; once shown it
 * stays shown (a failure note dims like numbers do) until the selection is
 * cleared. main.ts owns the request (one per settled change, the previous one
 * aborted).
 */
import type { IndicatorSummary, IndicatorValueStats } from "./api";
import { grainCount, grainLabel } from "./choropleth";
import { formatValue, shareText } from "./format";

/** `/stats`-shaped numbers for the selection; just `count` with no indicator. */
export type SelectionStats = Pick<IndicatorValueStats, "count">
  & Partial<Pick<IndicatorValueStats, "p05" | "p50" | "p95" | "count_at_cap">>;

export interface CardState {
  visible: boolean;
  /** A response (numbers or a failure) arrived since the selection appeared. */
  answered: boolean;
  /** The selection changed since `stats` were fetched. */
  stale: boolean;
  /** Bumped on every change: a response for an older token is dropped. */
  token: number;
  /** `null` before the first response, or after a failed one. */
  stats: SelectionStats | null;
}

export type CardEvent =
  | { type: "selection-changed" }
  /** The indicator or grain is changing under a standing selection. */
  | { type: "context-changed" }
  | { type: "response"; token: number; stats: SelectionStats | null }
  | { type: "cleared" };

export const INITIAL_CARD: CardState = { visible: false, answered: false, stale: false, token: 0, stats: null };

export function nextCardState(state: CardState, event: CardEvent): CardState {
  switch (event.type) {
    case "selection-changed":
      return { ...state, visible: true, stale: true, token: state.token + 1 };
    case "context-changed":
      // Dim what is shown and drop any answer in flight; ask nothing yet.
      if (!state.visible) return state;
      return { ...state, stale: true, token: state.token + 1 };
    case "response":
      if (event.token !== state.token || !state.visible) return state;
      return { ...state, answered: true, stale: false, stats: event.stats };
    case "cleared":
      return { visible: false, answered: false, stale: false, token: state.token + 1, stats: null };
  }
}

/** What the card's numbers are about: an indicator at a grain, and its AOI-wide stats. */
export interface CardContext {
  indicator: IndicatorSummary | null;
  grain: string | null;
  aoiStats: IndicatorValueStats | null;
}

/**
 * The context while `indicator` is being rendered at `grain`: the card asks
 * about what is being drawn, not what was, and has no AOI median to compare
 * with until that indicator's stats arrive. With no indicator: blocks.
 */
export function renderingContext(indicator: IndicatorSummary | null, grain: string | null): CardContext {
  return { indicator, grain: indicator ? grain : "block", aoiStats: null };
}

/**
 * Fill `container` from `state`. `ctx` is what the stats were fetched for:
 * its indicator (`null`: block count only), its grain, and the legend's
 * AOI-wide numbers, for the comparison line.
 */
export function renderSelectionCard(
  container: HTMLElement,
  state: CardState,
  { indicator, grain, aoiStats }: CardContext,
): void {
  // Hidden only before the first answer: re-hiding a shown card while a
  // request is pending would re-open it on the phone sheet over a later pin.
  container.hidden = !state.visible || !state.answered;
  container.classList.toggle("is-stale", state.stale);
  if (container.hidden) return;

  const line = (cls: string, text: string): HTMLElement => {
    const p = document.createElement("p");
    p.className = cls;
    p.textContent = text;
    return p;
  };
  const stats = state.stats;
  if (stats === null) {
    container.replaceChildren(line("selection-card__note", "Could not summarise this selection."));
    return;
  }
  const word = grainLabel(grain).toLowerCase();
  const lines = [line("selection-card__title", `Selection · ${grainCount(stats.count, grain)}`)];
  if (indicator && stats.count === 0) {
    lines.push(line("selection-card__note", `No ${word} inside`));
  } else if (indicator && stats.p50 != null) {
    lines.push(line("selection-card__value", `Median ${formatValue(stats.p50, indicator)}`));
    if (indicator.cap_value != null) {
      lines.push(line("selection-card__note", `${shareText(stats.count_at_cap ?? 0, stats.count)}% at the cap`));
    } else if (stats.p05 != null && stats.p95 != null) {
      lines.push(line(
        "selection-card__note",
        `5–95%: ${formatValue(stats.p05, { unit: null })} – ${formatValue(stats.p95, indicator)}`,
      ));
    }
    if (aoiStats?.p50 != null) {
      lines.push(line("selection-card__note", `Whole AOI: median ${formatValue(aoiStats.p50, indicator)}`));
    }
  }
  container.replaceChildren(...lines);
}

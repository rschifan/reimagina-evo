/**
 * The selection summary card, bottom-right of the map.
 *
 * It must not flicker while a shape is drawn or dragged: a new selection keeps
 * the previous numbers on screen, dimmed, until the one response for the
 * settled shape replaces them in a single paint. The card never shows a
 * placeholder, and stays hidden until its first numbers arrive. main.ts owns
 * the request (one per settled change, the previous one aborted).
 */
import type { IndicatorSummary, IndicatorValueStats } from "./api";
import { grainCount, grainLabel } from "./choropleth";
import { formatValue, shareText } from "./format";

/** `/stats`-shaped numbers for the selection; just `count` with no indicator. */
export type SelectionStats = Pick<IndicatorValueStats, "count">
  & Partial<Pick<IndicatorValueStats, "p05" | "p50" | "p95" | "count_at_cap">>;

export interface CardState {
  visible: boolean;
  /** The selection changed since `stats` were fetched. */
  stale: boolean;
  /** Bumped on every change: a response for an older token is dropped. */
  token: number;
  /** `null` before the first response, or after a failed one. */
  stats: SelectionStats | null;
}

export type CardEvent =
  | { type: "selection-changed" }
  | { type: "response"; token: number; stats: SelectionStats | null }
  | { type: "cleared" };

export const INITIAL_CARD: CardState = { visible: false, stale: false, token: 0, stats: null };

export function nextCardState(state: CardState, event: CardEvent): CardState {
  switch (event.type) {
    case "selection-changed":
      return { ...state, visible: true, stale: true, token: state.token + 1 };
    case "response":
      if (event.token !== state.token || !state.visible) return state;
      return { ...state, stale: false, stats: event.stats };
    case "cleared":
      return { visible: false, stale: false, token: state.token + 1, stats: null };
  }
}

/**
 * Fill `container` from `state`. `indicator` is the one the stats were
 * fetched for (`null`: block count only); `aoiStats` are the legend's
 * AOI-wide numbers, for the comparison line.
 */
export function renderSelectionCard(
  container: HTMLElement,
  state: CardState,
  indicator: IndicatorSummary | null,
  aoiStats: { p50: number | null } | null,
  grain: string | null = "block",
): void {
  const pending = state.stats === null && state.stale;
  container.hidden = !state.visible || pending;
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

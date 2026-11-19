/**
 * Phone layout: the inspector and the selection card share one bottom sheet,
 * and the one opened last is the one shown. CSS hides the card marked
 * `data-behind`; this decides which that is.
 */
export type SheetCard = "inspector" | "selection";

/** `prev`/`now`: which cards are visible. Returns the card to show, or null. */
export function sheetTop(
  top: SheetCard | null,
  prev: Record<SheetCard, boolean>,
  now: Record<SheetCard, boolean>,
): SheetCard | null {
  for (const c of ["inspector", "selection"] as const) {
    if (now[c] && !prev[c]) return c; // newly opened wins
  }
  if (top && now[top]) return top;
  return now.inspector ? "inspector" : now.selection ? "selection" : null;
}

/**
 * Watch both cards' `hidden` and mark the one behind. Returns `raise`, which
 * `pin()` calls on a real new pin: a re-render of the same pin (indicator
 * switched, values arrived) must not jump over a newer selection card, and
 * `hidden` alone cannot tell a re-pin (hide, then show in one task) from that.
 */
export function wireSheet(
  inspector: HTMLElement | null,
  selection: HTMLElement | null,
): (card: SheetCard) => void {
  if (!inspector || !selection) return () => undefined;
  const els = { inspector, selection };
  let prev = { inspector: !inspector.hidden, selection: !selection.hidden };
  let top: SheetCard | null = null;
  const sync = (): void => {
    const now = { inspector: !inspector.hidden, selection: !selection.hidden };
    top = sheetTop(top, prev, now);
    prev = now;
    for (const c of ["inspector", "selection"] as const) {
      els[c].toggleAttribute("data-behind", now[c] && top !== c);
    }
  };
  const obs = new MutationObserver(sync);
  for (const el of [inspector, selection]) obs.observe(el, { attributes: true, attributeFilter: ["hidden"] });
  return (card) => {
    top = card;
    sync();
  };
}

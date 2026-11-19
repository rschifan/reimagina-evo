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

/** Watch both cards' `hidden` and mark the one behind. */
export function wireSheet(inspector: HTMLElement | null, selection: HTMLElement | null): void {
  if (!inspector || !selection) return;
  const els = { inspector, selection };
  let prev = { inspector: !inspector.hidden, selection: !selection.hidden };
  let top: SheetCard | null = null;
  const sync = (records: MutationRecord[] = []): void => {
    // A pin on another block re-renders the card within one task (hide, then
    // show), which `hidden` alone cannot tell from nothing happening.
    if (records.some((r) => r.type === "childList")) prev = { ...prev, inspector: false };
    const now = { inspector: !inspector.hidden, selection: !selection.hidden };
    top = sheetTop(top, prev, now);
    prev = now;
    for (const c of ["inspector", "selection"] as const) {
      els[c].toggleAttribute("data-behind", now[c] && top !== c);
    }
  };
  const obs = new MutationObserver(sync);
  for (const el of [inspector, selection]) obs.observe(el, { attributes: true, attributeFilter: ["hidden"] });
  obs.observe(inspector, { childList: true });
}

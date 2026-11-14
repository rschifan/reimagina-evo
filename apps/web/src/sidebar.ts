/**
 * Collapsing the map sidebar, and turning it into a drawer when narrow.
 *
 * Two problems, one control.
 *
 * **Wide.** The panel is 1,173 px of content in an 853 px column on a
 * laptop — one unbroken scroll of four stacked sections — and there was no
 * way to put it away. A user who wants the map has to live with a third of
 * the window spent on controls they are not currently touching.
 *
 * **Narrow.** There was no breakpoint at all: the sidebar was a fixed 320 px
 * column at every width. Measured at 390 px, it took **82 %** of the
 * viewport and left the map a 140 px strip, with the draw tools overlapping
 * MapLibre's zoom control. The page was not usable on a phone, and nothing
 * said so.
 *
 * Below the breakpoint the same control opens and closes an overlay drawer,
 * and the drawer starts *closed* — the map is what the page is for, and a
 * panel covering it on arrival hides the thing the user came for.
 */

/** Where a sidebar beside a map stops being a sidebar. Matches `style.css`. */
export const DRAWER_BREAKPOINT = 860;

export interface SidebarController {
  collapsed: () => boolean;
  toggle: () => void;
  set: (collapsed: boolean) => void;
  dispose: () => void;
}

/**
 * Whether the drawer layout applies at this width.
 *
 * Reads `matchMedia` rather than `innerWidth` so it cannot disagree with the
 * stylesheet: one breakpoint, expressed once, queried the same way the CSS
 * evaluates it.
 */
export function isDrawerWidth(): boolean {
  return window.matchMedia(`(max-width: ${DRAWER_BREAKPOINT}px)`).matches;
}

export function enableSidebar(
  app: HTMLElement,
  toggle: HTMLButtonElement,
  scrim: HTMLElement | null,
  onChange?: (collapsed: boolean) => void,
): SidebarController {
  let collapsed = isDrawerWidth();

  const glyph = toggle.querySelector<HTMLElement>("[aria-hidden]");
  const label = toggle.querySelector<HTMLElement>(".sr-only");

  const paint = (): void => {
    app.classList.toggle("is-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    const action = collapsed ? "Show panel" : "Hide panel";
    if (glyph) glyph.textContent = collapsed ? "⟩" : "⟨";
    if (label) label.textContent = action;
    // The title carries the shortcut because the control is a glyph: a user
    // who finds the button learns the key, which is the only route to the
    // keyboard path that does not require reading the shortcuts sheet.
    toggle.title = `${action} (\\)`;
    if (scrim) scrim.hidden = collapsed;
    onChange?.(collapsed);
  };

  const set = (next: boolean): void => {
    if (next === collapsed) return;
    collapsed = next;
    paint();
  };

  const onToggle = (): void => set(!collapsed);
  const onScrim = (): void => set(true);

  // ⚠ Esc closes the drawer, and only the drawer. On a wide screen the
  // sidebar is part of the layout rather than something laid over the map,
  // so collapsing it on Esc would be an unrelated side effect of a key the
  // user pressed to dismiss something else — the draw mode, the palette, a
  // tooltip.
  const onKey = (event: KeyboardEvent): void => {
    if (event.key !== "Escape") return;
    if (!collapsed && isDrawerWidth()) set(true);
  };

  // Crossing the breakpoint changes what "collapsed" means, so re-paint and
  // close. A drawer that inherits "open" from the wide layout lands on the
  // user as a panel covering the map they were just looking at.
  const media = window.matchMedia(`(max-width: ${DRAWER_BREAKPOINT}px)`);
  const onMedia = (event: MediaQueryListEvent): void => set(event.matches);

  toggle.addEventListener("click", onToggle);
  scrim?.addEventListener("click", onScrim);
  document.addEventListener("keydown", onKey);
  media.addEventListener("change", onMedia);
  paint();

  return {
    collapsed: () => collapsed,
    toggle: onToggle,
    set,
    dispose: () => {
      toggle.removeEventListener("click", onToggle);
      scrim?.removeEventListener("click", onScrim);
      document.removeEventListener("keydown", onKey);
      media.removeEventListener("change", onMedia);
    },
  };
}

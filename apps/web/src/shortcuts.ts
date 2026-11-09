/**
 * Single-key shortcuts for the map page, and the list the sheet renders.
 *
 * Kept apart from the palette so the two can disagree about *what* they run
 * without disagreeing about *when*: both refuse to fire while the user is
 * typing, and both go through the same guard to do it.
 *
 * The set is deliberately small. A shortcut nobody remembers is a key the
 * user cannot type in a search box, so the bar for adding one is that it is
 * worth losing that key everywhere on the page.
 */
import { isTypingTarget } from "./palette";
import type { Shortcut } from "./palette";

/** Shown in the `?` sheet, and the source of the hints in the palette. */
export const SHORTCUTS: Shortcut[] = [
  { keys: "⌘K", description: "Command palette" },
  { keys: "?", description: "This list" },
  { keys: "\\", description: "Show or hide the panel" },
  { keys: "f", description: "Fit the map to the active AOI" },
  { keys: "r", description: "Draw a rectangle selection" },
  { keys: "p", description: "Draw a polygon selection" },
  { keys: "c", description: "Draw a circle selection" },
  { keys: "esc", description: "Clear the selection" },
];

export interface ShortcutHandlers {
  onPalette: () => void;
  onFit: () => void;
  onPanel: () => void;
  onDraw: (shape: "rectangle" | "polygon" | "circle") => void;
  onClear: () => void;
  isPaletteOpen: () => boolean;
}

export function wireShortcuts(handlers: ShortcutHandlers): () => void {
  const onKey = (event: KeyboardEvent): void => {
    // The palette owns the keyboard while it is open, including Escape.
    if (handlers.isPaletteOpen()) return;
    // ⚠ Never while the user is typing. A bare `f` that fits the map while
    // someone is filling in a field makes the field unusable, and the bug
    // reads as "the map jumps on its own".
    if (isTypingTarget(event.target)) return;
    // A modified key belongs to the browser or the palette, not here.
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    switch (event.key) {
      case "\\":
        event.preventDefault();
        handlers.onPanel();
        break;
      case "f":
        event.preventDefault();
        handlers.onFit();
        break;
      case "r":
        event.preventDefault();
        handlers.onDraw("rectangle");
        break;
      case "p":
        event.preventDefault();
        handlers.onDraw("polygon");
        break;
      case "c":
        event.preventDefault();
        handlers.onDraw("circle");
        break;
      case "Escape":
        // Not prevented: Escape has other owners on this page — the drawer,
        // a draw mode mid-shape — and swallowing it here would make the key
        // mean "clear selection" and nothing else.
        handlers.onClear();
        break;
      default:
        break;
    }
  };

  document.addEventListener("keydown", onKey);
  return () => document.removeEventListener("keydown", onKey);
}

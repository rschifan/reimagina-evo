/**
 * The command palette and the shortcuts sheet.
 *
 * Built from the DOM directly rather than as a component, because this page
 * is vanilla TypeScript and adding a framework for one overlay would be a
 * worse trade than writing fifty lines of `createElement`.
 *
 * Three things this has to get right, because they are what separates a
 * palette from a filtered list:
 *
 * - **It must not steal typing.** The open shortcut is ignored while the
 *   user is in a field, and so are the single-key shortcuts. A palette that
 *   opens when you type `k` into a search box is worse than no palette.
 * - **Focus must come back.** Closing returns focus to whatever had it, or
 *   a keyboard user is dropped at the top of the document every time.
 * - **The list must not jump.** The highlighted row follows the arrow keys
 *   and is scrolled into view, and the mouse does not fight it.
 */
import {
  highlight,
  rankCommands,
  type Command,
  type ScoredCommand,
} from "./commands";

export interface Shortcut {
  /** As shown to the user — "⌘K", "?", "\\". */
  keys: string;
  description: string;
}

export interface PaletteController {
  open: () => void;
  close: () => void;
  isOpen: () => boolean;
  /** Replace the command set — the AOI and indicator lists arrive async. */
  setCommands: (commands: Command[]) => void;
  dispose: () => void;
}

/**
 * Whether a keystroke landed somewhere the user is typing.
 *
 * `isContentEditable` is in here because a rich-text field is not an
 * `<input>` and a shortcut that fires inside one is the same bug wearing a
 * different tag.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || !el.tagName) return false;
  const tag = el.tagName.toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return el.isContentEditable === true;
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

export function enablePalette(
  initial: Command[],
  shortcuts: Shortcut[],
): PaletteController {
  let commands = initial;
  let open = false;
  let active = 0;
  let shown: ScoredCommand[] = [];
  let restoreFocus: HTMLElement | null = null;

  const root = el("div", "palette");
  root.hidden = true;
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-label", "Command palette");

  const panel = el("div", "palette__panel");
  const input = el("input", "palette__input");
  input.type = "text";
  input.placeholder = "Type a command, indicator or place…";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "true");
  input.setAttribute("aria-controls", "palette-list");
  input.autocomplete = "off";

  const list = el("ul", "palette__list");
  list.id = "palette-list";
  list.setAttribute("role", "listbox");

  const empty = el("div", "palette__empty");
  empty.textContent = "No matching command";
  empty.hidden = true;

  const footer = el("div", "palette__footer");
  footer.innerHTML =
    '<span><kbd>↑</kbd><kbd>↓</kbd> move</span>' +
    '<span><kbd>↵</kbd> run</span>' +
    '<span><kbd>esc</kbd> close</span>' +
    '<span><kbd>?</kbd> shortcuts</span>';

  panel.append(input, list, empty, footer);
  root.append(panel);
  document.body.append(root);

  // ── Shortcuts sheet ───────────────────────────────────────────────────
  const sheet = el("div", "palette sheet");
  sheet.hidden = true;
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-modal", "true");
  sheet.setAttribute("aria-label", "Keyboard shortcuts");
  const sheetPanel = el("div", "palette__panel sheet__panel");
  sheetPanel.innerHTML =
    '<h2 class="sheet__title">Keyboard shortcuts</h2><dl class="sheet__list">' +
    shortcuts
      .map(
        (s) =>
          `<div class="sheet__row"><dt>${s.description}</dt>` +
          `<dd><kbd>${s.keys.replace(/ /g, "</kbd><kbd>")}</kbd></dd></div>`,
      )
      .join("") +
    "</dl>";
  sheet.append(sheetPanel);
  document.body.append(sheet);

  function renderList(): void {
    shown = rankCommands(commands, input.value);
    list.textContent = "";
    empty.hidden = shown.length > 0;
    if (active >= shown.length) active = Math.max(0, shown.length - 1);

    let lastGroup = "";
    shown.forEach((entry, index) => {
      if (entry.command.group !== lastGroup) {
        lastGroup = entry.command.group;
        const heading = el("li", "palette__group");
        heading.textContent = lastGroup;
        heading.setAttribute("role", "presentation");
        list.append(heading);
      }
      const item = el("li", "palette__item");
      item.setAttribute("role", "option");
      item.id = `palette-item-${index}`;
      item.setAttribute("aria-selected", index === active ? "true" : "false");
      if (index === active) item.classList.add("is-active");
      if (entry.command.active) item.classList.add("is-current");

      const label = el("span", "palette__label");
      for (const part of highlight(entry.command.title, entry.ranges)) {
        if (part.match) {
          const mark = el("mark");
          mark.textContent = part.text;
          label.append(mark);
        } else {
          label.append(document.createTextNode(part.text));
        }
      }
      item.append(label);
      if (entry.command.hint) {
        const hint = el("span", "palette__hint");
        hint.textContent = entry.command.hint;
        item.append(hint);
      }
      // `mousedown`, not `click`: `click` fires after the input has already
      // lost focus and the blur handler has closed the overlay, so the row
      // under the cursor is gone before the event lands.
      item.addEventListener("mousedown", (event) => {
        event.preventDefault();
        run(index);
      });
      item.addEventListener("mousemove", () => {
        if (active === index) return;
        active = index;
        paintActive();
      });
      list.append(item);
    });
    paintActive();
  }

  function paintActive(): void {
    const items = list.querySelectorAll<HTMLElement>(".palette__item");
    items.forEach((item, index) => {
      const on = index === active;
      item.classList.toggle("is-active", on);
      item.setAttribute("aria-selected", on ? "true" : "false");
      if (on) {
        input.setAttribute("aria-activedescendant", item.id);
        item.scrollIntoView({ block: "nearest" });
      }
    });
  }

  function run(index: number): void {
    const entry = shown[index];
    if (!entry) return;
    // Close first. A command that opens a dialog or moves focus would
    // otherwise be fighting the palette's own teardown for the same focus.
    close();
    void entry.command.run();
  }

  function openPalette(): void {
    if (open) return;
    restoreFocus = document.activeElement as HTMLElement | null;
    open = true;
    root.hidden = false;
    input.value = "";
    active = 0;
    renderList();
    input.focus();
  }

  function close(): void {
    if (!open) return;
    open = false;
    // ⚠ Blur before hiding, and unconditionally.
    //
    // An element that holds focus when its container is hidden can keep
    // `document.activeElement`. This input then matches `isTypingTarget`,
    // and every single-key shortcut on the page — `?`, `\`, `f`, the draw
    // keys — is swallowed by a text field the user cannot see. Measured:
    // running one command from the palette killed every shortcut until the
    // user clicked somewhere else, which reads as "the shortcuts work
    // sometimes".
    //
    // `restoreFocus` does not save us here: the element the user came from
    // is usually `<body>`, and `body.focus()` is a no-op, so focus would
    // simply stay where it was.
    input.blur();
    root.hidden = true;
    // Back to where the user was. Without this a keyboard user is returned
    // to the top of the document after every command.
    const target = restoreFocus;
    restoreFocus = null;
    if (target?.isConnected && typeof target.focus === "function") {
      target.focus();
    }
  }

  function toggleSheet(): void {
    sheet.hidden = !sheet.hidden;
  }

  const onInput = (): void => {
    active = 0;
    renderList();
  };

  const onPanelKey = (event: KeyboardEvent): void => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      active = Math.min(active + 1, shown.length - 1);
      paintActive();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      active = Math.max(active - 1, 0);
      paintActive();
    } else if (event.key === "Enter") {
      event.preventDefault();
      run(active);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  };

  const onGlobalKey = (event: KeyboardEvent): void => {
    if (!sheet.hidden && event.key === "Escape") {
      event.preventDefault();
      sheet.hidden = true;
      return;
    }
    const k = event.key.toLowerCase();
    if ((event.metaKey || event.ctrlKey) && k === "k") {
      event.preventDefault();
      open ? close() : openPalette();
      return;
    }
    if (open) return;
    // ⚠ Never while the user is typing. A `?` shortcut that fires inside the
    // catalog's search box makes the search box unusable, which is a worse
    // bug than having no shortcut.
    if (isTypingTarget(event.target)) return;
    if (event.key === "?") {
      event.preventDefault();
      toggleSheet();
    }
  };

  const onBackdrop = (event: MouseEvent): void => {
    if (event.target === root) close();
  };
  const onSheetBackdrop = (event: MouseEvent): void => {
    if (event.target === sheet) sheet.hidden = true;
  };

  input.addEventListener("input", onInput);
  panel.addEventListener("keydown", onPanelKey);
  document.addEventListener("keydown", onGlobalKey);
  root.addEventListener("mousedown", onBackdrop);
  sheet.addEventListener("mousedown", onSheetBackdrop);

  return {
    open: openPalette,
    close,
    isOpen: () => open,
    setCommands: (next) => {
      commands = next;
      if (open) renderList();
    },
    dispose: () => {
      input.removeEventListener("input", onInput);
      panel.removeEventListener("keydown", onPanelKey);
      document.removeEventListener("keydown", onGlobalKey);
      root.removeEventListener("mousedown", onBackdrop);
      sheet.removeEventListener("mousedown", onSheetBackdrop);
      root.remove();
      sheet.remove();
    },
  };
}

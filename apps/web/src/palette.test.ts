// @vitest-environment jsdom
import { expect, it } from "vitest";

import type { Command } from "./commands";
import { enablePalette } from "./palette";

// Review: ⌘K opened the palette on the list built at the last AOI change, so
// it offered "Show Buildings" with Buildings already on, and the previous
// AOI's indicators. The list is read when the palette opens.
// jsdom has no layout, so no scrollIntoView.
Element.prototype.scrollIntoView = () => {};

it("⌘K opens the palette on the current commands", () => {
  let title = "Show Buildings";
  const commands = (): Command[] => [{ id: "layer", title, group: "Layer", run: () => {} }];
  const palette = enablePalette(commands, []);
  title = "Hide Buildings";
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }));
  expect(palette.isOpen()).toBe(true);
  expect(document.getElementById("palette-list")?.textContent).toContain("Hide Buildings");
  palette.dispose();
});

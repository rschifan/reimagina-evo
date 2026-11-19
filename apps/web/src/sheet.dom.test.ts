// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { wireSheet } from "./sheet";

const tick = (): Promise<void> => new Promise((r) => setTimeout(r, 0));

function setup() {
  const inspector = document.createElement("aside");
  const selection = document.createElement("aside");
  inspector.hidden = selection.hidden = true;
  const raise = wireSheet(inspector, selection);
  const behind = (el: HTMLElement): boolean => el.hasAttribute("data-behind");
  return { inspector, selection, raise, behind };
}

describe("wireSheet", () => {
  it("a re-render of the same pin does not jump over a newer selection", async () => {
    const { inspector, selection, raise, behind } = setup();
    inspector.hidden = false;
    raise("inspector");
    await tick();
    selection.hidden = false;
    await tick();
    expect(behind(inspector)).toBe(true);
    inspector.replaceChildren(document.createElement("p")); // values arrived / indicator switched
    await tick();
    expect(behind(inspector)).toBe(true);
    expect(behind(selection)).toBe(false);
  });

  it("a new pin (hide then show, then raise) comes back on top", async () => {
    const { inspector, selection, raise, behind } = setup();
    inspector.hidden = false;
    await tick();
    selection.hidden = false;
    await tick();
    expect(behind(inspector)).toBe(true);
    inspector.hidden = true;
    inspector.hidden = false;
    raise("inspector");
    await tick();
    expect(behind(inspector)).toBe(false);
    expect(behind(selection)).toBe(true);
  });
});

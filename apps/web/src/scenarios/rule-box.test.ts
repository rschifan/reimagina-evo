// @vitest-environment jsdom
import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import RuleBox from "./RuleBox.svelte";

const VALUES = { levels: 3, unit_size: 66, people_per_household: 3.6, lot_coverage: 0.6 };

let component: ReturnType<typeof mount> | null = null;
afterEach(() => {
  if (component) unmount(component);
  component = null;
  document.body.replaceChildren();
});

function open(props: Record<string, unknown> = {}) {
  const handlers = {
    onapply: vi.fn(),
    onsave: vi.fn(),
    ondelete: vi.fn(),
    oncancel: vi.fn(),
  };
  const target = document.body.appendChild(document.createElement("div"));
  component = mount(RuleBox, {
    target,
    props: {
      mode: "new",
      values: VALUES,
      needsScenarioName: false,
      defaultName: "Scenario 1",
      overlapNote: null,
      busy: false,
      error: null,
      draggable: true,
      ...handlers,
      ...props,
    },
  });
  flushSync();
  const input = (label: string) =>
    target.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;
  const button = (text: string) =>
    [...target.querySelectorAll("button")].find((b) => b.textContent?.trim() === text)!;
  const type = (label: string, value: string) => {
    const el = input(label);
    el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    flushSync();
  };
  return { target, handlers, input, button, type };
}

describe("the rule box", () => {
  it("prefills values and shows coverage in percent", () => {
    const { input } = open();
    expect(input("Storeys").value).toBe("3");
    expect(input("m² per home").value).toBe("66");
    expect(input("People per home").value).toBe("3.6");
    expect(input("Lot coverage (%)").value).toBe("60");
  });

  it("coverage is entered in percent", () => {
    const { type, button, handlers, target } = open();
    type("Lot coverage (%)", "45");
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith({ ...VALUES, lot_coverage: 0.45 }, undefined);
    for (const bad of ["0", "120"]) {
      handlers.onapply.mockClear();
      type("Lot coverage (%)", bad);
      button("Apply").click();
      flushSync();
      expect(handlers.onapply).not.toHaveBeenCalled();
      expect(target.textContent).toMatch(/Lot coverage must be more than 0 and at most 100 %/);
    }
  });

  // B18: a coverage of 0.625 showed as 63 and Save wrote 0.63 back, though
  // only the storeys were changed.
  it("keeps a coverage it did not touch exactly as it was", () => {
    const { input, type, button, handlers } = open({ mode: "rule", values: { ...VALUES, lot_coverage: 0.625 } });
    expect(input("Lot coverage (%)").value).toBe("62.5");
    type("Storeys", "4");
    button("Save").click();
    expect(handlers.onsave).toHaveBeenCalledWith({ ...VALUES, levels: 4, lot_coverage: 0.625 });
  });

  // B19: "3,5" in a dot-decimal browser binds as empty, which read as "not
  // set" and silently dropped the number from the rule.
  it("refuses a number the browser could not parse instead of dropping it", () => {
    const { input, button, handlers, target } = open();
    Object.defineProperty(input("People per home"), "validity", { value: { badInput: true } });
    button("Apply").click();
    flushSync();
    expect(handlers.onapply).not.toHaveBeenCalled();
    expect(target.textContent).toMatch(/People per home is not a number/);
    expect(input("People per home").getAttribute("aria-invalid")).toBe("true");
  });

  // B24: a refusal names its field, and the field says so.
  it("marks the field a server refusal is about", () => {
    const { input } = open({ error: { field: "unit_size", text: "unit_size must be more than 15" } });
    expect(input("m² per home").getAttribute("aria-invalid")).toBe("true");
    expect(input("m² per home").getAttribute("aria-describedby")).toBe("rule-box-error");
    expect(input("Storeys").getAttribute("aria-invalid")).toBe("false");
  });

  it("asks for a scenario name only on the baseline", () => {
    expect(open().target.querySelector('input[aria-label="Scenario name"]')).toBeNull();
    unmount(component!);
    component = null;
    const { input, button, handlers } = open({ needsScenarioName: true });
    expect(input("Scenario name").value).toBe("Scenario 1");
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith(VALUES, "Scenario 1");
  });

  it("a rule box offers Save, Delete and Cancel", () => {
    const { button, handlers } = open({ mode: "rule" });
    expect(button("Apply")).toBeUndefined();
    button("Save").click();
    expect(handlers.onsave).toHaveBeenCalledWith(VALUES);
    button("Delete rule").click();
    expect(handlers.ondelete).toHaveBeenCalled();
    button("Cancel").click();
    expect(handlers.oncancel).toHaveBeenCalled();
  });

  it("says which later rules win, and for which numbers", () => {
    const note = "Rule 3 also sets storeys: where they overlap, the later rule wins.";
    const { target } = open({ mode: "rule", overlapNote: note });
    expect(target.textContent).toContain(note);
  });

  it("says why a rule in several pieces cannot be dragged", () => {
    const { target } = open({ mode: "rule", draggable: false });
    expect(target.textContent).toMatch(/several pieces/);
  });

  it("shows a refusal from the server", () => {
    const { target } = open({ error: { field: null, text: "No blocks fall inside this area." } });
    expect(target.textContent).toMatch(/No blocks fall inside this area/);
  });

  it("Escape cancels", () => {
    const { target, handlers } = open();
    target.firstElementChild!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(handlers.oncancel).toHaveBeenCalled();
  });

  // B14: closing mid-recompute hid the refusal, which is shown only here.
  it("Escape does nothing while the recompute runs", () => {
    const { target, handlers } = open({ busy: true });
    target.firstElementChild!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(handlers.oncancel).not.toHaveBeenCalled();
  });

  it("buttons are disabled while busy", () => {
    const { button } = open({ busy: true });
    expect(button("Apply").disabled).toBe(true);
  });
});


describe("numbers left empty", () => {
  const EMPTY = {};

  it("opens empty, saying each block keeps its own value", () => {
    const { input, target } = open({ values: EMPTY });
    expect(input("Storeys").value).toBe("");
    expect(input("Storeys").placeholder).toBe("as is");
    expect(target.textContent).toMatch(/empty keeps each block's own value/);
  });

  it("sends only the numbers typed", () => {
    const { type, button, handlers } = open({ values: EMPTY });
    type("People per home", "5");
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith({ people_per_household: 5 }, undefined);
  });

  it("refuses a rule with no number", () => {
    const { button, handlers, target } = open({ values: EMPTY });
    button("Apply").click();
    flushSync();
    expect(handlers.onapply).not.toHaveBeenCalled();
    expect(target.textContent).toMatch(/Set at least one number/);
  });
});

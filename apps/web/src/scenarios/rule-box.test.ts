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
    expect(input("Lot coverage").value).toBe("60");
  });

  it("coverage is entered in percent", () => {
    const { type, button, handlers, target } = open();
    type("Lot coverage", "45");
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith({ ...VALUES, lot_coverage: 0.45 }, undefined, allSet(VALUES));
    // Out of range is the server's to refuse, in these units; the box sends it.
    handlers.onapply.mockClear();
    type("Lot coverage", "120");
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith({ ...VALUES, lot_coverage: 1.2 }, undefined, allSet(VALUES));
    expect(target.textContent).not.toMatch(/must be/);
  });

  // B18: a coverage of 0.625 showed as 63 and Save wrote 0.63 back, though
  // only the storeys were changed.
  it("keeps a coverage it did not touch exactly as it was", () => {
    const { input, type, button, handlers } = open({ mode: "rule", values: { ...VALUES, lot_coverage: 0.625 } });
    expect(input("Lot coverage").value).toBe("62.5");
    type("Storeys", "4");
    button("Save").click();
    expect(handlers.onsave).toHaveBeenCalledWith({ ...VALUES, levels: 4, lot_coverage: 0.625 }, allSet(VALUES));
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
    expect(handlers.onapply).toHaveBeenCalledWith(VALUES, "Scenario 1", allSet(VALUES));
  });

  it("a rule box offers Save, Delete and Cancel", () => {
    const { button, handlers } = open({ mode: "rule" });
    expect(button("Apply")).toBeUndefined();
    button("Save").click();
    expect(handlers.onsave).toHaveBeenCalledWith(VALUES, allSet(VALUES));
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
    expect(target.textContent).toMatch(/Empty keeps each block's own value/);
  });

  it("sends only the numbers typed", () => {
    const { type, button, handlers, target } = open({ values: EMPTY });
    type("People per home", "5");
    choose(target, "People per home unit", "=");
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith({ people_per_household: 5 }, undefined, { people_per_household: "set" });
  });

  it("leaves a rule with no number for the server to refuse", () => {
    const { button, handlers } = open({ values: EMPTY });
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith({}, undefined, {});
  });
});

describe("a choice per number", () => {
  it("an existing set rule opens with every number on =", () => {
    const { target } = open({ mode: "rule" });
    for (const group of ["Storeys unit", "m² per home unit", "People per home unit", "Lot coverage unit"]) {
      expect(pressed(target, group, "=").getAttribute("aria-pressed")).toBe("true");
    }
  });

  it("one rule sets one number and changes another", () => {
    const { button, type, handlers, target } = open({ values: {} });
    type("Lot coverage", "40");
    choose(target, "Lot coverage unit", "=");
    type("Storeys", "20");
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith(
      { levels: 20, lot_coverage: 0.4 },
      undefined,
      { levels: "pct", lot_coverage: "set" },
    );
  });

  it("a change rule is applied with signed numbers and each number's unit", () => {
    const { button, type, handlers, target } = open({ values: {} });
    type("Storeys", "1");
    choose(target, "Storeys unit", "+ storeys");
    type("m² per home", "-10");
    type("Lot coverage", "5");
    choose(target, "Lot coverage unit", "+ points");
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith(
      { levels: 1, unit_size: -10, lot_coverage: 0.05 },
      undefined,
      { levels: "abs", unit_size: "pct", lot_coverage: "abs" },
    );
  });

  it("coverage as a percentage change is sent as typed, not as a share", () => {
    const { button, type, handlers } = open({ values: {} });
    type("Lot coverage", "5");
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith(
      { lot_coverage: 5 }, undefined, { lot_coverage: "pct" },
    );
  });

  it("an existing change rule opens with each number's own choice", () => {
    const { input, target } = open({
      mode: "rule",
      values: { unit_size: -10, lot_coverage: 0.05, levels: 4 },
      ruleUnits: { unit_size: "pct", levels: "set", lot_coverage: "abs" },
    });
    expect(input("m² per home").value).toBe("-10");
    expect(input("Lot coverage").value).toBe("5");
    expect(pressed(target, "m² per home unit", "+ %").getAttribute("aria-pressed")).toBe("true");
    expect(pressed(target, "Storeys unit", "=").getAttribute("aria-pressed")).toBe("true");
    expect(pressed(target, "Lot coverage unit", "+ points").getAttribute("aria-pressed")).toBe("true");
  });

  it("an existing change rule opens with the keyboard in its first number", () => {
    const { input } = open({
      mode: "rule",
      values: { levels: 20 },
      ruleUnits: { levels: "pct" },
    });
    expect(document.activeElement).toBe(input("Storeys"));
  });

  it("an empty number of a saved set rule starts at =, like its other numbers", () => {
    // In a rule that is all "=", a new number on "+ %" would silently make
    // "4 storeys" into "+4 %".
    const { type, button, handlers, target } = open({ mode: "rule", values: { lot_coverage: 0.4 } });
    expect(pressed(target, "Storeys unit", "=").getAttribute("aria-pressed")).toBe("true");
    type("Storeys", "4");
    button("Save").click();
    expect(handlers.onsave).toHaveBeenCalledWith({ levels: 4, lot_coverage: 0.4 }, { levels: "set", lot_coverage: "set" });
  });

  it("a zero change is not read back", () => {
    const { type, target } = open({ values: {} });
    type("Storeys", "0");
    expect(target.querySelector('[aria-label="This rule"]')).toBeNull();
  });

  it("a coverage change in points is sent as a share", () => {
    // Range refusals come from the server, worded in points.
    const { type, button, handlers, target } = open({ values: {} });
    type("Lot coverage", "150");
    choose(target, "Lot coverage unit", "+ points");
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith({ lot_coverage: 1.5 }, undefined, { lot_coverage: "abs" });
  });

  it("an empty number starts at + %", () => {
    // "+30" read as 30 storeys when a planner meant 30 % was the bug.
    const { target } = open({ values: {} });
    for (const group of ["Storeys unit", "m² per home unit", "People per home unit", "Lot coverage unit"]) {
      expect(pressed(target, group, "+ %").getAttribute("aria-pressed")).toBe("true");
    }
  });

  it("reads each typed number back in words before it is applied", () => {
    const { type, target } = open({ values: {} });
    type("Storeys", "30");
    type("m² per home", "10");
    choose(target, "m² per home unit", "+ m²");
    type("Lot coverage", "40");
    choose(target, "Lot coverage unit", "=");
    const readBack = target.querySelector('[aria-label="This rule"]')!.textContent!;
    expect(readBack).toMatch(/Storeys \+30 %/);
    expect(readBack).toMatch(/m² per home \+10 m²/);
    expect(readBack).toMatch(/Lot coverage = 40 %/);
  });

  it("a set coverage under 1 % is accepted, as real blocks have it", () => {
    const { button, type, handlers, target } = open({ values: {} });
    type("Lot coverage", "0.5");
    choose(target, "Lot coverage unit", "=");
    button("Apply").click();
    expect(handlers.onapply).toHaveBeenCalledWith({ lot_coverage: 0.005 }, undefined, { lot_coverage: "set" });
  });

  it("shows the server's refusal of a number, worded in the box's units", () => {
    const plain = "Lot coverage must be more than 0 and at most 100 %, got 120 %.";
    const { input, target } = open({ error: { field: "lot_coverage", text: plain } });
    expect(target.textContent).toContain(plain);
    expect(input("Lot coverage").getAttribute("aria-invalid")).toBe("true");
  });
});

function pressed(target: Element, group: string, text: string): HTMLButtonElement {
  return [...target.querySelectorAll<HTMLButtonElement>(`[aria-label="${group}"] button`)].find(
    (b) => b.textContent?.trim() === text,
  )!;
}

function choose(target: Element, group: string, text: string) {
  pressed(target, group, text).click();
  flushSync();
}

/** Every number of `values` as "=". */
function allSet(values: Record<string, unknown>): Record<string, string> {
  return Object.fromEntries(Object.keys(values).map((k) => [k, "set"]));
}

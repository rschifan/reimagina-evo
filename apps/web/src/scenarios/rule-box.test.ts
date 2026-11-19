// @vitest-environment jsdom
import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import RuleBox from "./RuleBox.svelte";

const VALUES = { levels: 3, unitSize: 66, peoplePerHousehold: 3.6, lotCoverage: 0.6 };

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
      overriddenBy: null,
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
    expect(handlers.onapply).toHaveBeenCalledWith({ ...VALUES, lotCoverage: 0.45 }, undefined);
    for (const bad of ["0", "120"]) {
      handlers.onapply.mockClear();
      type("Lot coverage (%)", bad);
      button("Apply").click();
      flushSync();
      expect(handlers.onapply).not.toHaveBeenCalled();
      expect(target.textContent).toMatch(/Lot coverage must be between 1 and 100 %/);
    }
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

  it("says which rule overrides this one", () => {
    const { target } = open({ mode: "rule", overriddenBy: 3 });
    expect(target.textContent).toMatch(/Rule 3 overrides this one where they overlap/);
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

  it("buttons are disabled while busy", () => {
    const { button } = open({ busy: true });
    expect(button("Apply").disabled).toBe(true);
  });
});

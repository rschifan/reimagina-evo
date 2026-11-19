// @vitest-environment jsdom
//
// The rule form, mounted and submitted the way a planner submits it. The
// store tests call `applyRule` directly, which is exactly how a form the
// browser refused to submit went unnoticed: the m² input's `step` made its
// own default value invalid, and native validation blocked every submit.
import { flushSync, mount, unmount } from "svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

const store = vi.hoisted(() => ({
  ws: {
    scenarios: [{ id: "A", name: "Alpha" }],
    selectedId: "A",
    drawn: {
      type: "Feature",
      properties: {},
      geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] },
    },
    rules: [],
    slug: "edificability_block_density",
    comparison: null,
    densityValidMax: 2000,
    floorsValidMax: 200,
  },
  applyRule: vi.fn(async () => {}),
  compare: vi.fn(async () => {}),
  recompute: vi.fn(async () => {}),
  removeRuleGroup: vi.fn(async () => {}),
  removeScenario: vi.fn(async () => {}),
}));
vi.mock("./store.svelte", () => store);

import RulePanel from "./RulePanel.svelte";

let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
  if (component) unmount(component);
  component = null;
  document.body.replaceChildren();
});

describe("the rule form", () => {
  it("submits its default numbers to applyRule", () => {
    const target = document.body.appendChild(document.createElement("div"));
    component = mount(RulePanel, { target });
    flushSync();

    const submit = target.querySelector<HTMLButtonElement>('button[type="submit"]');
    expect(submit).not.toBeNull();
    submit!.click();

    expect(store.applyRule).toHaveBeenCalledWith(store.ws.drawn, {
      levels: 6,
      unitSize: 60,
      peoplePerHousehold: 3.6,
    });
  });
});

// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { createBanner } from "./banner";

function setup() {
  const el = document.createElement("div");
  el.hidden = true;
  return { el, set: createBanner(el) };
}

describe("createBanner", () => {
  it("shows loading as a status and hides when its channel clears", () => {
    const { el, set } = setup();
    set("loading", "Loading Distance to park…");
    expect(el.hidden).toBe(false);
    expect(el.getAttribute("role")).toBe("status");
    expect(el.textContent).toBe("Loading Distance to park…");
    set("loading", null);
    expect(el.hidden).toBe(true);
  });

  it("shows an error over loading, as an alert", () => {
    const { el, set } = setup();
    set("map", "The map could not load some data");
    set("loading", "Loading Distance to park…");
    expect(el.textContent).toBe("The map could not load some data");
    expect(el.getAttribute("role")).toBe("alert");
    expect(el.dataset.kind).toBe("error");
  });

  it("a finished load does not hide another channel's error", () => {
    const { el, set } = setup();
    set("map", "tile timed out");
    set("loading", "Loading…");
    set("loading", null);
    set("values", null);
    expect(el.hidden).toBe(false);
    expect(el.textContent).toBe("tile timed out");
  });

  it("falls back to the next message when the top one clears", () => {
    const { el, set } = setup();
    set("values", "Could not load the values of Park");
    set("map", "tile timed out");
    set("map", null);
    expect(el.textContent).toBe("Could not load the values of Park");
    set("values", null);
    expect(el.hidden).toBe(true);
  });
});

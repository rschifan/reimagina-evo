// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DRAWER_BREAKPOINT, enableSidebar, isDrawerWidth } from "./sidebar";

/** Drive `matchMedia`, which jsdom does not implement. */
function setViewport(width: number): void {
  const listeners = new Set<(e: MediaQueryListEvent) => void>();
  (window as unknown as { __mq: Set<unknown> }).__mq = listeners;
  // A partial `MediaQueryList`: only the three members `enableSidebar`
  // touches. Cast through `unknown` because filling in `onchange`,
  // `addListener`, `removeListener` and `dispatchEvent` would be four stubs
  // nothing calls.
  window.matchMedia = ((query: string) => {
    const max = Number(/max-width:\s*(\d+)px/.exec(query)?.[1] ?? Infinity);
    return {
      matches: width <= max,
      media: query,
      addEventListener: (_: string, fn: (e: MediaQueryListEvent) => void) => {
        listeners.add(fn);
      },
      removeEventListener: (_: string, fn: (e: MediaQueryListEvent) => void) => {
        listeners.delete(fn);
      },
    };
  }) as unknown as typeof window.matchMedia;
}

function fire(matches: boolean): void {
  const listeners = (window as unknown as {
    __mq: Set<(e: MediaQueryListEvent) => void>;
  }).__mq;
  for (const fn of listeners) fn({ matches } as MediaQueryListEvent);
}

function mount() {
  document.body.innerHTML = `
    <main class="app">
      <button id="t" aria-expanded="true"><span aria-hidden="true">⟨</span><span class="sr-only">Hide panel</span></button>
      <div id="s" hidden></div>
      <aside id="sidebar"></aside>
    </main>`;
  return {
    app: document.querySelector<HTMLElement>(".app")!,
    toggle: document.querySelector<HTMLButtonElement>("#t")!,
    scrim: document.querySelector<HTMLElement>("#s")!,
  };
}

describe("isDrawerWidth", () => {
  it("reads the same breakpoint the stylesheet uses", () => {
    setViewport(DRAWER_BREAKPOINT);
    expect(isDrawerWidth()).toBe(true);
    setViewport(DRAWER_BREAKPOINT + 1);
    expect(isDrawerWidth()).toBe(false);
  });
});

describe("enableSidebar", () => {
  beforeEach(() => setViewport(1440));

  it("starts open on a wide screen", () => {
    const { app, toggle, scrim } = mount();
    enableSidebar(app, toggle, scrim);
    expect(app.classList.contains("is-collapsed")).toBe(false);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  // ⚠ The map is what the page is for. A drawer covering it on arrival
  // hides the thing the user came to see, and on a phone it covered 86 % of
  // the viewport.
  it("starts closed at drawer width", () => {
    setViewport(390);
    const { app, toggle, scrim } = mount();
    enableSidebar(app, toggle, scrim);
    expect(app.classList.contains("is-collapsed")).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });

  it("toggles, and says so to assistive technology", () => {
    const { app, toggle, scrim } = mount();
    const controller = enableSidebar(app, toggle, scrim);
    controller.toggle();
    expect(app.classList.contains("is-collapsed")).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.querySelector(".sr-only")?.textContent).toBe("Show panel");
    controller.toggle();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(toggle.querySelector(".sr-only")?.textContent).toBe("Hide panel");
  });

  it("names the shortcut in the title, because the label is a glyph", () => {
    const { app, toggle, scrim } = mount();
    enableSidebar(app, toggle, scrim);
    expect(toggle.title).toContain("\\");
  });

  it("shows the scrim only while the drawer is open", () => {
    setViewport(390);
    const { app, toggle, scrim } = mount();
    const controller = enableSidebar(app, toggle, scrim);
    expect(scrim.hidden).toBe(true);
    controller.toggle();
    expect(scrim.hidden).toBe(false);
  });

  it("closes when the scrim is clicked", () => {
    setViewport(390);
    const { app, toggle, scrim } = mount();
    const controller = enableSidebar(app, toggle, scrim);
    controller.toggle();
    scrim.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(controller.collapsed()).toBe(true);
  });

  it("closes the drawer on Escape", () => {
    setViewport(390);
    const { app, toggle, scrim } = mount();
    const controller = enableSidebar(app, toggle, scrim);
    controller.toggle();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(controller.collapsed()).toBe(true);
  });

  // ⚠ On a wide screen the sidebar is part of the layout, not something
  // laid over the map, so collapsing it on Escape would be an unrelated
  // side effect of a key pressed to dismiss something else.
  it("leaves a wide layout alone on Escape", () => {
    const { app, toggle, scrim } = mount();
    const controller = enableSidebar(app, toggle, scrim);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(controller.collapsed()).toBe(false);
  });

  it("closes when the viewport crosses into drawer width", () => {
    const { app, toggle, scrim } = mount();
    const controller = enableSidebar(app, toggle, scrim);
    expect(controller.collapsed()).toBe(false);
    fire(true);
    expect(controller.collapsed()).toBe(true);
  });

  it("reports every change so the map can resize", () => {
    // MapLibre only learns the canvas changed size when told; without this
    // the reclaimed space renders as empty page.
    const onChange = vi.fn();
    const { app, toggle, scrim } = mount();
    const controller = enableSidebar(app, toggle, scrim, onChange);
    onChange.mockClear();
    controller.toggle();
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("detaches every listener on dispose", () => {
    setViewport(390);
    const { app, toggle, scrim } = mount();
    const controller = enableSidebar(app, toggle, scrim);
    controller.toggle();
    controller.dispose();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(controller.collapsed()).toBe(false);
  });
});

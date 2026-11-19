import { describe, expect, it, vi } from "vitest";

vi.mock("./hover-highlight", () => ({ setHoverGloballySuspended: vi.fn() }));
vi.mock("./tooltip", () => ({ setTooltipGloballySuspended: vi.fn() }));

import { setHoverGloballySuspended } from "./hover-highlight";
import { syncMapGestures } from "./selection";
import { clickHitsSelectionHandle } from "./selection-handle";
import { setTooltipGloballySuspended } from "./tooltip";

const handler = () => ({ enable: vi.fn(), disable: vi.fn() });
const fakeMap = () =>
  ({ dragPan: handler(), boxZoom: handler(), doubleClickZoom: handler() }) as unknown as Parameters<typeof syncMapGestures>[0];

describe("syncMapGestures", () => {
  it("suspends hover and tooltips only while a shape is being drawn", () => {
    for (const [mode, drawing] of [["rectangle", true], ["polygon", true], ["circle", true], ["select", false], ["static", false]] as const) {
      vi.mocked(setHoverGloballySuspended).mockClear();
      vi.mocked(setTooltipGloballySuspended).mockClear();
      syncMapGestures(fakeMap(), mode);
      expect(setHoverGloballySuspended, mode).toHaveBeenCalledExactlyOnceWith(drawing);
      expect(setTooltipGloballySuspended, mode).toHaveBeenCalledExactlyOnceWith(drawing);
    }
  });
});

describe("clickHitsSelectionHandle", () => {
  const mapWith = (...ids: string[]) =>
    ({ queryRenderedFeatures: () => ids.map((id) => ({ layer: { id } })) }) as unknown as Parameters<typeof clickHitsSelectionHandle>[0];

  it("is true on a terra-draw vertex or the radius handle", () => {
    expect(clickHitsSelectionHandle(mapWith("td-point", "indicator-overlay"), [0, 0])).toBe(true);
    expect(clickHitsSelectionHandle(mapWith("td-point-marker"), [0, 0])).toBe(true);
    expect(clickHitsSelectionHandle(mapWith("__selection_handle__dot"), [0, 0])).toBe(true);
  });

  it("is false inside the shape or on a block, so those clicks still pin", () => {
    expect(clickHitsSelectionHandle(mapWith("td-polygon", "td-polygon-outline", "indicator-overlay"), [0, 0])).toBe(false);
    expect(clickHitsSelectionHandle(mapWith(), [0, 0])).toBe(false);
  });
});

import { describe, expect, it, vi } from "vitest";

vi.mock("./hover-highlight", () => ({ setHoverGloballySuspended: vi.fn() }));
vi.mock("./tooltip", () => ({ setTooltipGloballySuspended: vi.fn() }));

import { setHoverGloballySuspended } from "./hover-highlight";
import { hitsSelectionHandle, syncMapGestures } from "./selection";
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

describe("hitsSelectionHandle", () => {
  // A style holding `style` layers, whose features under the point are on `hit`.
  const mapWith = (style: string[], hit: string[]) => {
    const queried: Array<string[] | undefined> = [];
    const map = {
      getLayer: (id: string) => (style.includes(id) ? { id } : undefined),
      queryRenderedFeatures: (_p: unknown, opts?: { layers?: string[] }) => {
        queried.push(opts?.layers);
        return hit.filter((id) => opts?.layers?.includes(id)).map((id) => ({ layer: { id } }));
      },
    } as unknown as Parameters<typeof hitsSelectionHandle>[0];
    return { map, queried };
  };
  const ALL = ["td-point", "td-point-marker", "__selection_handle__dot", "td-polygon", "indicator_overlay__fill"];

  it("is true on a terra-draw vertex or the radius handle", () => {
    for (const id of ["td-point", "td-point-marker", "__selection_handle__dot"]) {
      expect(hitsSelectionHandle(mapWith(ALL, [id, "indicator_overlay__fill"]).map, [0, 0]), id).toBe(true);
    }
  });

  it("is false inside the shape or on a block, so those clicks still pin", () => {
    expect(hitsSelectionHandle(mapWith(ALL, ["td-polygon", "indicator_overlay__fill"]).map, [0, 0])).toBe(false);
    expect(hitsSelectionHandle(mapWith(ALL, []).map, [0, 0])).toBe(false);
  });

  it("queries only the handle layers in the style, and nothing without them", () => {
    const some = mapWith(["td-point", "indicator_overlay__fill"], []);
    hitsSelectionHandle(some.map, [0, 0]);
    expect(some.queried).toEqual([["td-point"]]);
    const none = mapWith(["indicator_overlay__fill"], []);
    expect(hitsSelectionHandle(none.map, [0, 0])).toBe(false);
    expect(none.queried).toEqual([]);
  });
});

// @vitest-environment jsdom
import type { Map as MapLibreMap } from "maplibre-gl";
import { expect, it } from "vitest";

import type { TileSource } from "./api";
import { wireRasterToggles } from "./layers";

// Review: an AOI switch rebuilt the toggles unchecked but left the layers they
// had turned on painted, with no checkbox left to turn them off.
it("rebuilding the toggles hides the layers the old ones had turned on", () => {
  const visibility = new Map<string, string>();
  const map = {
    getLayer: (id: string) => (visibility.has(id) ? {} : undefined),
    setLayoutProperty: (id: string, _key: string, value: string) => visibility.set(id, value),
  } as unknown as MapLibreMap;
  const overlay = {
    layer_id: "aggregation_overlay::x",
    source_mode: "aggregation_overlay",
    logical_name: "Overlay",
    source_config: { target_feature_type_slug: "block" },
  } as unknown as TileSource;
  for (const id of ["agg_overlay__aggregation_overlay__x__fill", "agg_overlay__aggregation_overlay__x__line"]) {
    visibility.set(id, "none");
  }
  const container = document.createElement("div");
  wireRasterToggles(container, map, [overlay]);
  const box = container.querySelector("input[type='checkbox']") as HTMLInputElement;
  box.checked = true;
  box.dispatchEvent(new Event("change"));
  expect([...visibility.values()]).toEqual(["visible", "visible"]);

  wireRasterToggles(container, map, [overlay]);
  expect((container.querySelector("input[type='checkbox']") as HTMLInputElement).checked).toBe(false);
  expect([...visibility.values()]).toEqual(["none", "none"]);
});

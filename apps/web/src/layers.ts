/**
 * Map-layer wiring.
 *
 * Consumes /tile-sources from the API, transforms each live_mvt layer
 * into a MapLibre source + fill/line style, and attaches UI toggles in
 * the sidebar. The `indicator_overlay` layer is installed but left hidden
 * until `choropleth.ts` configures it — that separation keeps the input-
 * layer scaffold independent of indicator styling.
 */
import type { Map as MapLibreMap } from "maplibre-gl";

import type { TileSource } from "./api";

export interface InputLayerSpec {
  layerId: string;         // infra/tiles/layers.json layer_id
  sourceName: string;      // MapLibre source name
  sourceLayerName: string; // MVT source layer name inside each tile
  typeSlug: string;        // serving_geo.get_tile query_params filter value
  label: string;           // UI label
  fillColor: string;
  fillOpacity: number;
  lineColor: string;
  lineWidth: number;
  visible: boolean;
  minzoom?: number;
}

export const INPUT_LAYERS: InputLayerSpec[] = [
  {
    layerId: "baseline_live:municipality",
    sourceName: "baseline_municipalities",
    sourceLayerName: "features",
    typeSlug: "municipality",
    label: "Municipalities (alcaldías)",
    fillColor: "#374151",
    fillOpacity: 0.10,
    lineColor: "#9ca3af",
    lineWidth: 1.2,
    visible: true,
    minzoom: 6,
  },
  {
    layerId: "baseline_live:ageb",
    sourceName: "baseline_agebs",
    sourceLayerName: "features",
    typeSlug: "ageb",
    label: "AGEBs",
    fillColor: "#1f2937",
    fillOpacity: 0.05,
    lineColor: "#6b7280",
    lineWidth: 0.6,
    visible: true,
    minzoom: 11,
  },
  {
    layerId: "baseline_live:block",
    sourceName: "baseline_blocks",
    sourceLayerName: "features",
    typeSlug: "block",
    label: "Blocks (manzanas)",
    fillColor: "transparent",
    fillOpacity: 0,
    lineColor: "#4b5563",
    lineWidth: 0.3,
    visible: true,
    minzoom: 13,
  },
  {
    layerId: "baseline_live:building",
    sourceName: "baseline_buildings",
    sourceLayerName: "features",
    typeSlug: "building",
    label: "Buildings (Overture)",
    fillColor: "#93c5fd",
    fillOpacity: 0.35,
    lineColor: "#60a5fa",
    lineWidth: 0,
    visible: false,
    minzoom: 14,
  },
];

export interface TileSourceCatalog {
  baselineTilesUrl: string;
  indicatorOverlayTilesUrl: string;
}


export function extractTileSources(sources: TileSource[]): TileSourceCatalog {
  const baseline = sources.find((s) => s.layer_id === "baseline_live");
  const indicator = sources.find((s) => s.layer_id === "indicator_overlay");
  if (!baseline?.source_config.tiles_url_template) {
    throw new Error("API did not return a baseline_live tiles_url_template");
  }
  if (!indicator?.source_config.tiles_url_template) {
    throw new Error("API did not return an indicator_overlay tiles_url_template");
  }
  return {
    baselineTilesUrl: baseline.source_config.tiles_url_template,
    indicatorOverlayTilesUrl: indicator.source_config.tiles_url_template,
  };
}


export function installInputLayers(
  map: MapLibreMap,
  catalog: TileSourceCatalog,
): void {
  for (const layer of INPUT_LAYERS) {
    const tileUrl = `${catalog.baselineTilesUrl}?type_slug=${encodeURIComponent(layer.typeSlug)}`;
    map.addSource(layer.sourceName, {
      type: "vector",
      tiles: [tileUrl],
      minzoom: layer.minzoom ?? 0,
      maxzoom: 16,
    });

    if (layer.fillColor !== "transparent" && layer.fillOpacity > 0) {
      map.addLayer({
        id: `${layer.sourceName}__fill`,
        type: "fill",
        source: layer.sourceName,
        "source-layer": layer.sourceLayerName,
        layout: { visibility: layer.visible ? "visible" : "none" },
        paint: {
          "fill-color": layer.fillColor,
          "fill-opacity": layer.fillOpacity,
        },
      });
    }
    map.addLayer({
      id: `${layer.sourceName}__line`,
      type: "line",
      source: layer.sourceName,
      "source-layer": layer.sourceLayerName,
      layout: { visibility: layer.visible ? "visible" : "none" },
      paint: {
        "line-color": layer.lineColor,
        "line-width": layer.lineWidth,
      },
    });
  }
}


export function wireLayerToggles(
  container: HTMLElement,
  map: MapLibreMap,
): void {
  container.innerHTML = "";
  for (const layer of INPUT_LAYERS) {
    const label = document.createElement("label");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = layer.visible;
    checkbox.addEventListener("change", () => {
      const visibility = checkbox.checked ? "visible" : "none";
      for (const suffix of ["__fill", "__line"]) {
        const id = `${layer.sourceName}${suffix}`;
        if (map.getLayer(id)) {
          map.setLayoutProperty(id, "visibility", visibility);
        }
      }
    });
    const text = document.createElement("span");
    text.className = "layer-label";
    text.textContent = layer.label;
    label.append(checkbox, text);
    container.append(label);
  }
}

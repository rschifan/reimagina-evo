/**
 * Entry point for the reimagina read-side web app.
 *
 * Boots MapLibre with a minimal offline style (no basemap — the
 * prototype avoids external tile dependencies and licensing drift),
 * loads the tile-source catalog from the API, and wires input-layer
 * toggles. Indicator choropleth + downloads land in a follow-up commit.
 */
import maplibregl from "maplibre-gl";
import type { StyleSpecification } from "maplibre-gl";

import { listTileSources } from "./api";
import { extractTileSources, installInputLayers, wireLayerToggles } from "./layers";

const CUAUHTEMOC_CENTER: [number, number] = [-99.137, 19.432];

function setStatus(message: string): void {
  const el = document.getElementById("status");
  if (el) el.textContent = message;
}


function emptyStyle(): StyleSpecification {
  return {
    version: 8,
    name: "reimagina-empty",
    sources: {},
    layers: [
      {
        id: "background",
        type: "background",
        paint: { "background-color": "#0b1220" },
      },
    ],
  };
}


async function main(): Promise<void> {
  setStatus("Connecting to reimagina API…");

  let catalog;
  try {
    const sources = await listTileSources();
    catalog = extractTileSources(sources);
  } catch (err) {
    setStatus(`Failed to load tile sources: ${(err as Error).message}`);
    throw err;
  }

  const map = new maplibregl.Map({
    container: "map",
    style: emptyStyle(),
    center: CUAUHTEMOC_CENTER,
    zoom: 12,
    maxZoom: 18,
    minZoom: 5,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

  map.on("load", () => {
    installInputLayers(map, catalog);
    const toggleContainer = document.getElementById("layer-toggles");
    if (toggleContainer) {
      wireLayerToggles(toggleContainer, map);
    }
    setStatus("Map ready. Spatial input layers loaded.");
  });
  map.on("error", (e) => {
    setStatus(`Map error: ${e.error?.message ?? "unknown"}`);
  });
}


void main();

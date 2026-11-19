/**
 * The parts of a map every page that draws one shares: how it is built
 * (style, worker, controls, attribution) and how the view lands on a pilot.
 *
 * Moved out of `main.ts` when the scenario workspace grew a map of its own —
 * `main.ts` is an entry with side effects, so importing from it would start
 * the whole map app.
 */
// ⚠ Namespace import, not a default one. maplibre-gl 6 dropped its default
// export, so `import maplibregl from "maplibre-gl"` stops compiling (TS1192).
import * as maplibregl from "maplibre-gl";
import type { Map as MapLibreMap, StyleSpecification } from "maplibre-gl";
// maplibre-gl 6 ships its web worker as a separate module instead of
// inlining it, so the bundler has to emit it and the library has to be
// told where it landed. Without this the map renders its canvas and
// controls, then fails at "Worker failed to load" and parses no tiles.
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";

import { listAttributions, type AOIRow } from "./api";
import { minzoomForGrain } from "./layers";

/** No basemap: licence hygiene, see the attribution note in `installMapControls`. */
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


/**
 * When a map error stops being shown. MapLibre emits `error` per failed
 * request (one tile timeout is enough) and nothing when it recovers, so the
 * message goes on the first `idle` that ends a render cycle with no new
 * error: it stays while the failing cycle's data is missing and clears once
 * the map has drawn cleanly again (a pan, a zoom, a hover repaint).
 */
export function mapErrorBanner(
  show: (message: string) => void,
  hide: (message: string) => void,
): { error: (message: string) => void; idle: () => void } {
  let shown: string | null = null;
  let errorSinceIdle = false;
  return {
    error(message) {
      shown = message;
      errorSinceIdle = true;
      show(message);
    },
    idle() {
      if (shown !== null && !errorSinceIdle) {
        hide(shown);
        shown = null;
      }
      errorSinceIdle = false;
    },
  };
}


export function flyToAoi(map: MapLibreMap, aoi: AOIRow): void {
  // Prefer the features bbox over the AOI polygon bbox. For irregular
  // AOIs (Mexicali muni — mostly desert with the urban core packed in
  // one corner) the polygon bbox frames the whole muni and the user
  // ends up looking at empty land. The features bbox is the envelope
  // of actual loaded blocks, so fitBounds lands them on the urban
  // extent. Fall back to the polygon bbox when no features have been
  // loaded yet (e.g. fresh AOI before ingest).
  const target = aoi.features_bbox ?? aoi.bbox;
  if (!target) return;
  const [minLon, minLat, maxLon, maxLat] = target;
  // ``maxZoom`` caps the fit so a tiny AOI doesn't slingshot past
  // street level; ``padding`` keeps the polygon clear of UI chrome. The
  // floor keeps a wide AOI or a phone screen where blocks (and the overlay
  // painting them) draw.
  const camera = map.cameraForBounds(
    [
      [minLon, minLat],
      [maxLon, maxLat],
    ],
    { padding: 60, maxZoom: 14 },
  );
  if (!camera) return;
  const zoom = Math.max(camera.zoom ?? 0, minzoomForGrain("block") ?? 0);
  map.easeTo({ ...camera, zoom, duration: 700 });
}


/**
 * Navigation, scale and the data attribution every map here must carry.
 *
 * One function so the scenario workspace's map cannot ship without the
 * credit line the map page carries — see the licence note inside.
 */
function installMapControls(map: MapLibreMap): void {
  // ⚠ `showCompass: true`, which it was not.
  //
  // With no basemap there is nothing in the frame that says which way is
  // north — no coastline, no street grid, no labels — so a rotated map is
  // unrecoverable by looking at it. The compass is also the only way back
  // from an accidental rotate, which a trackpad makes easy to do and hard
  // to notice.
  map.addControl(
    new maplibregl.NavigationControl({ showCompass: true, visualizePitch: true }),
    "top-right",
  );
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

  // ⚠ A licence obligation, not a nicety.
  //
  // OpenStreetMap is ODbL and displaying it without credit is a breach, and
  // the reason this map has no external basemap in the first place is
  // licence hygiene. Publishing OSM-derived streets without the credit
  // would spend exactly what that decision was protecting.
  //
  // The text comes from the API, which derives it from the licence recorded
  // against each source that actually has features. Hardcoding it here
  // would credit OpenStreetMap on a synthetic territory — a false
  // provenance claim in a product whose argument is that its provenance is
  // checkable — and crediting a registered-but-unloaded source would claim
  // the map shows data it does not.
  void listAttributions()
    .then((rows) => {
      if (rows.length === 0) return;
      map.addControl(
        new maplibregl.AttributionControl({
          customAttribution: rows.map((row) => row.text),
          // ⚠ `compact: true`, and the text is NOT shortened to achieve it.
          //
          // Expanded, four real sources render as one 200-character line
          // spanning the whole viewport:
          //
          //   © OpenStreetMap contributors | IMIP Mexicali (Datos abiertos
          //   del IMIP) | Overture Maps Foundation (ODbL-1.0 / Overture
          //   terms) | INEGI (Libre uso — Términos de Uso de la Información
          //   del INEGI)
          //
          // The obvious fix is to trim each credit, and that is the wrong
          // one: the wording comes from `data_source_catalog.license` via
          // `attribution.py`, whose own comment warns that an entry claiming
          // "this exact wording is what the licence asks for" is worse than
          // the honest fallback when it is wrong. Editing four providers'
          // credit lines for layout is a licence judgement, and not one to
          // make for a line break.
          //
          // Compact keeps every word and moves it behind the standard
          // MapLibre (i) control — the same affordance every multi-source
          // map uses, one click away and always visible.
          compact: true,
        }),
        "bottom-right",
      );
      // ⚠ `compact: true` is not enough on its own, and the reason is in
      // MapLibre's `_updateCompact`:
      //
      //     this._container.classList.add(
      //       'maplibregl-compact', 'maplibregl-compact-show')
      //
      // It adds BOTH classes, so a compact control renders *expanded* on
      // load and only collapses once the user first interacts with the map.
      // Until then four real sources are still a 200-character bar across
      // the viewport — which is exactly what `compact: true` was added to
      // stop, so without this the fix does nothing for anyone who has not
      // panned yet.
      //
      // Removing `-show` is stable: `_updateCompact` only re-adds it when
      // `maplibregl-compact` is absent, and that class stays. Done on the
      // next frame so the control's own first pass has already run.
      requestAnimationFrame(() => {
        document
          .querySelectorAll(".maplibregl-ctrl-attrib.maplibregl-compact-show")
          .forEach((el) => el.classList.remove("maplibregl-compact-show"));
      });
    })
    .catch((err) => {
      // Loud, because silence here is a licence breach rather than a
      // missing nicety: a map that drew OSM geometry and failed to credit
      // it must say so somewhere a developer will see.
      console.error("could not resolve data attribution", err);
    });
}


/** A map on `container` with the shared style, worker and controls. */
export function createMap(
  container: HTMLElement | string,
  center: [number, number],
  zoom: number,
): MapLibreMap {
  maplibregl.setWorkerUrl(maplibreWorkerUrl);
  const map = new maplibregl.Map({
    container,
    style: emptyStyle(),
    center,
    zoom,
    maxZoom: 18,
    minZoom: 5,
    // `installMapControls` adds the one attribution control, carrying the
    // data credits; MapLibre's default would be a second (i) button.
    attributionControl: false,
  });
  installMapControls(map);
  return map;
}

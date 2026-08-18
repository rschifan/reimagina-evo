/**
 * Central map tooltip controller.
 *
 * Responsibilities:
 *   - Own a single tooltip DOM node, positioned absolutely over the map.
 *   - Dispatch mousemove / mouseleave to every registered
 *     :interface:`TooltipProvider` and show the first non-null hit.
 *   - Respect MapLibre layer visibility: a hidden layer must not
 *     contribute a tooltip, even if its provider is still registered.
 *
 * Everything else (baseline vectors, aggregation overlays, raster
 * pixel readout) plugs in via providers — tooltip behaviour for a new
 * layer type is one provider implementation, not a new branch in the
 * main mouse handler.
 */
import type { Map as MapLibreMap, MapLayerMouseEvent } from "maplibre-gl";

import { fmtNumber } from "./format";


export interface TooltipContent {
  /** Short label shown bold (layer identity). */
  title: string;
  /** Optional value/detail line under the title. */
  detail?: string;
}


export interface TooltipProvider {
  /** Stable id for debugging / deregistration. */
  id: string;
  /** MapLibre layer ids this provider listens on (for hit detection). */
  layerIds: string[];
  /**
   * Probe the map at the given mouse position. Return null to pass and
   * let the next provider try. Return a Promise for async providers
   * (e.g. raster pixel readout via HTTP) — the controller will await
   * it and drop the result if the pointer moved away in the meantime.
   */
  probe(args: {
    map: MapLibreMap;
    event: MapLayerMouseEvent;
  }): TooltipContent | Promise<TooltipContent | null> | null;
}


// ── Global suspend bus ───────────────────────────────────────────────
//
// Same pattern as :func:`setHoverGloballySuspended` in
// hover-highlight.ts. Tooltips are suspended together with hover
// while the user is drawing or editing a selection shape — modes
// are mutually exclusive in production map tools (Felt, Mapbox
// Studio): if you're committed to a draw gesture, no other mousemove
// affordance should fire.
let _tooltipGloballySuspended = false;
const _liveTooltipInstances = new Set<TooltipController>();


export function setTooltipGloballySuspended(suspended: boolean): void {
  if (_tooltipGloballySuspended === suspended) return;
  _tooltipGloballySuspended = suspended;
  if (suspended) {
    // Hide any currently-visible tooltip on every live controller so
    // a tooltip from before the draw gesture doesn't linger through
    // the operation.
    for (const inst of _liveTooltipInstances) {
      try { inst.hide(); } catch (err) { console.warn("tooltip hide:", err); }
    }
  }
}


export class TooltipController {
  private readonly element: HTMLDivElement;
  private readonly providers: TooltipProvider[] = [];
  private lastRequestId = 0;

  constructor(private readonly map: MapLibreMap, container: HTMLElement) {
    this.element = document.createElement("div");
    this.element.className = "map-tooltip";
    this.element.setAttribute("role", "status");
    this.element.setAttribute("aria-live", "polite");
    this.element.style.display = "none";
    container.append(this.element);

    this.map.on("mousemove", (e) => this.onMove(e));
    this.map.on("mouseout", () => this.hide());

    // Register for the global-suspend bus so the
    // ``setTooltipGloballySuspended(true)`` call can synchronously
    // hide our visible tooltip.
    _liveTooltipInstances.add(this);
  }

  /**
   * Register a provider. Later registrations are queried first — this
   * matches the visual z-order: an overlay added on top of baseline
   * vectors should claim the tooltip before the baseline's own
   * provider gets a chance.
   */
  register(provider: TooltipProvider): () => void {
    this.providers.unshift(provider);
    return () => {
      const i = this.providers.indexOf(provider);
      if (i >= 0) this.providers.splice(i, 1);
    };
  }

  private async onMove(event: MapLayerMouseEvent): Promise<void> {
    // Globally suspended (e.g. selection draw/edit in flight) — no
    // tooltip should fire. Hide any in-flight content too in case
    // the suspension flipped between event arrival and dispatch.
    if (_tooltipGloballySuspended) {
      this.hide();
      return;
    }
    const reqId = ++this.lastRequestId;
    for (const p of this.providers) {
      if (!this.isAnyLayerVisible(p.layerIds)) continue;
      let content: TooltipContent | null;
      try {
        const maybe = p.probe({ map: this.map, event });
        content = maybe instanceof Promise ? await maybe : maybe;
      } catch {
        content = null;
      }
      // If a later mousemove arrived while we awaited an async probe,
      // don't render a stale result.
      if (reqId !== this.lastRequestId) return;
      if (content) {
        this.show(content, event);
        return;
      }
    }
    this.hide();
  }

  private isAnyLayerVisible(layerIds: string[]): boolean {
    return layerIds.some((id) => {
      if (!this.map.getLayer(id)) return false;
      const vis = this.map.getLayoutProperty(id, "visibility");
      return vis !== "none";
    });
  }

  private show(content: TooltipContent, event: MapLayerMouseEvent): void {
    const title = document.createElement("strong");
    title.textContent = content.title;
    this.element.replaceChildren(title);
    if (content.detail) {
      const detail = document.createElement("div");
      detail.className = "map-tooltip-detail";
      detail.textContent = content.detail;
      this.element.append(detail);
    }
    // Position near the cursor; MapLibre's event.point is in pixel
    // coords relative to the map container, matching our parent.
    this.element.style.left = `${event.point.x + 12}px`;
    this.element.style.top = `${event.point.y + 12}px`;
    this.element.style.display = "block";
  }

  public hide(): void {
    this.element.style.display = "none";
  }
}


// ── Built-in providers ──────────────────────────────────────────────


/**
 * Provider over one MapLibre vector layer. Uses `queryRenderedFeatures`
 * at the mouse point and hands the first hit feature to the
 * ``format`` callback. Returning null from format means "I recognise
 * this layer but the feature has nothing interesting" — falls through.
 */
export function mvtProvider(opts: {
  id: string;
  layerId: string;
  format(feature: GeoJSON.Feature): TooltipContent | null;
}): TooltipProvider {
  return {
    id: opts.id,
    layerIds: [opts.layerId],
    probe({ map, event }) {
      const features = map.queryRenderedFeatures(event.point, {
        layers: [opts.layerId],
      });
      if (features.length === 0) return null;
      return opts.format(features[0] as unknown as GeoJSON.Feature);
    },
  };
}


/**
 * Provider over a titiler COG. Calls ``{endpoint}/point/{lng},{lat}``
 * with the given COG URL, debounced so rapid mousemoves don't fan out
 * into a storm of HTTP requests. The last result within the debounce
 * window is cached in-memory so a stationary cursor stays fast.
 *
 * The raster layer id is included in ``layerIds`` so the controller
 * only fires the probe when the layer is currently visible — a hidden
 * raster must not trigger HTTP calls.
 */
export function cogPointProvider(opts: {
  id: string;
  layerId: string;           // MapLibre layer id (visibility gate)
  title: string;             // human-readable raster name
  cogUrl: string;            // file:// URL passed to titiler
  endpointBase: string;      // e.g. "/api/cog" — resolved once
}): TooltipProvider {
  const debounceMs = 150;
  let lastFire = 0;
  let pending: Promise<TooltipContent | null> | null = null;
  let cached: { key: string; content: TooltipContent | null } | null = null;

  return {
    id: opts.id,
    layerIds: [opts.layerId],
    probe({ event }) {
      // Snap coordinates to a 5-decimal grid (~1m) so tiny jitter
      // reuses the cache.
      const lng = event.lngLat.lng.toFixed(5);
      const lat = event.lngLat.lat.toFixed(5);
      const key = `${lng},${lat}`;
      if (cached && cached.key === key) return cached.content;

      const now = performance.now();
      if (now - lastFire < debounceMs && pending) return pending;
      lastFire = now;

      const url =
        `${opts.endpointBase}/point/${lng},${lat}?url=${encodeURIComponent(opts.cogUrl)}`;

      pending = fetch(url, { headers: { Accept: "application/json" } })
        .then((r) => (r.ok ? r.json() : null))
        .then((payload: { values?: (number | null)[] } | null) => {
          const val = payload?.values?.[0];
          const content: TooltipContent | null =
            val === undefined || val === null || Number.isNaN(val)
              ? null
              : { title: opts.title, detail: fmtNumber(val) };
          cached = { key, content };
          return content;
        })
        .catch(() => null);
      return pending;
    },
  };
}



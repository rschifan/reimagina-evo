/**
 * AOI picker state — fetches `/aois`, exposes the current selection,
 * persists the choice across reloads, and notifies subscribers when
 * it changes.
 *
 * The module owns one piece of state (`activeAoiId`) and makes it
 * observable via `subscribe(...)`. Pieces of the app that depend on
 * the active AOI (tile URL builders, the indicator stats endpoint,
 * the map fly-to) wire themselves up via `subscribe` and never read
 * the storage directly.
 *
 * Why a hand-rolled subscriber pattern over a state library: the rest
 * of the web app is plain TS + MapLibre with no framework, so adding
 * one event emitter is cheaper than introducing a dependency that
 * pays for itself only when several pieces of state need observing.
 */

import type { AOIRow } from "./api";

const STORAGE_KEY = "reimagina.activeAoiId";

type Listener = (aoi: AOIRow | null) => void;


let _knownAois: AOIRow[] = [];
let _activeAoiId: string | null = null;
const _listeners = new Set<Listener>();


/** Replace the cached AOI list and re-emit the current selection
 *  through the listener bus so subscribers get an authoritative
 *  snapshot once they're wired (the first call is the bootstrap). */
export function setKnownAois(aois: AOIRow[]): void {
  _knownAois = aois;
  // Restore a previous choice when the storage value still resolves
  // to a known AOI; otherwise pick the first one as a sane default
  // so the UI is never blank on first load.
  const stored = _readStorage();
  const restored = aois.find((a) => a.aoi_id === stored)?.aoi_id ?? null;
  _activeAoiId = restored ?? aois[0]?.aoi_id ?? null;
  _emit();
}


export function getActiveAoiId(): string | null {
  return _activeAoiId;
}


function getActiveAoi(): AOIRow | null {
  if (_activeAoiId === null) return null;
  return _knownAois.find((a) => a.aoi_id === _activeAoiId) ?? null;
}


export function setActiveAoiId(aoiId: string | null): void {
  if (aoiId !== null && !_knownAois.some((a) => a.aoi_id === aoiId)) {
    // Silently bail rather than throw — keeps callers free to dispatch
    // user-driven choices without a try/catch wrapper. Stale ids land
    // here on the rare race where the catalog refreshes mid-session.
    return;
  }
  _activeAoiId = aoiId;
  _writeStorage(aoiId);
  _emit();
}


export function subscribe(listener: Listener): () => void {
  _listeners.add(listener);
  // Fire once with the current state so subscribers see "now" without
  // a separate get-then-subscribe ceremony.
  listener(getActiveAoi());
  return () => _listeners.delete(listener);
}


/** Render an AOI dropdown into ``select``. Updates the active AOI on
 *  change. Pre-selects whichever AOI is currently active so the UI
 *  matches state on first paint. */
export function populateAoiSelect(select: HTMLSelectElement): void {
  select.innerHTML = "";
  for (const aoi of _knownAois) {
    const option = document.createElement("option");
    option.value = aoi.aoi_id;
    option.textContent = aoi.name;
    if (aoi.aoi_id === _activeAoiId) option.selected = true;
    select.appendChild(option);
  }
  select.addEventListener("change", () => {
    setActiveAoiId(select.value || null);
  });
}


function _emit(): void {
  const current = getActiveAoi();
  for (const listener of _listeners) listener(current);
}


function _readStorage(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    // Private browsing or storage disabled — fall back to ephemeral.
    return null;
  }
}


function _writeStorage(value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, value);
  } catch {
    // Same reason as _readStorage; persistence is best-effort.
  }
}

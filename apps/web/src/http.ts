/**
 * Shared HTTP primitives for every page in this app.
 *
 * Extracted from ``api.ts`` when the catalog page arrived: both bundles need
 * the same request/abort semantics, and the alternative — the catalog
 * importing ``api.ts`` — would couple it to the map's ``IndicatorSummary`` /
 * ``TileSource`` typing and make every map change a catalog change.
 *
 * This module is framework-free on purpose. It is imported by the vanilla map
 * app and by the Svelte catalog page, so it must not reach for either.
 */

export const API_BASE = "/api";

export class ApiError extends Error {
  constructor(message: string, readonly response: Response) {
    super(message);
    this.name = "ApiError";
  }
}

/** GET ``path`` under {@link API_BASE} and parse JSON, throwing
 *  {@link ApiError} on a non-2xx so callers can branch on status. */
export async function jsonRequest<T>(
  path: string,
  init: { signal?: AbortSignal; headers?: Record<string, string> } = {},
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json", ...(init.headers ?? {}) },
    signal: init.signal,
  });
  if (!response.ok) {
    throw new ApiError(`${response.status} ${response.statusText} on ${path}`, response);
  }
  return response.json() as Promise<T>;
}

/** True when ``err`` is the AbortError raised by ``fetch`` / ``signal``
 *  cancellations. Both DOMException-shaped (browsers) and plain-Error-
 *  shaped (some test runtimes) instances surface ``name === "AbortError"``,
 *  so name-checking is the portable predicate. Centralised here so every
 *  abort-aware request site stays in lock-step. */
export function isAbortError(err: unknown): boolean {
  return (err as { name?: string } | null)?.name === "AbortError";
}

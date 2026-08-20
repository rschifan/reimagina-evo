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

/** Request ``path`` under {@link API_BASE} and parse JSON, throwing
 *  {@link ApiError} on a non-2xx so callers can branch on status.
 *
 *  GET by default. ``body`` is JSON-encoded and sets the method to POST unless
 *  one is given; `Content-Type` is attached only when there is a body, because
 *  sending it on a GET makes some proxies preflight a request that needs none. */
export async function jsonRequest<T>(
  path: string,
  init: {
    signal?: AbortSignal;
    headers?: Record<string, string>;
    method?: string;
    body?: unknown;
  } = {},
): Promise<T> {
  const hasBody = init.body !== undefined;
  const response = await fetch(`${API_BASE}${path}`, {
    method: init.method ?? (hasBody ? "POST" : "GET"),
    headers: {
      Accept: "application/json",
      ...(hasBody ? { "Content-Type": "application/json" } : {}),
      ...(init.headers ?? {}),
    },
    body: hasBody ? JSON.stringify(init.body) : undefined,
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

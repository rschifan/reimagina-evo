/**
 * An exported-but-empty environment variable is "unset", not "a value".
 *
 * `make dev` exports both of these, so a shell where `.env` was never sourced
 * hands Vite `""` rather than nothing — and `??` only catches `undefined`.
 * `Number("")` is `0`, so with `strictPort: true` the dev server binds an
 * arbitrary free port while the Makefile's banner promises 5173; and an empty
 * proxy target points /api at nothing while the config's own comment warns
 * that a misdirected proxy "returns plausible-looking 404s rather than a
 * connection error".
 *
 * The Python side already spells this out, in `bootstrap_db`: "An empty string
 * is 'unset', not 'a DSN that happens to be blank'."
 */

import { afterEach, describe, expect, it, vi } from "vitest";

// `@types/node` is not a dependency of this package and adding one for a test
// would be the tail wagging the dog. Vitest runs on node, so `process` is
// there at runtime; this declares only as much of it as the test touches.
declare const process: { env: Record<string, string | undefined> };

const KEYS = ["VITE_PORT", "REIMAGINA_API_URL"] as const;

// ⚠ The specifier is a variable, not a literal, and that is deliberate.
// `vite.config.ts` does not type-check on its own — it reads `process.env`
// without `@types/node` installed, and carries a `test:` key that vite's
// `defineConfig` does not accept — and a literal import would pull it into
// tsconfig's program and turn eight latent errors into a red `tsc --noEmit`.
// Those errors predate this test and fixing them needs a dependency or a
// tsconfig change, so this keeps them where they were rather than smuggling
// an unrelated change in behind a test. Vitest resolves this at runtime.
const CONFIG = "../vite.config.ts";

async function loadConfig() {
  // The config reads `process.env` at module scope, so it has to be
  // re-evaluated after the environment changes.
  vi.resetModules();
  const module = await import(CONFIG);
  return module.default as {
    server: { port: number; proxy: Record<string, { target: string }> };
  };
}

describe("vite dev server config", () => {
  const saved = new Map<string, string | undefined>();
  for (const key of KEYS) saved.set(key, process.env[key]);

  afterEach(() => {
    for (const key of KEYS) {
      const value = saved.get(key);
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });

  it("treats an empty VITE_PORT as unset", async () => {
    process.env.VITE_PORT = "";
    const config = await loadConfig();
    expect(config.server.port).toBe(5173);
  });

  it("still honours a VITE_PORT that is set", async () => {
    process.env.VITE_PORT = "5199";
    const config = await loadConfig();
    expect(config.server.port).toBe(5199);
  });

  it("treats an empty REIMAGINA_API_URL as unset", async () => {
    process.env.REIMAGINA_API_URL = "";
    const config = await loadConfig();
    expect(config.server.proxy["/api"].target).toBe("http://127.0.0.1:8001");
  });

  it("still honours a REIMAGINA_API_URL that is set", async () => {
    process.env.REIMAGINA_API_URL = "http://127.0.0.1:9001";
    const config = await loadConfig();
    expect(config.server.proxy["/api"].target).toBe("http://127.0.0.1:9001");
  });
});

describe("vite build entries", () => {
  // ⚠ Naming `rollupOptions.input` replaces Vite's entry discovery, so a page
  // missing here is silently not built — and `dist/` is gitignored, so no
  // review would show it gone.
  it("builds every page the top bar links to", async () => {
    vi.resetModules();
    const module = await import(CONFIG);
    const input = (module.default as { build: { rollupOptions: { input: Record<string, string> } } })
      .build.rollupOptions.input;
    expect(Object.keys(input).sort()).toEqual(["catalog", "index", "ops", "scenarios"]);
    expect(input.scenarios).toMatch(/\/scenarios\.html$/);
  });
});

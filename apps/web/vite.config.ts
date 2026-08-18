import { resolve } from "node:path";

import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// Dev server proxies /api to the FastAPI uvicorn so the browser sees a
// same-origin URL and the API's CORS config doesn't need to special-case
// the Vite port. Override the target with REIMAGINA_API_URL.
//
// Port 8000 is deliberately NOT the default: another project on this
// machine owns it, and a proxy silently pointed at a foreign API returns
// plausible-looking 404s rather than a connection error. Run the API with
//   uvicorn apps.api.app:app --host 127.0.0.1 --port 8001
//
// Production deployments should serve the built artefacts behind a
// reverse proxy that performs the same mapping.
//
// ── Two entries, one config ──────────────────────────────────────────
//
// `index.html` is the MapLibre app: vanilla TS, hand-written CSS, no
// framework. `catalog.html` is the indicator catalog: Svelte 5 runes,
// Tailwind, shadcn. They share `src/http.ts`, `src/format.ts` and
// `src/theme.css`, and nothing else.
//
// The Svelte plugin is deliberately NOT scoped with `include`. Scoping it to
// `src/catalog/**` looks safer and is not: component libraries ship their own
// `.svelte` sources (lucide, bits-ui) under node_modules, and excluding them
// leaves rollup to parse a Svelte file as JavaScript. It also protects
// nothing — a Svelte parse error fails the whole build whichever entry pulls
// it in. What actually keeps the map bundle clean is that nothing in its
// module graph imports Svelte or Tailwind; the plugin only transforms
// `.svelte` files, and the map has none.
export default defineConfig({
  plugins: [svelte(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.REIMAGINA_API_URL ?? "http://127.0.0.1:8001",
        changeOrigin: false,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  build: {
    target: "es2022",
    sourcemap: true,
    rollupOptions: {
      // ⚠ Naming `input` REPLACES Vite's default entry discovery. Omit
      // `index` here and the map app silently stops being built — and
      // `dist/` is gitignored, so nothing in review would show it. Both
      // entries must stay listed.
      input: {
        index: resolve(__dirname, "index.html"),
        catalog: resolve(__dirname, "catalog.html"),
      },
    },
  },
  test: {
    // Vitest reads this file — there is no vitest.config.ts. The existing
    // suites (format, selection-geom) are pure and must keep running with
    // no DOM, so `node` stays the default and only the catalog opts into
    // jsdom.
    environmentMatchGlobs: [["src/catalog/**", "jsdom"]],
  },
});

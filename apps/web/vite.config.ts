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
    // `strictPort`, so a busy port is an error rather than a silent move to
    // 5174. Vite's default is to increment, which leaves the Makefile's
    // banner — and the browser tab you already have open — pointing at a port
    // this server is not on, or worse at whatever else is there. `make dev`
    // preflights both ports and names the holder; this makes sure Vite agrees
    // rather than papering over it.
    port: Number(process.env.VITE_PORT ?? 5173),
    strictPort: true,
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
      // All three entries must stay listed.
      input: {
        index: new URL("./index.html", import.meta.url).pathname,
        catalog: new URL("./catalog.html", import.meta.url).pathname,
        ops: new URL("./ops.html", import.meta.url).pathname,
      },
    },
  },
  test: {
    // Vitest reads this file — there is no vitest.config.ts. The default
    // environment stays `node` so the pure suites (format, selection-geom)
    // keep running without a DOM. A catalog test that needs one opts in
    // per file with a `// @vitest-environment jsdom` docblock; Vitest 4
    // removed `environmentMatchGlobs`, and one docblock per DOM test is
    // less machinery than a projects config for a single glob.
    environment: "node",
  },
});

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
export default defineConfig({
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
  },
});

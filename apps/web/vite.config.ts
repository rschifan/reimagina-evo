import { defineConfig } from "vite";

// Dev server proxies /api to the FastAPI uvicorn (default port 8000)
// so the browser sees a same-origin URL and the API's CORS config
// doesn't need to special-case the Vite port. Override the API target
// with REIMAGINA_API_URL when a stale process is squatting on 8000 or
// when the API is run on a non-default port.
//
// Production deployments should serve the built artefacts behind a
// reverse proxy that performs the same mapping.
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.REIMAGINA_API_URL ?? "http://127.0.0.1:8000",
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

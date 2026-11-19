# `tests/e2e` — the app in a real browser

What everything else cannot see. The web suite (`apps/web`, vitest) tests
modules in jsdom; `tests/integration` tests the API without a client. Between
them is the thing a user actually opens, and it was unverified: the whole
frontend stack — svelte, vite, vitest, jsdom, terra-draw, tailwind-variants —
was upgraded in one commit on the strength of `npm run build` passing and 73
unit tests passing. A build that succeeds says nothing about whether the map
draws.

It doesn't, as it happens, unless something is serving tiles — which is the
other thing this suite is for. It distinguishes *the app is broken* from *the
tile server is not running*, two states that look identical in a browser and
are reported identically by every other suite: as nothing at all.

## Running it

Opt-in, because it needs two servers and a browser. Once, install the
Playwright driver (without touching `package.json` or the lockfile) and the
Chromium it drives:

```bash
cd apps/web && npm install --no-save playwright && npx playwright install chromium
```

Then, with `make dev` running (an API on `http://127.0.0.1:8001`, the web dev
server on `http://localhost:5173`):

```bash
make test-e2e
```

On other ports, tell the suite where the servers are:

```bash
make dev API_PORT=8123 WEB_PORT=5199
REIMAGINA_E2E_API=http://127.0.0.1:8123 REIMAGINA_E2E_WEB=http://localhost:5199 make test-e2e
```

⚠ The web server is `localhost`, not `127.0.0.1`: Vite listens on
`localhost`, which macOS resolves to `::1`, so nothing answers on 127.0.0.1.

The pages need data to show: any migrated database with a territory loaded
works, for example the one `scripts/acceptance_run.sh` leaves behind.

## What it will not do

It does not assert pixels. A screenshot comparison would fail on a font
substitution and pass on a map rendering the wrong city, which is the wrong
sensitivity in both directions. It asserts what the page reports about itself:
that it loaded, that it reached the API, that it raised no console error, and
that the requests it made are the ones the tile contract specifies.

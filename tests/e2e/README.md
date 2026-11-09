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

Opt-in, because it needs two servers and a browser:

```bash
REIMAGINA_E2E=1 make test-e2e
```

It expects, and checks for, an API on `:8001` and the web dev server on
`:5173`:

```bash
REIMAGINA_APP_DSN=... python -m apps.api
cd apps/web && npm run dev
```

Chromium and Playwright are already installed; nothing is downloaded.

## What it will not do

It does not assert pixels. A screenshot comparison would fail on a font
substitution and pass on a map rendering the wrong city, which is the wrong
sensitivity in both directions. It asserts what the page reports about itself:
that it loaded, that it reached the API, that it raised no console error, and
that the requests it made are the ones the tile contract specifies.

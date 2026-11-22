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

Opt-in, because it needs two servers, a populated database and a browser.

**Once**, install Playwright and its Chromium inside the web application, so
the browser driver resolves them:

```bash
cd apps/web
npm install --no-save playwright
```

On macOS, install Chromium:

```bash
npx playwright install chromium
```

On Linux, include the browser's system libraries (requires package-manager
privileges):

```bash
npx playwright install --with-deps chromium
```

Return to the repository root with `cd ../..` before running Make targets.

**Each time**, load a territory if the database is empty
(`scripts/acceptance_run.sh` generates one, and drops and recreates the
database named by `DATABASE_URL`), start the servers, and run the suite from
the repository root:

```bash
make dev          # in another terminal: API, web dev server and worker
make test-e2e     # sets REIMAGINA_E2E=1
```

It expects the API on `http://127.0.0.1:8001` and the web dev server on
`http://localhost:5173` — `localhost`, because Vite listens there and on some
machines that is IPv6 only. It skips, naming what is missing, when either does
not answer or Playwright does not resolve. On other ports, say where they are:

```bash
make dev API_PORT=8123 WEB_PORT=5199
REIMAGINA_E2E_API=http://127.0.0.1:8123 REIMAGINA_E2E_WEB=http://localhost:5199 make test-e2e
```

## What it will not do

It does not assert pixels. A screenshot comparison would fail on a font
substitution and pass on a map rendering the wrong city, which is the wrong
sensitivity in both directions. It asserts what the page reports about itself:
that it loaded, that it reached the API, that it raised no console error, and
that the requests it made are the ones the tile contract specifies.

## The scenario editor flow (`test_scenario_flow.py`)

Draws, applies, edits, drags, overlaps and deletes rules through the real page
and asserts what the map paints. **It writes**, so it runs only with
`REIMAGINA_E2E_WRITES=1`, and only against a scratch stack — never the
database a person is working in:

```bash
# In a separate scratch checkout, configure/review all five DSNs.
# See docs/INSTALLATION.md: acceptance drops and recreates its target.
export DATABASE_URL=postgresql://localhost:5432/reimagina_e2e_scratch
export REIMAGINA_APP_DSN=$DATABASE_URL DATABASE_URL_DIRECT=$DATABASE_URL
export TEST_DATABASE_URL=postgresql://localhost:5432/reimagina_e2e_scratch_test
export DATABASE_URL_ADMIN=postgresql://localhost:5432/postgres
scripts/acceptance_run.sh

# Its own API, worker and web server, on ports nothing else uses.
uv run uvicorn apps.api.app:app --host 127.0.0.1 --port 8011 &
uv run python -m apps.worker &
(cd apps/web && VITE_PORT=5192 REIMAGINA_API_URL=http://127.0.0.1:8011 npx vite --host 127.0.0.1) &

REIMAGINA_E2E=1 REIMAGINA_E2E_WRITES=1 \
  REIMAGINA_E2E_API=http://127.0.0.1:8011 REIMAGINA_E2E_WEB=http://127.0.0.1:5192 \
  uv run pytest tests/e2e/test_scenario_flow.py -q -rs
```

⚠ Export all five DSNs above so they override `.env`, and configure
`REIMAGINA_API_BASE_URL`, `REIMAGINA_TILE_*`, `REIMAGINA_API_CORS_ORIGINS`
for the scratch ports. `load_dotenv()` searches upward and never overrides,
so anything left unset is read from `.env` — and the worker's queue lives in
whatever `DATABASE_URL_DIRECT` names, so an unset one would take runs from the
real stack's queue.

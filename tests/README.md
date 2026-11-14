# Test suites and where each one runs

| suite | gate | needs |
|---|---|---|
| `tests/unit` | CI + `make test` | nothing |
| `tests/contract` | CI + `make test` | nothing |
| `tests/integration` | CI + `make test-integration` | Postgres + PostGIS |
| `tests/conformance` | CI + `make test-conformance` | Postgres + PostGIS |
| `tests/performance` | **no gate — nothing fails if it rots** | Postgres, seeded data |
| `tests/e2e` | `make test-e2e`, opt-in | API + web dev server + Chromium |

`make check` runs lint, unit, contract and the web build. It does **not** run
integration or conformance, which need a database; CI runs those in a separate
job with a Postgres service.

`tests/e2e` is the only suite that opens the app in a browser. Everything
else sees the web app through jsdom or not at all, which is how the entire
frontend stack was upgraded in one commit with the map untested: `npm run
build` passed and 73 unit tests passed, and neither opens a page.

## The ungated suite

`tests/performance` is reachable from no CI job and no Makefile target. It has
already cost us once: an engine-coupled module sat there importing a task queue
long after the quarantine convention had been applied everywhere else, and
nothing noticed because nothing ran it. That module is gone, but the gap that
hid it is not.

Its assertions are latency budgets calibrated on one developer's laptop
(`perf/manifest.py`), so putting it in CI as-is would trade a silent suite for a
flaky one. Until the budgets are either machine-relative or generous enough to
survive a shared runner, this stays a hand-run suite — and this note exists so
that is a decision rather than an accident.

Run it deliberately:

```bash
pytest tests/performance
```

## A bare `pytest` is not the fast lane

`testpaths = ["tests"]` means a bare `pytest` collects every suite, including
`tests/integration` — whose session fixture ends by dropping every managed
schema from `TEST_DATABASE_URL`. Name the suites you want.

## Conformance

`tests/conformance` asserts the `Orchestrator` port contract against each
implementation this branch ships, and is the instrument for comparing ops-plane
branches. See [`conformance/README.md`](conformance/README.md).

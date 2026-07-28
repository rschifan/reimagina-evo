# Engine-coupled tests

Tests under `engine/<name>/` bind to one specific ops-plane implementation —
they import its client library, its task module, or the tables only it writes.

They are quarantined so an ops-plane branch can drop one subtree without
touching the rest of the suite. A pytest marker would not be enough: these
modules import engine code **at module scope**, so once that code is deleted the
failure is a *collection* error, not a skip.

The same convention applies in two places:

```bash
pytest tests/integration --ignore=tests/integration/engine/procrastinate
pytest tests/performance --ignore=tests/performance/engine/procrastinate
```

`tests/performance/engine/procrastinate/` holds the queue timing harness, which
imports `reimagina.perf.procrastinate_bench` — a module every ops-plane branch
deletes along with the engine it measures.

Everything outside `engine/` must stay engine-agnostic: those tests exercise the
domain through the `Orchestrator` port or through the storage layer, and every
branch is expected to keep them green.

## Where each suite runs

| suite | gate |
|---|---|
| `tests/unit`, `tests/contract` | CI, no database |
| `tests/integration` | CI, Postgres service |
| `tests/conformance` | CI, Postgres service — the port contract, per implementation |
| `tests/performance` | **no gate** — run by hand; nothing fails if it rots |

`tests/performance` being ungated is why its engine coupling went unnoticed
until the quarantine convention was applied there too.

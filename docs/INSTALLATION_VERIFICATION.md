# Installation verification — 20 November 2026

This record covers the corrections on `codex/installation-docs-fixes`, based
on commit `93dd6ca`. Follow [INSTALLATION.md](INSTALLATION.md) to reproduce
the setup. Results apply to these environments and commands.

## Tested environments

| Environment | Application runtime | Database | Installation and synthetic acceptance |
|---|---|---|---|
| macOS 15.7.9 ARM64 | Python 3.12.11, Node 24.22.2, uv 0.7.14 | Docker PostgreSQL 16.2 / PostGIS 3.4.2 / pgRouting 3.6.1 | Passed from fresh dependencies and isolated databases |
| macOS 15.7.9 ARM64 | Same | Native Postgres.app 17.10 / PostGIS 3.5.6 / pgRouting 3.6.3 | Passed in a separately initialized cluster |
| Debian 12 ARM64 container | Python 3.12.11, Node 24.22.3, uv 0.7.14 | Same isolated Docker service, separate databases | Passed with documented GEOS/compiler/CMake prerequisites |
| Debian 12 x86_64 container | Same | Same isolated Docker service, separate databases | Passed under emulation on ARM64 |

macOS host tools and dependency caches already existed. Linux containers used
fresh dependency environments. Alternative ports and a separate Compose
project/volume avoided collisions with existing services. Native Linux service
provisioning and private real-data ingestion were not exercised.

## Checks

| Check | macOS | Linux ARM64 | Linux x86_64 |
|---|---|---|---|
| Ruff lint/format and mypy | Passed | Passed | Passed |
| Python unit/contract | 1,092 passed; none skipped | 1,092 passed; none skipped | 1,092 passed; none skipped |
| Web typecheck/build | Passed | Passed | Passed |
| Web unit tests | 440 passed | 440 passed | 440 passed |
| Database integration | 569 passed; 1 skipped | 568 passed; 2 skipped | 568 passed; 2 skipped |
| Orchestrator conformance | 21 passed; 1 skipped | 21 passed; 1 skipped | 21 passed; 1 skipped |
| Browser tests, including scenario writes | 41 passed | 41 passed | 41 passed |

Commands: `make install`, `make migrate`, `scripts/acceptance_run.sh`,
`make check`, `REIMAGINA_REQUIRE_DB=1 make test-integration`,
`REIMAGINA_REQUIRE_DB=1 make test-conformance`, and
`REIMAGINA_E2E_WRITES=1 make test-e2e` against populated scratch stacks.
Browser prerequisites and alternate-port variables are documented in
[the browser-test guide](../tests/e2e/README.md).

The final unit/contract run included the complete local delivery package and
had no skips. Earlier source-only runs skipped six delivery-artifact checks. The
conformance skip is cancellation on the synchronous implementation, which
finishes before submission returns. Linux integration additionally skipped
optional ogr2ogr/PMTiles export; the other skip requires a dedicated pooler
refusal-test database. No suite was accepted on an all-skipped result.

## Regression evidence

- An HTTP/ASGI burst with eight requests, one connection and two request
  threads failed before the API fix and passed afterward. Connection waiting
  is now asynchronous. Checkout/return operations finish across cancellation
  before capacity is released. Browser assertions and timeouts were unchanged.
- Subprocess tests reproduced overwritten exports, dotenv shell execution,
  unsafe target mismatches and surviving descendants. These now pass,
  including SIGINT/SIGTERM and failed service startup.
- URL-encoded credentials/database names failed bootstrap configuration before
  the fix. An authenticated bootstrap with reserved password characters and a
  database name containing spaces passed afterward.
- Standalone test configuration loads the checkout's explicit `.env`, preserves
  exports, rejects missing targets and checks libpq's effective database name,
  including URL query overrides.
- Pre-commit configuration, documentation links, installation anchors and
  `git diff --check` passed validation.

Initial Linux validation encountered macOS metadata files introduced while
transferring the working changes. Those harness files were removed and the
clean-source suites were rerun.

## Validation incident

Early conformance validation used the default local `reimagina_test` database
because the test setup did not reliably load `.env`. Its fixture dropped
managed schemas during teardown. The previous contents are unknown and no
restoration was attempted. The audit log records that target; a later
read-only check found only the DBOS schema among the platform schemas.

Final runs used explicit isolated targets. The corrected fixtures no longer
select an implicit local database. The original repository checkout was not
modified.

## Scope limits

Performance budgets, SIGKILL recovery with a 20,000-block territory and private
Azure/Overture/OSM data loading were not run. The existing private delivery
sources were audited and their four PDFs regenerated in the client bundle.
No release was published.

## Delivery-package verification

The existing architecture map, database/API reference, technical manual and
capability evidence were corrected in place. The reference was regenerated
from a fresh PostgreSQL 16 database migrated to r052 and the current OpenAPI
contract. All four PDFs were rendered, visually reviewed and checked for
portable references, complete figures, table layout and contents-page numbering.
The font licence and pinned font/diagram provenance accompany the sources.

Final `make check` passed on macOS and Debian ARM64/x86_64: 1,092 Python
unit/contract tests with no skips, 440 web tests, lint/type checks and web build.
Linux checks used the exported source plus delivery files and a local Git
index, as the manual instructs. New regressions cover stale-schema refusal,
source/archive correspondence, manifest provenance and portable PDF references.
The client bundle includes the actual working source snapshot; its base commit
alone does not reproduce the changes. The manifest and SHA-256 checks identify
the supplied bytes without asserting publication or contractual acceptance.

# Installation on macOS and Linux

This is the canonical local installation guide. It covers software setup,
synthetic verification and restart procedures. Real territory ingestion is a
separate operation requiring the private mirror described in [BOOTSTRAP.md](BOOTSTRAP.md).

## 1. Prerequisites

Install Git, Make, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and [Node.js](https://nodejs.org/en/download). On macOS, Apple's Command Line
Tools provide Git and Make (`xcode-select --install`). On Debian/Ubuntu,
`sudo apt-get update` followed by `sudo apt-get install git make curl ca-certificates`
provides basic tools. Follow the linked vendor instructions for uv and Node;
open a new terminal if their installers change your PATH.

Use Python **3.12** for this installation procedure. The package declares
`>=3.12`, but newer interpreters were not verified in this installation audit.
Node must satisfy **`^22.22.2 || ^24.15.0 || >=26`**: for example, 24.1.0 is too old.
Use a supported release within that range. npm is included with Node.

The November 2026 installation audit exercised these environments:

| Environment | Python | Node | Result and limits |
|---|---|---|---|
| macOS 15.7.9 ARM64 | 3.12.11 | 24.22.2 | Docker and Postgres.app database paths; fresh dependencies with existing host tools/caches |
| Debian 12 ARM64 container | 3.12.11 | 24.22.3 | Docker-backed database; GEOS headers required |
| Debian 12 x86_64 container | 3.12.11 | 24.22.3 | Docker-backed database; emulated on ARM64 |

All used uv 0.7.14. This is evidence for those combinations, not a claim that
every macOS/Linux release or native Linux database installation was tested.
The Ubuntu package path below is based on official package availability.

On **Linux ARM64**, `exactextract` builds from source for Python 3.12. Before
installing Python dependencies, install GEOS headers, a C/C++ compiler and CMake:

```bash
sudo apt-get update
sudo apt-get install build-essential cmake libgeos-dev
```

The tested Linux x86_64 installation used a wheel without additional GEOS
packages. Native GDAL is separate: ordinary installation uses Python wheels
with their geospatial libraries. `ogr2ogr` (GDAL 3.8+) is needed only for the
optional PMTiles export workflow.

For the database, choose Docker Engine/Desktop with Compose v2, or PostgreSQL
16+ with PostGIS 3.4+ and pgRouting 3.6+. Reserve ports 8001 (API) and 5173 (web).
The Docker stack also uses 5433 (direct PostgreSQL), 5432 (PgBouncer), 3000
(Martin) and 3001 (PMTiles host). Allow several GB for environments and Docker
images, plus separate capacity for data; real mirror storage is workload dependent.

## 2. Clone and select Python

```bash
git clone https://github.com/rschifan/reimagina-evo.git
cd reimagina-evo
uv python install 3.12.11
export UV_PYTHON=3.12.11
export UV_LOCKED=1
uv --version
uv python find
node --version
npm --version
make --version
```

Keep `UV_PYTHON` and `UV_LOCKED` exported in terminals used for this checkout.
`UV_LOCKED=1` also makes subsequent `uv run` commands reject a stale lockfile.
Python tools run through `uv run`; installing dependencies does not activate
`.venv` in your shell.

## 3. Configure all five database URLs

```bash
cp .env.example .env
```

Edit `.env` at the repository root. Select exactly one database block:
Docker (Path A) or host PostgreSQL (Path B). Exported shell variables take
precedence over `.env`; unset stale exports when switching configurations.
Use plain `KEY=value` entries. `.env` is configuration, not a shell script;
commands and shell substitutions are not supported. Keep it out of Git.

| Variable | Required target |
|---|---|
| `DATABASE_URL` | Application database, direct PostgreSQL connection for migrations/loaders/CLI |
| `REIMAGINA_APP_DSN` | Same application database; PgBouncer for Docker, direct for host PostgreSQL |
| `DATABASE_URL_DIRECT` | Same application database, direct connection for DBOS checkpoints and LISTEN/NOTIFY |
| `TEST_DATABASE_URL` | Separate disposable database whose name contains `test` |
| `DATABASE_URL_ADMIN` | Existing maintenance database (usually `postgres`) on the same direct server; role able to create databases and extensions |

The bootstrap role needs PostgreSQL superuser privileges to create the
untrusted PostGIS/pgRouting extensions. These settings are for local evaluation.
Keep the API on loopback: its routes are unauthenticated, and the current RLS
policies do not provide tenant isolation. This guide does not establish a
production deployment procedure. See the [API security scope](architecture/API.md)
and [open architecture issues](architecture/TODO.md).

Passwords in URLs must be **percent-encoded**, for example `@` becomes `%40`
and `#` becomes `%23`. Encode only the username/password component, not the
whole URL. An exported DSN must also be shell quoted:
`export DATABASE_URL='postgresql://user:encoded_password@localhost:5433/reimagina'`.
Shell quoting and URL encoding solve different problems.

For Docker, use a strong **URL-safe** `POSTGRES_PASSWORD` consisting of
letters, digits, underscores and hyphens. The Compose Martin URL interpolates
that password directly, so other reserved characters are unsuitable for this
unmodified stack. Set the same password in `POSTGRES_PASSWORD` and all five
Path A DSNs. Keep `POSTGRES_DB` consistent with the application database name.
Only `REIMAGINA_APP_DSN` uses port 5432; the other four use 5433. PgBouncer
exposes only the application database and cannot substitute for the direct
migration/admin connection.

For a host database, comment out Path A DSNs and uncomment all five Path B
DSNs. Those examples assume your OS username is a PostgreSQL role with
passwordless local TCP access and database/extension privileges. Otherwise,
put an explicit role, encoded password, host and port in **each** URL.

## 4. Prepare PostgreSQL

### Path A: Docker

Check that Docker is running, then start only the database and pooler:

```bash
docker compose version
docker compose -f infra/docker/docker-compose.yml --env-file .env up -d postgres pgbouncer
docker compose -f infra/docker/docker-compose.yml --env-file .env ps
docker compose -f infra/docker/docker-compose.yml --env-file .env exec postgres pg_isready
```

Wait until PostgreSQL and PgBouncer report healthy and `pg_isready` reports
accepting connections. Inspect the PostgreSQL/PgBouncer logs with
`docker compose -f infra/docker/docker-compose.yml --env-file .env logs postgres pgbouncer`
if they do not. Do not start Martin
until migrations install its SQL tile functions in step 5.

`POSTGRES_PASSWORD` is applied when the volume is first initialized. Editing
it later does not change an existing role's password; use the original
credentials or deliberately update the database role. Preserve volumes holding
valuable data.

### Path B: native PostgreSQL

On macOS, use a PostgreSQL 16+ installation from
[Postgres.app](https://postgresapp.com/documentation/) or
[Homebrew](https://formulae.brew.sh/formula/postgresql@17). Put that
installation's `bin` directory on PATH and start its server using its own
instructions. Postgres.app was verified with PostgreSQL 17.10, PostGIS 3.5.6
and pgRouting 3.6.3. Extension packages must match the running server's major
version and architecture. Do not assume that installing a Homebrew PostgreSQL
formula also supplies pgRouting.

On **Ubuntu 24.04**, the official `universe` archive contains
[PostGIS 3.4](https://packages.ubuntu.com/noble/postgresql-16-postgis-3) and
[pgRouting 3.6.1](https://packages.ubuntu.com/noble/postgresql-16-pgrouting):

```bash
sudo add-apt-repository universe
sudo apt-get update
sudo apt-get install postgresql-16 postgresql-client-16 postgresql-16-postgis-3 postgresql-16-pgrouting
sudo systemctl enable --now postgresql
```

`add-apt-repository` is supplied by `software-properties-common` if absent.
As the local administrator, use `sudo -u postgres psql` and create a dedicated
local development login with `CREATE ROLE reimagina_app LOGIN SUPERUSER;`,
then set its password with `\password reimagina_app`. Configure all five DSNs
with this role and the host server's port. This assumes a new role; inspect
existing roles before changing an existing installation. Other distributions
use different package names and service managers.

For either native path, connect to the maintenance database using your
configured administrator credentials (psql prompts for the password):

```bash
pg_isready -h localhost -p 5432
psql -h localhost -p 5432 -U reimagina_app -d postgres -W
```

Use your own role/port if different. In psql, check extension availability:

```sql
SELECT name, default_version
FROM pg_available_extensions
WHERE name IN ('postgis', 'pgrouting');
```

Both rows must appear with supported versions before proceeding. If one is
missing, install its matching server extension package or choose the Docker
path. Database readiness alone does not prove extension availability.
`make migrate` creates both databases; if creating them manually, run separate
commands (`createdb reimagina_app` and `createdb reimagina_test`). A second
positional argument to `createdb` is a description, not another database.

## 5. Install dependencies and migrate

From the repository root:

```bash
make install
make migrate
uv run python -m apps.cli.app verify-schema
uv run alembic heads
uv run alembic current
```

`make install` runs `uv sync --locked --extra dev` and `npm ci`. It creates
`.venv` and `apps/web/node_modules`, using the committed dependency locks.
A stale `uv.lock` fails instead of silently resolving new versions; do not
regenerate locks just to bypass an installation error. npm rejects unsupported
Node versions. Linux ARM64 errors mentioning `GEOSConfig.cmake` indicate the
missing prerequisites from step 1.

`make migrate` creates missing application/test databases, migrates both to
the current Alembic head, and initializes feature/indicator catalogs, assets
and the DBOS schema. Existing databases are upgraded without a reset.
Expect migration/bootstrap output and `verify-schema` reporting
`"all_present": true`. Compare `alembic current` with `alembic heads`; the
revision changes over time, so use the checked-out repository's head rather
than a fixed revision number.

For Docker, the full stack may now be started:

```bash
make stack-up
docker compose -f infra/docker/docker-compose.yml --env-file .env ps
```

Martin can now find the migrated SQL functions. Native PostgreSQL users can
skip this: the development API also serves live tiles.

## 6. Start and check the application

```bash
make dev
```

This starts the API at `http://127.0.0.1:8001`, the web application at
`http://localhost:5173`, and the worker that executes queued runs. Live tiles
use `/api/tiles` through Vite's API proxy. To change ports, use
`make dev API_PORT=8123 WEB_PORT=5199`; always pass the same `API_PORT` to
separate `make dev-api` and `make dev-web` terminals. `make dev-worker` starts
the worker separately. The worker requires the direct database URL.

In another terminal:

```bash
curl --fail --max-time 30 http://127.0.0.1:8001/health/ready
curl --fail --max-time 30 http://127.0.0.1:8001/indicators
curl --fail --max-time 30 http://localhost:5173/
```

Expect HTTP 200 and indicator catalog data. Open the web address in a browser
and inspect the map, catalog and run pages. A newly migrated database has no
territory geometry, so an empty map is expected until data is loaded.
A successful web build or readiness request alone does not verify browser
interaction or tile rendering. Check the terminal logs for connection errors;
runs remaining queued usually indicate an absent or disconnected worker.

## 7. Verify with synthetic data

**The acceptance script drops and recreates its application database.** Use a
separate scratch checkout with its own `.env`, a distinct application database
and a distinct test database. Stop its API and worker before resetting it.
Do not copy a real-data `.env` and run acceptance unchanged.

For example, clone a second checkout and repeat the prerequisites/install
steps there. If reusing an already-running host/Docker PostgreSQL, configure
its scratch `.env` with all five URLs, using the actual role, credentials and
direct port. The following example assumes the host Path B passwordless role:

```dotenv
DATABASE_URL=postgresql://localhost/reimagina_acceptance_scratch
REIMAGINA_APP_DSN=postgresql://localhost/reimagina_acceptance_scratch
DATABASE_URL_DIRECT=postgresql://localhost/reimagina_acceptance_scratch
TEST_DATABASE_URL=postgresql://localhost/reimagina_acceptance_test
DATABASE_URL_ADMIN=postgresql://localhost/postgres
```

For an existing Docker server use its direct port **5433 for all five scratch
URLs**; PgBouncer's existing database mapping does not include the scratch
name. Do not start a second Compose stack with the same fixed ports/project.
Remove stale DSN exports so they cannot override the scratch `.env`, review
each target, then run from that scratch repository root:

```bash
make migrate
scripts/acceptance_run.sh
make dev
```

`make migrate` creates the separate test database. Acceptance resets only its
application database. The scripts reject mismatched direct/admin endpoints and
inherited `PGSERVICE` or `PGHOSTADDR` overrides; an omitted URL port follows
`PGPORT`, then defaults to 5432. Use explicit ports for scratch configurations.

The script generates geometry near Mexicali, computes all twelve indicators,
loads a raster, exercises zonal statistics and rollups, authors a scenario,
runs a separate worker and decodes live tiles. It needs no private mirror or
external data downloads after installation. Every stage must exit
successfully. Then repeat the API/browser checks against this populated stack.

For additional checks:

```bash
make check
REIMAGINA_REQUIRE_DB=1 make test-integration
REIMAGINA_REQUIRE_DB=1 make test-conformance
```

`make check` covers lint, Python unit/contract tests, web typecheck/build and
web unit tests; it does not include the database suites. Test fixtures destroy
managed schemas in `TEST_DATABASE_URL`, so that database must be disposable
and named with `test`. Read skip reasons: database suites can otherwise skip
when their prerequisites are missing, and some implementation-specific skips
are intentional. Do not treat an all-skipped run as database verification.
Integration and conformance require an explicitly configured `TEST_DATABASE_URL`;
they do not fall back to a local database name. The guard checks libpq's
effective database name, including any URL query override.

For real browser assertions, follow [tests/e2e/README.md](../tests/e2e/README.md)
to install Playwright/Chromium and run `make test-e2e` against the populated
stack. It can skip when servers or Playwright are unavailable. Scenario write
tests additionally require `REIMAGINA_E2E_WRITES=1` and a scratch database;
those skips are expected in a read-only browser run.

## 8. Stop and restart

Press Ctrl-C in `make dev` to stop the API, web server and worker. If one child
exits, the supervisor stops its siblings and returns a failure; inspect the
first process error before restarting. Run `make dev` again to restart.
If using separate terminals, stop each process with Ctrl-C.

For Docker, `make stack-down` stops the database and tile services while
preserving the volume; restart PostgreSQL/PgBouncer, run `make migrate` after
code updates, then `make stack-up`. Do not remove the volume unless you intend
to erase its databases. Stop a native PostgreSQL server through its own
service manager only when no other application needs it.

## 9. Load real territory separately

Synthetic verification does not require private credentials. Real INEGI data
requires an authorized local mirror under `data/sources/lake/`; Overture,
OpenStreetMap and optional WorldPop loads require network access. Obtain the
mirror layout and credentials from the maintainer and follow
[BOOTSTRAP.md](BOOTSTRAP.md). `scripts/mexicali_run.sh` is a destructive data
load, not an installation or health check. Preserve real-data databases and
never point synthetic acceptance at them.
